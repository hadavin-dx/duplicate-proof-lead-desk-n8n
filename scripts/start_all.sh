#!/usr/bin/env bash
# Start the demo mail/CRM server and the desk in the background (Linux).
# Usage: scripts/start_all.sh [keyed|blind]     (default keyed). Run from the repository root.
# Ports and paths come from the desk configuration (DESK_* environment variables), not from this file.
# Lead owner, lead stage and reminder are on with the demo file demo_only/leadflow.json and reminders to reminder@example.test unless you set DESK_LEADFLOW_FILE yourself; set DESK_LEADFLOW_FILE=off to turn them off.
# Logs and process ids go under the folder that holds the database file (git-ignored).
set -eu
mode="${1:-keyed}"
case "$mode" in keyed|blind) ;; *) echo "mode must be keyed or blind" >&2; exit 1 ;; esac
export PYTHONPATH="src:."
export DESK_LEADFLOW_FILE="${DESK_LEADFLOW_FILE-demo_only/leadflow.json}"
if [ "$DESK_LEADFLOW_FILE" != off ] && [ -n "$DESK_LEADFLOW_FILE" ]; then export DESK_NUDGE_TO="${DESK_NUDGE_TO-reminder@example.test}"; fi
data_dir="$(python -c 'from lead_desk.config import Config; print(Config.from_env().db_path.parent)')"
fakes_port="$(python -c 'from lead_desk.config import Config; print(Config.from_env().fakes_port)')"
mkdir -p "$data_dir"
if [ -e "$data_dir/fakes.pid" ] || [ -e "$data_dir/desk.pid" ]; then
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
