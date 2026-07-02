import asyncio
import json
import logging
import os
from dataclasses import dataclass
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import discord
import feedparser
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
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
        digest = sha256(self.url.encode("utf-8")).hexdigest()[:16]
        return f"{self.platform}:{self.name}:{digest}"


class _PatreonLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self._current_meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}

        if tag == "a":
            href = attr_map.get("href", "").strip()
            if "/posts/" in href:
                self._current_href = href
                self._current_text = []
        elif tag == "meta":
            property_name = attr_map.get("property", "") or attr_map.get("name", "")
            content = attr_map.get("content", "")
            if property_name and content:
                self._current_meta[property_name.lower()] = content.strip()

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            text = data.strip()
            if text:
                self._current_text.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href is not None:
            title = unescape(" ".join(self._current_text).strip())
            href = self._current_href.strip()
            if href:
                self.links.append({"href": href, "title": title})
            self._current_href = None
            self._current_text = []

    @property
    def page_title(self) -> str:
        return (
            self._current_meta.get("og:title")
            or self._current_meta.get("twitter:title")
            or self._current_meta.get("title")
            or "New update"
        )


def _normalize_url(url: str) -> str:
    return url.strip()


def _patreon_sitemap_url(url: str) -> str:
    normalized = _normalize_url(url)
    parsed = urlsplit(normalized)

    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts[-1] == "sitemap":
        return normalized.rstrip("/")

    creator_slug = ""
    if len(path_parts) >= 2 and path_parts[0] == "c":
        creator_slug = path_parts[1]
    elif path_parts:
        creator_slug = path_parts[0]

    if creator_slug:
        sitemap_path = f"/{creator_slug}/sitemap"
    else:
        sitemap_path = "/sitemap"

    return urlunsplit((parsed.scheme, parsed.netloc, sitemap_path, "", ""))


def _is_patreon_platform(source: FeedSource) -> bool:
    return source.platform.strip().lower() == "patreon"


def _patreon_request(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    with urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read().decode(charset, errors="replace")
    return body


def _patreon_latest_from_html(html: str) -> dict[str, str] | None:
    parser = _PatreonLinkParser()
    parser.feed(html)

    if not parser.links:
        return None

    first_link = parser.links[0]
    href = first_link.get("href", "").strip()
    if not href:
        return None

    if href.startswith("/"):
        href = f"https://www.patreon.com{href}"

    title = first_link.get("title", "").strip() or parser.page_title or "New update"
    return {
        "id": href,
        "link": href,
        "title": title,
    }


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    if not value.isdigit():
        logger.warning(
            "%s is not a valid integer ID (%r). It will be treated as unset.",
            name,
            value,
        )
        return None
    return int(value)


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_sources(env_var: str, platform: str) -> list[FeedSource]:
    raw = os.getenv(env_var, "[]")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{env_var} must be valid JSON") from exc

    if not isinstance(payload, list):
        raise RuntimeError(f"{env_var} must be a JSON array")

    sources: list[FeedSource] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            raise RuntimeError(f"{env_var}[{idx}] must be an object")

        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        role_ids_raw = item.get("roleIds", [])

        if not name:
            raise RuntimeError(f"{env_var}[{idx}].name is required")
        if not url:
            raise RuntimeError(f"{env_var}[{idx}].url is required")
        if not isinstance(role_ids_raw, list):
            raise RuntimeError(f"{env_var}[{idx}].roleIds must be an array")

        role_ids: list[int] = []
        for role_idx, role in enumerate(role_ids_raw):
            role_text = str(role).strip()
            if not role_text.isdigit():
                raise RuntimeError(
                    f"{env_var}[{idx}].roleIds[{role_idx}] must be a Discord role ID"
                )
            role_ids.append(int(role_text))

        sources.append(
            FeedSource(
                platform=platform,
                name=name,
                url=url,
                role_ids=role_ids,
            )
        )

    return sources


def _load_state(state_path: Path) -> dict[str, str]:
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("State file is invalid; starting with an empty state")
        return {}

    if not isinstance(data, dict):
        return {}

    result: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            result[key] = value
    return result


def _save_state(state_path: Path, state: dict[str, str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp_path.replace(state_path)


def _normalize_guild_config(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}

    guilds_raw = raw.get("guilds") if "guilds" in raw else raw
    if not isinstance(guilds_raw, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for guild_id, payload in guilds_raw.items():
        guild_key = str(guild_id).strip()
        if not guild_key.isdigit() or not isinstance(payload, dict):
            continue

        channel_raw = payload.get("announcement_channel_id")
        if isinstance(channel_raw, int):
            channel_id = channel_raw
        elif isinstance(channel_raw, str) and channel_raw.isdigit():
            channel_id = int(channel_raw)
        else:
            channel_id = None

        roles_raw = payload.get("source_roles", {})
        source_roles: dict[str, list[int]] = {}
        if isinstance(roles_raw, dict):
            for source_key, role_list in roles_raw.items():
                if not isinstance(source_key, str) or not isinstance(role_list, list):
                    continue
                parsed_roles: list[int] = []
                for role in role_list:
                    role_text = str(role).strip()
                    if role_text.isdigit():
                        parsed_roles.append(int(role_text))
                source_roles[source_key] = parsed_roles

        normalized[guild_key] = {
            "announcement_channel_id": channel_id,
            "source_roles": source_roles,
        }

    return normalized


def _load_guild_config(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Guild config file is invalid; starting with empty config")
        return {}

    return _normalize_guild_config(raw)


def _save_guild_config(path: Path, guild_config: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    payload = {"guilds": guild_config}
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    temp_path.replace(path)


def _get_or_create_guild_entry(guild_id: int) -> dict[str, Any]:
    key = str(guild_id)
    entry = GUILD_CONFIG.get(key)
    if entry is None:
        entry = {
            "announcement_channel_id": None,
            "source_roles": {},
        }
        GUILD_CONFIG[key] = entry
    return entry


def _guild_channel_id(guild_id: int) -> int | None:
    entry = GUILD_CONFIG.get(str(guild_id))
    if not entry:
        return None
    channel_id = entry.get("announcement_channel_id")
    if isinstance(channel_id, int):
        return channel_id
    return None


def _guild_role_ids_for_source(guild_id: int, source: FeedSource) -> list[int]:
    entry = GUILD_CONFIG.get(str(guild_id), {})
    source_roles = entry.get("source_roles", {})
    if isinstance(source_roles, dict):
        role_ids = source_roles.get(source.key)
        if isinstance(role_ids, list):
            parsed: list[int] = []
            for role_id in role_ids:
                text = str(role_id).strip()
                if text.isdigit():
                    parsed.append(int(text))
            return parsed

    return source.role_ids


async def _fetch_latest_entry(source: FeedSource) -> dict[str, str] | None:
    if _is_patreon_platform(source):
        try:
            html = await asyncio.to_thread(_patreon_request, _patreon_sitemap_url(source.url))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning("Failed to fetch Patreon page for %s: %s", source.name, exc)
            return None

        latest = await asyncio.to_thread(_patreon_latest_from_html, html)
        if latest is None:
            logger.warning("No Patreon posts found on sitemap for %s", source.name)
        return latest

    parsed = await asyncio.to_thread(feedparser.parse, source.url)

    if not parsed.entries:
        return None

    entry: Any = parsed.entries[0]
    entry_id = str(
        entry.get("id")
        or entry.get("guid")
        or entry.get("link")
        or f"{entry.get('title', '')}:{entry.get('published', '')}"
    ).strip()
    link = str(entry.get("link") or "").strip()
    title = str(entry.get("title") or "New update").strip()

    if not entry_id:
        return None

    return {
        "id": entry_id,
        "link": link,
        "title": title,
    }


def _build_role_mentions(source: FeedSource) -> str:
    if not source.role_ids:
        return ""
    return " ".join(f"<@&{role_id}>" for role_id in source.role_ids)


def _normalize_platform(platform: str) -> str:
    return platform.strip().lower()


def _sources_for_platform(platform: str) -> list[FeedSource]:
    normalized = _normalize_platform(platform)
    if normalized == "royalroad":
        return ROYALROAD_SOURCES
    if normalized == "patreon":
        return PATREON_SOURCES
    return []


def _find_source(platform: str, story: str) -> FeedSource | None:
    story_key = story.strip().lower()
    return next(
        (source for source in _sources_for_platform(platform) if source.name.lower() == story_key),
        None,
    )


async def _story_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    platform = str(ctx.options.get("platform") or "").strip().lower()
    partial = str(ctx.value or "").strip().lower()

    if platform in {"royalroad", "patreon"}:
        names = [source.name for source in _sources_for_platform(platform)]
    else:
        names = [source.name for source in ALL_SOURCES_FOR_COMMANDS]

    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(name)

    if partial:
        deduped = [name for name in deduped if partial in name.lower()]

    return deduped[:25]


TOKEN = _required_env("DISCORD_TOKEN")
DEFAULT_ANNOUNCEMENT_CHANNEL_ID = _optional_int_env("ANNOUNCEMENT_CHANNEL_ID")
CHECK_INTERVAL_MINUTES = float(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
ANNOUNCE_ON_FIRST_RUN = _parse_bool_env("ANNOUNCE_ON_FIRST_RUN", default=False)

if CHECK_INTERVAL_MINUTES <= 0:
    raise RuntimeError("CHECK_INTERVAL_MINUTES must be greater than 0")

ROYALROAD_SOURCES = _parse_sources("ROYALROAD_FEEDS_JSON", "RoyalRoad")
PATREON_SOURCES = _parse_sources("PATREON_FEEDS_JSON", "Patreon")
# Scheduled checks remain RoyalRoad-only for now.
ALL_SOURCES = [*ROYALROAD_SOURCES]
ALL_SOURCES_FOR_COMMANDS = [*ROYALROAD_SOURCES, *PATREON_SOURCES]

if not ALL_SOURCES:
    logger.warning("No feed sources configured. The bot will run but find no updates.")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_data_dir() -> Path:
    raw = os.getenv("MERIS_DATA_DIR", "").strip()
    if not raw:
        return PROJECT_ROOT / "data"

    configured = Path(raw).expanduser()
    if configured.is_absolute():
        return configured
    return (PROJECT_ROOT / configured).resolve()


DATA_DIR = _resolve_data_dir()
STATE_PATH = DATA_DIR / "state.json"
STATE = _load_state(STATE_PATH)
GUILD_CONFIG_PATH = DATA_DIR / "guild_config.json"
GUILD_CONFIG = _load_guild_config(GUILD_CONFIG_PATH)

CHECK_LOCK = asyncio.Lock()
GUILD_CONFIG_LOCK = asyncio.Lock()
STARTUP_CHECK_DONE = False

# Pycord currently expects a default loop to exist during client construction.
asyncio.set_event_loop(asyncio.new_event_loop())
bot = discord.Bot(intents=discord.Intents.default())


async def _get_announcement_channel_for_guild(
    guild_id: int,
) -> discord.abc.Messageable | None:
    configured_id = _guild_channel_id(guild_id)
    channel_id = configured_id or DEFAULT_ANNOUNCEMENT_CHANNEL_ID
    if channel_id is None:
        return None

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            logger.warning(
                "Guild %s has invalid or inaccessible announcement channel %s",
                guild_id,
                channel_id,
            )
            return None
    if not isinstance(channel, discord.abc.Messageable):
        logger.warning("Channel %s is not messageable", channel_id)
        return None
    return channel


async def _announce_to_guild(
    guild_id: int,
    source: FeedSource,
    entry: dict[str, str],
) -> bool:
    channel = await _get_announcement_channel_for_guild(guild_id)
    if channel is None:
        return False

    role_ids = _guild_role_ids_for_source(guild_id, source)
    role_mentions = " ".join(f"<@&{role_id}>" for role_id in role_ids)
    lines = []
    if role_mentions:
        lines.append(role_mentions)
    lines.append(f"**New {source.platform} update: {source.name}**")
    if entry.get("title"):
        lines.append(entry["title"])
    if entry.get("link"):
        lines.append(entry["link"])

    await channel.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions(roles=True),
    )
    return True


async def _announce(source: FeedSource, entry: dict[str, str]) -> int:
    announced = 0
    for guild in bot.guilds:
        try:
            sent = await _announce_to_guild(guild.id, source, entry)
        except Exception as exc:
            logger.exception(
                "Failed to announce to guild %s for source %s: %s",
                guild.id,
                source.name,
                exc,
            )
            continue
        if sent:
            announced += 1

    return announced


async def _check_one_source(source: FeedSource, announce_on_first_seen: bool) -> str:
    latest = await _fetch_latest_entry(source)
    if latest is None:
        logger.info("No entries for source %s", source.name)
        return "no_entries"

    previous_id = STATE.get(source.key)

    if previous_id is None:
        STATE[source.key] = latest["id"]
        _save_state(STATE_PATH, STATE)
        if announce_on_first_seen:
            sent_count = await _announce(source, latest)
            if sent_count == 0:
                logger.info("No guild announcement channel configured for %s", source.name)
                return "no_targets"
            logger.info("First run announcement sent for %s in %d guild(s)", source.name, sent_count)
            return "announced"
        logger.info("Primed initial state for %s", source.name)
        return "primed"

    if previous_id == latest["id"]:
        return "unchanged"

    sent_count = await _announce(source, latest)
    if sent_count == 0:
        logger.info("No guild announcement channel configured for %s", source.name)
        STATE[source.key] = latest["id"]
        _save_state(STATE_PATH, STATE)
        return "no_targets"
    STATE[source.key] = latest["id"]
    _save_state(STATE_PATH, STATE)
    logger.info("Announced update for %s in %d guild(s)", source.name, sent_count)
    return "announced"


async def run_update_check(*, trigger: str) -> str:
    if CHECK_LOCK.locked():
        return "A check is already running."

    async with CHECK_LOCK:
        announced = 0
        unchanged = 0
        primed = 0
        no_entries = 0
        no_targets = 0
        errors = 0

        announce_first = ANNOUNCE_ON_FIRST_RUN and trigger == "startup"

        for source in ALL_SOURCES:
            try:
                outcome = await _check_one_source(source, announce_first)
            except Exception as exc:
                errors += 1
                logger.exception("Failed checking source %s: %s", source.name, exc)
                continue

            if outcome == "announced":
                announced += 1
            elif outcome == "unchanged":
                unchanged += 1
            elif outcome == "primed":
                primed += 1
            elif outcome == "no_entries":
                no_entries += 1
            elif outcome == "no_targets":
                no_targets += 1

        summary = (
            f"Check complete. announced={announced}, unchanged={unchanged}, "
            f"primed={primed}, no_entries={no_entries}, no_targets={no_targets}, errors={errors}"
        )
        logger.info(summary)
        return summary


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def scheduled_update_check() -> None:
    await run_update_check(trigger="scheduled")


@scheduled_update_check.before_loop
async def before_scheduled_update_check() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    global STARTUP_CHECK_DONE

    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "n/a")

    if DEFAULT_ANNOUNCEMENT_CHANNEL_ID is None and not GUILD_CONFIG:
        logger.warning(
            "No announcement channels configured. Use /set_announcement_channel in each server."
        )

    if not scheduled_update_check.is_running():
        scheduled_update_check.start()
        logger.info(
            "Scheduled checks started (every %.2f minute(s))", CHECK_INTERVAL_MINUTES
        )

    if not STARTUP_CHECK_DONE:
        STARTUP_CHECK_DONE = True
        await run_update_check(trigger="startup")


@bot.slash_command(
    name="check_updates",
    description="Force an immediate RoyalRoad update check.",
)
async def check_updates(ctx: discord.ApplicationContext) -> None:
    await ctx.defer(ephemeral=True)
    summary = await run_update_check(trigger="manual")
    await ctx.followup.send(summary, ephemeral=True)


@bot.slash_command(
    name="set_announcement_channel",
    description="Set the announcement channel to the channel where this command is used.",
    default_member_permissions=discord.Permissions(manage_guild=True),
)
async def set_announcement_channel(ctx: discord.ApplicationContext) -> None:
    await ctx.defer(ephemeral=True)

    if ctx.guild_id is None or ctx.channel_id is None:
        await ctx.followup.send("Unable to determine the current channel.", ephemeral=True)
        return

    guild_id = int(ctx.guild_id)
    channel_id = int(ctx.channel_id)

    async with GUILD_CONFIG_LOCK:
        entry = _get_or_create_guild_entry(guild_id)
        entry["announcement_channel_id"] = channel_id
        try:
            _save_guild_config(GUILD_CONFIG_PATH, GUILD_CONFIG)
        except OSError as exc:
            logger.exception("Failed to save guild config: %s", exc)
            await ctx.followup.send(
                "Failed to save server config. Check file permissions and try again.",
                ephemeral=True,
            )
            return

    await ctx.followup.send(
        f"Announcement channel for this server is now <#{channel_id}>.",
        ephemeral=True,
    )


@bot.slash_command(
    name="reannounce_last_update",
    description="Re-post the latest update link for a selected source.",
    default_member_permissions=discord.Permissions(manage_guild=True),
)
@discord.option(
    "platform",
    str,
    description="Source platform",
    choices=["royalroad", "patreon"],
)
@discord.option(
    "story",
    str,
    description="Story/source name",
    autocomplete=_story_autocomplete,
)
async def reannounce_last_update(
    ctx: discord.ApplicationContext,
    platform: str,
    story: str,
) -> None:
    await ctx.defer(ephemeral=True)

    if ctx.guild_id is None:
        await ctx.followup.send(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    sources = _sources_for_platform(platform)
    if not sources:
        await ctx.followup.send(
            f"No {platform} sources are configured.",
            ephemeral=True,
        )
        return

    selected_source = _find_source(platform, story)
    if selected_source is None:
        available = ", ".join(source.name for source in sources)
        await ctx.followup.send(
            f"Story not found for {platform}. Available: {available}",
            ephemeral=True,
        )
        return

    try:
        latest = await _fetch_latest_entry(selected_source)
    except Exception as exc:
        logger.exception(
            "Failed to fetch latest entry for %s: %s", selected_source.name, exc
        )
        await ctx.followup.send(
            "Failed to fetch the latest entry for that source.",
            ephemeral=True,
        )
        return

    if latest is None:
        await ctx.followup.send(
            "No entries found for that source.",
            ephemeral=True,
        )
        return

    try:
        sent = await _announce_to_guild(int(ctx.guild_id), selected_source, latest)
    except Exception as exc:
        logger.exception(
            "Failed to re-announce latest entry for %s: %s", selected_source.name, exc
        )
        await ctx.followup.send(
            "Failed to post in the announcement channel. Check channel configuration and bot permissions.",
            ephemeral=True,
        )
        return

    if not sent:
        await ctx.followup.send(
            "No announcement channel configured for this server. Run /set_announcement_channel in your target channel.",
            ephemeral=True,
        )
        return

    await ctx.followup.send(
        f"Re-announced latest {selected_source.platform} update for {selected_source.name}.",
        ephemeral=True,
    )


@bot.slash_command(
    name="set_story_roles",
    description="Set role mentions for a story in this server (comma-separated role IDs).",
    default_member_permissions=discord.Permissions(manage_guild=True),
)
@discord.option(
    "platform",
    str,
    description="Source platform",
    choices=["royalroad", "patreon"],
)
@discord.option(
    "story",
    str,
    description="Story/source name",
    autocomplete=_story_autocomplete,
)
@discord.option(
    "role_ids",
    str,
    description="Comma-separated Discord role IDs. Leave empty to reset to feed defaults.",
    required=False,
    default="",
)
async def set_story_roles(
    ctx: discord.ApplicationContext,
    platform: str,
    story: str,
    role_ids: str,
) -> None:
    await ctx.defer(ephemeral=True)

    if ctx.guild_id is None:
        await ctx.followup.send(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    selected_source = _find_source(platform, story)
    if selected_source is None:
        await ctx.followup.send(
            "Story not found for that platform.",
            ephemeral=True,
        )
        return

    parsed_roles: list[int] = []
    role_text = role_ids.strip()
    if role_text:
        parts = [part.strip() for part in role_text.split(",") if part.strip()]
        for part in parts:
            if not part.isdigit():
                await ctx.followup.send(
                    f"Invalid role ID: {part}",
                    ephemeral=True,
                )
                return
            parsed_roles.append(int(part))

    guild_id = int(ctx.guild_id)
    async with GUILD_CONFIG_LOCK:
        entry = _get_or_create_guild_entry(guild_id)
        source_roles = entry.get("source_roles")
        if not isinstance(source_roles, dict):
            source_roles = {}
            entry["source_roles"] = source_roles

        if role_text:
            source_roles[selected_source.key] = parsed_roles
        else:
            source_roles.pop(selected_source.key, None)

        try:
            _save_guild_config(GUILD_CONFIG_PATH, GUILD_CONFIG)
        except OSError as exc:
            logger.exception("Failed saving guild role overrides: %s", exc)
            await ctx.followup.send(
                "Failed to save server config. Check file permissions and try again.",
                ephemeral=True,
            )
            return

    if role_text:
        await ctx.followup.send(
            f"Updated role mentions for {selected_source.name} in this server.",
            ephemeral=True,
        )
    else:
        await ctx.followup.send(
            f"Reset role mentions for {selected_source.name} to feed defaults.",
            ephemeral=True,
        )


@bot.slash_command(
    name="debug_latest_update",
    description="Show what the bot currently sees as the latest update for a source.",
)
@discord.option(
    "platform",
    str,
    description="Source platform",
    choices=["royalroad", "patreon"],
)
@discord.option(
    "story",
    str,
    description="Story/source name",
    autocomplete=_story_autocomplete,
)
async def debug_latest_update(
    ctx: discord.ApplicationContext,
    platform: str,
    story: str,
) -> None:
    await ctx.defer(ephemeral=True)

    selected_source = _find_source(platform, story)
    if selected_source is None:
        available = ", ".join(source.name for source in _sources_for_platform(platform))
        await ctx.followup.send(
            f"Story not found for {platform}. Available: {available or 'none'}",
            ephemeral=True,
        )
        return

    try:
        latest = await _fetch_latest_entry(selected_source)
    except Exception as exc:
        logger.exception(
            "Failed to debug latest entry for %s: %s", selected_source.name, exc
        )
        await ctx.followup.send(
            f"Failed to check {selected_source.platform} / {selected_source.name}: {exc}",
            ephemeral=True,
        )
        return

    if latest is None:
        checked_url = (
            _patreon_sitemap_url(selected_source.url)
            if _is_patreon_platform(selected_source)
            else selected_source.url
        )
        await ctx.followup.send(
            (
                f"No updates found for {selected_source.platform} / {selected_source.name}.\n"
                f"Checked: {checked_url}"
            ),
            ephemeral=True,
        )
        return

    await ctx.followup.send(
        (
            f"Latest detected update for {selected_source.platform} / {selected_source.name}:\n"
            f"Title: {latest['title']}\n"
            f"Link: {latest['link']}\n"
            f"ID: {latest['id']}"
        ),
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(TOKEN)
