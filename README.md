# ⚡STORM NET⚡ Telegram Bot

Render-ready Telegram points/referral/redeem-code bot with GitHub-backed JSON persistence.

## Files
- `bot.py` — all application logic
- `data.json` — single JSON database
- `requirements.txt` — dependencies
- `runtime.txt` — Python runtime
- `README.md` — setup/deployment guide
- `configs/` — optional directory for legitimate configuration files

## Render environment variables

Required:
- `BOT_TOKEN`
- `OWNER_ID`
- `GITHUB_TOKEN`
- `GITHUB_REPO` (`owner/repository`)

Optional:
- `ADMIN_IDS` — comma-separated Telegram User IDs
- `GITHUB_BRANCH` — default `main`
- `GITHUB_DATA_PATH` — default `data.json`
- `PORT` — supplied by Render

## Build / Start

Build:
```bash
pip install -r requirements.txt
```

Start:
```bash
python bot.py
```

The bot uses Telegram polling and starts a Flask health server for Render.

## Redeem Code Creation

There is **no combined format** such as `code|points|expiry|limit`.

Admin selects:

`🎟️ Redeem Codes` → `➕ Create Code`

Then the bot asks one question at a time:
1. Redeem code
2. Points reward
3. Expiration (`24h`, `7d`, or `0`)
4. Maximum users
5. Auto-post yes/no

## Auto Codes

Automatic random redeem codes use settings in `data.json`, including reward, user limit, expiry, interval, daily limit, code length and prefix.

## Channel + Discussion

Codes can be posted to the configured channel and/or its discussion group. The bot needs appropriate Telegram permissions in every destination.

## Optional Collaboration

Collaboration is **OFF by default**. When enabled, admins can add partner channel owners and their channel/discussion destinations.

## Anti-Cheat

- Referral state is keyed by Telegram User ID.
- Clearing Telegram chat history does not reset referral state.
- A referral reward cannot be awarded twice.
- Self-referrals are rejected.
- Five consecutive suspicious attempts can trigger a temporary block.
- Default block duration is 24 hours.
- No permanent ban.
- Re-opening an already-used referral link alone is not treated as a new fraud event.

## Persistence

Render's local filesystem is not permanent storage. `data.json` is persisted to the configured GitHub repository. Keep the repository private and never put `GITHUB_TOKEN` inside source code or JSON.

## Important

Use configuration/redeem distribution only for legitimate, authorized services. Do not use the bot to bypass ISP billing, access controls, or network restrictions.
