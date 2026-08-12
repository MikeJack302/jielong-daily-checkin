[CmdletBinding()]
param(
    [string[]]$Times = @("06:00"),
    [string]$TaskName = "Jielong-Daily-Web-Checkin",
    [string]$TargetTitle = "",
    [string]$ActiveFrom = "",
    [string]$ActiveUntil = "",
    [switch]$Remove,
    [switch]$SkipSetup
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Remove) {
    $ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $ExistingTask) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task: $TaskName"
    }
    else {
        Write-Host "Scheduled task does not exist: $TaskName"
    }
    exit 0
}

$ParsedTimes = foreach ($TimeText in $Times) {
    $Parsed = [DateTime]::MinValue
    if (-not [DateTime]::TryParseExact(
        $TimeText,
        "HH:mm",
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None,
        [ref]$Parsed
    )) {
        throw "Invalid time: $TimeText. Use HH:mm, for example 21:30."
    }
    $Parsed
}

$VenvDir = Join-Path $ScriptDir ".web-venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$CheckinScript = Join-Path $ScriptDir "web_checkin.py"
$Requirements = Join-Path $ScriptDir "web_requirements.txt"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    $PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $PythonLauncher) {
        & $PythonLauncher.Source -3.11 -m venv $VenvDir
    }
    else {
        $SystemPython = Get-Command python -ErrorAction Stop
        & $SystemPython.Source -m venv $VenvDir
    }
}

& $PythonExe -m pip install --disable-pip-version-check -r $Requirements
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Python dependencies."
}

if (-not $SkipSetup) {
    $ConfigFile = Join-Path $ScriptDir "web_config.json"
    if ([string]::IsNullOrWhiteSpace($TargetTitle) -and -not (Test-Path -LiteralPath $ConfigFile)) {
        throw "TargetTitle is required for first setup. Example: -TargetTitle 'Daily check-in'"
    }

    $SetupArguments = @($CheckinScript, "--setup")
    if (-not [string]::IsNullOrWhiteSpace($TargetTitle)) {
        $SetupArguments += @("--target-title", $TargetTitle)
    }
    if (-not [string]::IsNullOrWhiteSpace($ActiveFrom)) {
        $SetupArguments += @("--active-from", $ActiveFrom)
    }
    if (-not [string]::IsNullOrWhiteSpace($ActiveUntil)) {
        $SetupArguments += @("--active-until", $ActiveUntil)
    }

    & $PythonExe @SetupArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Initial QR login setup failed. The scheduled task was not created."
    }
}

$Arguments = '"{0}"' -f $CheckinScript
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $Arguments `
    -WorkingDirectory $ScriptDir

$Triggers = foreach ($Parsed in $ParsedTimes) {
    New-ScheduledTaskTrigger -Daily -At $Parsed
}

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Triggers `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Jielong web check-in. Reuses a local browser session after initial QR login." `
    -Force | Out-Null

Write-Host "Scheduled task created: $TaskName"
Write-Host "Daily run times: $($Times -join ', ')"
Write-Host "Log directory: $(Join-Path $ScriptDir 'logs')"
