import asyncio, json, logging, os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import discord, feedparser
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("fiction-update-bot")


@dataclass(frozen=True)
class FeedSource:
    platform: str
    name: str
    url: str
    role_ids: list[int]

    @property
    def key(self) -> str:
        return (
            f"{self.platform}:{self.name}:{sha256(self.url.encode()).hexdigest()[:16]}"
        )


def _api_get(url: str, headers: dict[str, str] | None = None) -> Any:
    h = {"User-Agent": "Mozilla/5.0"}
    if headers:
        h.update(headers)
    with urlopen(Request(url, headers=h), timeout=20) as r:
        return json.loads(r.read().decode())


def _rss_latest(url: str) -> dict[str, str] | None:
    parsed = feedparser.parse(url)
    if not parsed.entries:
        return None
    e = parsed.entries[0]
    eid = str(
        e.get("id")
        or e.get("guid")
        or e.get("link")
        or f"{e.get('title', '')}:{e.get('published', '')}"
    ).strip()
    if not eid:
        return None
    return {
        "id": eid,
        "link": str(e.get("link") or "").strip(),
        "title": str(e.get("title") or "New update").strip(),
    }


_PATREON_TOKEN: str | None = None
_PATREON_REFRESH: str | None = os.getenv("PATREON_REFRESH_TOKEN", "").strip() or None
_PATREON_CID: str | None = os.getenv("PATREON_CLIENT_ID", "").strip() or None
_PATREON_SECRET: str | None = os.getenv("PATREON_CLIENT_SECRET", "").strip() or None


def _patreon_token() -> str | None:
    global _PATREON_TOKEN
    if _PATREON_TOKEN:
        return _PATREON_TOKEN
    _PATREON_TOKEN = os.getenv("PATREON_ACCESS_TOKEN", "").strip() or None
    if _PATREON_TOKEN:
        return _PATREON_TOKEN
    if not (_PATREON_CID and _PATREON_SECRET and _PATREON_REFRESH):
        return None
    try:
        body = f"grant_type=refresh_token&refresh_token={_PATREON_REFRESH}&client_id={_PATREON_CID}&client_secret={_PATREON_SECRET}"
        req = Request(
            "https://www.patreon.com/api/oauth2/token",
            data=body.encode(),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urlopen(req, timeout=20) as r:
            _PATREON_TOKEN = json.loads(r.read().decode()).get("access_token")
        return _PATREON_TOKEN
    except Exception as e:
        logger.warning("Failed to get Patreon token: %s", e)
        return None


def _patreon_latest(url: str) -> dict[str, str] | None:
    if "patreon.com/rss/" in url.lower():
        return _rss_latest(url)
    parsed = urlsplit(url.strip())
    parts = [p for p in parsed.path.split("/") if p]
    slug = (
        (parts[1] if len(parts) >= 2 and parts[0] == "c" else parts[0])
        if parts
        else None
    )
    if not slug:
        return None
    for item in _api_get(f"https://www.patreon.com/api/search?q={slug}").get(
        "data", []
    ):
        iid, iurl = item.get("id", ""), item.get("attributes", {}).get("url", "")
        if iid.startswith("campaign_") and slug in iurl:
            cid = iid.removeprefix("campaign_")
            break
    else:
        return None
    token = _patreon_token()
    if token:
        data = _api_get(
            f"https://www.patreon.com/api/oauth2/v2/campaigns/{cid}/posts?fields%5Bpost%5D=title,url,is_public&page%5Bcount%5D=200",
            {"Authorization": f"Bearer {token}"},
        )
    else:
        logger.info("Set PATREON_ACCESS_TOKEN for member-only post detection.")
        data = _api_get(f"https://www.patreon.com/api/campaigns/{cid}/posts")
    posts = data.get("data", [])
    if not posts:
        return None
    a = posts[-1].get("attributes", {})
    pu = (a.get("url") or "").strip()
    if not pu:
        return None
    if pu.startswith("/"):
        pu = f"https://www.patreon.com{pu}"
    return {
        "id": pu,
        "link": pu,
        "title": (a.get("title") or "").strip() or "New update",
    }


def _required_env(n: str) -> str:
    v = os.getenv(n, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env: {n}")
    return v


TOKEN = _required_env("DISCORD_TOKEN")
DEFAULT_CHANNEL_ID: int | None = None
ci = os.getenv("ANNOUNCEMENT_CHANNEL_ID", "").strip()
if ci:
    DEFAULT_CHANNEL_ID = int(ci) if ci.isdigit() else None
CHECK_INTERVAL = float(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
if CHECK_INTERVAL <= 0:
    raise RuntimeError("CHECK_INTERVAL_MINUTES must be > 0")
ANNOUNCE_FIRST = os.getenv("ANNOUNCE_ON_FIRST_RUN", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _parse_sources(env: str, platform: str) -> list[FeedSource]:
    raw = json.loads(os.getenv(env, "[]"))
    if not isinstance(raw, list):
        raise RuntimeError(f"{env} must be a JSON array")
    out: list[FeedSource] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RuntimeError(f"{env}[{i}] must be an object")
        name, url = str(item.get("name", "")).strip(), str(item.get("url", "")).strip()
        if not name or not url:
            raise RuntimeError(f"{env}[{i}] needs name and url")
        rids = item.get("roleIds", [])
        if not isinstance(rids, list):
            raise RuntimeError(f"{env}[{i}].roleIds must be an array")
        out.append(
            FeedSource(
                platform=platform,
                name=name,
                url=url,
                role_ids=[int(x) for x in rids if str(x).strip().isdigit()],
            )
        )
    return out


ROYALROAD_SOURCES = _parse_sources("ROYALROAD_FEEDS_JSON", "RoyalRoad")
PATREON_SOURCES = _parse_sources("PATREON_FEEDS_JSON", "Patreon")
ALL_SOURCES = [*ROYALROAD_SOURCES, *PATREON_SOURCES]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = (
    Path(raw).expanduser()
    if (raw := os.getenv("MERIS_DATA_DIR", "").strip())
    else PROJECT_ROOT / "data"
)
DATA_DIR = DATA_DIR if DATA_DIR.is_absolute() else (PROJECT_ROOT / DATA_DIR).resolve()
STATE_PATH, GUILD_CONFIG_PATH = DATA_DIR / "state.json", DATA_DIR / "guild_config.json"


def _load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_PATH)


STATE = _load_state()
GUILD_CONFIG: dict[str, dict[str, Any]] = {}
try:
    raw = (
        json.loads(open(GUILD_CONFIG_PATH, encoding="utf-8").read())
        if GUILD_CONFIG_PATH.exists()
        else {}
    )
    gc = raw.get("guilds") if isinstance(raw, dict) and "guilds" in raw else raw
    if isinstance(gc, dict):
        for gid, p in gc.items():
            if isinstance(gid, str) and gid.isdigit() and isinstance(p, dict):
                ch = p.get("announcement_channel_id")
                ch_id = (
                    int(ch)
                    if isinstance(ch, (int, str)) and str(ch).isdigit()
                    else None
                )
                sr = {}
                for sk, rl in p.get("source_roles", {}).items():
                    if isinstance(sk, str) and isinstance(rl, list):
                        sr[sk] = [int(x) for x in rl if str(x).strip().isdigit()]
                GUILD_CONFIG[gid] = {
                    "announcement_channel_id": ch_id,
                    "source_roles": sr,
                }
except (OSError, json.JSONDecodeError):
    pass

GUILD_CONFIG_LOCK = asyncio.Lock()
asyncio.set_event_loop(asyncio.new_event_loop())
bot = discord.Bot(intents=discord.Intents.default())


async def _latest(source: FeedSource) -> dict[str, str] | None:
    if source.platform.strip().lower() == "patreon":
        return await asyncio.to_thread(_patreon_latest, source.url)
    return await asyncio.to_thread(_rss_latest, source.url)


async def _channel(guild_id: int) -> discord.abc.Messageable | None:
    entry = GUILD_CONFIG.get(str(guild_id), {})
    cid = entry.get("announcement_channel_id") or DEFAULT_CHANNEL_ID
    if not cid:
        return None
    ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
    return ch if isinstance(ch, discord.abc.Messageable) else None


def _role_mentions(guild_id: int, source: FeedSource) -> str:
    entry = GUILD_CONFIG.get(str(guild_id), {})
    rids = entry.get("source_roles", {}).get(source.key) or source.role_ids
    return " ".join(f"<@&{r}>" for r in rids)


async def _announce(
    source: FeedSource, entry: dict[str, str], guild_id: int | None = None
) -> int:
    count = 0
    for g in [g for g in bot.guilds if guild_id is None or g.id == guild_id]:
        ch = await _channel(g.id)
        if not ch:
            continue
        try:
            rm = _role_mentions(g.id, source)
            parts = ([rm] if rm else []) + [
                f"**New {source.platform} update: {source.name}**",
                entry.get("title", ""),
                entry.get("link", ""),
            ]
            await ch.send(
                "\n".join(filter(None, parts)),
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            count += 1
        except Exception as e:
            logger.exception("Failed to announce to guild %s: %s", g.id, e)
    return count


async def _check_one(source: FeedSource, announce_first: bool) -> str:
    latest = await _latest(source)
    if not latest:
        logger.info("No entries for %s", source.name)
        return "no_entries"
    prev = STATE.get(source.key)
    if prev is None:
        STATE[source.key] = latest["id"]
        _save_state(STATE)
        if announce_first:
            c = await _announce(source, latest)
            return f"announced:{c}" if c else "no_targets"
        logger.info("Primed %s", source.name)
        return "primed"
    if prev == latest["id"]:
        return "unchanged"
    c = await _announce(source, latest)
    STATE[source.key] = latest["id"]
    _save_state(STATE)
    return f"announced:{c}" if c else "no_targets"


_check_lock = asyncio.Lock()
_startup_done = False


async def _check(trigger: str) -> str:
    async with _check_lock:
        af = ANNOUNCE_FIRST and trigger == "startup"
        c = dict.fromkeys(
            ("announced", "unchanged", "primed", "no_entries", "no_targets", "errors"),
            0,
        )
        for src in ALL_SOURCES:
            try:
                out = await _check_one(src, af)
                if out.startswith("announced:"):
                    c["announced"] += 1
                elif out in c:
                    c[out] += 1
            except Exception as e:
                c["errors"] += 1
                logger.exception("Failed checking %s: %s", src.name, e)
    s = ", ".join(f"{k}={v}" for k, v in c.items())
    logger.info("Check complete. %s", s)
    return s


@tasks.loop(minutes=CHECK_INTERVAL)
async def _scheduled():
    await _check(trigger="scheduled")


@_scheduled.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    global _startup_done
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "n/a")
    if DEFAULT_CHANNEL_ID is None and not GUILD_CONFIG:
        logger.warning(
            "No announcement channels configured. Use /set_announcement_channel."
        )
    if not _scheduled.is_running():
        _scheduled.start()
        logger.info("Scheduled checks started (every %.2f min)", CHECK_INTERVAL)
    if not _startup_done:
        _startup_done = True
        await _check(trigger="startup")


@bot.slash_command(name="check_updates", description="Force an immediate update check.")
async def check_updates(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    await ctx.followup.send(await _check(trigger="manual"), ephemeral=True)


@bot.slash_command(
    name="set_announcement_channel",
    description="Set the announcement channel to the current channel.",
    default_member_permissions=discord.Permissions(manage_guild=True),
)
async def set_announcement_channel(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    if ctx.guild_id is None or ctx.channel_id is None:
        return await ctx.followup.send(
            "Unable to determine the current channel.", ephemeral=True
        )
    async with GUILD_CONFIG_LOCK:
        entry = GUILD_CONFIG.setdefault(
            str(ctx.guild_id), {"announcement_channel_id": None, "source_roles": {}}
        )
        entry["announcement_channel_id"] = int(ctx.channel_id)
        _save_guild_config()
    await ctx.followup.send(
        f"Announcement channel is now <#{ctx.channel_id}>.", ephemeral=True
    )


@bot.slash_command(
    name="reannounce_last_update",
    description="Re-post the latest update link.",
    default_member_permissions=discord.Permissions(manage_guild=True),
)
@discord.option(
    "platform", str, description="Source platform", choices=["royalroad", "patreon"]
)
@discord.option(
    "story",
    str,
    description="Story name",
    autocomplete=lambda ctx: [
        s.name
        for s in (
            ROYALROAD_SOURCES
            if str(ctx.options.get("platform", "")).strip().lower() == "royalroad"
            else (
                PATREON_SOURCES
                if str(ctx.options.get("platform", "")).strip().lower() == "patreon"
                else ALL_SOURCES
            )
        )
        if (not ctx.value or ctx.value.lower() in s.name.lower())
    ][:25],
)
async def reannounce_last_update(
    ctx: discord.ApplicationContext, platform: str, story: str
):
    await ctx.defer(ephemeral=True)
    if ctx.guild_id is None:
        return await ctx.followup.send("Must be used in a server.", ephemeral=True)
    sources = (
        ROYALROAD_SOURCES
        if platform.strip().lower() == "royalroad"
        else PATREON_SOURCES
    )
    src = next((s for s in sources if s.name.lower() == story.strip().lower()), None)
    if not src:
        return await ctx.followup.send(
            f"Story not found. Available: {', '.join(s.name for s in sources)}",
            ephemeral=True,
        )
    latest = await _latest(src)
    if not latest:
        return await ctx.followup.send("No entries found.", ephemeral=True)
    c = await _announce(src, latest, int(ctx.guild_id))
    if not c:
        return await ctx.followup.send(
            "No announcement channel configured. Use /set_announcement_channel.",
            ephemeral=True,
        )
    await ctx.followup.send(
        f"Re-announced latest {src.platform} update for {src.name}.", ephemeral=True
    )


@bot.slash_command(
    name="set_story_roles",
    description="Set role mentions for a story. Comma-separated role IDs.",
    default_member_permissions=discord.Permissions(manage_guild=True),
)
@discord.option(
    "platform", str, description="Source platform", choices=["royalroad", "patreon"]
)
@discord.option(
    "story",
    str,
    description="Story name",
    autocomplete=lambda ctx: [
        s.name
        for s in (
            ROYALROAD_SOURCES
            if str(ctx.options.get("platform", "")).strip().lower() == "royalroad"
            else (
                PATREON_SOURCES
                if str(ctx.options.get("platform", "")).strip().lower() == "patreon"
                else ALL_SOURCES
            )
        )
        if (not ctx.value or ctx.value.lower() in s.name.lower())
    ][:25],
)
@discord.option(
    "role_ids",
    str,
    description="Comma-separated Discord role IDs. Empty to reset.",
    required=False,
    default="",
)
async def set_story_roles(
    ctx: discord.ApplicationContext, platform: str, story: str, role_ids: str
):
    await ctx.defer(ephemeral=True)
    if ctx.guild_id is None:
        return await ctx.followup.send("Must be used in a server.", ephemeral=True)
    sources = (
        ROYALROAD_SOURCES
        if platform.strip().lower() == "royalroad"
        else PATREON_SOURCES
    )
    src = next((s for s in sources if s.name.lower() == story.strip().lower()), None)
    if not src:
        return await ctx.followup.send("Story not found.", ephemeral=True)
    parsed = [int(p) for p in role_ids.split(",") if p.strip().isdigit()]
    if role_ids.strip() and len(parsed) != len(
        [p for p in role_ids.split(",") if p.strip()]
    ):
        return await ctx.followup.send("Invalid role ID in list.", ephemeral=True)
    async with GUILD_CONFIG_LOCK:
        entry = GUILD_CONFIG.setdefault(
            str(ctx.guild_id), {"announcement_channel_id": None, "source_roles": {}}
        )
        if not isinstance(entry["source_roles"], dict):
            entry["source_roles"] = {}
        if role_ids.strip():
            entry["source_roles"][src.key] = parsed
        else:
            entry["source_roles"].pop(src.key, None)
        _save_guild_config()
    await ctx.followup.send(
        f"{'Updated' if role_ids.strip() else 'Reset'} role mentions for {src.name}.",
        ephemeral=True,
    )


@bot.slash_command(
    name="debug_latest_update",
    description="Show the latest detected update for a source.",
)
@discord.option(
    "platform", str, description="Source platform", choices=["royalroad", "patreon"]
)
@discord.option(
    "story",
    str,
    description="Story name",
    autocomplete=lambda ctx: [
        s.name
        for s in (
            ROYALROAD_SOURCES
            if str(ctx.options.get("platform", "")).strip().lower() == "royalroad"
            else (
                PATREON_SOURCES
                if str(ctx.options.get("platform", "")).strip().lower() == "patreon"
                else ALL_SOURCES
            )
        )
        if (not ctx.value or ctx.value.lower() in s.name.lower())
    ][:25],
)
async def debug_latest_update(
    ctx: discord.ApplicationContext, platform: str, story: str
):
    await ctx.defer(ephemeral=True)
    sources = (
        ROYALROAD_SOURCES
        if platform.strip().lower() == "royalroad"
        else PATREON_SOURCES
    )
    src = next((s for s in sources if s.name.lower() == story.strip().lower()), None)
    if not src:
        return await ctx.followup.send(
            f"Story not found. Available: {', '.join(s.name for s in sources) or 'none'}",
            ephemeral=True,
        )
    latest = await _latest(src)
    if not latest:
        return await ctx.followup.send(
            f"No updates found for {src.platform} / {src.name}.\nSource URL: {src.url}",
            ephemeral=True,
        )
    await ctx.followup.send(
        f"Latest for {src.platform} / {src.name}:\nTitle: {latest['title']}\nLink: {latest['link']}\nID: {latest['id']}",
        ephemeral=True,
    )


def _save_guild_config():
    GUILD_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GUILD_CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"guilds": GUILD_CONFIG}, f, indent=2, sort_keys=True)
    tmp.replace(GUILD_CONFIG_PATH)


if __name__ == "__main__":
    bot.run(TOKEN)
