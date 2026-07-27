param(
    [int]$Port = 8765,
    [string]$RuntimeState = '',
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if ([string]::IsNullOrWhiteSpace($RuntimeState)) {
    $RuntimeState = Join-Path (Join-Path $ProjectRoot 'runtime') 'workbench'
}
New-Item -ItemType Directory -Path $RuntimeState -Force | Out-Null
$ControlScript = Join-Path $ProjectRoot 'scripts\workbench_control.ps1'
$ControlOutput = Join-Path $RuntimeState 'launcher-control.stdout.log'
$ControlError = Join-Path $RuntimeState 'launcher-control.stderr.log'
$LauncherLog = Join-Path $RuntimeState 'launcher.log'
$WorkbenchUrl = "http://127.0.0.1:$Port/"

function Write-LauncherLog {
    param([string]$Message)
    $timestamp = (Get-Date).ToUniversalTime().ToString('o')
    Add-Content -LiteralPath $LauncherLog -Value "$timestamp $Message" -Encoding UTF8
}

function Show-LaunchError {
    param([string]$Message)
    if ($NoOpen) {
        return
    }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shell.Popup($Message, 0, 'Ozon V2 工具台', 16) | Out-Null
    }
    catch {
        return
    }
}

$controlArguments = @(
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    "`"$ControlScript`"",
    '-Action',
    'Start',
    '-Port',
    "$Port",
    '-RuntimeState',
    "`"$RuntimeState`""
)
$controlProcess = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList $controlArguments `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $ControlOutput `
    -RedirectStandardError $ControlError `
    -PassThru
$controlDeadline = (Get-Date).AddSeconds(20)
while (-not $controlProcess.HasExited -and (Get-Date) -lt $controlDeadline) {
    Start-Sleep -Milliseconds 100
    $controlProcess.Refresh()
}
if (-not $controlProcess.HasExited) {
    Write-LauncherLog "launch.failed reason=control_timeout port=$Port"
    Show-LaunchError "工具台启动超时。请查看：`n$LauncherLog"
    Write-Output "WORKBENCH_START_FAILED CONTROL_TIMEOUT LOG=$LauncherLog"
    exit 1
}
$controlProcess.WaitForExit()
$controlProcess.Refresh()
$controlExitCode = [int]$controlProcess.ExitCode
$controlText = if (Test-Path -LiteralPath $ControlOutput) { ((Get-Content -LiteralPath $ControlOutput -Raw -Encoding UTF8 | Out-String).Trim()) } else { '' }
$errorText = if (Test-Path -LiteralPath $ControlError) { ((Get-Content -LiteralPath $ControlError -Raw -Encoding UTF8 | Out-String).Trim()) } else { '' }
if ($controlExitCode -ne 0) {
    Write-LauncherLog "launch.failed exit_code=$controlExitCode output=$controlText error=$errorText"
    Show-LaunchError "工具台启动失败。请查看：`n$LauncherLog"
    Write-Output "WORKBENCH_START_FAILED EXIT=$controlExitCode LOG=$LauncherLog"
    exit $controlExitCode
}

Write-LauncherLog "launch.ready port=$Port output=$controlText"
if (-not $NoOpen) {
    $edgeCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_) }
    $edgeCommand = Get-Command 'msedge.exe' -ErrorAction SilentlyContinue
    $edgePath = if ($null -ne $edgeCommand) { $edgeCommand.Source } elseif ($edgeCandidates.Count -gt 0) { $edgeCandidates[0] } else { $null }
    if ($null -ne $edgePath) {
        Start-Process -FilePath $edgePath -ArgumentList @($WorkbenchUrl) | Out-Null
    }
    else {
        Start-Process -FilePath "microsoft-edge:$WorkbenchUrl" | Out-Null
    }
}
Write-Output "WORKBENCH_READY URL=$WorkbenchUrl"
exit 0



