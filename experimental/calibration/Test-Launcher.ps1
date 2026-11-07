$ErrorActionPreference='Stop'
$launcher=Join-Path $PSScriptRoot 'Start-Once.ps1'
$shell=(Get-Process -Id $PID).Path
$directory=Join-Path ([IO.Path]::GetTempPath()) ('DingTalkBridge-launcher-test-'+[Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($directory)
$config=Join-Path $directory 'synthetic-config.json'
[IO.File]::WriteAllText($config,'{}',[Text.UTF8Encoding]::new($false))
$missing=Join-Path $directory 'missing-file.json'
$cases=[Collections.Generic.List[object]]::new()
function Check([string[]]$Arguments,[int]$ExitCode,[string]$Status,[string]$Reason,[string]$ConfigEnvironment=$null) {
    $info=[Diagnostics.ProcessStartInfo]::new();$info.FileName=$shell;$info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $info.EnvironmentVariables.Remove('DINGTALKBRIDGE_CONFIG')
    if($null -ne $ConfigEnvironment){$info.EnvironmentVariables['DINGTALKBRIDGE_CONFIG']=$ConfigEnvironment}
    $all=@('-NoProfile','-File',$launcher)+$Arguments
    $quoted=foreach($arg in $all){if($arg.Contains('"')-or $arg.EndsWith('\')){throw 'Invalid test argument'};'"'+$arg+'"'}
    $info.Arguments=$quoted -join ' '
    $process=[Diagnostics.Process]::Start($info)
    try {
        $stdout=$process.StandardOutput.ReadToEndAsync();$stderr=$process.StandardError.ReadToEndAsync()
        if(-not $process.WaitForExit(15000)){throw 'Launcher negative test timeout'}
        $out=$stdout.GetAwaiter().GetResult();$err=$stderr.GetAwaiter().GetResult();$result=$out|ConvertFrom-Json
        if($process.ExitCode -ne $ExitCode -or $err -or $result.status -ne $Status -or $result.reason -ne $Reason){throw 'Unexpected launcher negative test result'}
        if($result.ui_launched -ne $false -or $result.authorization_created_by_launcher -ne $false -or $result.launcher_captures_pixels -ne $false){throw 'A negative test enabled an active capability'}
        if($out.Contains($directory) -or $out.Contains($config) -or $out.Contains($missing)){throw 'Unsanitized launcher output'}
        $cases.Add([pscustomobject]@{status=$result.status;reason=$result.reason;passed=$true})
    } finally {try{if(-not $process.HasExited){$process.Kill()}}catch{};$process.Dispose()}
}
try {
    Check @('-SelfTest') 0 'launcher_self_test_passed' 'nine_pure_checks_no_children'
    Check @('-CheckOnly','-ConfigPath',$missing) 3 'rejected' 'config_unavailable'
    Check @('-CheckOnly') 3 'rejected' 'config_unavailable' $missing
    Check @('-CheckOnly','-ConfigPath',$config,'-PythonPath',$missing) 3 'rejected' 'python_unavailable' $missing
    Check @('-CheckOnly') 3 'rejected' 'config_unavailable' ''
    $tokens=$null;$errors=$null;[void][Management.Automation.Language.Parser]::ParseFile($launcher,[ref]$tokens,[ref]$errors)
    if($errors.Count){throw 'Launcher syntax error'}
    $report=[ordered]@{status='passed';cases=$cases.Count;pure_self_checks=9;ui_shown=$false;target_queried=$false;pixels_read=$false;authorization_created=$false;launcher_sha256=(Get-FileHash -LiteralPath $launcher -Algorithm SHA256).Hash.ToLowerInvariant();checks=$cases}
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'launcher-test-result.json'),($report|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
    [pscustomobject]@{status='passed';cases=$cases.Count;pure_self_checks=9;ui_shown=$false;target_queried=$false;pixels_read=$false}
} finally {
    $full=[IO.Path]::GetFullPath($directory);$tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
    if(-not $full.StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase)-or(Split-Path -Leaf $full)-notlike 'DingTalkBridge-launcher-test-*'){throw 'Unsafe test cleanup path'}
    Remove-Item -LiteralPath $config
    Remove-Item -LiteralPath $full
}
