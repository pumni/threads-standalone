$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$requirements = Join-Path $env:RUNNER_TEMP 'locked-wheel-smoke-requirements.txt'
$dist = Join-Path $env:GITHUB_WORKSPACE 'dist'
$wheel = Get-ChildItem -LiteralPath $dist -Filter 'threads_standalone-*.whl' |
    Select-Object -First 1
if (-not $wheel) { throw 'built wheel was not found' }
$env:THREADS_STANDALONE_WHEEL_PATH = $wheel.FullName

uv export --locked --no-emit-project --output-file $requirements | Out-Null
$venv = Join-Path $env:RUNNER_TEMP 'wheel-smoke-venv'
uv venv --python 3.14 $venv
if ($env:OS -eq 'Windows_NT') {
    $python = Join-Path $venv 'Scripts/python.exe'
    $cli = Join-Path $venv 'Scripts/threads-local.exe'
} else {
    $python = Join-Path $venv 'bin/python'
    $cli = Join-Path $venv 'bin/threads-local'
}
uv pip sync --python $python $requirements
uv pip install --python $python --no-deps $wheel.FullName

Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$env:THREADS_STANDALONE_WHEEL_SMOKE = '1'
$junit = Join-Path $env:RUNNER_TEMP 'wheel-smoke.xml'
Push-Location $env:RUNNER_TEMP
try {
    & $cli --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'installed threads-local --help failed' }
    & $cli api --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'installed threads-local api --help failed' }
    & $python (Join-Path $PSScriptRoot 'ci.py') wheel-smoke $junit
    if ($LASTEXITCODE -ne 0) { throw 'installed-wheel acceptance failed' }
} finally {
    Pop-Location
}
