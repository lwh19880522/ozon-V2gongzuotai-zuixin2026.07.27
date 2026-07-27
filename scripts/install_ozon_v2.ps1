param(
    [string]$PythonCommand = '',
    [string]$SkillTargetRoot = '',
    [string]$ShortcutPath = '',
    [int]$Port = 8765,
    [switch]$NoShortcut,
    [switch]$NoSkills,
    [switch]$NoStart,
    [switch]$NoOpen,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$VenvRoot = Join-Path $ProjectRoot '.venv'
$VenvPython = Join-Path $VenvRoot 'Scripts\python.exe'
$SkillInstallerScript = Join-Path $ProjectRoot 'scripts\install_codex_skills.ps1'
$ShortcutScript = Join-Path $ProjectRoot 'scripts\create_workbench_shortcut.ps1'
$LauncherScript = Join-Path $ProjectRoot 'scripts\launch_workbench.ps1'
$DoctorScript = Join-Path $ProjectRoot 'scripts\verify_ozon_v2_install.ps1'

function Write-Step {
    param([string]$Message)
    Write-Output "[Ozon V2] $Message"
}

function Resolve-BasePython {
    if (-not [string]::IsNullOrWhiteSpace($PythonCommand)) {
        $explicit = Get-Command $PythonCommand -ErrorAction SilentlyContinue
        if ($null -eq $explicit) {
            throw "The requested Python command was not found: $PythonCommand"
        }
        return $explicit.Source
    }
    foreach ($name in @('python.exe', 'python')) {
        $candidate = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $candidate) {
            return $candidate.Source
        }
    }
    throw 'Python was not found. Install Python 3.11 or newer first.'
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$Description
    )
    Write-Step $Description
    if ($DryRun) {
        Write-Output "DRY_RUN $FilePath $($Arguments -join ' ')"
        return
    }
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

$basePython = Resolve-BasePython
Invoke-Checked -FilePath $basePython -Arguments @(
    '-c',
    "import sys; assert sys.version_info >= (3, 11), sys.version"
) -Description 'Check Python version'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Invoke-Checked -FilePath $basePython -Arguments @(
        '-m',
        'venv',
        $VenvRoot
    ) -Description 'Create the project virtual environment'
}
else {
    Write-Step 'Reuse the existing .venv environment'
}

$installPython = if ($DryRun -and -not (Test-Path -LiteralPath $VenvPython)) {
    $VenvPython
}
else {
    (Resolve-Path -LiteralPath $VenvPython).Path
}
Invoke-Checked -FilePath $installPython -Arguments @(
    '-m',
    'pip',
    'install',
    '--disable-pip-version-check',
    '-e',
    $ProjectRoot
) -Description 'Install or update workbench, MCP, and Skill dependencies'

if (-not $NoSkills) {
    $skillArguments = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $SkillInstallerScript,
        '-ProjectRoot',
        $ProjectRoot
    )
    if (-not [string]::IsNullOrWhiteSpace($SkillTargetRoot)) {
        $skillArguments += @('-SkillTargetRoot', $SkillTargetRoot)
    }
    if ($DryRun) {
        $skillArguments += '-DryRun'
    }
    Invoke-Checked -FilePath 'powershell.exe' -Arguments $skillArguments `
        -Description 'Install the three version-matched Codex Skills'
}

if (-not $NoShortcut) {
    $shortcutArguments = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $ShortcutScript
    )
    if (-not [string]::IsNullOrWhiteSpace($ShortcutPath)) {
        $shortcutArguments += @('-ShortcutPath', $ShortcutPath)
    }
    Invoke-Checked -FilePath 'powershell.exe' -Arguments $shortcutArguments `
        -Description 'Create the desktop one-click launcher'
}

if (-not $NoStart) {
    $launchArguments = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $LauncherScript,
        '-Port',
        "$Port"
    )
    if ($NoOpen) {
        $launchArguments += '-NoOpen'
    }
    Invoke-Checked -FilePath 'powershell.exe' -Arguments $launchArguments `
        -Description 'Start and open the Ozon V2 workbench'
}

if ($DryRun) {
    Write-Output "INSTALL_DRY_RUN_OK ROOT=$ProjectRoot"
}
else {
    $doctorArguments = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $DoctorScript,
        '-ProjectRoot',
        $ProjectRoot,
        '-HealthUrl',
        "http://127.0.0.1:$Port/api/health"
    )
    if (-not [string]::IsNullOrWhiteSpace($SkillTargetRoot)) {
        $doctorArguments += @('-SkillTargetRoot', $SkillTargetRoot)
    }
    if (-not [string]::IsNullOrWhiteSpace($ShortcutPath)) {
        $doctorArguments += @('-ShortcutPath', $ShortcutPath)
    }
    if ($NoSkills) {
        $doctorArguments += '-SkipSkills'
    }
    if ($NoShortcut) {
        $doctorArguments += '-SkipShortcut'
    }
    if ($NoStart) {
        $doctorArguments += '-SkipHealth'
    }
    Invoke-Checked -FilePath 'powershell.exe' -Arguments $doctorArguments `
        -Description 'Verify the complete Ozon V2 installation'
    Write-Output "INSTALL_OK ROOT=$ProjectRoot URL=http://127.0.0.1:$Port/"
}
