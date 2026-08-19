[CmdletBinding()]
param(
    [string[]]$Times = @("06:00", "06:15", "06:35", "07:00", "07:30", "08:00"),
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

$NotBefore = ($ParsedTimes | Sort-Object | Select-Object -First 1).ToString("HH:mm")
$Arguments = '"{0}" --not-before {1}' -f $CheckinScript, $NotBefore
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $Arguments `
    -WorkingDirectory $ScriptDir

$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$DailyTriggers = foreach ($Parsed in $ParsedTimes) {
    New-ScheduledTaskTrigger -Daily -At $Parsed
}
$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Triggers = @($DailyTriggers) + @($LogonTrigger)

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

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
    -Description "Jielong web check-in with post-06:00 retry windows and an after-logon catch-up." `
    -Force | Out-Null

Write-Host "Scheduled task created: $TaskName"
Write-Host "Daily run times: $($Times -join ', ')"
Write-Host "Catch-up: runs after Windows logon when it is past $NotBefore"
Write-Host "Retry behavior: a later time automatically checks again when an earlier run fails."
Write-Host "Log directory: $(Join-Path $ScriptDir 'logs')"
