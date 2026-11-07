# Structured pipe-only helper. Never invoke directly to print real OCR text.
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding=[System.Text.UTF8Encoding]::new($false)
$result=@{schema_version=1;source='windows_ocr_screen';kind='screen_context';status='ocr_unavailable';text='';line_count=0;word_count=0;confidence=0;numeric_confidence_available=$false}
$phase='request'
$capture=$null
try {
    $requestText=[Console]::In.ReadToEnd().TrimStart([char]0xfeff)
    if($requestText.Length -gt 65536){throw 'request_limit'}
    $request=$requestText | ConvertFrom-Json
    $g=$request.geometry
    if($g.coordinate_space -ne 'physical_screen_pixels' -or $g.dpi_awareness -ne 'per_monitor_v2' -or $g.host_class -ne 'im_chat::DTIMChatComponent'){throw 'geometry_contract'}
    if($g.geometry_safe -ne $true -or @($g.composer_exclusions_screen).Count -lt 1){throw 'geometry_proof'}
    $phase='native_compile'
    $framework=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
    $references=@('System.Drawing.dll',(Join-Path $framework 'WPF\UIAutomationClient.dll'),(Join-Path $framework 'WPF\UIAutomationTypes.dll'),(Join-Path $framework 'WPF\WindowsBase.dll'))
    Add-Type -Path (Join-Path $PSScriptRoot 'SafeWindowCrop.cs') -ReferencedAssemblies $references | Out-Null
    if(-not [SafeWindowCrop]::EnterPhysicalPixels()){throw 'dpi_unavailable'}
    $excluded=New-Object 'System.Collections.Generic.List[int[]]'
    foreach($rect in @($g.composer_exclusions_screen)+@($g.editable_exclusions_screen)){
        if($null -ne $rect){$excluded.Add([int[]]$rect)}
    }
    $phase='capture'
    $capture=[SafeWindowCrop]::Capture([long]$g.hwnd,[long]$g.message_hwnd,[int]$g.pid,[long]$g.common_ancestor_hwnd,[int[]]$g.window_rect_screen,[int[]]$g.message_window_rect_screen,[int[]]$g.message_rect_screen,$excluded.ToArray(),[string]$request.stop_file)
    $result.status=$capture.Status
    if($capture.Status -eq 'available'){
        if(Test-Path -LiteralPath $request.stop_file){throw 'stopped'}
        . (Join-Path $PSScriptRoot 'LocalOcr.ps1')
        $phase='recognize'
        $ocr=Invoke-LocalWindowsOcr -Png $capture.Png
        if(Test-Path -LiteralPath $request.stop_file){throw 'stopped'}
        $result.status=if([string]::IsNullOrWhiteSpace($ocr.text)){'no_text'}else{'available'}
        $result.text=$ocr.text
        $result.observed_at=$capture.ObservedAt
        $result.line_count=$ocr.line_count
        $result.word_count=$ocr.word_count
        $result.width=$capture.Width
        $result.height=$capture.Height
        $result.language=$ocr.language
        $result.counts_method=$ocr.counts_method
        $result.capture_method='PrintWindow_clipped_dc'
        $result.partial=$true
    }
} catch {
    $result.text=''
    $result.status='ocr_unavailable'
    $result.error_code='capture_or_ocr_failed'
    $result.error_stage=if($phase -eq 'recognize' -and $script:OcrPhase){$script:OcrPhase}else{$phase}
    $result.error_type=$_.Exception.GetType().Name
} finally {
    if($null -ne $capture -and $null -ne $capture.Png){
        [Array]::Clear($capture.Png,0,$capture.Png.Length)
        $capture.Png=$null
    }
}
[Console]::Out.WriteLine(($result | ConvertTo-Json -Depth 5 -Compress))
