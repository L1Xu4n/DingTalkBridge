param([switch]$Foreground)
# Start one native collector; the Python byte lock rejects duplicate instances.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
$settings = Join-Path $projectRoot 'config.local.json'
if (-not (Test-Path -LiteralPath $settings)) { $settings = Join-Path $projectRoot 'config.example.json' }
$arguments = @('-m','dingtalkbridge','--config',$settings,'native','--mode','capture','--duration','0')
if ($Foreground) {
    & $pythonPath @arguments
    exit $LASTEXITCODE
}
$runtime = Join-Path $projectRoot 'runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$runId = [Guid]::NewGuid().ToString('N')
$quoted = @($arguments | ForEach-Object { '"' + $_.Replace('"','\"') + '"' })
$process = Start-Process -FilePath $pythonPath -ArgumentList $quoted -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime "$runId-summary.json") -RedirectStandardError (Join-Path $runtime "$runId-error.log")
[pscustomobject]@{supervisor_pid=$process.Id;status_command='.\scripts\Bridge.ps1 status';stop_command='.\scripts\Stop-Native.ps1'} | ConvertTo-Json
