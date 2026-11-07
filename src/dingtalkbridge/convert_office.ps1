param([string]$Source, [string]$Target, [string]$Sheet, [string]$CellRange)
$ErrorActionPreference = 'Stop'
$application = $null
$document = $null
$ownsApplication = $false
try {
    $extension = [IO.Path]::GetExtension($Source).ToLowerInvariant()
    if ($extension -in @('.doc','.docx')) {
        $application = New-Object -ComObject Word.Application
        $ownsApplication = $application.Documents.Count -eq 0
        if (-not $ownsApplication) { throw 'Office application has open user documents' }
        $application.Visible = $false
        $application.DisplayAlerts = 0
        $application.AutomationSecurity = 3
        $document = $application.Documents.Open($Source, $false, $true, $false)
        $document.ExportAsFixedFormat($Target, 17)
    } elseif ($extension -in @('.ppt','.pptx')) {
        $application = New-Object -ComObject PowerPoint.Application
        $ownsApplication = $application.Presentations.Count -eq 0
        if (-not $ownsApplication) { throw 'Office application has open user documents' }
        $application.AutomationSecurity = 3
        $document = $application.Presentations.Open($Source, -1, 0, 0)
        $document.SaveAs($Target, 32)
    } elseif ($extension -in @('.xls','.xlsx','.xlsm')) {
        $application = New-Object -ComObject Excel.Application
        $ownsApplication = $application.Workbooks.Count -eq 0
        if (-not $ownsApplication) { throw 'Office application has open user documents' }
        $application.Visible = $false
        $application.DisplayAlerts = $false
        $application.AskToUpdateLinks = $false
        $application.AutomationSecurity = 3
        $document = $application.Workbooks.Open($Source, 0, $true)
        $worksheet = if ($Sheet) { $document.Worksheets.Item($Sheet) } else { $document.Worksheets.Item(1) }
        if ($CellRange) { $worksheet.PageSetup.PrintArea = $CellRange }
        $worksheet.ExportAsFixedFormat(0, $Target)
    } else {
        throw 'Unsupported Office format'
    }
} finally {
    try {
        if ($document) { $document.Close(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) }
    } finally {
        if ($application) {
            if ($ownsApplication) { $application.Quit() }
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($application)
        }
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
