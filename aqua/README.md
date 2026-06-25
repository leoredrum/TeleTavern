# Aqua — Telegram Tavern bot instance
#
# This bot lives at ~/Documents/telegramtavern/aqua/.
# Source of truth (bot.py, pipeline.py, director.py, …) is at the monorepo root.
# The .py copies here exist for the run.sh venv launcher — they are NOT source
# of record and are gitignored.

## Quick start

```bash
cd ~/Documents/telegramtavern/aqua

# 1. Copy template, fill in your real bot token
cp .env.example .env
$EDITOR .env

# 2. Bootstrap venv (only first time)
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

# 3. Make sure character card is in place
ls data/Aqua.png          # must exist

# 4. Run
./run.sh                  # foreground
./run.sh --bg             # background, logs to bot.log
```

## Verify

```bash
# Repo-level check that .env is sane and not in git
~/Documents/telegramtavern/scripts/check_config.sh aqua
```

## What is tracked in git

Only this `README.md` and the `.env.example`. Everything else inside this
directory (`.env`, `data/*.db`, `venv/`, `bot.log`, `*.py` copies, `__pycache__/`)
is deployment/runtime state and is gitignored.
