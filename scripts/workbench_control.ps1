param(
    [ValidateSet('Start', 'Stop', 'Restart', 'Status')]
    [string]$Action = 'Status',
    [int]$Port = 8765,
    [string]$RuntimeState = ''
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if ([string]::IsNullOrWhiteSpace($RuntimeState)) {
    $RuntimeState = Join-Path (Join-Path $ProjectRoot 'runtime') 'workbench'
}
$StateFile = Join-Path $RuntimeState 'workbench.json'
$LifecycleLog = Join-Path $RuntimeState 'lifecycle.log'
$StdoutLog = Join-Path $RuntimeState 'server.stdout.log'
$StderrLog = Join-Path $RuntimeState 'server.stderr.log'
$StartScript = Join-Path $ProjectRoot 'scripts\start_workbench.py'
$HealthUrl = "http://127.0.0.1:$Port/api/health"
$StopUrl = "http://127.0.0.1:$Port/api/runtime/stop"

function Initialize-RuntimeState {
    New-Item -ItemType Directory -Path $RuntimeState -Force | Out-Null
}

function Write-LifecycleLog {
    param([string]$Message)
    Initialize-RuntimeState
    $timestamp = (Get-Date).ToUniversalTime().ToString('o')
    Add-Content -LiteralPath $LifecycleLog -Value "$timestamp $Message" -Encoding UTF8
}

function Read-WorkbenchState {
    if (-not (Test-Path -LiteralPath $StateFile)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        Write-LifecycleLog "state.invalid path=$StateFile error=$($_.Exception.Message)"
        return $null
    }
}

function Write-WorkbenchState {
    param(
        [string]$Status,
        [int]$ProcessId = 0,
        [string]$StartedAt = ''
    )
    Initialize-RuntimeState
    $now = (Get-Date).ToUniversalTime().ToString('o')
    if ([string]::IsNullOrWhiteSpace($StartedAt)) {
        $StartedAt = $now
    }
    $payload = [ordered]@{
        pid = $ProcessId
        port = $Port
        project_root = $ProjectRoot
        started_at = $StartedAt
        updated_at = $now
        status = $Status
    }
    $temporaryFile = "$StateFile.$PID.tmp"
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporaryFile -Encoding UTF8
    Move-Item -LiteralPath $temporaryFile -Destination $StateFile -Force
}

function Test-WorkbenchHealth {
    try {
        $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
        return $response.ok -eq $true -and $response.code -eq 'health.ok'
    }
    catch {
        return $false
    }
}

function Get-RecordedProcessInfo {
    param($State)
    if ($null -eq $State) {
        return $null
    }
    $recordedPid = 0
    if (-not [int]::TryParse([string]$State.pid, [ref]$recordedPid) -or $recordedPid -le 0) {
        return $null
    }
    try {
        return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $recordedPid" -ErrorAction Stop
    }
    catch {
        $process = Get-Process -Id $recordedPid -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            return $null
        }
        return [pscustomobject]@{
            ProcessId = $process.Id
            ExecutablePath = $process.Path
            CommandLine = $null
        }
    }
}

function Test-RecordedProcessBelongsToWorkbench {
    param($State, $ProcessInfo)
    if ($null -eq $State -or $null -eq $ProcessInfo) {
        return $false
    }
    if ([string]$State.project_root -ine $ProjectRoot -or [int]$State.port -ne $Port) {
        return $false
    }
    $executableName = [IO.Path]::GetFileName([string]$ProcessInfo.ExecutablePath)
    if ($executableName -notmatch '^python(\.exe)?$') {
        return $false
    }
    $commandLine = [string]$ProcessInfo.CommandLine
    return $commandLine.IndexOf($StartScript, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine -match "--port\s+$Port(?:\s|$)"
}

function Wait-ForHealth {
    param([int]$Seconds = 10)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-WorkbenchHealth) {
            return $true
        }
        Start-Sleep -Milliseconds 200
    }
    return $false
}

function Wait-ForStop {
    param([int]$Seconds = 5)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-WorkbenchHealth)) {
            return $true
        }
        Start-Sleep -Milliseconds 150
    }
    return $false
}

function Start-Workbench {
    if (Test-WorkbenchHealth) {
        $state = Read-WorkbenchState
        $pidText = if ($null -ne $state -and [int]$state.pid -gt 0) { " PID=$($state.pid)" } else { '' }
        Write-Output "ALREADY_RUNNING$pidText URL=$HealthUrl"
        $script:ResultCode = 0
        return
    }

    Initialize-RuntimeState
    $staleState = Read-WorkbenchState
    if ($null -ne $staleState) {
        Write-LifecycleLog "state.stale_ignored pid=$($staleState.pid) recorded_project=$($staleState.project_root)"
    }

    $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        $pythonCommand = Get-Command 'python' -ErrorAction SilentlyContinue
    }
    if ($null -eq $pythonCommand) {
        Write-LifecycleLog 'start.failed reason=python_not_found'
        Write-Output "START_FAILED PYTHON_NOT_FOUND LOG=$LifecycleLog"
        $script:ResultCode = 1
        return
    }

    $pythonExecutable = $pythonCommand.Source
    $venvRoot = Split-Path -Parent (Split-Path -Parent $pythonCommand.Source)
    $venvConfig = Join-Path $venvRoot 'pyvenv.cfg'
    if (Test-Path -LiteralPath $venvConfig) {
        $executableLine = Get-Content -LiteralPath $venvConfig -Encoding UTF8 |
            Where-Object { $_ -match '^executable\s*=' } |
            Select-Object -First 1
        if ($executableLine -match '^executable\s*=\s*(.+)$') {
            $pythonExecutable = $Matches[1].Trim()
        }
    }
    if ([string]::IsNullOrWhiteSpace($pythonExecutable) -or -not (Test-Path -LiteralPath $pythonExecutable)) {
        Write-LifecycleLog 'start.failed reason=python_resolution_failed'
        Write-Output "START_FAILED PYTHON_RESOLUTION_FAILED LOG=$LifecycleLog"
        $script:ResultCode = 1
        return
    }

    $env:PYTHONPATH = Join-Path $ProjectRoot 'src'
    $arguments = @(
        "`"$StartScript`"",
        '--host',
        '127.0.0.1',
        '--port',
        "$Port",
        '--runtime-dir',
        "`"$RuntimeState`""
    )
    $process = Start-Process -FilePath $pythonExecutable `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -PassThru

    $startedAt = (Get-Date).ToUniversalTime().ToString('o')
    Write-WorkbenchState -Status 'starting' -ProcessId $process.Id -StartedAt $startedAt
    Write-LifecycleLog "start.spawned pid=$($process.Id) port=$Port"

    if (Wait-ForHealth -Seconds 10) {
        Write-WorkbenchState -Status 'running' -ProcessId $process.Id -StartedAt $startedAt
        Write-LifecycleLog "start.succeeded pid=$($process.Id) port=$Port"
        Write-Output "STARTED PID=$($process.Id) URL=$HealthUrl"
        $script:ResultCode = 0
        return
    }

    $process.Refresh()
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    Write-WorkbenchState -Status 'failed' -ProcessId $process.Id -StartedAt $startedAt
    Write-LifecycleLog "start.failed pid=$($process.Id) port=$Port stderr=$StderrLog"
    Write-Output "START_FAILED URL=$HealthUrl LOG=$LifecycleLog"
    $script:ResultCode = 1
    return
}

function Stop-Workbench {
    $state = Read-WorkbenchState
    $healthIsOnline = Test-WorkbenchHealth
    if ($healthIsOnline) {
        try {
            Invoke-RestMethod -Uri $StopUrl -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
        }
        catch {
            Write-LifecycleLog "stop.api_unavailable error=$($_.Exception.Message)"
        }
        if (Wait-ForStop -Seconds 5) {
            $stoppedPid = if ($null -ne $state) { [int]$state.pid } else { 0 }
            $startedAt = if ($null -ne $state) { [string]$state.started_at } else { '' }
            Write-WorkbenchState -Status 'stopped' -ProcessId $stoppedPid -StartedAt $startedAt
            Write-LifecycleLog "stop.succeeded pid=$stoppedPid mode=api"
            Write-Output "STOPPED PID=$stoppedPid"
            $script:ResultCode = 0
            return
        }
    }

    if ($null -ne $state -and
        [string]$state.project_root -ieq $ProjectRoot -and
        [int]$state.port -eq $Port -and
        [string]$state.status -ieq 'stopped') {
        Write-Output "NOT_RUNNING URL=$HealthUrl"
        $script:ResultCode = 0
        return
    }

    $processInfo = Get-RecordedProcessInfo $state
    if ($null -eq $processInfo) {
        $stoppedPid = if ($null -ne $state) { [int]$state.pid } else { 0 }
        $startedAt = if ($null -ne $state) { [string]$state.started_at } else { '' }
        Write-WorkbenchState -Status 'stopped' -ProcessId $stoppedPid -StartedAt $startedAt
        Write-Output "NOT_RUNNING URL=$HealthUrl"
        $script:ResultCode = 0
        return
    }
    if (-not (Test-RecordedProcessBelongsToWorkbench $state $processInfo)) {
        Write-LifecycleLog "stop.blocked pid=$($state.pid) reason=unexpected_process"
        Write-Output "STOP_BLOCKED_UNEXPECTED_PROCESS PID=$($state.pid)"
        $script:ResultCode = 1
        return
    }

    Stop-Process -Id ([int]$state.pid) -Force
    Wait-Process -Id ([int]$state.pid) -Timeout 5 -ErrorAction SilentlyContinue
    Write-WorkbenchState -Status 'stopped' -ProcessId ([int]$state.pid) -StartedAt ([string]$state.started_at)
    Write-LifecycleLog "stop.succeeded pid=$($state.pid) mode=validated_process"
    Write-Output "STOPPED PID=$($state.pid)"
    $script:ResultCode = 0
    return
}

function Show-WorkbenchStatus {
    $state = Read-WorkbenchState
    if (Test-WorkbenchHealth) {
        $pidText = if ($null -ne $state -and [int]$state.pid -gt 0) { " PID=$($state.pid)" } else { '' }
        Write-Output "RUNNING$pidText URL=$HealthUrl"
        $script:ResultCode = 0
        return
    }
    $processInfo = Get-RecordedProcessInfo $state
    if (Test-RecordedProcessBelongsToWorkbench $state $processInfo) {
        Write-Output "UNHEALTHY PID=$($state.pid) URL=$HealthUrl"
        $script:ResultCode = 1
        return
    }
    Write-Output "NOT_RUNNING URL=$HealthUrl"
    $script:ResultCode = 1
    return
}

$script:ResultCode = 0
switch ($Action) {
    'Start' {
        Start-Workbench
        exit $script:ResultCode
    }
    'Stop' {
        Stop-Workbench
        exit $script:ResultCode
    }
    'Status' {
        Show-WorkbenchStatus
        exit $script:ResultCode
    }
    'Restart' {
        Stop-Workbench
        if ($script:ResultCode -ne 0) {
            exit $script:ResultCode
        }
        Start-Workbench
        if ($script:ResultCode -eq 0) {
            Write-Output "RESTARTED URL=$HealthUrl"
        }
        exit $script:ResultCode
    }
}





