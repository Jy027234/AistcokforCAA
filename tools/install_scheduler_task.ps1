[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,

    [string]$DataDir,

    [string]$TaskName = "AQuant Scheduler Worker",

    [ValidateRange(1, 60)]
    [int]$RecoveryMinutes = 5,

    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$workerPath = Join-Path $repoRoot "tools\scheduler_worker.py"
if (-not $DataDir) {
    $DataDir = Join-Path $repoRoot "deploy\universe-snapshot"
}

$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
$resolvedDataDir = (Resolve-Path -LiteralPath $DataDir).Path
$metaPath = Join-Path $resolvedDataDir "meta.sqlite"

if (-not (Test-Path -LiteralPath $workerPath -PathType Leaf)) {
    throw "找不到 scheduler worker：$workerPath"
}
if (-not (Test-Path -LiteralPath $metaPath -PathType Leaf)) {
    throw "数据目录缺少 meta.sqlite：$resolvedDataDir"
}

# 与 worker 自己的预检一致，先在注册前确认解释器具备真实采集所需依赖。
& $resolvedPython -c "import baostock, pytest" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "所选 Python 无法导入 baostock 与 pytest：$resolvedPython"
}

$summary = [ordered]@{
    TaskName = $TaskName
    PythonPath = $resolvedPython
    WorkerPath = $workerPath
    DataDir = $resolvedDataDir
    RecoveryMinutes = $RecoveryMinutes
    Validation = "passed"
}

if ($ValidateOnly) {
    [pscustomobject]$summary
    return
}

$arguments = ('"{0}" --data-dir "{1}"' -f $workerPath, $resolvedDataDir)
$action = New-ScheduledTaskAction `
    -Execute $resolvedPython `
    -Argument $arguments `
    -WorkingDirectory $repoRoot

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$recoveryTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $RecoveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$definition = New-ScheduledTask `
    -Action $action `
    -Trigger @($logonTrigger, $recoveryTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "A-Quant Lab 独立调度 worker；产品库决定实际采集时间。"

Register-ScheduledTask -TaskName $TaskName -InputObject $definition -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$summary.State = [string]$task.State
$summary.TriggerCount = @($task.Triggers).Count
$summary.NextRecoveryCheck = $info.NextRunTime
$summary.LastTaskResult = ('0x{0:X8}' -f [uint32]$info.LastTaskResult)
[pscustomobject]$summary
