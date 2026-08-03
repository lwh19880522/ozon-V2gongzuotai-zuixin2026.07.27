param(
    [Parameter(Mandatory = $true)]
    [int]$WaitForPid,
    [Parameter(Mandatory = $true)]
    [int]$Port,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeState,
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [switch]$OpenEdgeAfterRestart
)

$ErrorActionPreference = 'Stop'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python.exe -ErrorAction Stop).Source
}
$Helper = Join-Path $ProjectRoot 'scripts\workbench_restart_helper.py'
$Arguments = @(
    $Helper,
    '--wait-for-pid', "$WaitForPid",
    '--port', "$Port",
    '--runtime-state', $RuntimeState,
    '--project-root', $ProjectRoot
)
if ($OpenEdgeAfterRestart) {
    $Arguments += '--open-edge-after-restart'
}
& $Python @Arguments
exit $LASTEXITCODE
