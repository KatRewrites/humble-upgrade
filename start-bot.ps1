Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $py -c "from zoneinfo import ZoneInfo; ZoneInfo('America/Chicago')" 2>$null
if ($LASTEXITCODE -ne 0) { & $py -m pip install -r requirements.txt }
Write-Host "Starting friend-bot with .venv Python..."
& $py bot.py
