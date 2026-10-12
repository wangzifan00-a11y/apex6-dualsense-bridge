$ErrorActionPreference = 'Stop'
$bridgeRoot = Split-Path $PSScriptRoot -Parent
& python -X utf8 (Join-Path $PSScriptRoot 'promote_modular_release.py')
if ($LASTEXITCODE -ne 0) { throw 'Release promotion failed.' }
$bridgeRelease = (Get-Content -LiteralPath (Join-Path $bridgeRoot 'latest-build.txt') -Raw).Trim()
$bridgeTarget = Join-Path $bridgeRelease '八爪鱼震动桥.exe'
$bridgeOldLink = Join-Path $bridgeRoot '八爪鱼震动桥-旧版蓝牙.lnk'
$bridgeOldHash = if (Test-Path -LiteralPath $bridgeOldLink -PathType Leaf) {
    (Get-FileHash -LiteralPath $bridgeOldLink -Algorithm SHA256).Hash
} else { $null }
$bridgeShell = New-Object -ComObject WScript.Shell
$bridgeLinks = @(
    @{Name='八爪鱼震动桥.lnk'; Arguments='--receiver-mode'},
    @{Name='八爪鱼震动桥-2.4G接收器.lnk'; Arguments='--receiver-mode'},
    @{Name='八爪鱼震动桥-蓝牙.lnk'; Arguments=''},
    @{Name='八爪鱼震动桥-依赖检查与安装.lnk'; Arguments='--setup --receiver-mode'}
)
$bridgeResults = @()
foreach ($bridgeLink in $bridgeLinks) {
    $bridgePath = Join-Path $bridgeRoot $bridgeLink.Name
    $bridgeShortcut = $bridgeShell.CreateShortcut($bridgePath)
    $bridgeShortcut.TargetPath = $bridgeTarget
    $bridgeShortcut.Arguments = $bridgeLink.Arguments
    $bridgeShortcut.WorkingDirectory = $bridgeRelease
    $bridgeShortcut.Save()
    $bridgeReadback = $bridgeShell.CreateShortcut($bridgePath)
    if ($bridgeReadback.TargetPath -ne $bridgeTarget -or $bridgeReadback.Arguments -ne $bridgeLink.Arguments) {
        throw ('Shortcut verification failed: ' + $bridgeLink.Name)
    }
    $bridgeResults += [pscustomobject]@{Name=$bridgeLink.Name; Target=$bridgeReadback.TargetPath; Arguments=$bridgeReadback.Arguments}
}
if ($null -ne $bridgeOldHash -and (Get-FileHash -LiteralPath $bridgeOldLink -Algorithm SHA256).Hash -ne $bridgeOldHash) { throw 'Legacy Bluetooth shortcut changed.' }
$bridgeAudit = (Get-Content -LiteralPath (Join-Path $bridgeRoot 'latest-portable-audit.txt') -Raw).Trim()
$bridgeResults | ConvertTo-Json | Set-Content -LiteralPath (Join-Path (Split-Path $bridgeAudit -Parent) 'shortcuts.json') -Encoding utf8
$bridgeResults | ConvertTo-Json
