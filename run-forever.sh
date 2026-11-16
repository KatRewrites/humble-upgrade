#!/usr/bin/env bash
# Keeps The Humble Upgrade running on the box; restarts it 10s after any exit.
cd "$(dirname "$0")"
exec 9>run-forever.lock
flock -n 9 || { echo "supervisor already running"; exit 0; }
while true; do
  echo "[$(date '+%F %T')] starting bot" >> supervisor.log
  PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 .venv/bin/python bot.py >> bot.out.log 2>> bot.err.log
  echo "[$(date '+%F %T')] bot exited code $?; restarting in 10s" >> supervisor.log
  sleep 10
done
