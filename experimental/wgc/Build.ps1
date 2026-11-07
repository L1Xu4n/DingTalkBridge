param(
    [ValidateSet('all','metadata','gpu-test','once')][string]$Target='all',
    [string]$MsvcRoot='C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207',
    [string]$SdkRoot='C:\Program Files (x86)\Windows Kits\10',
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$')][string]$SdkVersion='10.0.26100.0'
)
$ErrorActionPreference='Stop'
$compiler=Join-Path $MsvcRoot 'bin\Hostx64\x64\cl.exe'
if(-not (Test-Path -LiteralPath $compiler)){throw 'The installed MSVC compiler is unavailable.'}
$include=Join-Path $SdkRoot "Include\$SdkVersion"
if(-not (Test-Path -LiteralPath (Join-Path $include 'cppwinrt\winrt\Windows.Graphics.Capture.h'))){throw 'The installed C++/WinRT SDK is unavailable.'}
# This changes only the current build process environment, never system settings.
$env:INCLUDE="$MsvcRoot\include;$include\ucrt;$include\um;$include\shared;$include\winrt;$include\cppwinrt"
$env:LIB="$MsvcRoot\lib\x64;$SdkRoot\Lib\$SdkVersion\um\x64;$SdkRoot\Lib\$SdkVersion\ucrt\x64"
$env:PATH="$MsvcRoot\bin\Hostx64\x64;$env:PATH"
if($Target -in @('all','metadata')){
    & $compiler /nologo /EHsc /std:c++17 /O2 /DUNICODE /D_UNICODE (Join-Path $PSScriptRoot 'MetadataProbe.cpp') ("/Fo"+(Join-Path $PSScriptRoot 'MetadataProbe.obj')) ("/Fe"+(Join-Path $PSScriptRoot 'MetadataProbe.exe')) /link windowsapp.lib dwmapi.lib user32.lib
    if($LASTEXITCODE -ne 0){throw 'Metadata probe compilation failed.'}
}
if($Target -in @('all','gpu-test')){
    & $compiler /nologo /EHsc /std:c++17 /O2 (Join-Path $PSScriptRoot 'GpuMaskSelfTest.cpp') ("/Fo"+(Join-Path $PSScriptRoot 'GpuMaskSelfTest.obj')) ("/Fe"+(Join-Path $PSScriptRoot 'GpuMaskSelfTest.exe')) /link d3d11.lib dxgi.lib
    if($LASTEXITCODE -ne 0){throw 'Synthetic GPU test compilation failed.'}
}
# Compilation only. No probe, GPU test or capture process is launched.

if($Target -in @('all','once')){
    & $compiler /nologo /EHsc /std:c++17 /O2 /DUNICODE /D_UNICODE (Join-Path $PSScriptRoot 'WgcOnce.cpp') ("/Fo"+(Join-Path $PSScriptRoot 'WgcOnce.obj')) ("/Fe"+(Join-Path $PSScriptRoot 'WgcOnce.exe')) /link windowsapp.lib d3d11.lib dxgi.lib dwmapi.lib user32.lib bcrypt.lib
    if($LASTEXITCODE -ne 0){throw 'One-shot backend compilation failed.'}
    $manifest=@{schema='dingtalkbridge.wgc-once-build.v1'}
    foreach($pair in @(@('producer_cpp','WgcOnce.cpp'),@('guard_header','OnceGuard.h'),@('gpu_header','GpuRoiMask.h'),@('producer_exe','WgcOnce.exe'))){$manifest[$pair[0]]=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $pair[1]) -Algorithm SHA256).Hash.ToLowerInvariant()}
    [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot 'WgcOnce.build.json'),($manifest|ConvertTo-Json)+[Environment]::NewLine,[System.Text.UTF8Encoding]::new($false))
}