# Stop what start_all.ps1 started (only those process ids) and delete the lead book, so the next run starts empty.
# Run from the repository root. It deletes the database file, its -wal/-shm/.lock files, the logs and the pid files,
# and refuses to run when the database path is not inside the repository's data folder.
$ErrorActionPreference = 'Stop'
$env:PYTHONPATH = 'src;.'
# a plain whole number of seconds: 0, or one to three digits with no leading zero (same rule as reset.sh)
$stopWait = 10
if ($env:RESET_STOP_WAIT_SECONDS) {
    if ($env:RESET_STOP_WAIT_SECONDS -cnotmatch '^(0|[1-9][0-9]{0,2})$') {
        Write-Host 'refused: RESET_STOP_WAIT_SECONDS must be a whole number of seconds from 0 to 999 with no leading zero; nothing was stopped or deleted'
        exit 2
    }
    $stopWait = [int]$env:RESET_STOP_WAIT_SECONDS
}
$db = (python -c "from lead_desk.config import Config; print(Config.from_env().db_path)").Trim()
$dataDir = Split-Path -Parent $db
# real paths: a junction or symlink inside data\ that points elsewhere is followed (same check as reset.sh)
$inside = python -c "import os, sys; d = os.path.normcase(os.path.realpath(sys.argv[1])); r = os.path.normcase(os.path.realpath('data')); print(d == r or d.startswith(r + os.sep))" $dataDir
if ("$inside".Trim() -ne 'True') {
    Write-Host "refused: the database path is not inside this repository's data folder, so nothing was stopped or deleted"
    exit 2
}
foreach ($name in 'desk', 'fakes') {
    $pidFile = "$dataDir\$name.pid"
    if (Test-Path $pidFile) {
        $procId = [int](Get-Content $pidFile)
        # only stop a process that still is the desk or the demo server (a process id can be reused by another program)
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
        if ($null -eq $proc) {
            # that process id is gone already
        } elseif ($proc.CommandLine -match 'lead_desk\.server|demo_only\.fakes_server') {
            Stop-Process -Id $procId -ErrorAction SilentlyContinue
            try { Wait-Process -Id $procId -Timeout $stopWait -ErrorAction Stop } catch { }
            if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
                Write-Host "refused: process $procId did not stop in time; nothing was deleted"
                exit 5
            }
        } else {
            Write-Host "refused: pid $procId in $pidFile is not the desk or the demo server; nothing was stopped or deleted (delete the pid file yourself if it is stale)"
            exit 6
        }
        Remove-Item $pidFile
    }
}
# a desk started by hand (no pid file) or one that is still running: nothing may hold the database or answer on a port
python scripts/reset_check.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Start-Sleep -Seconds 1
foreach ($f in $db, "$db-wal", "$db-shm", "$db.lock", "$dataDir\desk.log", "$dataDir\desk.err.log",
    "$dataDir\fakes.log", "$dataDir\fakes.err.log", "$dataDir\crash.flag") {
    if (Test-Path $f) { Remove-Item -Force $f }
}
Write-Host 'reset done'
