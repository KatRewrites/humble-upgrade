#!/bin/sh
# Railway entrypoint for Humble Upgrade (not used on the box).
# WAIT_FOR_DB=1: don't log in to Discord until the real DB has been uploaded to the
# volume. This stops the Railway copy from starting on an empty DB at cutover.
# BOOTSTRAP_DB_URL: optional one-time HTTPS URL (e.g. private bucket presigned GET)
# to seed /data/bot.db when the volume is empty. Cleared after cutover.
set -e
DB="${DATABASE_PATH:-/data/bot.db}"
if [ -n "${BOOTSTRAP_DB_URL:-}" ] && [ ! -s "$DB" ]; then
  echo "[entrypoint] bootstrapping $DB from BOOTSTRAP_DB_URL"
  mkdir -p "$(dirname "$DB")"
  python - <<'PY'
import os, sys, tempfile, pathlib, urllib.request
url = os.environ["BOOTSTRAP_DB_URL"]
dest = pathlib.Path(os.environ.get("DATABASE_PATH", "/data/bot.db"))
dest.parent.mkdir(parents=True, exist_ok=True)
tmp = dest.with_suffix(dest.suffix + ".tmp")
try:
    with urllib.request.urlopen(url, timeout=300) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    size = tmp.stat().st_size
    if size < 1024:
        raise SystemExit(f"bootstrap download too small: {size} bytes")
    tmp.replace(dest)
    print(f"[entrypoint] wrote {dest} ({size} bytes)")
except Exception as e:
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    print(f"[entrypoint] bootstrap failed: {e}", file=sys.stderr)
    raise
PY
fi
if [ "${WAIT_FOR_DB:-0}" = "1" ]; then
  n=0
  while [ ! -s "$DB" ]; do
    if [ $((n % 12)) -eq 0 ]; then
      echo "[entrypoint] waiting for $DB (upload the DB to the volume to start the bot)"
    fi
    n=$((n + 1))
    sleep 5
  done
  echo "[entrypoint] found $DB ($(wc -c < "$DB") bytes), starting bot"
fi
# Stale lock from a previous container is harmless (pid check), but clear it anyway.
rm -f /app/bot.lock
exec "$@"
