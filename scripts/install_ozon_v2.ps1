param(
    [string]$PythonCommand = '',
    [int]$Port = 8765,
    [switch]$NoShortcut,
    [switch]$NoStart,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$VenvRoot = Join-Path $ProjectRoot '.venv'
$VenvPython = Join-Path $VenvRoot 'Scripts\python.exe'
$ShortcutScript = Join-Path $ProjectRoot 'scripts\create_workbench_shortcut.ps1'
$ControlScript = Join-Path $ProjectRoot 'scripts\workbench_control.ps1'

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

if (-not $NoShortcut) {
    Invoke-Checked -FilePath 'powershell.exe' -Arguments @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $ShortcutScript
    ) -Description 'Create the desktop one-click launcher'
}

if (-not $NoStart) {
    Invoke-Checked -FilePath 'powershell.exe' -Arguments @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $ControlScript,
        '-Action',
        'Start',
        '-Port',
        "$Port"
    ) -Description 'Start the Ozon V2 workbench'
}

if ($DryRun) {
    Write-Output "INSTALL_DRY_RUN_OK ROOT=$ProjectRoot"
}
else {
    Write-Output "INSTALL_OK ROOT=$ProjectRoot URL=http://127.0.0.1:$Port/"
}
