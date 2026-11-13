#!/usr/bin/env bash
# Stop what start_all.sh started (only those process ids) and delete the lead book, so the next run starts empty.
# Run from the repository root with bash (it uses [[ ]]): `sh scripts/reset.sh` fails where sh is dash. It deletes the database file, its -wal/-shm/.lock files, the logs and the pid files,
# and refuses to run when the database path is not inside the repository's data/ folder.
set -eu
# a feature test, not the name BASH_VERSION (dash takes any variable from the environment): only a shell that runs [[ ]] passes
case "$( (eval '[[ a == a ]]') 2>/dev/null && echo bash)" in bash) ;; *) echo "refused: run this script with bash (bash scripts/reset.sh); this shell is not bash, so its [[ ]] tests would not work and a safety check could be skipped" >&2; exit 2 ;; esac
# a plain whole number of seconds: 0, or one to three digits with no leading zero (a shell reads 08 as a bad octal)
case "${RESET_STOP_WAIT_SECONDS:-10}" in
  0|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) ;;
  *) echo "refused: RESET_STOP_WAIT_SECONDS must be a whole number of seconds from 0 to 999 with no leading zero; nothing was stopped or deleted" >&2; exit 2 ;;
esac
export PYTHONPATH="src:."
db="$(python -c 'from lead_desk.config import Config; print(Config.from_env().db_path)')"
data_dir="$(dirname "$db")"
inside="$(python -c 'import os, sys; d = os.path.realpath(sys.argv[1]); r = os.path.realpath("data"); print(d == r or d.startswith(r + os.sep))' "$data_dir")"
if [[ "$inside" != "True" ]]; then
  echo "refused: the database path is not inside this repository's data/ folder, so nothing was stopped or deleted" >&2
  exit 2
fi
# without ps the program behind a process id cannot be told, so nothing is stopped and nothing is deleted
if ! command -v ps >/dev/null 2>&1 && { [[ -e "$data_dir/desk.pid" ]] || [[ -e "$data_dir/fakes.pid" ]]; }; then
  echo "refused: the ps command is not installed, so the desk and the demo server cannot be told from other programs; nothing was stopped or deleted (install procps, or stop them yourself and delete the pid files)" >&2
  exit 3
fi
# a process that has ended but is not yet collected by its parent (state Z) is stopped
alive() { kill -0 "$1" 2>/dev/null && [[ "$(ps -p "$1" -o stat= 2>/dev/null | cut -c1)" != "Z" ]]; }
for name in desk fakes; do
  pidfile="$data_dir/$name.pid"
  if [[ -e "$pidfile" ]]; then
    pid="$(cat "$pidfile")"
    # only stop a process that still is the desk or the demo server (a pid can be reused by another program);
    # a process that has ended but is not yet collected (state Z) shows no program name, so it counts as gone
    what="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    if [[ "$(ps -p "$pid" -o stat= 2>/dev/null | cut -c1)" == "Z" ]]; then what=""; fi
    case "$what" in
      *lead_desk.server*|*demo_only.fakes_server*)
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 $(( ${RESET_STOP_WAIT_SECONDS:-10} * 10 ))); do alive "$pid" || break; sleep 0.1; done
        if alive "$pid"; then
          echo "refused: process $pid did not stop in time; nothing was deleted" >&2
          exit 5
        fi ;;
      "") ;; # that process id is gone already
      *) echo "refused: pid $pid in $pidfile is not the desk or the demo server; nothing was stopped or deleted (delete the pid file yourself if it is stale)" >&2
         exit 6 ;;
    esac
    rm -f "$pidfile"
  fi
done
# a desk started by hand (no pid file) or one that is still running: nothing may hold the database or answer on a port
python scripts/reset_check.py || exit $?
sleep 1
rm -f "$db" "$db-wal" "$db-shm" "$db.lock" "$data_dir/desk.log" "$data_dir/fakes.log" "$data_dir/crash.flag"
echo "reset done"
