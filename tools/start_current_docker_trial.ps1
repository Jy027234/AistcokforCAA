# Start the qualified local release without rebuilding the source tree.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeEnv = Join-Path $projectRoot 'deploy/local-trial/docker-runtime.env'
$releasePins = Join-Path $projectRoot 'deploy/local-trial/usability-20261101.compose.json'
foreach ($required in @($runtimeEnv, $releasePins)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required deployment file is missing: $required"
    }
}
Push-Location -LiteralPath $projectRoot
try {
    & docker compose -p a --env-file $runtimeEnv `
        -f docker-compose.yml -f docker-compose.real.yml -f $releasePins `
        up -d --no-build --wait --wait-timeout 90 aquant scheduler financial-worker
    if ($LASTEXITCODE -ne 0) { throw "Qualified Docker release did not become healthy (exit $LASTEXITCODE)." }
}
finally { Pop-Location }
