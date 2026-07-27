param(
    [string]$ShortcutPath = ''
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$LauncherScript = Join-Path $ProjectRoot 'scripts\launch_workbench.ps1'
if ([string]::IsNullOrWhiteSpace($ShortcutPath)) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $ShortcutPath = Join-Path $desktop 'Ozon V2 工具台.lnk'
}

$powerShellPath = Join-Path $PSHOME 'powershell.exe'
$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$LauncherScript`""
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $powerShellPath
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.Description = 'Ozon V2 工具台一键启动 (One-click Workbench)'
$edgePath = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
if (Test-Path -LiteralPath $edgePath) {
    $shortcut.IconLocation = "$edgePath,0"
}
else {
    $shortcut.IconLocation = "$powerShellPath,0"
}
$shortcut.Save()
Write-Output "SHORTCUT_CREATED PATH=$ShortcutPath"
