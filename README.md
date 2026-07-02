# Meris

Discord bot that monitors RoyalRoad and Patreon for new posts and announces them.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your values
python src/bot.py
```

## Environment

See `.env.example` for all options. Minimum required: `DISCORD_TOKEN`, and at least one of `ROYALROAD_FEEDS_JSON` or `PATREON_FEEDS_JSON`.

Patreon member-only posts require a creator access token from https://www.patreon.com/portal/registration/register-clients — set `PATREON_ACCESS_TOKEN` in `.env`.

## Slash Commands

`/check_updates` — force check all feeds
`/set_announcement_channel` — set where updates are posted (per-server)
`/reannounce_last_update` — repost latest entry
`/set_story_roles` — ping specific roles per story
`/debug_latest_update` — show what the bot sees as latest
