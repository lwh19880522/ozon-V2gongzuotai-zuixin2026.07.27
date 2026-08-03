param(
    [int]$Port = 8765,
    [string]$RuntimeState = '',
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python.exe -ErrorAction Stop).Source
}
$Launcher = Join-Path $ProjectRoot 'scripts\workbench_launcher.py'
$Arguments = @($Launcher, '--port', "$Port")
if (-not [string]::IsNullOrWhiteSpace($RuntimeState)) {
    $Arguments += @('--runtime-state', $RuntimeState)
}
if ($NoOpen) {
    $Arguments += '--no-open'
}
& $Python @Arguments
exit $LASTEXITCODE


