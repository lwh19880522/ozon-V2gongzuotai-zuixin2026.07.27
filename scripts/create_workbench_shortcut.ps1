param(
    [string]$ShortcutPath = ''
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$LauncherScript = Join-Path $ProjectRoot 'scripts\workbench_launcher.py'
if ([string]::IsNullOrWhiteSpace($ShortcutPath)) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $ShortcutPath = Join-Path $desktop 'Ozon V2 工具台.lnk'
}

$pythonwPath = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonwPath)) {
    $pythonwPath = (Get-Command pythonw.exe -ErrorAction Stop).Source
}
$arguments = "`"$LauncherScript`""
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $pythonwPath
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.Description = 'Ozon V2 工具台一键启动 (One-click Workbench)'
$edgePath = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
if (Test-Path -LiteralPath $edgePath) {
    $shortcut.IconLocation = "$edgePath,0"
}
else {
    $shortcut.IconLocation = "$pythonwPath,0"
}
$shortcut.Save()
Write-Output "SHORTCUT_CREATED PATH=$ShortcutPath"
