@echo off
cd /d "%~dp0"

REM Single-instance: if bot.py already running from this folder, exit
powershell -NoProfile -Command ^
  "$cwd=(Get-Location).Path; $hits=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($cwd.Replace('\','\\')) -and $_.CommandLine -match 'bot\.py' }; if($hits){ exit 2 }"
if errorlevel 2 (
  echo [%date% %time%] already running - skip >> "%~dp0\bot-autostart.log"
  exit /b 0
)

".venv\Scripts\python.exe" -m pip install -q tzdata >nul 2>&1
".venv\Scripts\python.exe" -c "from zoneinfo import ZoneInfo; ZoneInfo('America/Chicago')" || (
  echo [%date% %time%] timezone check failed >> "%~dp0\bot-autostart.log"
  exit /b 1
)
echo [%date% %time%] starting Humble Upgrade >> "%~dp0\bot-autostart.log"
".venv\Scripts\python.exe" bot.py >> "%~dp0\bot-autostart.log" 2>&1
echo [%date% %time%] bot exited code %ERRORLEVEL% >> "%~dp0\bot-autostart.log"
