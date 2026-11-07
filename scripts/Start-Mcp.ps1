[CmdletBinding()]
param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
if (-not $ConfigPath -and -not (Test-Path -LiteralPath 'Env:\DINGTALKBRIDGE_CONFIG')) {
    $localConfig = Join-Path $projectRoot 'config.local.json'
    if (Test-Path -LiteralPath $localConfig) { $ConfigPath = $localConfig }
}
$pythonCommand = (Get-Command python.exe -ErrorAction Stop).Source
$arguments = @('-m', 'dingtalkbridge')
if ($ConfigPath) { $arguments += @('--config', $ConfigPath) }
$arguments += 'serve'
& $pythonCommand @arguments
exit $LASTEXITCODE
