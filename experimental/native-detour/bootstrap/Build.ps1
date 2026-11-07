param(
    [ValidateSet('synthetic','production','all')][string]$Target='synthetic',
    [ValidateSet('stub','root')][string]$SyntheticCore='stub',
    [string]$MsvcRoot,
    [string]$SdkRoot='C:\Program Files (x86)\Windows Kits\10',
    [string]$SdkVersion,
    [string]$DingTalkPath=$env:DINGTALKBRIDGE_CLIENT,
    [string]$OutputRoot
)
$ErrorActionPreference='Stop'
if (-not $MsvcRoot) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere)) { throw 'Install Visual Studio C++ Build Tools or pass -MsvcRoot' }
    $installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $installation) { throw 'MSVC x64 component is not installed' }
    $MsvcRoot = (Get-ChildItem -LiteralPath (Join-Path $installation 'VC\Tools\MSVC') -Directory | Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1).FullName
}
if (-not $SdkVersion) {
    $SdkVersion = (Get-ChildItem -LiteralPath (Join-Path $SdkRoot 'Include') -Directory | Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } | Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1).Name
}
if (-not $OutputRoot) { $OutputRoot = Join-Path $PSScriptRoot 'bin\0.6.2' }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
if ($Target -ne 'synthetic') {
    if (-not $DingTalkPath) {
        $candidates = @(Get-Process -Name DingTalk -ErrorAction SilentlyContinue | ForEach-Object { $_.Path } | Where-Object { $_ } | Sort-Object -Unique)
        if ($candidates.Count -ne 1) { throw 'Start DingTalk or pass -DingTalkPath to its DingTalk.exe' }
        $DingTalkPath = $candidates[0]
    }
    $DingTalkPath = (Resolve-Path -LiteralPath $DingTalkPath).Path
    if ([IO.Path]::GetFileName($DingTalkPath) -ine 'DingTalk.exe' -or -not (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $DingTalkPath) 'MainFrame.dll'))) { throw 'Expected DingTalk.exe beside MainFrame.dll' }
}
$compiler=Join-Path $MsvcRoot 'bin\Hostx64\x64\cl.exe'
if(-not(Test-Path -LiteralPath $compiler)){throw 'Installed x64 MSVC compiler unavailable'}
$sdkInclude=Join-Path $SdkRoot "Include\$SdkVersion"
$env:INCLUDE="$MsvcRoot\include;$sdkInclude\ucrt;$sdkInclude\um;$sdkInclude\shared"
$env:LIB="$MsvcRoot\lib\x64;$SdkRoot\Lib\$SdkVersion\um\x64;$SdkRoot\Lib\$SdkVersion\ucrt\x64"
$env:PATH="$MsvcRoot\bin\Hostx64\x64;$env:PATH"
$nativeRoot=Split-Path -Parent $PSScriptRoot
$utf8=[System.Text.UTF8Encoding]::new($false)
# Encode a Windows path as a C++ wide string literal.
function CppString([string]$value){return 'L"'+$value.Replace('\','\\').Replace('"','\"')+'"'}
# Run the installed compiler and stop immediately when it fails.
function Compile([string[]]$Arguments){& $compiler @Arguments;if($LASTEXITCODE -ne 0){throw 'Native bootstrap compilation failed'}}
foreach($flavor in @('synthetic','production')){
    if($Target -ne 'all' -and $Target -ne $flavor){continue}
    $out=Join-Path $OutputRoot $flavor
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    $synthetic=$flavor -eq 'synthetic'
    $suffix=if($synthetic){'Synthetic'}else{''}
    $fixtureExe=Join-Path $out 'DingTalkBridgeNativeSyntheticHost.exe'
    $controller=Join-Path $out "BridgeController$suffix.exe"
    $dll=Join-Path $out "BridgeBootstrap$suffix.dll"
    $exePath=if($synthetic){$fixtureExe}else{$DingTalkPath}
    $main=Join-Path (Split-Path -Parent $exePath) 'MainFrame.dll'
    $flavorNum=if($synthetic){2}else{1};$macro=if($synthetic){1}else{0}
    $header=@"
#pragma once
#define DTB_NATIVE_SYNTHETIC $macro
#define BRIDGE_SYNTHETIC DTB_NATIVE_SYNTHETIC
namespace bridge_native {
constexpr unsigned kBuildFlavor=$flavorNum;
constexpr wchar_t kExpectedTargetPath[]=$(CppString $exePath);
constexpr wchar_t kExpectedControllerPath[]=$(CppString $controller);
constexpr wchar_t kExpectedDllPath[]=$(CppString $dll);
constexpr wchar_t kExpectedMainFramePath[]=$(CppString $main);
}
"@
    [IO.File]::WriteAllText((Join-Path $out 'BuildIdentity.h'),$header,$utf8)
    $base=@('/nologo','/utf-8','/EHsc','/MD','/D_ITERATOR_DEBUG_LEVEL=0','/std:c++17','/O2','/W4','/guard:cf','/GS','/DUNICODE','/D_UNICODE',"/DDTB_NATIVE_SYNTHETIC=$macro",('/I'+$out),('/I'+$PSScriptRoot))
    if($synthetic){Compile ($base+@((Join-Path $PSScriptRoot 'SyntheticHost.cpp'),('/Fo'+(Join-Path $out 'Host.obj')),('/Fe'+$fixtureExe),'/link','/guard:cf','/DYNAMICBASE','/NXCOMPAT','user32.lib','advapi32.lib'))}
    Compile ($base+@((Join-Path $PSScriptRoot 'Controller.cpp'),('/Fo'+(Join-Path $out 'Controller.obj')),('/Fe'+$controller),'/link','/guard:cf','/DYNAMICBASE','/NXCOMPAT','user32.lib','advapi32.lib','bcrypt.lib'))
    $core=if($synthetic -and $SyntheticCore -eq 'stub'){Join-Path $PSScriptRoot 'SyntheticCore.cpp'}else{Join-Path $nativeRoot 'RootDetourCore.cpp'}
    if(-not(Test-Path -LiteralPath $core)){throw 'RootDetourCore.cpp unavailable; production cannot use the synthetic stub'}
    $objects=@();$sources=@((Join-Path $PSScriptRoot 'BootstrapDll.cpp'),$core)
    if($core -notlike '*SyntheticCore.cpp'){
        $sources+=@((Join-Path $nativeRoot 'payload\payload_parser.cpp'),(Join-Path $nativeRoot 'payload\safe_read.cpp'),(Join-Path $nativeRoot 'history\HistoryReader.cpp'))
        foreach($name in @('buffer.c','hook.c','trampoline.c','hde\hde64.c')){$sources+=Join-Path $nativeRoot "minhook\src\$name"}
    }
    foreach($source in $sources){
        $stem=[IO.Path]::GetFileNameWithoutExtension($source);$obj=Join-Path $out ($stem+'.obj');$objects+=$obj
        $flags=$base+@('/c',$source,('/Fo'+$obj),('/I'+$nativeRoot),('/I'+(Join-Path $nativeRoot 'minhook\include')))
        if($source.EndsWith('.c')){$flags+=@('/TC')}
        Compile $flags
    }
    & (Join-Path $MsvcRoot 'bin\Hostx64\x64\link.exe') /nologo /DLL /guard:cf /DYNAMICBASE /NXCOMPAT @objects ("/OUT:$dll") user32.lib advapi32.lib bcrypt.lib version.lib
    if($LASTEXITCODE -ne 0){throw 'Native bootstrap DLL link failed'}
    $manifest=@{schema='dingtalkbridge.native-bootstrap-build.v1';flavor=$flavor;synthetic_core=if($synthetic){$SyntheticCore}else{'root'};target_path=$exePath;client_hash_required=$false;compatibility='notification400-layout-v1';controller_sha256=(Get-FileHash -LiteralPath $controller -Algorithm SHA256).Hash.ToLowerInvariant();dll_sha256=(Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash.ToLowerInvariant()}
    [IO.File]::WriteAllText((Join-Path $out 'build.json'),($manifest|ConvertTo-Json)+[Environment]::NewLine,$utf8)
}
# Build only. Never launches/attaches to DingTalk or any process.
