[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $taskRoot
$taskSettings = @{}
foreach ($taskLine in Get-Content -LiteralPath 'real-trial.env' -Encoding utf8) {
    if ($taskLine -match '^\s*(AQUANT_[A-Z0-9_]+)=(.*)$') {
        $taskSettings[$Matches[1]] = $Matches[2].Trim()
    }
}
$taskData = (Resolve-Path -LiteralPath $taskSettings['AQUANT_HOST_DATA_DIR']).Path
$taskPipeline = (Resolve-Path -LiteralPath $taskSettings['AQUANT_HOST_PIPELINE_DIR']).Path
$taskRuntimeEnv = Join-Path $taskRoot 'deploy\local-trial\docker-runtime.env'
if (-not (Test-Path -LiteralPath $taskRuntimeEnv -PathType Leaf)) { throw 'Run tools/docker_runtime_env.py first.' }
foreach ($taskContainerName in @('a-aquant-1', 'a-scheduler-1')) {
    $taskRunning = & docker inspect $taskContainerName --format '{{.State.Running}}'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot verify the existing A-Quant runtime; no host process stopped.' }
    if ($taskRunning -eq 'true') {
        throw 'Docker already owns this data directory. Do not repeat Windows SQLite handoff; verify preserved facts with docker exec instead.'
    }
}

# Compare the existing container mounts before allowing any runtime handoff.
$taskOldMounts = & docker inspect a-aquant-1 --format '{{json .Mounts}}'
if ($LASTEXITCODE -ne 0) { throw 'Existing A-Quant deployment not found; no host process stopped.' }
foreach ($taskBinding in @(@('/data', $taskData), @('/app/deploy/agentctl-q0', $taskPipeline))) {
    $taskMounted = @($taskOldMounts | ConvertFrom-Json | Where-Object Destination -eq $taskBinding[0])
    if ($taskMounted.Count -ne 1 -or $taskMounted[0].Type -ne 'bind' -or
        [IO.Path]::GetFullPath($taskMounted[0].Source.Replace('/', '\')) -ne $taskBinding[1]) {
        throw ('Data mount changed for ' + $taskBinding[0] + '; no host process stopped.')
    }
}
$taskRecovery = Get-ScheduledTask -TaskName 'AQuant Scheduler Worker' -ErrorAction SilentlyContinue
if ($taskRecovery -and -not (@($taskRecovery.Actions | Where-Object { $_.Arguments.Contains($taskRoot) }).Count)) {
    throw 'Scheduler recovery task belongs to another project; no task changed.'
}
$taskWorkers = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains((Join-Path $taskRoot 'tools\scheduler_worker.py')) -and $_.CommandLine.Contains($taskData)
})
$taskApi = $null
$taskListener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($taskListener) {
    $taskExpectedPid = [int](Get-Content -LiteralPath 'deploy/local-trial/api.pid')
    if ($taskListener.OwningProcess -ne $taskExpectedPid) { throw 'API PID identity differs; no process stopped.' }
    $taskApi = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskExpectedPid)
    if ($taskApi.Name -ne 'python.exe' -or -not $taskApi.CommandLine.Contains('-m uvicorn main:app --app-dir apps/api')) {
        throw 'API command identity differs; no process stopped.'
    }
}
$taskActivePipeline = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains($taskRoot) -and
    ($_.CommandLine.Contains('daily_run.py') -or $_.CommandLine.Contains('daily_research_run.py'))
})
if ($taskActivePipeline.Count) { throw 'A project pipeline is still active; retry handoff after it completes.' }
$taskEvidence = Join-Path $taskRoot ('tmp\docker-redeploy-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $taskEvidence | Out-Null
$taskPrior = [ordered]@{
    dataDir = $taskData; pipelineDir = $taskPipeline
    hostApiPid = $(if ($taskApi) { $taskApi.ProcessId } else { $null })
    hostWorkerPids = @($taskWorkers | ForEach-Object ProcessId)
    recoveryTaskWasEnabled = [bool]($taskRecovery -and $taskRecovery.Settings.Enabled)
}
$taskPrior | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $taskEvidence 'handoff.json') -Encoding utf8
if ($taskRecovery) {
    Disable-ScheduledTask -TaskName 'AQuant Scheduler Worker' | Out-Null
    Stop-ScheduledTask -TaskName 'AQuant Scheduler Worker'
}
foreach ($taskWorker in $taskWorkers) { Stop-Process -Id $taskWorker.ProcessId }
if ($taskApi) { Stop-Process -Id $taskApi.ProcessId }
Write-Output 'Stopped only the verified project Windows API/worker; recovery task disabled.'

# Flush the old Windows WAL before Linux opens this exact database.
& python -c "import sqlite3,sys; con=sqlite3.connect(sys.argv[1],timeout=10); row=con.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(); print('Checkpoint:',row); assert row[0]==0; con.close()" (Join-Path $taskData 'meta.sqlite')
if ($LASTEXITCODE -ne 0) { throw 'WAL checkpoint failed; Docker not started.' }
& python tools/docker_data_inventory.py --data-dir $taskData --output (Join-Path $taskEvidence 'before.json') --backup (Join-Path $taskEvidence 'meta-backup.sqlite')
if ($LASTEXITCODE -ne 0) { throw 'Backup or inventory failed; Docker not started.' }
& docker compose --env-file $taskRuntimeEnv -f docker-compose.yml -f docker-compose.real.yml up -d --no-build aquant scheduler
if ($LASTEXITCODE -ne 0) { throw ('Docker startup failed. Preserved backup and handoff state: ' + $taskEvidence) }
$taskEvidence | Set-Content -LiteralPath 'deploy/local-trial/docker-acceptance-path.txt'
Write-Output ('Docker started. Preservation evidence: ' + $taskEvidence)
