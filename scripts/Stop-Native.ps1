# Cooperatively stop only the bridge collector; DingTalk remains running.
& (Join-Path $PSScriptRoot 'Bridge.ps1') native --mode stop
exit $LASTEXITCODE
