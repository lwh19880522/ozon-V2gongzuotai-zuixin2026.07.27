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
$ControlScript = Join-Path $ProjectRoot 'scripts\workbench_control.ps1'
$RestartStatusFile = Join-Path $RuntimeState 'restart-status.json'
$WorkbenchUrl = "http://127.0.0.1:$Port/"

function Open-WorkbenchInEdge {
    $edgeCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_) }
    $edgeCommand = Get-Command 'msedge.exe' -ErrorAction SilentlyContinue
    $edgePath = if ($null -ne $edgeCommand) { $edgeCommand.Source } elseif ($edgeCandidates.Count -gt 0) { $edgeCandidates[0] } else { $null }
    if ($null -ne $edgePath) {
        Start-Process -FilePath $edgePath -ArgumentList @($WorkbenchUrl) | Out-Null
        return
    }
    Start-Process -FilePath "microsoft-edge:$WorkbenchUrl" | Out-Null
}

function Write-RestartStatus {
    param([string]$Status, [int]$Attempt, [string]$Details)
    New-Item -ItemType Directory -Path $RuntimeState -Force | Out-Null
    $payload = [ordered]@{
        status = $Status
        attempt = $Attempt
        port = $Port
        previous_pid = $WaitForPid
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
        details = $Details
    }
    $temporaryFile = "$RestartStatusFile.$PID.tmp"
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporaryFile -Encoding UTF8
    Move-Item -LiteralPath $temporaryFile -Destination $RestartStatusFile -Force
}

Write-RestartStatus -Status 'waiting_for_exit' -Attempt 0 -Details "Waiting for PID $WaitForPid to exit."
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    if ($null -eq (Get-Process -Id $WaitForPid -ErrorAction SilentlyContinue)) {
        break
    }
    Start-Sleep -Milliseconds 150
}
if ($null -ne (Get-Process -Id $WaitForPid -ErrorAction SilentlyContinue)) {
    Write-RestartStatus -Status 'failed' -Attempt 0 -Details "Previous PID $WaitForPid did not exit before timeout."
    exit 1
}

for ($attempt = 1; $attempt -le 2; $attempt++) {
    $attemptOutput = Join-Path $RuntimeState "restart-attempt-$attempt.stdout.log"
    $attemptError = Join-Path $RuntimeState "restart-attempt-$attempt.stderr.log"
    $controlArguments = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$ControlScript`"", '-Action', 'Start', '-Port', "$Port",
        '-RuntimeState', "`"$RuntimeState`""
    )
    $controlProcess = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList $controlArguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $attemptOutput `
        -RedirectStandardError $attemptError `
        -PassThru
    $controlDeadline = (Get-Date).AddSeconds(15)
    while (-not $controlProcess.HasExited -and (Get-Date) -lt $controlDeadline) {
        Start-Sleep -Milliseconds 100
        $controlProcess.Refresh()
    }
    if ($controlProcess.HasExited) {
        $controlProcess.WaitForExit()
        $controlProcess.Refresh()
        $exitCode = [int]$controlProcess.ExitCode
    }
    else {
        $exitCode = 1
    }
    $outputText = if (Test-Path -LiteralPath $attemptOutput) { (Get-Content -LiteralPath $attemptOutput -Raw -Encoding UTF8 | Out-String).Trim() } else { '' }
    $errorText = if (Test-Path -LiteralPath $attemptError) { (Get-Content -LiteralPath $attemptError -Raw -Encoding UTF8 | Out-String).Trim() } else { '' }
    $details = "$outputText $errorText".Trim()
    if ($exitCode -eq 0) {
        if ($OpenEdgeAfterRestart) {
            try {
                Open-WorkbenchInEdge
                $details = "$details Edge workbench opened for extension reconnect.".Trim()
            }
            catch {
                $details = "$details Edge open failed: $($_.Exception.Message)".Trim()
            }
        }
        Write-RestartStatus -Status 'succeeded' -Attempt $attempt -Details $details
        exit 0
    }
    Write-RestartStatus -Status 'retrying' -Attempt $attempt -Details $details
    if ($attempt -lt 2) {
        Start-Sleep -Milliseconds 500
    }
}

Write-RestartStatus -Status 'failed' -Attempt 2 -Details 'Workbench restart failed after one retry.'
exit 1

