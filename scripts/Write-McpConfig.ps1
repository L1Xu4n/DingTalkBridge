param([string]$OutputPath)
# Generate machine-specific MCP paths locally; the resulting file is gitignored.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) { $OutputPath = Join-Path $projectRoot 'mcp.local.json' }
$settings = Join-Path $projectRoot 'config.local.json'
if (-not (Test-Path -LiteralPath $settings)) { $settings = Join-Path $projectRoot 'config.example.json' }
$configuration = @{mcpServers=@{dingtalkbridge=@{
    command=(Get-Command python.exe -ErrorAction Stop).Source
    args=@('-m','dingtalkbridge','serve')
    env=@{PYTHONPATH=(Join-Path $projectRoot 'src');PYTHONUTF8='1';DINGTALKBRIDGE_CONFIG=$settings}
}}}
[IO.File]::WriteAllText([IO.Path]::GetFullPath($OutputPath),($configuration | ConvertTo-Json -Depth 6)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
Write-Output ([IO.Path]::GetFullPath($OutputPath))
