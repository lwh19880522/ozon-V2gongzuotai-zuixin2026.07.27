param(
    [string]$ProjectRoot = '',
    [string]$SkillTargetRoot = '',
    [string]$ShortcutPath = '',
    [string]$HealthUrl = 'http://127.0.0.1:8765/api/health',
    [switch]$SkipHealth,
    [switch]$SkipShortcut,
    [switch]$SkipSkills
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
}
if ([string]::IsNullOrWhiteSpace($SkillTargetRoot)) {
    $SkillTargetRoot = Join-Path (Join-Path $env:USERPROFILE '.codex') 'skills'
}
if ([string]::IsNullOrWhiteSpace($ShortcutPath)) {
    # Keep executable PowerShell source ASCII-only. Windows PowerShell 5 reads
    # UTF-8 files without a BOM using the active ANSI code page, which can
    # corrupt a literal Chinese shortcut name on a new computer.
    $shortcutName = "Ozon V2 $([char]0x5DE5)$([char]0x5177)$([char]0x53F0).lnk"
    $ShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) $shortcutName
}

$failures = New-Object 'System.Collections.Generic.List[string]'

function Add-Failure {
    param([string]$Code)
    [void]$failures.Add($Code)
    Write-Host "CHECK_FAILED CODE=$Code"
}

function Test-RequiredFile {
    param(
        [string]$Path,
        [string]$MissingCode,
        [string]$SuccessCode
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Add-Failure -Code $MissingCode
        return $false
    }
    Write-Host "CHECK_OK CODE=$SuccessCode"
    return $true
}

function Test-FileTreeMatches {
    param(
        [string]$SourceRoot,
        [string]$TargetRoot
    )
    if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
        return $false
    }

    $sourceFiles = @(Get-ChildItem -LiteralPath $SourceRoot -Recurse -File)
    $targetFiles = @(Get-ChildItem -LiteralPath $TargetRoot -Recurse -File)
    if ($sourceFiles.Count -ne $targetFiles.Count) {
        return $false
    }

    foreach ($sourceFile in $sourceFiles) {
        $relativePath = $sourceFile.FullName.Substring($SourceRoot.Length).TrimStart([char[]]"\/")
        $targetFile = Join-Path $TargetRoot $relativePath
        if (-not (Test-Path -LiteralPath $targetFile -PathType Leaf)) {
            return $false
        }
        $sourceHash = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash
        $targetHash = (Get-FileHash -LiteralPath $targetFile -Algorithm SHA256).Hash
        if ($sourceHash -ne $targetHash) {
            return $false
        }
    }
    return $true
}

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    Add-Failure -Code 'project_root_missing'
}

$venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$pythonReady = Test-RequiredFile -Path $venvPython `
    -MissingCode 'venv_python_missing' -SuccessCode 'venv_python'
Test-RequiredFile -Path (Join-Path $ProjectRoot 'scripts\start_workbench.py') `
    -MissingCode 'workbench_server_missing' -SuccessCode 'workbench_server' | Out-Null
Test-RequiredFile -Path (Join-Path $ProjectRoot '.codex-plugin\plugin.json') `
    -MissingCode 'plugin_manifest_missing' -SuccessCode 'plugin_manifest' | Out-Null
Test-RequiredFile -Path (Join-Path $ProjectRoot '.mcp.json') `
    -MissingCode 'mcp_config_missing' -SuccessCode 'mcp_config' | Out-Null

if ($pythonReady) {
    & $venvPython -c "import ozon_v2, PIL, fastmcp"
    if ($LASTEXITCODE -ne 0) {
        Add-Failure -Code 'runtime_dependencies_unavailable'
    }
    else {
        Write-Output 'CHECK_OK CODE=runtime_dependencies'
    }
}

$skillNames = @(
    'ozon-product-media-generator',
    'ozon-intelligent-field-drafter',
    'ozon-store-content-risk-optimizer'
)
if (-not $SkipSkills) {
    foreach ($skillName in $skillNames) {
        $sourceSkillRoot = Join-Path (Join-Path $ProjectRoot 'skills') $skillName
        $installedSkillRoot = Join-Path $SkillTargetRoot $skillName
        if (-not (Test-Path -LiteralPath $sourceSkillRoot -PathType Container) -or
            -not (Test-Path -LiteralPath $installedSkillRoot -PathType Container)) {
            Add-Failure -Code "skill_missing:$skillName"
            continue
        }
        if (-not (Test-FileTreeMatches -SourceRoot $sourceSkillRoot -TargetRoot $installedSkillRoot)) {
            Add-Failure -Code "skill_version_mismatch:$skillName"
            continue
        }
        Write-Output "CHECK_OK CODE=skill:$skillName"
    }
}

if (-not $SkipShortcut) {
    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
        Add-Failure -Code 'desktop_shortcut_missing'
    }
    else {
        try {
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($ShortcutPath)
            $expectedLauncher = Join-Path $ProjectRoot 'scripts\launch_workbench.ps1'
            if ([string]$shortcut.Arguments -notlike "*$expectedLauncher*") {
                Add-Failure -Code 'desktop_shortcut_target_invalid'
            }
            else {
                Write-Output 'CHECK_OK CODE=desktop_shortcut'
            }
        }
        catch {
            Add-Failure -Code 'desktop_shortcut_unreadable'
        }
    }
}

if (-not $SkipHealth) {
    try {
        $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3
        if ($health.ok -ne $true -or $health.code -ne 'health.ok') {
            Add-Failure -Code 'workbench_health_invalid'
        }
        else {
            Write-Output "CHECK_OK CODE=workbench_health URL=$HealthUrl"
        }
    }
    catch {
        Add-Failure -Code 'workbench_health_offline'
    }
}

if ($failures.Count -gt 0) {
    Write-Output "DOCTOR_FAILED COUNT=$($failures.Count)"
    exit 1
}

$partial = $SkipHealth -or $SkipShortcut -or $SkipSkills
Write-Output "DOCTOR_PASSED PARTIAL=$partial ROOT=$ProjectRoot"
exit 0
