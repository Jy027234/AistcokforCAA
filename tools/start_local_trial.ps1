[CmdletBinding()]
param(
    [string]$PythonPath = 'C:\Users\admin\AppData\Local\Programs\Python\Python314\python.exe',
    [string]$DataDir,
    [switch]$ApiOnly,
    [switch]$WorkerOnly
)
$ErrorActionPreference = 'Stop'
if ($ApiOnly -and $WorkerOnly) { throw 'ApiOnly and WorkerOnly cannot be used together.' }
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskPython = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not $DataDir) { $DataDir = Join-Path $taskRoot 'deploy\universe-snapshot' }
$taskData = (Resolve-Path -LiteralPath $DataDir).Path
# The real Docker deployment and Windows processes must not share a WAL file.
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $taskDockerIds = @(& docker ps --filter ('label=com.docker.compose.project.working_dir=' + $taskRoot) --format '{{.ID}}' 2>$null)
    if ($LASTEXITCODE -eq 0) {
        foreach ($taskDockerId in $taskDockerIds) {
            if (-not $taskDockerId) { continue }
            $taskMountJson = & docker inspect $taskDockerId --format '{{json .Mounts}}'
            if ($LASTEXITCODE -ne 0) { throw 'Cannot verify the active Docker data owner; no Windows process started.' }
            foreach ($taskMount in ($taskMountJson | ConvertFrom-Json)) {
                if ($taskMount.Type -eq 'bind' -and $taskMount.Destination -eq '/data') {
                    $taskMountSource = [IO.Path]::GetFullPath($taskMount.Source.Replace('/', '\'))
                    if ($taskMountSource -eq $taskData) {
                        throw 'This data directory is active in Docker. Use http://127.0.0.1:8080; stop the real Docker deployment before starting Windows API or worker.'
                    }
                }
            }
        }
    }
}
# Restore the saved local trial settings, including the explicitly labelled fee assumption.
$taskEnv = Join-Path $taskRoot 'real-trial.env'
if (Test-Path -LiteralPath $taskEnv -PathType Leaf) {
    foreach ($taskLine in Get-Content -LiteralPath $taskEnv -Encoding utf8) {
        if ($taskLine -match '^\s*(AQUANT_[A-Z0-9_]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim(), 'Process')
        }
    }
}
$env:PYTHONPATH = Join-Path $taskRoot 'src'
$env:PYTHONUTF8 = '1'
$env:AQUANT_DATA_DIR = $taskData
$env:AQUANT_RESET_DATA = '0'
$env:AQUANT_SNAPSHOT_ID = ''
$env:AQUANT_IDENTITY_MODE = 'LOCAL_LOOPBACK_DEMO'
$env:AQUANT_TRIAL_SUBJECT = 'user:demo'
$env:AQUANT_RESEARCH_CANDIDATES_FILE = Join-Path $taskRoot 'data\s2-structured\research-candidates.json'
$env:AQUANT_DAILY_MODE = 'RESEARCH_SIMULATION'
$taskLogs = Join-Path $taskRoot 'deploy\local-trial'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
if (-not $WorkerOnly) {
    $taskListener = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue
    if (-not $taskListener) {
        $taskApi = Start-Process -FilePath $taskPython -ArgumentList '-m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8000' -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLogs 'api.stdout.log') -RedirectStandardError (Join-Path $taskLogs 'api.stderr.log')
        $taskApi.Id | Set-Content -LiteralPath (Join-Path $taskLogs 'api.pid')
        Write-Output "Local trial API started: $($taskApi.Id)"
    } else { Write-Output 'Port 8000 is already listening; existing process retained.' }
}
if (-not $ApiOnly) {
    # Serialise the recovery task and a manual launch before inventory + spawn.
    $taskHasher = [System.Security.Cryptography.SHA256]::Create()
    try { $taskKey = [BitConverter]::ToString($taskHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($taskRoot.ToLowerInvariant()))).Replace('-', '') }
    finally { $taskHasher.Dispose() }
    $taskMutex = New-Object System.Threading.Mutex($false, ('Local\AQuantSchedulerLaunch-' + $taskKey))
    $taskAcquired = $false
    try {
        try { $taskAcquired = $taskMutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $taskAcquired = $true }
        if (-not $taskAcquired) { Write-Output 'Another local scheduler launch is in progress; existing launcher retained.' }
        else {
            $taskWorkers = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'tools[\\/]+scheduler_worker\.py' -and $_.CommandLine -match [regex]::Escape($taskRoot) })
            if ($taskWorkers.Count -eq 0) {
                $taskArgs = '"{0}" --data-dir "{1}" --interpreter "{2}"' -f (Join-Path $taskRoot 'tools\scheduler_worker.py'), $taskData, $taskPython
                $taskWorker = Start-Process -FilePath $taskPython -ArgumentList $taskArgs -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLogs 'scheduler.stdout.log') -RedirectStandardError (Join-Path $taskLogs 'scheduler.stderr.log')
                $taskWorker.Id | Set-Content -LiteralPath (Join-Path $taskLogs 'scheduler.pid')
                Write-Output "Local scheduler worker started: $($taskWorker.Id)"
            } else { Write-Output 'Existing local scheduler worker retained.' }
        }
    } finally {
        if ($taskAcquired) { $taskMutex.ReleaseMutex() }
        $taskMutex.Dispose()
    }
}
