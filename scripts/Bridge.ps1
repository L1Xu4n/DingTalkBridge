[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$BridgeArguments)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
$localConfig = Join-Path $projectRoot 'config.local.json'
if ((Test-Path -LiteralPath $localConfig) -and -not (Test-Path -LiteralPath 'Env:\DINGTALKBRIDGE_CONFIG') -and -not ($BridgeArguments | Where-Object { $_ -eq '--config' -or $_ -like '--config=*' })) {
    $BridgeArguments = @('--config', $localConfig) + $BridgeArguments
}
$pythonCommand = (Get-Command python.exe -ErrorAction Stop).Source
& $pythonCommand -m dingtalkbridge @BridgeArguments
exit $LASTEXITCODE
