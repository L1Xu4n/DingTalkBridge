[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$PythonPath,
    [switch]$CheckOnly,
    [switch]$SelfTest
)
# Explicit entry point only. This script never confirms, signals an event, or reads pixels.
# The separate visible UI still requires the person's current checkbox + button action.
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$expectedImage = 'D:\Program Files\DingDing\main\current_new\DingTalk.exe'
$failureReason = 'launcher_validation_failed'
$uiLaunched = $false

function Write-SafeState([string]$Status, [string]$Reason = '') {
    [ordered]@{status=$Status;reason=$Reason;ui_launched=$script:uiLaunched;authorization_created_by_launcher=$false;launcher_captures_pixels=$false} | ConvertTo-Json -Compress
}
function Require([bool]$Condition) { if (-not $Condition) { throw 'launcher_validation_failed' } }
function Absolute-File([string]$PathValue) {
    Require (-not [string]::IsNullOrWhiteSpace($PathValue))
    $expanded = [Environment]::ExpandEnvironmentVariables($PathValue)
    $full = [IO.Path]::GetFullPath($expanded)
    Require (Test-Path -LiteralPath $full -PathType Leaf)
    return $full
}
function Selected-Config([string]$Explicit, [bool]$ExplicitWasBound, [string]$EnvironmentValue, [bool]$EnvironmentExists, [string]$LocalDefault) {
    if ($ExplicitWasBound) { return (Absolute-File $Explicit) }
    if ($EnvironmentExists) { return (Absolute-File $EnvironmentValue) }
    return (Absolute-File $LocalDefault)
}
function Integer-Value($Value) {
    Require ($Value -is [int] -or $Value -is [long] -or $Value -is [uint32] -or $Value -is [uint64])
    return [long]$Value
}
function Handle-Value($Value) {
    Require ($Value -is [string] -and $Value -match '^[0-9a-fA-F]{1,16}$')
    $number = [Convert]::ToInt64($Value,16)
    # MetadataProbe's JSON-number ABI must represent the complete handle exactly.
    Require ($number -gt 0 -and $number -le 9007199254740991L)
    return $number
}
function Rect-Array($Value) {
    Require ($null -ne $Value)
    if ($null -ne $Value.left) {
        $r = @((Integer-Value $Value.left),(Integer-Value $Value.top),(Integer-Value $Value.right),(Integer-Value $Value.bottom))
    } else {
        $x=Integer-Value $Value.x; $y=Integer-Value $Value.y; $width=Integer-Value $Value.width; $height=Integer-Value $Value.height
        $r=@($x,$y,($x+$width),($y+$height))
    }
    foreach ($coordinate in $r) { Require ($coordinate -ge -100000 -and $coordinate -le 100000) }
    Require ($r[2]-$r[0] -gt 0 -and $r[2]-$r[0] -le 16000 -and $r[3]-$r[1] -gt 0 -and $r[3]-$r[1] -le 16000)
    return ,$r
}
function Contains-Rect($Outer,$Inner) { return $Outer[0] -le $Inner[0] -and $Outer[1] -le $Inner[1] -and $Outer[2] -ge $Inner[2] -and $Outer[3] -ge $Inner[3] }
function Geometry-Request($Scene) {
    Require ($Scene.type -eq 'scene' -and $Scene.metadata_only -eq $true -and $Scene.text_properties_read -eq $false -and $Scene.capture_permitted -eq $false)
    Require ($Scene.coordinate_space -eq 'physical_screen_pixels' -and $Scene.dpi_awareness -eq 'per_monitor_v2')
    $candidate = $Scene.candidate_scene
    Require ($null -ne $candidate -and $candidate.toplevel.verified_process_path -ieq $script:expectedImage)
    $targetPid=Integer-Value $candidate.toplevel.pid; Require ($targetPid -gt 0 -and $targetPid -le [int]::MaxValue)
    $topHandle=Handle-Value $candidate.toplevel.hwnd; $semanticHandle=Handle-Value $candidate.selected_semantic_hwnd; $messageHandle=Handle-Value $candidate.selected_cef_hwnd
    Require ($topHandle -ne $semanticHandle -and $topHandle -ne $messageHandle -and $semanticHandle -ne $messageHandle)
    $topRect=Rect-Array $candidate.toplevel.window_rect_screen; $hostRect=Rect-Array $candidate.selected_semantic_rect_screen; $cefRect=Rect-Array $candidate.selected_cef_rect_screen; $roi=Rect-Array $candidate.message_roi_screen
    Require ((Contains-Rect $topRect $roi) -and (Contains-Rect $hostRect $roi) -and (Contains-Rect $cefRect $roi))
    return [ordered]@{metadata_mapping_only=$true;geometry=[ordered]@{
        # This is only a Win32 mapping request; MetadataProbe independently verifies it.
        # It does not change the scene's strict/effective input gate or authorize capture.
        geometry_safe=$true;coordinate_space='physical_screen_pixels';dpi_awareness='per_monitor_v2';verified_process_path=$script:expectedImage
        pid=$targetPid;hwnd=$semanticHandle;message_hwnd=$messageHandle;window_rect_screen=$hostRect;message_window_rect_screen=$cefRect;message_rect_screen=$roi
    }}
}
function Invoke-Private([string]$Executable,[string[]]$Arguments,[string]$InputLine='', [int]$TimeoutMs=30000) {
    $start=[Diagnostics.ProcessStartInfo]::new(); $start.FileName=$Executable; $start.UseShellExecute=$false; $start.CreateNoWindow=$true
    $start.RedirectStandardInput=$true; $start.RedirectStandardOutput=$true; $start.RedirectStandardError=$true
    $start.StandardOutputEncoding=[Text.UTF8Encoding]::new($false,$true); $start.StandardErrorEncoding=[Text.UTF8Encoding]::new($false,$true)
    $quoted=foreach($argument in $Arguments) {
        Require ($null -ne $argument -and -not $argument.Contains('"') -and -not $argument.Contains("`r") -and -not $argument.Contains("`n") -and -not $argument.EndsWith('\'))
        '"'+$argument+'"'
    }
    $start.Arguments=$quoted -join ' '
    $process=[Diagnostics.Process]::new(); $process.StartInfo=$start
    try {
        Require ($process.Start())
        $stdout=$process.StandardOutput.ReadToEndAsync(); $stderr=$process.StandardError.ReadToEndAsync()
        if ($InputLine) { $process.StandardInput.WriteLine($InputLine) }
        $process.StandardInput.Close()
        Require ($process.WaitForExit($TimeoutMs))
        Require ([Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($stdout,$stderr),3000))
        $out=$stdout.GetAwaiter().GetResult(); $err=$stderr.GetAwaiter().GetResult()
        Require ($process.ExitCode -eq 0 -and [string]::IsNullOrWhiteSpace($err) -and $out.Length -le 8MB)
        return $out
    } finally {
        try { if(-not $process.HasExited) { $process.Kill(); [void]$process.WaitForExit(3000) } } catch { }
        $process.Dispose()
    }
}
function Save-Json([string]$PathValue,$Value) { [IO.File]::WriteAllText($PathValue,($Value|ConvertTo-Json -Depth 80 -Compress)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false)) }
function Readiness-Reason($Reasons) {
    # Finite user-facing codes only. Never echo an arbitrary helper exception/string.
    foreach($reason in @($Reasons)) {
        if($reason -eq 'backend_not_compiled_or_stale'){return 'once_backend_not_compiled_or_stale'}
        if($reason -match '^(once_scene_|technical_scene_|once_alt|alternative_|once_known_input|once_owner|once_clip|once_padding)'){return 'technical_scene_evidence_incomplete'}
        if($reason -match '^(descriptor_|once_target_|once_window_|once_dwm_|once_wgc_|once_selected_)'){return 'fresh_source_or_target_changed'}
        if($reason -match '^backend_(binary|config|digest|binding)'){return 'backend_code_or_config_binding_changed'}
        if($reason -eq 'readiness_unavailable'){return 'backend_readiness_unavailable'}
    }
    return 'backend_not_ready'
}

try {
    if ($SelfTest) {
        # Pure synthetic parsing/config precedence. Never launches a process or opens UI.
        $top=@{left=10;top=20;right=2010;bottom=1220}; $message=@{x=600;y=200;width=1300;height=900}; $roi=@{left=600;top=200;right=1900;bottom=1000}
        $scene=[pscustomobject]@{type='scene';metadata_only=$true;text_properties_read=$false;capture_permitted=$false;coordinate_space='physical_screen_pixels';dpi_awareness='per_monitor_v2';candidate_scene=[pscustomobject]@{toplevel=[pscustomobject]@{pid=77;hwnd='ab10';verified_process_path=$expectedImage;window_rect_screen=[pscustomobject]$top};selected_semantic_hwnd='ab11';selected_cef_hwnd='ab12';selected_semantic_rect_screen=[pscustomobject]$message;selected_cef_rect_screen=[pscustomobject]$message;message_roi_screen=[pscustomobject]$roi}}
        $request=Geometry-Request $scene
        Require ($request.geometry.hwnd -eq [Convert]::ToInt64('ab11',16) -and $request.geometry.message_hwnd -eq [Convert]::ToInt64('ab12',16) -and $request.geometry.pid -eq 77)
        $scene.candidate_scene.selected_semantic_hwnd='bc21'; $scene.candidate_scene.selected_cef_hwnd='bc22'
        $changed=Geometry-Request $scene; Require ($changed.geometry.hwnd -ne $request.geometry.hwnd -and $changed.geometry.message_hwnd -ne $request.geometry.message_hwnd)
        $rejected=$false;try{Handle-Value 'not-a-handle'|Out-Null}catch{$rejected=$true};Require $rejected
        $rejected=$false;try{Rect-Array ([pscustomobject]@{left=0;top=0;right=0;bottom=100})|Out-Null}catch{$rejected=$true};Require $rejected
        $knownFile=$PSCommandPath
        Require ((Selected-Config $knownFile $true 'missing-env.json' $true 'missing-local.json') -eq $knownFile)
        Require ((Selected-Config '' $false $knownFile $true 'missing-local.json') -eq $knownFile)
        $rejected=$false;try{Selected-Config '' $false '' $true $knownFile|Out-Null}catch{$rejected=$true};Require $rejected
        Require ((Readiness-Reason @('technical_scene_gate_closed')) -eq 'technical_scene_evidence_incomplete')
        Require ((Readiness-Reason @('untrusted diagnostic text')) -eq 'backend_not_ready')
        Write-SafeState 'launcher_self_test_passed' 'nine_pure_checks_no_children'
        exit 0
    }
    $failureReason='config_unavailable'
    $selectedConfig=Selected-Config $ConfigPath ($PSBoundParameters.ContainsKey('ConfigPath')) $env:DINGTALKBRIDGE_CONFIG (Test-Path -LiteralPath 'Env:\DINGTALKBRIDGE_CONFIG') (Join-Path $projectRoot 'config.local.json')
    $failureReason='python_unavailable'
    $python=if($PSBoundParameters.ContainsKey('PythonPath')){Absolute-File $PythonPath}else{Absolute-File (Get-Command python.exe -ErrorAction Stop).Source}
    $failureReason='compiled_metadata_components_unavailable'
    $native=Absolute-File (Join-Path $projectRoot 'native\bin\DingTalkUia.exe')
    $metadataProbe=Absolute-File (Join-Path $projectRoot 'experimental\wgc\MetadataProbe.exe')
    $ui=Absolute-File (Join-Path $PSScriptRoot 'bin\DingTalkRegionCalibration.exe')
    $failureReason='once_backend_not_compiled'
    [void](Absolute-File (Join-Path $projectRoot 'experimental\wgc\WgcOnce.exe'))
    [void](Absolute-File (Join-Path $projectRoot 'experimental\wgc\WgcOnce.build.json'))
    $adapter=Absolute-File (Join-Path $PSScriptRoot 'BackendAdapter.py')
    $prepareBinding=Absolute-File (Join-Path $PSScriptRoot 'Prepare-BackendBinding.py')
    $prepareDescriptor=Absolute-File (Join-Path $PSScriptRoot 'Prepare-Descriptor.ps1')
    $runDirectory=Join-Path ([IO.Path]::GetTempPath()) ('DingTalkBridge-once-'+[Guid]::NewGuid().ToString('N'))
    [void][IO.Directory]::CreateDirectory($runDirectory)
    $scenePath=Join-Path $runDirectory 'scene.private.json'; $wgcPath=Join-Path $runDirectory 'wgc.private.json'
    $descriptorPath=Join-Path $runDirectory 'region.descriptor.json'; $bindingPath=Join-Path $runDirectory 'local.binding.json'; $auditPath=Join-Path $runDirectory 'result.once-audit.json'
    $failureReason='fresh_scene_unavailable'
    $sceneText=Invoke-Private $native @('--mode','scene','--process-path',$expectedImage)
    $scene=$sceneText|ConvertFrom-Json
    $request=Geometry-Request $scene
    [IO.File]::WriteAllText($scenePath,$sceneText,[Text.UTF8Encoding]::new($false))
    $failureReason='fresh_wgc_mapping_unavailable'
    $wgcText=Invoke-Private $metadataProbe @() ($request|ConvertTo-Json -Depth 12 -Compress)
    $wgc=$wgcText|ConvertFrom-Json
    Require ($wgc.status -eq 'metadata_ready_review_required' -and $wgc.capture_started -eq $false -and $wgc.frame_pool_created -eq $false -and $wgc.cpu_image_created -eq $false -and $wgc.capture_ready -eq $false)
    Require ($wgc.toplevel_pid -eq $scene.candidate_scene.toplevel.pid -and $wgc.toplevel_hwnd -eq (Handle-Value $scene.candidate_scene.toplevel.hwnd) -and $wgc.toplevel_class -eq $scene.candidate_scene.toplevel.native_class)
    $failureReason='application_identity_changed'
    $targetProcess=Get-Process -Id ([int]$scene.candidate_scene.toplevel.pid)
    Require ($targetProcess.Path -ieq $expectedImage -and $targetProcess.SessionId -eq [Diagnostics.Process]::GetCurrentProcess().SessionId)
    $version=[Diagnostics.FileVersionInfo]::GetVersionInfo($expectedImage)
    $applicationIdentity=[ordered]@{path=$expectedImage;file_version=$version.FileVersion;product_version=$version.ProductVersion;file_sha256=(Get-FileHash -LiteralPath $expectedImage -Algorithm SHA256).Hash.ToLowerInvariant();identity_observed_at=[DateTimeOffset]::UtcNow.ToString('o')}
    $wgc|Add-Member -NotePropertyName application_identity -NotePropertyValue $applicationIdentity -Force
    $wgc|Add-Member -NotePropertyName new_scene_proof_review_required -NotePropertyValue $true -Force
    $wgc|Add-Member -NotePropertyName policy -NotePropertyValue 'wgc_metadata_only_unconfirmed_roi' -Force
    Save-Json $wgcPath $wgc
    $failureReason='descriptor_metadata_pipeline_incomplete'
    & $prepareDescriptor -ScenePath $scenePath -WgcMetadataPath $wgcPath -OutputPath $descriptorPath -OnceBackendReview | Out-Null
    [void](Invoke-Private $ui @('--check-once-descriptor','--descriptor',$descriptorPath))
    $failureReason='backend_binding_unavailable'
    $bindingResult=(Invoke-Private $python @($prepareBinding,'--config',$selectedConfig,'--output',$bindingPath))|ConvertFrom-Json
    Require ($bindingResult.status -eq 'binding_prepared_not_authorization' -and $bindingResult.compiled_ready -eq $true -and $bindingResult.authorization_granted -eq $false)
    $failureReason='backend_not_ready'
    $ready=(Invoke-Private $python @('-I',$adapter,'readiness','--config',$selectedConfig,'--profile',$descriptorPath,'--ui-binding',$bindingPath) '' 45000)|ConvertFrom-Json
    $failureReason='backend_readiness_unavailable'
    Require ($ready.schema -eq 'dingtalkbridge.wgc-once-readiness.v1' -and $ready.authorization_granted -eq $false -and $ready.capture_started -eq $false -and $ready.pixels_read -eq $false)
    if($ready.ready -ne $true) { $failureReason=Readiness-Reason $ready.reasons; throw 'backend_not_ready' }
    $failureReason='technical_scene_evidence_incomplete'
    Require ($ready.technical_complete -eq $true -and $ready.known_strong_input_geometry_complete -eq $true -and @($ready.reasons).Count -eq 0)
    $failureReason='backend_code_or_config_binding_changed'
    $binding=Get-Content -LiteralPath $bindingPath -Raw -Encoding UTF8|ConvertFrom-Json
    Require ($ready.backend_digest -eq $binding.expected_backend_digest -and $ready.profile_sha256 -eq (Get-FileHash -LiteralPath $descriptorPath -Algorithm SHA256).Hash.ToLowerInvariant() -and $ready.scene_binding_sha256 -match '^[0-9a-f]{64}$')
    if($CheckOnly) { Write-SafeState 'check_only_ready_no_ui' 'current_person_confirmation_still_required'; exit 0 }
    $failureReason='confirmation_ui_unavailable'
    # This is the sole display point. No command-line confirmation or backend run occurs here.
    $uiLaunched=$true
    Write-SafeState 'opening_current_confirmation_ui' 'person_must_check_roi_and_click_once'
    [void](Invoke-Private $ui @('--show-once','--descriptor',$descriptorPath,'--backend-binding',$bindingPath,'--output',$auditPath) '' 600000)
    Write-SafeState 'confirmation_ui_closed' 'no_automatic_retry'
    exit 0
} catch {
    Write-SafeState 'rejected' $failureReason
    exit 3
}
