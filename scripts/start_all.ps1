# Start the demo mail/CRM server and the desk in the background (Windows PowerShell 5.1 or later).
# Usage: powershell -NoProfile -File scripts\start_all.ps1 [keyed|blind]     (default keyed). Run from the repository root.
# Ports and paths come from the desk configuration (DESK_* environment variables), not from this file.
# Lead owner, lead stage and reminder are on with the demo file demo_only/leadflow.json and reminders to reminder@example.test unless you set DESK_LEADFLOW_FILE yourself; set DESK_LEADFLOW_FILE=off to turn them off.
param([ValidateSet('keyed', 'blind')][string]$Mode = 'keyed')
$ErrorActionPreference = 'Stop'
$env:PYTHONPATH = 'src;.'
if ($null -eq $env:DESK_LEADFLOW_FILE) { $env:DESK_LEADFLOW_FILE = 'demo_only/leadflow.json' }
if ($env:DESK_LEADFLOW_FILE -cne 'off' -and $null -eq $env:DESK_NUDGE_TO) { $env:DESK_NUDGE_TO = 'reminder@example.test' }
$dataDir = (python -c "from lead_desk.config import Config; print(Config.from_env().db_path.parent)").Trim()
$fakesPort = (python -c "from lead_desk.config import Config; print(Config.from_env().fakes_port)").Trim()
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
if ((Test-Path "$dataDir\fakes.pid") -or (Test-Path "$dataDir\desk.pid")) {
    throw 'pid files exist: run scripts\reset.ps1 first'
}
$fakes = Start-Process python -ArgumentList '-m', 'demo_only.fakes_server', '--outbox', $Mode, '--port', $fakesPort `
    -PassThru -WindowStyle Hidden -RedirectStandardOutput "$dataDir\fakes.log" -RedirectStandardError "$dataDir\fakes.err.log"
$fakes.Id | Set-Content "$dataDir\fakes.pid"
$desk = Start-Process python -ArgumentList '-m', 'lead_desk.server' `
    -PassThru -WindowStyle Hidden -RedirectStandardOutput "$dataDir\desk.log" -RedirectStandardError "$dataDir\desk.err.log"
$desk.Id | Set-Content "$dataDir\desk.pid"
Write-Host "started: demo servers ($Mode outbox) pid $($fakes.Id), desk pid $($desk.Id)"
$deskPort = (python -c "from lead_desk.config import Config; print(Config.from_env().port)").Trim()
Write-Host "ports: desk $deskPort, demo CRM and outbox $fakesPort (check with: Invoke-WebRequest http://127.0.0.1:$deskPort/counts)"
Write-Host "logs: $dataDir\fakes.log and $dataDir\desk.log (and .err.log)"
