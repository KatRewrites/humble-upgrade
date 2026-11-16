# Humble Upgrade

A production Discord bot for a friend community: economy, games, social commands, translation, logging, and day-to-day operations.

Built and operated with AI-assisted development.

The bot is written in Python with [discord.py](https://github.com/Rapptz/discord.py), stores state in SQLite, and is hosted on [Railway](https://railway.com) with a persistent volume.

There is no automod, no auto-punishment, and the bot never DMs users.

## Features

| Area | What members get |
|------|------------------|
| Economy | `/balance` `/give` `/leaderboard` `/daily` (streaks) `/inventory` `/shop` `/borrow` and a small credit system |
| Games | `/guess` `/slots` `/rps` `/tictactoe` `/blackjack` `/poker` `/uno` `/trivia` `/pinball` `/higherlower` `/crash` `/deathroll` `/duel` |
| Social | `/ship` `/hug` `/headpat` `/cuddle` `/kiss` `/marry` `/divorce` `/relationship` `/prefs set` |
| Family | `/kid adopt` (married partners confirm) plus `/kid status` `/kid feed` `/kid play` `/kid nap` `/kid hug` `/kid gift` `/kid nickname` `/kid rename` |
| Fun | `/8ball` `/factcheck` `/meme` `/roast` (opt-in) `/crew` `/nuke` (cartoon) |
| Utility | `/timezone` `/time` `/afk` `/pinboard` `/translate` (plus auto-translate), welcome/leave, `/help` |
| Achievements | `/achievements` progress and unlock banners |
| Logging | Passive message log, live posts to a log channel, restricted `/log search` and `/log stats` |
| Ops | `/status`, `/backup create\|list\|restore`, `/maintenance` |

## Architecture

```
humble-upgrade/
  bot.py                # entrypoint, cog loader, slash-command sync
  config.py             # environment-backed settings
  cogs/                 # slash commands, buttons, listeners
  services/             # SQLite, economy, backups, chat, translation, kids
  assets/               # currency art and achievement banners
  Dockerfile            # Railway / container runtime
  docker-entrypoint.sh  # optional DB wait gate + bootstrap download
  railway.json          # one replica, restart always, volume at /data
  recover.sh            # local recovery, including a stale-gateway check
```

- **Cogs** own user-facing commands. Each feature (economy, games, Uno, social, kids, court, logging, etc.) lives in its own module under `cogs/`.
- **Services** own data and shared logic: `services/db.py` (schema-versioned SQLite), `services/economy.py`, `services/backup.py`, `services/local_chat.py`, `services/translator.py`, and others.
- **SQLite** is the system of record (`DATABASE_PATH`, default `data/bot.db`). Schema migrations are numbered and idempotent.
- **Chat** can run on a local Ollama model, or on OpenRouter when `CHAT_BACKEND=openrouter` and `OPENROUTER_API_KEY` are set (used on Railway).
- **Safety filters** in `services/chat_safety.py` and `services/user_text.py` block slurs and unsafe model output before anything is posted.

## Setup

Requirements: Python 3.10+, and a Discord application with the **Message Content** and **Server Members** privileged intents. Invite the bot with the `bot` and `applications.commands` scopes.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set DISCORD_TOKEN=...
python bot.py
```

On Windows, prefer `start-bot.bat` or `start-bot.ps1` so the venv Python (and `tzdata`) is used. `/timezone` aliases like `CST` fail on some system Pythons that ship without timezone data.

### Useful environment variables

See `.env.example` for the full list of names. The important ones:

- `DISCORD_TOKEN` — required
- `DISCORD_GUILD_ID` — instant slash-command sync for one server
- `DATABASE_PATH` / `BACKUP_DIR` — on Railway these default to `/data/bot.db` and `/data/db-backups`
- `BACKUP_KEEP` — snapshot retention (default **5**)
- `LOG_CHANNEL_ID` — live message log channel
- `CHAT_BACKEND=openrouter` and `OPENROUTER_API_KEY` — hosted chat
- `WAIT_FOR_DB=1` and `BOOTSTRAP_DB_URL` — cutover helpers (see below)

## Hosting on Railway

The live bot runs as a Docker service on Railway's Hobby plan, with a **persistent volume mounted at `/data`**. The database and daily snapshots live on that volume so they survive redeploys.

`railway.json` keeps **one replica**, restarts always, and sets `overlapSeconds: 0` so an old container stops before a new one logs into Discord. Two processes sharing one bot token will fight each other on the gateway.

The Docker entrypoint can:

- wait until `/data/bot.db` exists when `WAIT_FOR_DB=1` (so a fresh volume never logs in against an empty database)
- download a bootstrap snapshot from `BOOTSTRAP_DB_URL` when the volume is empty

Daily backups use SQLite's online backup API (WAL-safe). Retention is 5 snapshots. Space is checked **before** a new copy is written.

## Operations & troubleshooting

These are real incidents from running the bot in production, written the way I'd walk them in an interview.

### 1. Host pauses killed the Discord gateway, while health checks still said "healthy"

**Situation.** The bot originally ran on a shared host VM. When that VM paused or suspended, the Python process stayed up, so a simple "is the process running?" check looked healthy. The Discord gateway session was already dead, so the bot was silent in the server.

**Task.** Detect a stale gateway after a pause and recover automatically, instead of waiting for someone to notice chat had gone quiet.

**Action.** I compared the bot's error log for the last successful gateway login or resume against the last pause/suspend warning. If a pause happened *after* the last good session, `recover.sh` now force-restarts the process. A loop watchdog in the bot also records gateway freshness.

**Result.** After a host pause, recovery no longer trusted a living process alone. The bot came back on the gateway without a manual SSH restart.

### 2. Migrated the live bot from a self-hosted box to Railway with zero data loss

**Situation.** The self-hosted box was a single point of failure (pauses, disk, process babysitting). I needed the same bot, the same SQLite data, and no downtime overlap on the Discord token.

**Task.** Move hosting to Railway with a persistent volume, a repeatable Docker build, and a cutover that could not start the new copy on an empty database or run two copies at once.

**Action.** I added a `Dockerfile`, `railway.json`, and an entrypoint wait-gate (`WAIT_FOR_DB=1`). Variables were copied to Railway without printing secrets. The live database was snapshotted with SQLite's backup API (WAL folded into one file, integrity-checked). The old process was stopped first, the snapshot was uploaded to the volume (including a bootstrap-download path), and only then did the Railway container see `/data/bot.db` and log in.

**Result.** Cutover completed with **zero data loss**. Balances, marriages, quotes, and message history were intact. Later deploys are `railway up` — no more kill-and-restart scripts for routine releases.

### 3. OpenRouter returned HTTP 401 after the Railway cutover

**Situation.** After the move, the bot was online for commands, but hosted chat (the @mention replies) failed. Railway has no local Ollama, so chat depends on OpenRouter.

**Task.** Find why authentication failed and restore chat without guesswork.

**Action.** I checked Railway logs and traced the 401 to a bad or missing `OPENROUTER_API_KEY` environment variable after the cutover — the backend was set to OpenRouter, but the key wasn't valid in that environment. I corrected the variable and confirmed the startup log showed the intended model.

**Result.** Chat replies worked again. Soft-fail behavior (an emoji or a short "try again" instead of a crash) kept the rest of the bot usable while the key was wrong.

### 4. The Railway volume filled with daily database backups and broke #bot-logs

**Situation.** In November 2026, members reported that `#bot-logs` had stopped posting. Railway logs showed SQLite errors: **"database or disk is full."** The volume had been eaten by daily database snapshots. Because the logger wrote the message to SQLite *before* posting to the channel, every full-disk write also silenced the live log.

**Task.** Restore logging immediately, stop backups from filling the volume again, and keep the live database from being wedged.

**Action.** I reordered the logger so the channel post happens **first**, and a failed database insert is warning-logged instead of blocking the post. Backups now **prune old snapshots before** taking a new one (retention 5). A free-space guard skips a backup if there isn't roughly two database-sizes of headroom, and a failed snapshot is deleted so a half-written file can't keep eating space.

**Result.** `#bot-logs` started posting again, new backups no longer filled the volume, and a later redeploy wouldn't repeat the same "gift" of leftover snapshots.

## Skills demonstrated

Python · SQLite · Docker · Railway · Linux · Git · incident response · monitoring · discord.py · environment-based configuration · backups and restore · log analysis

## License

This public snapshot is shared as a portfolio project. Secrets, live databases, and private server data are not included.
