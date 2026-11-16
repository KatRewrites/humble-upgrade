#!/usr/bin/env bash
# Copies every non-empty variable from .env to the linked Railway service without
# printing any values (each value goes over stdin), then switches chat to OpenRouter.
# OPENROUTER_API_KEY comes from .env if present there, else it is asked for silently.
# (Putting the key in the box .env is safe: the box only uses it if CHAT_BACKEND is set.)
# Run after `railway link`.
# Usage: ./railway-set-vars.sh [--service NAME]
set -euo pipefail
cd "$(dirname "$0")"
svc=("$@")
python3 - <<'PY' > /tmp/.hu-envnames
import re
for line in open('.env', encoding='utf-8-sig'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k, v = line.split('=', 1)
    k = k.strip().removeprefix('export ').strip()
    v = v.strip().strip('"').strip("'")
    if re.fullmatch(r'[A-Z_][A-Z0-9_]*', k) and v:
        print(k)
PY
while read -r key; do
  python3 - "$key" <<'PY' | railway variable set "$key" --stdin --skip-deploys "${svc[@]}" >/dev/null
import sys
key = sys.argv[1]
for line in open('.env', encoding='utf-8-sig'):
    line = line.strip()
    if '=' in line and line.split('=', 1)[0].strip().removeprefix('export ').strip() == key:
        sys.stdout.write(line.split('=', 1)[1].strip().strip('"').strip("'"))
        break
PY
  echo "set $key"
done < /tmp/.hu-envnames
rm -f /tmp/.hu-envnames
railway variable set WAIT_FOR_DB=1 --skip-deploys "${svc[@]}" >/dev/null && echo "set WAIT_FOR_DB=1"

# Hosted chat (Railway has no Ollama).
if ! grep -qE '^[[:space:]]*(export[[:space:]]+)?OPENROUTER_API_KEY=[^[:space:]]' .env; then
  if [ -t 0 ]; then
    read -r -s -p "Paste the OpenRouter API key (input hidden): " OR_KEY; echo
    [ -n "$OR_KEY" ] && printf '%s' "$OR_KEY" | railway variable set OPENROUTER_API_KEY --stdin --skip-deploys "${svc[@]}" >/dev/null \
      && echo "set OPENROUTER_API_KEY"
    unset OR_KEY
  else
    echo "WARNING: OPENROUTER_API_KEY not in .env and no terminal to ask; set it with:"
    echo "  railway variable set OPENROUTER_API_KEY --stdin   (paste key, then Ctrl-D)"
  fi
fi
railway variable set CHAT_BACKEND=openrouter --skip-deploys "${svc[@]}" >/dev/null && echo "set CHAT_BACKEND=openrouter"
railway variable set OPENROUTER_MODEL=qwen/qwen-2.5-7b-instruct --skip-deploys "${svc[@]}" >/dev/null \
  && echo "set OPENROUTER_MODEL=qwen/qwen-2.5-7b-instruct"
echo "done (values were not printed)"
