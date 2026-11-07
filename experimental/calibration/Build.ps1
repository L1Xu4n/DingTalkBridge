param([string]$OutputPath = (Join-Path $PSScriptRoot 'bin\DingTalkRegionCalibration.exe'))
$ErrorActionPreference = 'Stop'
$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $framework 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'The installed .NET Framework 4.x compiler is unavailable.' }
$destination = [IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($destination)) -Force | Out-Null
& $compiler /nologo /codepage:65001 /target:winexe /optimize+ /platform:x64 "/out:$destination" "/reference:$framework\System.Windows.Forms.dll" "/reference:$framework\System.Drawing.dll" "/reference:$framework\System.Web.Extensions.dll" (Join-Path $PSScriptRoot 'RegionCalibration.cs') (Join-Path $PSScriptRoot 'BackendBridge.cs') (Join-Path $PSScriptRoot 'LifecycleTests.cs')
if ($LASTEXITCODE -ne 0) { throw "Calibration helper compilation failed ($LASTEXITCODE)." }
$fixture = Join-Path ([IO.Path]::GetDirectoryName($destination)) 'CalibrationLifecycleFixture.exe'
& $compiler /nologo /codepage:65001 /target:exe /optimize+ /platform:x64 "/out:$fixture" "/reference:$framework\System.Web.Extensions.dll" (Join-Path $PSScriptRoot 'LifecycleFixture.cs')
if ($LASTEXITCODE -ne 0) { throw "Synthetic lifecycle fixture compilation failed ($LASTEXITCODE)." }
Get-Item -LiteralPath $destination | Select-Object FullName,Length
Get-FileHash -LiteralPath $destination -Algorithm SHA256
