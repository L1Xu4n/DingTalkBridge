param(
    [ValidateSet('all','production','tests')][string]$Target='all',
    [string]$MsvcRoot='C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207',
    [string]$SdkRoot='C:\Program Files (x86)\Windows Kits\10',
    [string]$SdkVersion='10.0.26100.0'
)
$ErrorActionPreference='Stop'
$compiler=Join-Path $MsvcRoot 'bin\Hostx64\x64\cl.exe'
$librarian=Join-Path $MsvcRoot 'bin\Hostx64\x64\lib.exe'
$env:INCLUDE="$MsvcRoot\include;$SdkRoot\Include\$SdkVersion\ucrt;$SdkRoot\Include\$SdkVersion\um;$SdkRoot\Include\$SdkVersion\shared"
$env:LIB="$MsvcRoot\lib\x64;$SdkRoot\Lib\$SdkVersion\um\x64;$SdkRoot\Lib\$SdkVersion\ucrt\x64"
$env:PATH="$MsvcRoot\bin\Hostx64\x64;$env:PATH"
$sources=@('src/hook.c','src/buffer.c','src/trampoline.c','src/hde/hde64.c')
foreach($kind in @('production','tests')) {
    if($Target -ne 'all' -and $Target -ne $kind){continue}
    $out=Join-Path $PSScriptRoot "build\$kind"
    [void][System.IO.Directory]::CreateDirectory($out)
    $objects=@()
    foreach($source in $sources) {
        $obj=Join-Path $out (([System.IO.Path]::GetFileNameWithoutExtension($source))+'.obj')
        $flags=@('/nologo','/c','/TC','/O2','/W4','/WX','/guard:cf','/GS')
        if($source -ne 'src/hook.c'){$flags+=@('/wd4201','/wd4244','/wd4701')} # upstream anonymous unions / bounded byte offsets
        if($kind -eq 'tests'){$flags+='/DMH_STRICT_TEST'}
        & $compiler @flags (Join-Path $PSScriptRoot $source) ("/Fo"+$obj)
        if($LASTEXITCODE -ne 0){throw "$kind source compilation failed: $source"}
        $objects+=$obj
    }
    $lib=Join-Path $out 'minhook-strict.lib'
    & $librarian /nologo ("/OUT:"+$lib) @objects
    if($LASTEXITCODE -ne 0){throw "$kind library build failed"}
    $flags=@('/nologo','/EHsc','/std:c++17','/O2','/W4','/WX','/guard:cf','/GS')
    if($kind -eq 'tests'){$flags+='/DMH_STRICT_TEST'}
    & $compiler @flags (Join-Path $PSScriptRoot 'tests/Synthetic.cpp') $lib ("/Fo"+(Join-Path $out 'Synthetic.obj')) ("/Fe"+(Join-Path $out 'Synthetic.exe')) /link /DYNAMICBASE /NXCOMPAT /guard:cf
    if($LASTEXITCODE -ne 0){throw "$kind synthetic executable build failed"}
}
# Build only. This script never invokes a target process or injects a DLL.
