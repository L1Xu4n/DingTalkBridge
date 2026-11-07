param([string]$OutputPath)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) { $OutputPath = Join-Path $projectRoot 'dist\DingTalkBridge-0.6.2.zip' }
$archivePath = [IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($archivePath)) -Force | Out-Null
$packageItems = @('README.md','LICENSE','THIRD_PARTY_NOTICES.md','PROGRESS.md','TODO.md','config.example.json','mcp.example.json','pyproject.toml','src','native','scripts','tests','docs','experimental\native-detour','experimental\ocr','experimental\calibration','experimental\wgc','experimental\accessibility-probe','.gitignore','.gitattributes','.github')
$packageStage = Join-Path ([IO.Path]::GetTempPath()) ('DingTalkBridge-package-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $packageStage -Force | Out-Null
$sourceFiles = @()
# A fixed source allowlist excludes runtime evidence, user settings, databases and logs.
foreach ($item in $packageItems) {
    $sourcePath = Join-Path $projectRoot $item
    if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
        $stagedFile = Join-Path $packageStage $item
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($stagedFile)) -Force | Out-Null
        Copy-Item -LiteralPath $sourcePath -Destination $stagedFile
        $sourceFiles += $item
    }
    elseif (Test-Path -LiteralPath $sourcePath -PathType Container) {
        foreach ($sourceFile in Get-ChildItem -LiteralPath $sourcePath -Recurse -File) {
            $relativePath = $sourceFile.FullName.Substring($projectRoot.Length + 1)
            if ($relativePath -match '(^|\\)(__pycache__|bin|build|runtime|data|dist|jsi-readable)(\\|$)' -or $sourceFile.Extension -in @('.pyc','.db','.sqlite3','.jsonl','.exe','.dll','.obj','.pdb','.log')) { continue }
            if ($sourceFile.Name -match '(private|descriptor|binding|pending)\.json$' -or $sourceFile.Name -eq 'config.local.json') { continue }
            if ($item -eq 'experimental\ocr' -and $sourceFile.Name -notin @('Capture-Ocr.ps1','LocalOcr.ps1','SafeWindowCrop.cs')) { continue }
            if ($item -in @('experimental\calibration','experimental\wgc','experimental\accessibility-probe') -and $sourceFile.Extension -notin @('.cs','.cpp','.h','.py','.ps1','.md')) { continue }
            $stagedFile = Join-Path $packageStage $relativePath
            New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($stagedFile)) -Force | Out-Null
            Copy-Item -LiteralPath $sourceFile.FullName -Destination $stagedFile
            $sourceFiles += $relativePath
        }
    }
}
$manifestLines = @($sourceFiles | Sort-Object -Unique | ForEach-Object {
    $digest = (Get-FileHash -LiteralPath (Join-Path $packageStage $_) -Algorithm SHA256).Hash.ToLowerInvariant()
    $digest + '  ' + $_.Replace('\','/')
})
$manifestPath = Join-Path $packageStage 'MANIFEST.sha256'
[IO.File]::WriteAllLines($manifestPath, $manifestLines, (New-Object Text.UTF8Encoding($false)))
Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $projectRoot 'MANIFEST.sha256') -Force
Compress-Archive -Path (Join-Path $packageStage '*') -DestinationPath $archivePath -Force
$archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($archivePath + '.sha256'), ($archiveHash + '  ' + [IO.Path]::GetFileName($archivePath) + "`n"), (New-Object Text.UTF8Encoding($false)))
[pscustomobject]@{path=$archivePath;sha256=$archiveHash;source_files=$sourceFiles.Count} | ConvertTo-Json
# The uniquely named staging directory is retained in Temp for inspection.
