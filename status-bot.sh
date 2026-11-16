#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

LOCK="$ROOT/bot.lock"
PIDFILE="$ROOT/bot.pid"
BOTPY="$ROOT/bot.py"

echo "project=$ROOT"
echo "lock_exists=$([ -f "$LOCK" ] && echo true || echo false)"
echo "pidfile_exists=$([ -f "$PIDFILE" ] && echo true || echo false)"

if [ -f "$LOCK" ]; then
  echo "lock_pid=$(head -n1 "$LOCK" | tr -d '[:space:]')"
fi
if [ -f "$PIDFILE" ]; then
  echo "pidfile_pid=$(tr -d '[:space:]' <"$PIDFILE")"
fi

is_bot_pid() {
  local pid="$1"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local cmd
  cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
  case "$cmd" in
    *"$BOTPY"*|*bot.py*) ;;
    *) return 1 ;;
  esac
  # Prefer cwd match when readable
  if [ -r "/proc/$pid/cwd" ]; then
    local cwd
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
    [ "$cwd" = "$ROOT" ] || [ -z "$cwd" ] || return 0
    [ "$cwd" = "$ROOT" ] || return 1
  fi
  return 0
}

found=()
# Prefer pidfile / lock when they point at a live bot.py
for candidate in ""; do :; done
cands=()
if [ -f "$PIDFILE" ]; then cands+=("$(tr -d '[:space:]' <"$PIDFILE")"); fi
if [ -f "$LOCK" ]; then cands+=("$(head -n1 "$LOCK" | tr -d '[:space:]')"); fi
for pid in "${cands[@]}"; do
  if is_bot_pid "$pid"; then
    found+=("$pid")
  fi
done

# Fallback: scan processes whose cwd is this project and cmdline has bot.py
if [ "${#found[@]}" -eq 0 ]; then
  for proc in /proc/[0-9]*; do
    [ -r "$proc/cmdline" ] || continue
    pid=$(basename "$proc")
    cmd=$(tr '\0' ' ' <"$proc/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *bot.py*) ;;
      *) continue ;;
    esac
    cwd=$(readlink "$proc/cwd" 2>/dev/null || true)
    if [ "$cwd" = "$ROOT" ]; then
      found+=("$pid")
    fi
  done
fi

# dedupe
if [ "${#found[@]}" -gt 0 ]; then
  # unique
  uniq=()
  for p in "${found[@]}"; do
    skip=false
    for u in "${uniq[@]:-}"; do [ "$u" = "$p" ] && skip=true && break; done
    $skip || uniq+=("$p")
  done
  found=("${uniq[@]}")
  echo "running=true"
  echo "pids=${found[*]}"
  for p in "${found[@]}"; do
    if [ -r "/proc/$p/cmdline" ]; then
      echo "cmdline_$p=$(tr '\0' ' ' <"/proc/$p/cmdline")"
    fi
  done
  exit 0
else
  echo "running=false"
  if [ -f "$LOCK" ]; then
    echo "note=stale_lock_present"
  fi
  exit 1
fi
