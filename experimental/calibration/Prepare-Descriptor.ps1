param(
    [Parameter(Mandatory=$true)][string]$ScenePath,
    [Parameter(Mandatory=$true)][string]$WgcMetadataPath,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [switch]$OnceBackendReview
)
# This prepares metadata only. It never launches the UI or reads any pixels/text.
$ErrorActionPreference = 'Stop'
$expectedImage = 'D:\Program Files\DingDing\main\current_new\DingTalk.exe'
$sceneFile = Get-Item -LiteralPath $ScenePath
$wgcFile = Get-Item -LiteralPath $WgcMetadataPath
$scene = Get-Content -LiteralPath $sceneFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
$wgc = Get-Content -LiteralPath $wgcFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
if ($scene.type -ne 'scene' -or $scene.metadata_only -ne $true -or $scene.text_properties_read -ne $false -or $scene.capture_permitted -ne $false) { throw 'A metadata-only full scene report is required.' }
$gate = $scene.candidate_scene.calibration_gate
if ($gate.schema_version -ne 1 -or $gate.unclassified_provider_error_count -ne 0 -or $gate.classified_provider_error_count -ne $scene.candidate_scene.coverage.provider_errors -or $scene.candidate_scene.coverage.native_child_errors -ne 0) { throw 'Scene evidence contains unclassified/native errors. No descriptor was created.' }
if (-not $OnceBackendReview -and ($gate.technical_complete -ne $true -or $gate.known_strong_input_geometry_complete -ne $true -or $gate.technical_errors.Count -ne 0)) { throw 'Technical scene evidence is incomplete. The default pending-only display remains blocked.' }
if ($scene.candidate_scene.coverage.complete -ne $true -and ($gate.semantic_incomplete -ne $true -or $gate.semantic_error_codes.Count -eq 0)) { throw 'Incomplete automatic classification must have explicit semantic-only error evidence.' }
foreach ($code in $gate.semantic_error_codes) { if ($code -notin @('unknown_semantic','semantic_scan_incomplete')) { throw 'An unclassified semantic error remains. No descriptor was created.' } }
if ($wgc.status -ne 'metadata_ready_review_required' -or $wgc.capture_started -ne $false -or $wgc.cpu_image_created -ne $false -or $wgc.frame_pool_created -ne $false) { throw 'Uncaptured WGC metadata is required.' }
if ($wgc.application_identity.path -ine $expectedImage -or $scene.candidate_scene.toplevel.verified_process_path -ine $expectedImage) { throw 'Unexpected application path.' }
$now = [DateTimeOffset]::UtcNow
foreach ($sourceFile in @($sceneFile,$wgcFile)) {
    if ($sourceFile.LastWriteTimeUtc -lt $now.UtcDateTime.AddSeconds(-90) -or $sourceFile.LastWriteTimeUtc -gt $now.UtcDateTime.AddSeconds(30)) { throw 'Metadata is stale. Refresh both metadata-only reports first.' }
}
$candidate = $scene.candidate_scene
$targetPid = [int]$candidate.toplevel.pid
$targetProcess = Get-Process -Id $targetPid
if ($targetProcess.Path -ine $expectedImage -or $targetProcess.SessionId -ne [Diagnostics.Process]::GetCurrentProcess().SessionId) { throw 'The target process identity or session changed.' }
function Convert-Rect($rect) {
    if ($null -ne $rect.left) { return [ordered]@{left=[int]$rect.left;top=[int]$rect.top;right=[int]$rect.right;bottom=[int]$rect.bottom} }
    return [ordered]@{left=[int]$rect.x;top=[int]$rect.y;right=([int]$rect.x+[int]$rect.width);bottom=([int]$rect.y+[int]$rect.height)}
}
foreach ($required in @('selected_semantic_rect_screen','selected_semantic_native_class','selected_cef_rect_screen','selected_cef_native_class')) {
    if ($null -eq $candidate.$required) { throw "Full scene metadata is missing $required." }
}
$descriptor = [ordered]@{
    schema='dingtalkbridge.region-calibration-descriptor.v1'
    intent='pending_semantic_calibration_only'
    capture_permitted=$false
    authorization_granted=$false
    usable=$false
    independent_once_backend_review_required=[bool]$OnceBackendReview
    coordinate_space='physical_screen_pixels'
    issued_utc=$now.ToString('o')
    expires_utc=$now.AddMinutes(3).ToString('o')
    target=[ordered]@{
        path=$expectedImage
        file_version=[string]$wgc.application_identity.file_version
        file_sha256=([string]$wgc.application_identity.file_sha256).ToLowerInvariant()
        pid=$targetPid
        session_id=[int]$targetProcess.SessionId
        process_start_utc=([DateTimeOffset]$targetProcess.StartTime.ToUniversalTime()).ToString('o')
        process_start_filetime=$targetProcess.StartTime.ToUniversalTime().ToFileTimeUtc().ToString([Globalization.CultureInfo]::InvariantCulture)
        toplevel=[ordered]@{hwnd=[string]$candidate.toplevel.hwnd;class_name=[string]$candidate.toplevel.native_class;bounds=(Convert-Rect $candidate.toplevel.window_rect_screen)}
        semantic=[ordered]@{hwnd=[string]$candidate.selected_semantic_hwnd;class_name=[string]$candidate.selected_semantic_native_class;bounds=(Convert-Rect $candidate.selected_semantic_rect_screen)}
        message=[ordered]@{hwnd=[string]$candidate.selected_cef_hwnd;class_name=[string]$candidate.selected_cef_native_class;bounds=(Convert-Rect $candidate.selected_cef_rect_screen)}
    }
    candidate_roi_screen=(Convert-Rect $candidate.message_roi_screen)
    dwm_frame_bounds_screen=(Convert-Rect $wgc.dwm_frame_bounds_screen)
    sources=[ordered]@{
        scene=[ordered]@{path=$sceneFile.FullName;sha256=(Get-FileHash -LiteralPath $sceneFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
        wgc=[ordered]@{path=$wgcFile.FullName;sha256=(Get-FileHash -LiteralPath $wgcFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
    }
}
$destination = [IO.Path]::GetFullPath($OutputPath)
if ($destination -ieq $sceneFile.FullName -or $destination -ieq $wgcFile.FullName -or $destination -ieq $expectedImage -or -not $destination.EndsWith('.descriptor.json',[StringComparison]::OrdinalIgnoreCase)) { throw 'Use a separate .descriptor.json output file.' }
if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($destination)) -PathType Container)) { throw 'The output directory must already exist.' }
$json = $descriptor | ConvertTo-Json -Depth 20
[IO.File]::WriteAllText($destination,$json+[Environment]::NewLine,(New-Object Text.UTF8Encoding($false)))
Write-Output ([pscustomobject]@{status='descriptor_created_unconfirmed';path=$destination;authorization_granted=$false;usable=$false;ui_shown=$false;pixels_read=$false})
