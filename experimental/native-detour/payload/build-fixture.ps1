$ErrorActionPreference = 'Stop'
$compilerRoot = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207'
$sdkRoot = 'C:\Program Files (x86)\Windows Kits\10'
$sdkVersion = '10.0.26100.0'
$compiler = Join-Path $compilerRoot 'bin\Hostx64\x64\cl.exe'
$outputDirectory = Join-Path $PSScriptRoot 'build'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$env:PATH = (Split-Path $compiler) + ';' + $env:PATH
$arguments = @(
    '/nologo', '/std:c++20', '/EHsc', '/MD', '/W4', '/WX', '/O2', '/utf-8',
    ('/I' + (Join-Path $compilerRoot 'include')),
    ('/I' + (Join-Path $sdkRoot ('Include\' + $sdkVersion + '\ucrt'))),
    ('/I' + (Join-Path $sdkRoot ('Include\' + $sdkVersion + '\shared'))),
    ('/I' + (Join-Path $sdkRoot ('Include\' + $sdkVersion + '\um'))),
    ('/Fo' + $outputDirectory + '\'),
    ('/Fe' + (Join-Path $outputDirectory 'payload_fixture_tests.exe')),
    (Join-Path $PSScriptRoot 'safe_read.cpp'),
    (Join-Path $PSScriptRoot 'payload_parser.cpp'),
    (Join-Path $PSScriptRoot 'payload_fixture_tests.cpp'),
    '/link',
    ('/LIBPATH:' + (Join-Path $compilerRoot 'lib\x64')),
    ('/LIBPATH:' + (Join-Path $sdkRoot ('Lib\' + $sdkVersion + '\ucrt\x64'))),
    ('/LIBPATH:' + (Join-Path $sdkRoot ('Lib\' + $sdkVersion + '\um\x64'))),
    'kernel32.lib'
)
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw 'Payload fixture compilation failed.' }
& (Join-Path $outputDirectory 'payload_fixture_tests.exe')
if ($LASTEXITCODE -ne 0) { throw 'Payload fixture tests failed.' }
