#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
export PYTHONUNBUFFERED=1

LOCK="$ROOT/bot.lock"
PIDFILE="$ROOT/bot.pid"
LOG="$ROOT/bot.log"
BOTPY="$ROOT/bot.py"

is_our_bot_running() {
  check_pid() {
    local pid="$1"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    local cmd cwd
    cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *"$BOTPY"*|*bot.py*) ;;
      *) return 1 ;;
    esac
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
    [ -z "$cwd" ] || [ "$cwd" = "$ROOT" ]
  }
  if [ -f "$PIDFILE" ] && check_pid "$(tr -d '[:space:]' <"$PIDFILE")"; then
    return 0
  fi
  if [ -f "$LOCK" ] && check_pid "$(head -n1 "$LOCK" | tr -d '[:space:]')"; then
    return 0
  fi
  for proc in /proc/[0-9]*; do
    [ -r "$proc/cmdline" ] || continue
    cmd=$(tr '\0' ' ' <"$proc/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *bot.py*) ;;
      *) continue ;;
    esac
    cwd=$(readlink "$proc/cwd" 2>/dev/null || true)
    if [ "$cwd" = "$ROOT" ]; then
      return 0
    fi
  done
  return 1
}

if [ -f "$LOCK" ]; then
  if is_our_bot_running; then
    echo "Bot already running for $ROOT (lock present). Use status-bot.sh / stop-bot.sh."
    exit 1
  else
    echo "Removing stale lock: $LOCK"
    rm -f "$LOCK"
  fi
fi

if is_our_bot_running; then
  echo "Bot already running for $ROOT. Refusing duplicate start."
  exit 1
fi

nohup python bot.py >>"$LOG" 2>&1 &
NEWPID=$!
echo "$NEWPID" >"$PIDFILE"
sleep 0.5
if kill -0 "$NEWPID" 2>/dev/null; then
  echo "Started bot pid=$NEWPID log=$LOG"
  exit 0
else
  echo "Bot process exited immediately; check $LOG"
  exit 1
fi
