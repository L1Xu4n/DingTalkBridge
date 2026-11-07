param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
if (-not $SkipBuild) { & (Join-Path $PSScriptRoot 'Build.ps1') | Out-Null }
$executable = Join-Path $PSScriptRoot 'bin\DingTalkRegionCalibration.exe'
$testDirectory = Join-Path ([IO.Path]::GetTempPath()) ('DingTalkBridge-calibration-parameters-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testDirectory | Out-Null
$checks = New-Object 'System.Collections.Generic.List[object]'
function Invoke-Calibration([string[]]$ToolArguments,[int]$ExpectedExit,[string]$ExpectedStatus,[string]$ExpectedReason='') {
    # All tests use parameter-only, self-test, or cancel modes. --show is prohibited.
    if ('--show' -in $ToolArguments -or '--show-once' -in $ToolArguments -or '--check-live' -in $ToolArguments) { throw 'Live/UI modes are not allowed in this test suite.' }
    $processInfo = New-Object Diagnostics.ProcessStartInfo
    $processInfo.FileName = $executable
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $quoted = foreach ($argument in $ToolArguments) {
        if ($argument.Contains('"') -or $argument.EndsWith('\')) { throw 'Unsupported test argument.' }
        '"' + $argument + '"'
    }
    $processInfo.Arguments = $quoted -join ' '
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $processInfo
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    $process.Dispose()
    if ($exitCode -ne $ExpectedExit -or $stderr) { throw "Unexpected helper outcome: exit=$exitCode stderr=$stderr stdout=$stdout" }
    $result = $stdout | ConvertFrom-Json
    if (($ExpectedStatus -and $result.status -ne $ExpectedStatus) -or ($ExpectedReason -and $result.reason -ne $ExpectedReason)) { throw "Unexpected result: $stdout" }
    if ($result.authorization_granted -eq $true -or $result.usable -eq $true -or $result.ui_shown -eq $true -or $result.pixels_read -eq $true -or $result.ocr_started -eq $true) { throw 'A parameter test unexpectedly enabled an active capability.' }
    $checks.Add([pscustomobject]@{mode=($ToolArguments -join ' ');status=$result.status;reason=$result.reason;passed=$true})
    return $result
}
function Write-Json($Path,$Value) { [IO.File]::WriteAllText($Path,($Value | ConvertTo-Json -Depth 30),[Text.UTF8Encoding]::new($false)) }
function Rect([int]$Left,[int]$Top,[int]$Right,[int]$Bottom) { return [ordered]@{left=$Left;top=$Top;right=$Right;bottom=$Bottom} }
function Refresh-Sources {
    Write-Json $script:scenePath $script:scene
    Write-Json $script:wgcPath $script:wgc
    $script:descriptor.sources.scene.sha256=(Get-FileHash -LiteralPath $script:scenePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $script:descriptor.sources.wgc.sha256=(Get-FileHash -LiteralPath $script:wgcPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Json $script:descriptorPath $script:descriptor
}
try {
    $self = Invoke-Calibration @('--self-test') 0 'self_test_passed'
    $lifecycle = Invoke-Calibration @('--self-test-lifecycle') 0 'lifecycle_test_passed'
    if ($lifecycle.orphan_children -ne 0 -or $lifecycle.cases -ne 4) { throw 'Synthetic process lifecycle coverage did not pass.' }
    [void](Invoke-Calibration @() 2 $null)
    [void](Invoke-Calibration @('--confirm') 3 'rejected' 'unsupported_mode')
    [void](Invoke-Calibration @('--grant') 3 'rejected' 'unsupported_mode')
    [void](Invoke-Calibration @('--check') 3 'rejected' 'invalid_arguments')
    [void](Invoke-Calibration @('--check','--descriptor','relative.json') 3 'rejected' 'absolute_path_required')
    $now = [DateTimeOffset]::UtcNow
    $appPath = 'D:\Program Files\DingDing\main\current_new\DingTalk.exe'
    $fakeHash = 'a' * 64
    $topBounds=Rect 100 100 2100 1200
    $messageBounds=Rect 700 200 2080 1180
    $semanticBounds=Rect 600 160 2080 1180
    $roi=Rect 700 200 2080 1000
    $scenePath=Join-Path $testDirectory 'synthetic-scene.json'
    $wgcPath=Join-Path $testDirectory 'synthetic-wgc.json'
    $descriptorPath=Join-Path $testDirectory 'synthetic.descriptor.json'
    $scene=[ordered]@{
        type='scene';status='no_safe_unmasked_region';observed_at=$now.ToString('o');metadata_only=$true;text_properties_read=$false;capture_permitted=$false;coordinate_space='physical_screen_pixels';dpi_awareness='per_monitor_v2'
        candidate_scene=[ordered]@{
            toplevel=[ordered]@{hwnd='101';pid=123;session_id=1;native_class='Qt51511QWindowIcon';verified_process_path=$appPath;visible=$true;iconic=$false;window_rect_screen=$topBounds;dwm_frame_rect_screen=$topBounds;dwm_frame_hresult=0;dwm_cloak_hresult=0;dwm_cloaked=0;display_affinity_read_ok=$true;display_affinity=0}
            selected_cef_hwnd='103';selected_semantic_hwnd='102';selected_cef_rect_screen=$messageBounds;selected_semantic_rect_screen=$semanticBounds;selected_cef_native_class='CefBrowserWindow';selected_semantic_native_class='Qt51511QWindowIcon';message_roi_screen=$roi
            coverage=[ordered]@{complete=$true;provider_errors=0;native_child_errors=0}
            calibration_gate=[ordered]@{schema_version=1;technical_complete=$true;technical_errors=@();known_strong_input_geometry_complete=$true;known_strong_input_rects_screen=@((Rect 700 1040 2080 1140));semantic_incomplete=$true;classified_provider_error_count=0;unclassified_provider_error_count=0;semantic_error_codes=@()}
            layout_whitelist_exemptions=0;outside_source_tree_ignored_count=0;root_window_value_exception_applied=$false;gpu_redaction_before_cpu_required=$true
        }
    }
    $wgc=[ordered]@{
        status='metadata_ready_review_required';capture_started=$false;frame_pool_created=$false;cpu_image_created=$false;capture_ready=$false;coordinate_mapping_ambiguous=$false
        item_created=$true;device_supported=$true;display_affinity_read=$true;create_hresult=0;display_affinity=0
        same_session=$true;verified_ancestry=$true;size_matches_dwm=$true;size_matches_window=$true;roi_mapping_valid=$true;native_metadata_stable=$true;new_scene_proof_review_required=$true
        coordinate_basis='dwm_extended_frame_bounds';toplevel_hwnd=257;toplevel_pid=123;toplevel_class='Qt51511QWindowIcon';window_rect_screen=$topBounds;dwm_frame_bounds_screen=$topBounds;cef_rect_screen=$messageBounds;selected_roi_screen=$roi
        item_width=2000;item_height=1100;roi_texture_pixels=(Rect 600 100 1980 900)
        application_identity=[ordered]@{path=$appPath;file_version='9.0.5.261029005';file_sha256=$fakeHash}
    }
    $descriptor=[ordered]@{
        schema='dingtalkbridge.region-calibration-descriptor.v1';intent='pending_semantic_calibration_only';capture_permitted=$false;coordinate_space='physical_screen_pixels';issued_utc=$now.ToString('o');expires_utc=$now.AddMinutes(3).ToString('o')
        target=[ordered]@{path=$appPath;file_version='9.0.5.261029005';file_sha256=$fakeHash;pid=123;session_id=1;process_start_utc=$now.AddHours(-1).ToString('o');toplevel=[ordered]@{hwnd='101';class_name='Qt51511QWindowIcon';bounds=$topBounds};semantic=[ordered]@{hwnd='102';class_name='Qt51511QWindowIcon';bounds=$semanticBounds};message=[ordered]@{hwnd='103';class_name='CefBrowserWindow';bounds=$messageBounds}}
        candidate_roi_screen=$roi
        dwm_frame_bounds_screen=$topBounds
        sources=[ordered]@{scene=[ordered]@{path=$scenePath;sha256=''};wgc=[ordered]@{path=$wgcPath;sha256=''}}
    }
    $descriptor.target.process_start_filetime=$now.AddHours(-1).UtcDateTime.ToFileTimeUtc().ToString([Globalization.CultureInfo]::InvariantCulture)
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 0 'descriptor_valid_unconfirmed')
    $descriptor.schema='dingtalkbridge.region-calibration-pending.v1'
    Write-Json $descriptorPath $descriptor
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'unsupported_schema')
    $descriptor.schema='dingtalkbridge.region-calibration-descriptor.v1'
    $descriptor.authorization_granted=$true
    Write-Json $descriptorPath $descriptor
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'unsafe_authorization_granted')
    $descriptor.authorization_granted=$false
    Refresh-Sources
    $scene.candidate_scene.coverage.complete=$false
    $scene.candidate_scene.coverage.provider_errors=3
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'unclassified_or_technical_scene_error')
    $scene.candidate_scene.calibration_gate.classified_provider_error_count=3
    $scene.candidate_scene.calibration_gate.semantic_error_codes=@('semantic_scan_incomplete')
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 0 'descriptor_valid_unconfirmed')
    $scene.candidate_scene.calibration_gate.technical_complete=$false
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'unsafe_technical_complete')
    [void](Invoke-Calibration @('--check-once-descriptor','--descriptor',$descriptorPath) 0 'once_preview_descriptor_valid_backend_required')
    $scene.candidate_scene.calibration_gate.technical_complete=$true
    $scene.candidate_scene.calibration_gate.unclassified_provider_error_count=1
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'unclassified_or_technical_scene_error')
    [void](Invoke-Calibration @('--check-once-descriptor','--descriptor',$descriptorPath) 3 'rejected' 'unclassified_or_technical_scene_error')
    $scene.candidate_scene.calibration_gate.unclassified_provider_error_count=0
    $scene.candidate_scene.calibration_gate.known_strong_input_geometry_complete=$false
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'unsafe_known_strong_input_geometry_complete')
    $scene.candidate_scene.calibration_gate.known_strong_input_geometry_complete=$true
    $scene.candidate_scene.calibration_gate.known_strong_input_rects_screen=@((Rect 700 900 2080 1140))
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'known_strong_input_overlaps_roi')
    $scene.candidate_scene.calibration_gate.known_strong_input_rects_screen=@((Rect 700 1040 2080 1140))
    $scene.candidate_scene.coverage.complete=$true
    $scene.candidate_scene.coverage.provider_errors=0
    $scene.candidate_scene.calibration_gate.classified_provider_error_count=0
    $scene.candidate_scene.calibration_gate.semantic_error_codes=@()
    $scene.candidate_scene.root_window_value_exception_applied=$true
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'unsafe_root_window_value_exception_applied')
    $scene.candidate_scene.root_window_value_exception_applied=$false
    $wgc.capture_started=$true
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'unsafe_capture_started')
    $wgc.capture_started=$false
    $wgc.roi_texture_pixels.right=1979
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'texture_mapping_mismatch')
    $wgc.roi_texture_pixels.right=1980
    $descriptor.candidate_roi_screen=Rect 500 200 2080 1000
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'roi_outside_message_bounds')
    $descriptor.candidate_roi_screen=$roi
    $descriptor.target.path='C:\Windows\System32\notepad.exe'
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'unexpected_app_path')
    $descriptor.target.path=$appPath
    $descriptor.expires_utc=$now.AddSeconds(-1).ToString('o')
    Refresh-Sources
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'expired_or_invalid_descriptor_time')
    $descriptor.expires_utc=$now.AddMinutes(3).ToString('o')
    Refresh-Sources
    [IO.File]::AppendAllText($scenePath,' ')
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'source_hash_mismatch')
    Refresh-Sources
    $descriptor.sources.scene.path=$wgcPath
    $descriptor.sources.scene.sha256=$descriptor.sources.wgc.sha256
    Write-Json $descriptorPath $descriptor
    [void](Invoke-Calibration @('--check','--descriptor',$descriptorPath) 3 'rejected' 'distinct_sources_required')
    $descriptor.sources.scene.path=$scenePath
    Refresh-Sources
    $pending=Join-Path $testDirectory 'cancel.pending.json'
    # Prove explicit cancellation replaces an existing unusable pending marker.
    Write-Json $pending @{status='pending_review';user_semantic_confirmation=$true;authorization_granted=$false;usable=$false}
    [void](Invoke-Calibration @('--cancel','--output',$pending) 0 'cancelled')
    $cancelled=Get-Content -LiteralPath $pending -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cancelled.status -ne 'cancelled' -or $cancelled.user_semantic_confirmation -ne $false -or $cancelled.authorization_granted -ne $false -or $cancelled.usable -ne $false -or $null -ne $cancelled.confirmed_utc) { throw 'Cancellation failed to revoke the pending marker.' }
    [void](Invoke-Calibration @('--cancel','--output',(Join-Path $testDirectory 'not-pending.json')) 3 'rejected' 'pending_json_extension_required')
    $report=[ordered]@{status='passed';checked_at=[DateTimeOffset]::UtcNow.ToString('o');executable_sha256=(Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant();parameter_cases=$checks.Count;internal_checks=[int]$self.checks;lifecycle_cases=[int]$lifecycle.cases;orphan_children=[int]$lifecycle.orphan_children;ui_shown=$false;pixels_read=$false;ocr_started=$false;live_target_queried=$false;authorization_granted=$false;checks=$checks}
    Write-Json (Join-Path $PSScriptRoot 'test-result.json') $report
    [pscustomobject]@{status='passed';parameter_cases=$checks.Count;internal_checks=[int]$self.checks;lifecycle_cases=[int]$lifecycle.cases;orphan_children=[int]$lifecycle.orphan_children;ui_shown=$false;pixels_read=$false;live_target_queried=$false}
} finally {
    # Only delete files created in this unique, verified temporary test directory.
    $resolvedTestDirectory=[IO.Path]::GetFullPath($testDirectory)
    $resolvedTemporaryRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
    if (-not $resolvedTestDirectory.StartsWith($resolvedTemporaryRoot,[StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $resolvedTestDirectory) -notlike 'DingTalkBridge-calibration-parameters-*') { throw 'Unexpected test cleanup path.' }
    foreach ($testFile in Get-ChildItem -LiteralPath $resolvedTestDirectory -File -Force) { Remove-Item -LiteralPath $testFile.FullName -Force }
    Remove-Item -LiteralPath $resolvedTestDirectory
}
