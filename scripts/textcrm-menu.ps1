# textcrm menu: the everyday commands without typing them. Opened by the Desktop shortcut (as administrator).
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "py" }

function Run-Textcrm([string[]]$arguments) {
    & $python -m textcrm @arguments
    Write-Host ""
}

while ($true) {
    Write-Host ""
    Write-Host "textcrm" -ForegroundColor Cyan
    Write-Host "  1  Quo credits: enter the balance after a top-up"
    Write-Host "  2  Today's stats (texts, replies, appointments, failed sends)"
    Write-Host "  3  Check a customer by phone number"
    Write-Host "  4  Send my plans and settings to the cloud"
    Write-Host "  5  Test the cloud's Codex"
    Write-Host "  6  Show the PC run's last log lines"
    Write-Host "  7  Restart the PC run (only when no CDK read is running)"
    Write-Host "  Q  Quit"
    $choice = (Read-Host "Pick").Trim().ToUpper()
    switch ($choice) {
        "1" {
            $balance = Read-Host "New Quo credit balance in dollars (like 25 or 9.99)"
            if ($balance -notmatch '^\d+(\.\d{1,2})?$') { Write-Host "That isn't a dollar amount." -ForegroundColor Yellow; break }
            $line = Read-Host "Text me when a run would leave less than (Enter keeps your current line)"
            if ($line -match '^\d+(\.\d{1,2})?$') { Run-Textcrm @("credits", $balance, "--alert-below", $line) }
            else { Run-Textcrm @("credits", $balance) }
        }
        "2" { Run-Textcrm @("check", "--stats", (Get-Date -Format "yyyy-MM-dd")) }
        "3" {
            $phone = Read-Host "Phone number"
            if ($phone) { Run-Textcrm @("check", $phone) }
        }
        "4" { Run-Textcrm @("plans") }
        "5" { Run-Textcrm @("check", "--codex") }
        "6" { Get-Content (Join-Path $repo "logs\textcrm.log") -Tail 15 | ForEach-Object { $_.Substring(0, [Math]::Min(200, $_.Length)) } }
        "7" {
            Write-Host "Restart only when the morning CDK read is not running; Chrome will ask you to click Allow again." -ForegroundColor Yellow
            if ((Read-Host "Restart now? (y/n)").Trim().ToLower() -eq "y") {
                $running = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -match "textcrm run" }
                if ($running) {
                    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }
                    Write-Host "Stopped. It starts again by itself within a minute, on the latest code."
                } else {
                    # Through Explorer, so the run is not elevated like this window: same as the Startup copy.
                    Start-Process explorer.exe (Join-Path $repo "scripts\textcrm-run.cmd")
                    Write-Host "It wasn't running; started it."
                }
            }
        }
        "Q" { return }
        default { Write-Host "Pick 1-7 or Q." -ForegroundColor Yellow }
    }
}
