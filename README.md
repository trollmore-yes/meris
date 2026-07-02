# Fiction Update Discord Bot (Pycord)

A Discord bot that monitors RoyalRoad RSS feeds and posts new update links to an announcement channel while pinging feed-specific roles.

## Features

- Monitors RoyalRoad via RSS
- Per-feed role ping mapping from `.env`
- Automatic periodic checks while the bot is running
- Slash command `/check_updates` to force an immediate RoyalRoad check
- Slash command `/set_announcement_channel` to save the current channel as the announcement channel for the current server
- Slash command `/reannounce_last_update` to repost the latest link for a selected platform/story
- Slash command `/set_story_roles` to override story role pings per server
- Slash command `/debug_latest_update` to inspect what the bot currently sees as the latest update
- Persistent state in `data/state.json` to avoid duplicate announcements
- Per-server config in `data/guild_config.json`

## Requirements

- Python 3.10+
- A Discord bot token with access to your server
- Bot permissions in your server/channel:
  - View Channel
  - Send Messages
  - Mention Everyone, @here, and All Roles (or role mention permissions for target roles)
  - Use Application Commands
  - Manage Server (for `/set_announcement_channel`)

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill in your values.

4. Run the bot:

```bash
python src/bot.py
```

## Environment Variables

See `.env.example` for full examples.

- `DISCORD_TOKEN` (required)
- `ANNOUNCEMENT_CHANNEL_ID` (optional fallback for servers without explicit per-server setup)
- `CHECK_INTERVAL_MINUTES` (optional, default `10`)
- `ANNOUNCE_ON_FIRST_RUN` (optional, default `false`)
- `MERIS_DATA_DIR` (optional, directory for `state.json` and `guild_config.json`)
- `ROYALROAD_FEEDS_JSON` (required for RoyalRoad monitoring)
- `PATREON_FEEDS_JSON` (used for Patreon page monitoring and `/reannounce_last_update`)

### Data directory behavior

- Default data directory is `<project-root>/data`.
- If `MERIS_DATA_DIR` is set, state/config files are loaded from that directory.
- Relative `MERIS_DATA_DIR` values are resolved from the project root.

This is useful for production service layouts, for example:

- App code and venv: `/opt/meris`
- Env file: `/etc/meris/bot.env`
- Persistent state: `/var/lib/meris`

Set `MERIS_DATA_DIR=/var/lib/meris` in your service environment.

### Feed JSON format

`ROYALROAD_FEEDS_JSON` and `PATREON_FEEDS_JSON` use this format:

```json
[
  {
    "name": "Readable feed label",
    "url": "https://example.com/rss/feed"
  }
]
```

- `name`: Label shown in announcements
- `url`: RSS URL for that source
- `roleIds`: Discord role IDs to ping when that source updates

For Patreon entries, set `url` to the public creator/page URL you want monitored. The bot checks that creator's sitemap for the newest public post link instead of using RSS.
It supports both `https://www.patreon.com/c/<creator>` and `https://www.patreon.com/<creator>` URL formats.

## Behavior Notes

- On first startup with an empty state file, each feed is "primed" (latest entry recorded) to avoid old-post spam.
- If you want startup announcements for current latest entries, set `ANNOUNCE_ON_FIRST_RUN=true`.
- The bot stores per-server announcement settings in `data/guild_config.json`.
- In multi-server use, each server should run `/set_announcement_channel` once in its desired channel.

## Slash Command

- `/check_updates`
  - Forces an immediate check of all configured feeds.
  - Returns an ephemeral summary to the command user.
- `/set_announcement_channel`
  - Sets the announcement channel for the current server.
  - Saves to `data/guild_config.json` and applies immediately.
- `/reannounce_last_update`
  - Field `platform`: `royalroad` or `patreon`.
  - Field `story`: autocomplete list generated from configured feed source names.
  - Reposts the latest entry for the selected source to this server's announcement channel.
- `/set_story_roles`
  - Field `platform`: `royalroad` or `patreon`.
  - Field `story`: autocomplete list generated from configured feed source names.
  - Field `role_ids`: comma-separated role IDs for this server only.
  - Leave `role_ids` empty to clear override and use default role IDs from feed config.
- `/debug_latest_update`
  - Field `platform`: `royalroad` or `patreon`.
  - Field `story`: autocomplete list generated from configured feed source names.
  - Shows the latest detected title, link, and ID, or the exact URL checked if nothing is found.
