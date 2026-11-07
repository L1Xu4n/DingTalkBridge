[CmdletBinding()]
param([string]$DingTalkPath, [switch]$LegacyCollectors)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
# Compile project-owned sources only; no UI, capture, installation or settings change.
& (Join-Path $projectRoot 'experimental\native-detour\bootstrap\Build.ps1') -Target production -DingTalkPath $DingTalkPath
if ($LegacyCollectors) {
    & (Join-Path $projectRoot 'native\build.ps1')
    & (Join-Path $projectRoot 'experimental\calibration\Build.ps1')
    & (Join-Path $projectRoot 'experimental\wgc\Build.ps1') -Target all
}
Write-Output 'DingTalkBridge native builds completed. No capture was started.'
