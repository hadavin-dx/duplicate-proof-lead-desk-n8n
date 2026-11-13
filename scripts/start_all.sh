#!/usr/bin/env bash
# Start the demo mail/CRM server and the desk in the background (Linux).
# Usage: bash scripts/start_all.sh [keyed|blind]     (default keyed). Run from the repository root.
# Needs bash (it uses [[ ]]): `sh scripts/start_all.sh` fails where sh is dash.
# Ports and paths come from the desk configuration (DESK_* environment variables), not from this file.
# Demo defaults, each used only when you did not set that variable (a value you set, even an empty one, is kept): DESK_LEADFLOW_FILE
# is demo_only/leadflow.json, which turns on the lead owner, the lead stage and the reminder; and, unless DESK_LEADFLOW_FILE is
# off or empty, DESK_NUDGE_TO is reminder@example.test. To turn them off, set DESK_LEADFLOW_FILE=off.
# Logs and process ids go under the folder that holds the database file (git-ignored).
set -eu
# a feature test, not the name BASH_VERSION (dash takes any variable from the environment): only a shell that runs [[ ]] passes
case "$( (eval '[[ a == a ]]') 2>/dev/null && echo bash)" in bash) ;; *) echo "refused: run this script with bash (bash scripts/start_all.sh); this shell is not bash, so its [[ ]] tests would not work and a safety check could be skipped" >&2; exit 2 ;; esac
mode="${1:-keyed}"
case "$mode" in keyed|blind) ;; *) echo "mode must be keyed or blind" >&2; exit 1 ;; esac
export PYTHONPATH="src:."
export DESK_LEADFLOW_FILE="${DESK_LEADFLOW_FILE-demo_only/leadflow.json}"
if [[ "$DESK_LEADFLOW_FILE" != off ]] && [[ -n "$DESK_LEADFLOW_FILE" ]]; then export DESK_NUDGE_TO="${DESK_NUDGE_TO-reminder@example.test}"; fi
data_dir="$(python -c 'from lead_desk.config import Config; print(Config.from_env().db_path.parent)')"
fakes_port="$(python -c 'from lead_desk.config import Config; print(Config.from_env().fakes_port)')"
mkdir -p "$data_dir"
if [[ -e "$data_dir/fakes.pid" ]] || [[ -e "$data_dir/desk.pid" ]]; then
  echo "pid files exist: run scripts/reset.sh first" >&2
  exit 1
fi
python -m demo_only.fakes_server --outbox "$mode" --port "$fakes_port" >"$data_dir/fakes.log" 2>&1 &
echo $! >"$data_dir/fakes.pid"
python -m lead_desk.server >"$data_dir/desk.log" 2>&1 &
echo $! >"$data_dir/desk.pid"
echo "started: demo servers ($mode outbox) pid $(cat "$data_dir/fakes.pid"), desk pid $(cat "$data_dir/desk.pid")"
desk_port="$(python -c 'from lead_desk.config import Config; print(Config.from_env().port)')"
echo "ports: desk $desk_port, demo CRM and outbox $fakes_port (check with: curl http://127.0.0.1:$desk_port/counts)"
echo "logs: $data_dir/fakes.log and $data_dir/desk.log"
