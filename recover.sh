#!/usr/bin/env bash
# Rebuilds what a box reset can wipe, then makes sure Ollama and the bot run.
# Safe to run repeatedly: each step only acts when something is missing.
cd "$(dirname "$0")"
MODEL="${BOT_CHAT_MODEL:-qwen2.5:7b}"
say() { echo "[recover] $*"; }

# After the Railway cutover, the box copy must never start again (two copies on one
# token fight each other). RAILWAY_LIVE is created at cutover; delete it to roll back.
if [ -f RAILWAY_LIVE ]; then
  say "bot is hosted on Railway (RAILWAY_LIVE present); not starting the box copy"
  exit 0
fi

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import discord, aiosqlite, dotenv" 2>/dev/null; then
  say "rebuilding .venv"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || { say "FAILED venv"; exit 1; }
fi

if ! command -v ollama >/dev/null 2>&1; then
  say "installing ollama"
  command -v zstd >/dev/null 2>&1 || sudo apt-get install -y -q zstd >/dev/null 2>&1 \
    || { sudo apt-get update -q >/dev/null 2>&1; sudo apt-get install -y -q zstd >/dev/null 2>&1; }
  curl -fsSL https://ollama.com/install.sh | sh >/tmp/ollama-install.log 2>&1 || say "FAILED ollama install"
fi

if command -v ollama >/dev/null 2>&1; then
  if ! curl -s -o /dev/null http://127.0.0.1:11434/; then
    say "starting ollama"
    (setsid nohup ollama serve >/tmp/ollama.log 2>&1 &)
    sleep 5
  fi
  if ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
    say "pulling $MODEL"
    ollama pull "$MODEL" >/tmp/ollama-pull.log 2>&1 || say "FAILED model pull"
  fi
fi

if ! pgrep -f "bash ./run-forever.sh|bash run-forever.sh" >/dev/null; then
  say "starting bot supervisor"
  (setsid nohup ./run-forever.sh >/dev/null 2>&1 </dev/null &)
  sleep 20
fi

# After a box pause, the python process can stay up while Discord is dead.
# recover used to call that "healthy." Force a clean restart when the last
# successful gateway session is older than the last pause/suspend warning.
force_restart_stale_gateway() {
  local err="bot.err.log"
  [ -f "$err" ] || return 0
  local last_ok last_pause
  last_ok=$(grep -nE "Logged in as The Humble Upgrade|has connected to Gateway|has successfully RESUMED" "$err" | tail -n 1 | cut -d: -f1)
  last_pause=$(grep -n "Box was paused/suspended" "$err" | tail -n 1 | cut -d: -f1)
  [ -n "$last_pause" ] || return 0
  [ -n "$last_ok" ] || last_ok=0
  if [ "$last_pause" -gt "$last_ok" ]; then
    say "Discord session stale after box pause — restarting bot"
    pkill -9 -f "[.]venv/bin/python bot.py" 2>/dev/null || true
    # supervisor sleeps 10s then starts; give it a moment
    sleep 15
  fi
}

force_restart_stale_gateway

pgrep -f "bot.py" >/dev/null && say "bot running" || say "FAILED bot not running"
curl -s -o /dev/null -w "[recover] ollama http %{http_code}\n" http://127.0.0.1:11434/
