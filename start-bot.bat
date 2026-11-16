@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -q tzdata
".venv\Scripts\python.exe" -c "from zoneinfo import ZoneInfo; ZoneInfo('America/Denver'); ZoneInfo('America/Chicago')" || (
  echo ERROR: timezone data still missing in venv.
  echo Try: .venv\Scripts\python.exe -m pip install -U tzdata
  pause
  exit /b 1
)
echo Starting Humble Upgrade with venv Python...
".venv\Scripts\python.exe" bot.py
pause
