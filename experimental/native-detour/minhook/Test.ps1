param([switch]$SkipBuild)
$ErrorActionPreference='Stop'
if(-not $SkipBuild){& (Join-Path $PSScriptRoot 'Build.ps1') -Target all}
$cases=@('normal','open','suspend','get-context','set-context','resume','resume-persistent','protect','restore','flush','snapshot-first','snapshot-second','target-changed','changed-context','create-flush','hotpatch-above','guard-page','cross-page')
$results=@()
foreach($kind in @('production','tests')) {
    $runCases=if($kind -eq 'production'){@('normal')}else{$cases}
    foreach($case in $runCases) {
        $start=[System.Diagnostics.ProcessStartInfo]::new()
        $start.FileName=Join-Path $PSScriptRoot "build\$kind\Synthetic.exe"
        $start.Arguments=$case
        $start.UseShellExecute=$false
        $start.CreateNoWindow=$true
        $start.RedirectStandardOutput=$true
        $start.RedirectStandardError=$true
        $process=[System.Diagnostics.Process]::new()
        $process.StartInfo=$start
        try {
            if(-not $process.Start()){throw 'Synthetic process did not start.'}
            $stdout=$process.StandardOutput.ReadToEndAsync()
            $stderr=$process.StandardError.ReadToEndAsync()
            if(-not $process.WaitForExit(10000)){$process.Kill();$process.WaitForExit();throw "Synthetic $kind/$case exceeded 10s"}
            $text=$stdout.GetAwaiter().GetResult()
            $err=$stderr.GetAwaiter().GetResult()
            if($process.ExitCode -ne 0){throw "Synthetic $kind/$case failed ($($process.ExitCode)): $err $text"}
            $row=$text|ConvertFrom-Json
            if(-not $row.passed){throw "Synthetic $kind/$case assertion failed"}
            $row|Add-Member -NotePropertyName build -NotePropertyValue $kind
            $results+=$row
            Write-Output "$kind/$case passed"
        } finally {$process.Dispose()}
    }
}
$result=@{schema='dingtalkbridge.minhook-strict-tests.v1';scope='own-process synthetic only';real_target_hooked=$false;tests=$results}
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot 'test-result.json'),($result|ConvertTo-Json -Depth 8)+[Environment]::NewLine,[System.Text.UTF8Encoding]::new($false))
