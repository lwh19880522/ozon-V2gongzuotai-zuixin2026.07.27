param(
    [string]$ProjectRoot = '',
    [string]$SkillTargetRoot = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
}
else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
}
if ([string]::IsNullOrWhiteSpace($SkillTargetRoot)) {
    $SkillTargetRoot = Join-Path (Join-Path $env:USERPROFILE '.codex') 'skills'
}

$skillNames = @(
    'ozon-product-media-generator',
    'ozon-intelligent-field-drafter',
    'ozon-image-generation-controller'
)

function Get-RelativeFileMap {
    param([string]$Root)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Root)) {
        return $map
    }
    foreach ($file in Get-ChildItem -LiteralPath $Root -File -Recurse) {
        $relative = $file.FullName.Substring($Root.Length).TrimStart('\', '/')
        $map[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
    return $map
}

function Test-SkillTreesMatch {
    param(
        [string]$Source,
        [string]$Target
    )
    $sourceMap = Get-RelativeFileMap -Root $Source
    $targetMap = Get-RelativeFileMap -Root $Target
    if ($sourceMap.Count -ne $targetMap.Count) {
        return $false
    }
    foreach ($relative in $sourceMap.Keys) {
        if (-not $targetMap.ContainsKey($relative) -or $targetMap[$relative] -ne $sourceMap[$relative]) {
            return $false
        }
    }
    return $true
}

if ($DryRun) {
    foreach ($skillName in $skillNames) {
        $source = Join-Path (Join-Path $ProjectRoot 'skills') $skillName
        if (-not (Test-Path -LiteralPath (Join-Path $source 'SKILL.md'))) {
            throw "Skill source is missing: $source"
        }
        Write-Output "DRY_RUN_SKILL SOURCE=$source TARGET=$(Join-Path $SkillTargetRoot $skillName)"
    }
    Write-Output "SKILLS_INSTALL_DRY_RUN_OK TARGET=$SkillTargetRoot COUNT=$($skillNames.Count)"
    exit 0
}

New-Item -ItemType Directory -Path $SkillTargetRoot -Force | Out-Null
$timestamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
$backupRoot = Join-Path (Join-Path $SkillTargetRoot '.ozon-v2-backups') $timestamp

foreach ($skillName in $skillNames) {
    $source = Join-Path (Join-Path $ProjectRoot 'skills') $skillName
    $target = Join-Path $SkillTargetRoot $skillName
    if (-not (Test-Path -LiteralPath (Join-Path $source 'SKILL.md'))) {
        throw "Skill source is missing: $source"
    }
    if (Test-SkillTreesMatch -Source $source -Target $target) {
        Write-Output "SKILL_UP_TO_DATE NAME=$skillName TARGET=$target"
        continue
    }

    $staging = Join-Path $SkillTargetRoot ".$skillName.ozon-v2-new-$PID-$([Guid]::NewGuid().ToString('N'))"
    Copy-Item -LiteralPath $source -Destination $staging -Recurse
    if (Test-Path -LiteralPath $target) {
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        Move-Item -LiteralPath $target -Destination (Join-Path $backupRoot $skillName)
    }
    Move-Item -LiteralPath $staging -Destination $target
    Write-Output "SKILL_INSTALLED NAME=$skillName TARGET=$target"
}

Write-Output "SKILLS_INSTALL_OK TARGET=$SkillTargetRoot COUNT=$($skillNames.Count)"
