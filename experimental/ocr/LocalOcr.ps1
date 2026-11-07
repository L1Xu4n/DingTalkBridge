function Invoke-LocalWindowsOcr {
    param([Parameter(Mandatory=$true)][byte[]]$Png)
    $stream=$null
    $writer=$null
    $software=$null
    try {
        $script:OcrPhase='winrt_activation'
        Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
        [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
        [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime] | Out-Null
        [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
        [Windows.Storage.Streams.DataWriter,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
        [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
        [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
        $asTask=@([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        })[0]
        function Await-OcrOperation($operation,[Type]$resultType) {
            $task=$asTask.MakeGenericMethod($resultType).Invoke($null,@($operation))
            $task.GetAwaiter().GetResult()
        }
        $engine=[Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('zh-CN'))
        if($null -eq $engine){throw 'ocr_language_unavailable'}
        $script:OcrPhase='image_stream'
        $stream=[Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
        $writer=[Windows.Storage.Streams.DataWriter]::new($stream)
        $writer.WriteBytes($Png)
        $null=Await-OcrOperation ($writer.StoreAsync()) ([uint32])
        $null=$writer.DetachStream()
        $writer.Dispose();$writer=$null
        $stream.Seek(0)
        $script:OcrPhase='image_decode'
        $decoder=Await-OcrOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $software=Await-OcrOperation ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,[Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $script:OcrPhase='recognize'
        $recognized=Await-OcrOperation ($engine.RecognizeAsync($software)) ([Windows.Media.Ocr.OcrResult])
        $text=[string]$recognized.Text
        if([System.Text.Encoding]::UTF8.GetByteCount($text) -gt 131072){throw 'text_limit'}
        # WinPS 5.1's dynamic WinRT collection adapter may fail enumerating
        # OcrLine/OcrWord. Text itself is available; report explicit text-derived
        # line/token counts rather than pretending to expose engine word counts.
        $lineCount=[System.Text.RegularExpressions.Regex]::Matches($text,'[^\r\n]+').Count
        $wordCount=[System.Text.RegularExpressions.Regex]::Matches($text,'\S+').Count
        return @{text=$text;line_count=$lineCount;word_count=$wordCount;counts_method='recognized_text_lines_and_whitespace_tokens';language=$engine.RecognizerLanguage.LanguageTag}
    } finally {
        if($null -ne $writer){$writer.Dispose()}
        if($null -ne $software){$software.Dispose()}
        if($null -ne $stream){$stream.Dispose()}
    }
}
