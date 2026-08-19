[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ScriptDir ".web-venv\Scripts\python.exe"
$CheckinScript = Join-Path $ScriptDir "web_checkin.py"

if ($Remove) {
    [Environment]::SetEnvironmentVariable("SERVERCHAN_SENDKEY", $null, "User")
    Remove-Item Env:SERVERCHAN_SENDKEY -ErrorAction SilentlyContinue
    Write-Host "WeChat notification configuration removed."
    exit 0
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python environment not found. Run install_web_task.ps1 first."
}

$SecureKey = Read-Host "Paste the ServerChan SendKey (input is hidden)" -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
try {
    $SendKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
}

if ($SendKey -notmatch '^(SCT|sctp\d+t)') {
    throw "Invalid SendKey. It must start with SCT or sctp<uid>t."
}

[Environment]::SetEnvironmentVariable("SERVERCHAN_SENDKEY", $SendKey, "User")
$env:SERVERCHAN_SENDKEY = $SendKey

& $PythonExe $CheckinScript --test-notification
if ($LASTEXITCODE -ne 0) {
    throw "The SendKey was saved, but the test notification failed."
}

Write-Host "WeChat notification configured. A test message was sent."
Write-Host "The scheduled task will use the saved user environment variable."
