param([string]$OutputPath = (Join-Path $PSScriptRoot 'bin\DingTalkUia.exe'))
$ErrorActionPreference = 'Stop'
$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $framework 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework 4.x C# compiler is unavailable.' }
$destination = [IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($destination)) -Force | Out-Null
& $compiler /nologo /target:exe /optimize+ /platform:x64 "/out:$destination" "/reference:$framework\WPF\UIAutomationClient.dll" "/reference:$framework\WPF\UIAutomationTypes.dll" "/reference:$framework\WPF\WindowsBase.dll" "/reference:$framework\System.Web.Extensions.dll" "/reference:$framework\Accessibility.dll" (Join-Path $PSScriptRoot 'DingTalkUia.cs')
if ($LASTEXITCODE -ne 0) { throw "UIA helper compilation failed ($LASTEXITCODE)." }
Write-Output $destination
