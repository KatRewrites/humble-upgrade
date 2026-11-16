#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Flip maintenance so any last slash hits get "updating" (best-effort)
FLAG="$ROOT/data/maintenance.flag"
mkdir -p "$ROOT/data"
echo on > "$FLAG"
sleep 1


LOCK="$ROOT/bot.lock"
PIDFILE="$ROOT/bot.pid"
BOTPY="$ROOT/bot.py"

pids=()

collect_pid() {
  local p="$1"
  [[ "$p" =~ ^[0-9]+$ ]] || return 0
  pids+=("$p")
}

if [ -f "$PIDFILE" ]; then
  collect_pid "$(tr -d '[:space:]' <"$PIDFILE")"
fi
if [ -f "$LOCK" ]; then
  collect_pid "$(head -n1 "$LOCK" | tr -d '[:space:]')"
fi

# Also find by cmdline / project cwd (nohup often uses relative "python bot.py")
if command -v pgrep >/dev/null 2>&1; then
  while read -r p; do
    collect_pid "$p"
  done < <(pgrep -f "python.*bot\.py" 2>/dev/null || true)
fi
for proc in /proc/[0-9]*; do
  [ -r "$proc/cmdline" ] || continue
  cmd=$(tr '\0' ' ' <"$proc/cmdline" 2>/dev/null || true)
  case "$cmd" in
    *"$BOTPY"*|*bot.py*) ;;
    *) continue ;;
  esac
  cwd=$(readlink "$proc/cwd" 2>/dev/null || true)
  if [ "$cwd" = "$ROOT" ] || printf '%s' "$cmd" | grep -Fq "$BOTPY"; then
    collect_pid "$(basename "$proc")"
  fi
done

# Unique
mapfile -t pids < <(printf '%s\n' "${pids[@]:-}" | awk 'NF && !seen[$0]++')

if [ "${#pids[@]}" -eq 0 ]; then
  echo "No running bot.py found for $ROOT"
  rm -f "$LOCK" "$PIDFILE"
  exit 0
fi

echo "Stopping pids: ${pids[*]}"
for p in "${pids[@]}"; do
  kill "$p" 2>/dev/null || true
done
sleep 1
for p in "${pids[@]}"; do
  if kill -0 "$p" 2>/dev/null; then
    echo "Force killing $p"
    kill -9 "$p" 2>/dev/null || true
  fi
done
rm -f "$LOCK" "$PIDFILE"
echo "Stopped."
