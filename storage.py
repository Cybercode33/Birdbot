"""Small shared SQLite store for BirdBot's web dashboard and bot process."""

from __future__ import annotations

import sqlite3
import uuid
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from settings import COMMAND_PREFIX, DATA_PATH


# Tickets that remain unclaimed for this period are automatically archived by
# the single connected Discord bot.  Keep the value in the shared store so the
# dashboard and bot use the same policy.
UNCLAIMED_TICKET_TIMEOUT_SECONDS = 300

# Commands exposed by the website's Commands tab. Keeping this list in the
# shared store makes the API and Discord worker agree on valid settings.
DASHBOARD_COMMAND_NAMES = ("ping", "server", "profile", "kick", "ban")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BirdBotStore:
    """Use short-lived SQLite connections so the bot and web tasks can share safely."""

    def __init__(self, path: Path = DATA_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_cache: dict[str, tuple[float, object]] = {}
        self._initialize()

    _CACHE_MISS = object()

    def _cache_get(self, key: str) -> object:
        item = self._memory_cache.get(key)
        if not item:
            return self._CACHE_MISS
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._memory_cache.pop(key, None)
            return self._CACHE_MISS
        return value

    def _cache_set(self, key: str, value: object, ttl: float = 20.0) -> object:
        self._memory_cache[key] = (time.monotonic() + ttl, value)
        return value

    def _invalidate_cache(self) -> None:
        self._memory_cache.clear()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_guilds (
                    guild_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    member_count INTEGER NOT NULL DEFAULT 0,
                    icon_url TEXT,
                    banner_url TEXT,
                    owner_id TEXT,
                    owner_name TEXT,
                    created_at TEXT,
                    boost_level INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_profiles (
                    guild_id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL DEFAULT '',
                    avatar_path TEXT,
                    avatar_url TEXT,
                    updated_by TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_command_settings (
                    guild_id TEXT PRIMARY KEY,
                    prefix TEXT NOT NULL DEFAULT '!',
                    prefix_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_by TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS command_configs (
                    guild_id TEXT NOT NULL,
                    command_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    language TEXT NOT NULL DEFAULT 'en',
                    shortcuts TEXT NOT NULL DEFAULT '[]',
                    updated_by TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, command_name)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_activations (
                    guild_id TEXT PRIMARY KEY,
                    activated INTEGER NOT NULL,
                    activated_by TEXT NOT NULL,
                    activated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    display_name TEXT,
                    avatar_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS spotify_accounts (
                    user_id TEXT PRIMARY KEY,
                    spotify_user_id TEXT NOT NULL,
                    display_name TEXT,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    expires_at REAL NOT NULL DEFAULT 0,
                    scope TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS music_sessions (
                    guild_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_channels (
                    channel_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_categories (
                    category_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_roles (
                    role_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    managed INTEGER NOT NULL DEFAULT 0,
                    color TEXT NOT NULL DEFAULT '#000000',
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS command_requests (
                    request_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    command_name TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_members (
                    member_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    username TEXT,
                    global_name TEXT,
                    avatar_url TEXT,
                    joined_at TEXT,
                    roles TEXT NOT NULL DEFAULT '[]',
                    role_ids TEXT NOT NULL DEFAULT '[]',
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (member_id, guild_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_bans (
                    user_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    reason TEXT,
                    banned_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, guild_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_configs (
                    guild_id TEXT PRIMARY KEY,
                    setup_channel_id TEXT NOT NULL,
                    category_id TEXT,
                    options TEXT NOT NULL DEFAULT '[]',
                    support_role_ids TEXT NOT NULL DEFAULT '[]',
                    panel_layout TEXT NOT NULL DEFAULT 'select_menu',
                    priority TEXT NOT NULL DEFAULT 'medium',
                    max_open_tickets INTEGER NOT NULL DEFAULT 1,
                    require_description INTEGER NOT NULL DEFAULT 0,
                    description_prompt TEXT NOT NULL DEFAULT 'Please describe your request.',
                    custom_icon_url TEXT,
                    custom_icon_path TEXT,
                    log_channel_id TEXT,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL UNIQUE,
                    channel_name TEXT NOT NULL DEFAULT '',
                    creator_id TEXT NOT NULL,
                    creator_name TEXT NOT NULL,
                    option_label TEXT NOT NULL,
                    category_id TEXT,
                    category_name TEXT,
                    priority TEXT NOT NULL DEFAULT 'medium',
                    status TEXT NOT NULL DEFAULT 'open',
                    claimed_by TEXT,
                    claimed_by_name TEXT,
                    claimed_at TEXT,
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    closed_by TEXT,
                    closed_by_name TEXT,
                    transcript_url TEXT,
                    unclaimed_until TEXT,
                    timeout_started_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    ticket_id TEXT,
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    actor_name TEXT,
                    creator_id TEXT,
                    creator_name TEXT,
                    channel_id TEXT,
                    channel_name TEXT,
                    priority TEXT,
                    duration_seconds INTEGER,
                    transcript_url TEXT,
                    dm_status TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS spy_game_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    match_at TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    spy_id TEXT NOT NULL,
                    spy_name TEXT NOT NULL,
                    citizens TEXT NOT NULL DEFAULT '[]',
                    outcome TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT 'en'
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS spy_game_configs (
                    guild_id TEXT PRIMARY KEY,
                    min_players INTEGER NOT NULL DEFAULT 3,
                    max_players INTEGER NOT NULL DEFAULT 20,
                    question_timer_seconds INTEGER NOT NULL DEFAULT 30,
                    end_mode TEXT NOT NULL DEFAULT 'manual',
                    auto_end_rounds INTEGER NOT NULL DEFAULT 20,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    language TEXT NOT NULL DEFAULT 'en',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS roulette_game_configs (
                    guild_id TEXT PRIMARY KEY,
                    min_players INTEGER NOT NULL DEFAULT 2,
                    max_players INTEGER NOT NULL DEFAULT 20,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    language TEXT NOT NULL DEFAULT 'en',
                    wheel_mode TEXT NOT NULL DEFAULT 'multi',
                    wheel_color TEXT NOT NULL DEFAULT '#6B7280',
                    wheel_colors TEXT NOT NULL DEFAULT '[]',
                    turn_timer_seconds INTEGER NOT NULL DEFAULT 30,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tickets_guild_status ON tickets (guild_id, status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tickets_guild_created ON tickets (guild_id, created_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tickets_guild_creator_status ON tickets (guild_id, creator_id, status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_ticket_logs_guild_created ON ticket_logs (guild_id, created_at DESC, log_id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_ticket_logs_guild_ticket ON ticket_logs (guild_id, ticket_id, created_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_spy_game_logs_guild_match ON spy_game_logs (guild_id, match_at DESC, log_id DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_spy_game_configs_updated ON spy_game_configs (updated_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_roulette_game_configs_updated ON roulette_game_configs (updated_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_bot_members_guild_status_name ON bot_members (guild_id, is_bot, display_name COLLATE NOCASE)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_bot_members_guild_member_id ON bot_members (guild_id, member_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_command_requests_pending ON command_requests (status, created_at)")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(oauth_sessions)")}
            for name, definition in (
                ("refresh_token", "TEXT"),
                ("display_name", "TEXT"),
                ("avatar_url", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE oauth_sessions ADD COLUMN {name} {definition}")
            guild_columns = {row["name"] for row in connection.execute("PRAGMA table_info(bot_guilds)")}
            for name, definition in (
                ("icon_url", "TEXT"),
                ("banner_url", "TEXT"),
                ("owner_id", "TEXT"),
                ("owner_name", "TEXT"),
                ("created_at", "TEXT"),
                ("boost_level", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in guild_columns:
                    connection.execute(f"ALTER TABLE bot_guilds ADD COLUMN {name} {definition}")
            role_columns = {row["name"] for row in connection.execute("PRAGMA table_info(bot_roles)")}
            if "color" not in role_columns:
                connection.execute("ALTER TABLE bot_roles ADD COLUMN color TEXT NOT NULL DEFAULT '#000000'")
            command_columns = {row["name"] for row in connection.execute("PRAGMA table_info(command_requests)")}
            if "payload" not in command_columns:
                connection.execute("ALTER TABLE command_requests ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
            member_columns = {row["name"] for row in connection.execute("PRAGMA table_info(bot_members)")}
            for name, definition in (("username", "TEXT"), ("global_name", "TEXT"), ("role_ids", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in member_columns:
                    connection.execute(f"ALTER TABLE bot_members ADD COLUMN {name} {definition}")
            ticket_columns = {row["name"] for row in connection.execute("PRAGMA table_info(ticket_configs)")}
            for name, definition in (
                ("support_role_ids", "TEXT NOT NULL DEFAULT '[]'"),
                ("panel_layout", "TEXT NOT NULL DEFAULT 'select_menu'"),
                ("priority", "TEXT NOT NULL DEFAULT 'medium'"),
                ("max_open_tickets", "INTEGER NOT NULL DEFAULT 1"),
                ("require_description", "INTEGER NOT NULL DEFAULT 0"),
                ("description_prompt", "TEXT NOT NULL DEFAULT 'Please describe your request.'"),
                ("custom_icon_url", "TEXT"),
                ("custom_icon_path", "TEXT"),
                ("log_channel_id", "TEXT"),
            ):
                if name not in ticket_columns:
                    connection.execute(f"ALTER TABLE ticket_configs ADD COLUMN {name} {definition}")
            spy_config_columns = {row["name"] for row in connection.execute("PRAGMA table_info(spy_game_configs)")}
            for name, definition in (
                ("enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("language", "TEXT NOT NULL DEFAULT 'en'"),
                ("end_mode", "TEXT NOT NULL DEFAULT 'manual'"),
                ("auto_end_rounds", "INTEGER NOT NULL DEFAULT 20"),
            ):
                if name not in spy_config_columns:
                    connection.execute(f"ALTER TABLE spy_game_configs ADD COLUMN {name} {definition}")
            roulette_config_columns = {row["name"] for row in connection.execute("PRAGMA table_info(roulette_game_configs)")}
            for name, definition in (
                ("enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("language", "TEXT NOT NULL DEFAULT 'en'"),
                ("wheel_mode", "TEXT NOT NULL DEFAULT 'multi'"),
                ("wheel_color", "TEXT NOT NULL DEFAULT '#6B7280'"),
                ("wheel_colors", "TEXT NOT NULL DEFAULT '[]'"),
                ("turn_timer_seconds", "INTEGER NOT NULL DEFAULT 30"),
            ):
                if name not in roulette_config_columns:
                    connection.execute(f"ALTER TABLE roulette_game_configs ADD COLUMN {name} {definition}")
            ticket_record_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tickets)")}
            if "channel_name" not in ticket_record_columns:
                connection.execute("ALTER TABLE tickets ADD COLUMN channel_name TEXT NOT NULL DEFAULT ''")
            for name, definition in (
                ("claimed_at", "TEXT"),
                ("closed_by", "TEXT"),
                ("closed_by_name", "TEXT"),
                ("transcript_url", "TEXT"),
                ("unclaimed_until", "TEXT"),
                ("timeout_started_at", "TEXT"),
            ):
                if name not in ticket_record_columns:
                    connection.execute(f"ALTER TABLE tickets ADD COLUMN {name} {definition}")
            # Tickets created before timeout support need a deadline too.  Give
            # any still-open, unclaimed records one five-minute window from
            # this migration; claimed/closed records remain untouched.
            migration_deadline = (
                datetime.now(timezone.utc) + timedelta(seconds=UNCLAIMED_TICKET_TIMEOUT_SECONDS)
            ).isoformat()
            connection.execute(
                "UPDATE tickets SET unclaimed_until = ? "
                "WHERE status = 'open' AND claimed_by IS NULL AND unclaimed_until IS NULL",
                (migration_deadline,),
            )
            ticket_log_columns = {row["name"] for row in connection.execute("PRAGMA table_info(ticket_logs)")}
            if "dm_status" not in ticket_log_columns:
                connection.execute("ALTER TABLE ticket_logs ADD COLUMN dm_status TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tickets_unclaimed_deadline ON tickets (status, claimed_by, unclaimed_until)")

    def sync_bot_guilds(self, guilds: Iterable[object]) -> None:
        """Record the guilds seen by the one globally connected Discord client."""
        records = [
            (
                str(guild.id),
                guild.name,
                guild.member_count or 0,
                str(guild.icon.url) if getattr(guild, "icon", None) else None,
                str(guild.banner.url) if getattr(guild, "banner", None) else None,
                str(guild.owner_id) if guild.owner_id else None,
                guild.owner.display_name if guild.owner else "Unknown owner",
                guild.created_at.isoformat(),
                int(guild.premium_tier),
                utc_now(),
            )
            for guild in guilds
        ]
        now = utc_now()
        channel_records = [
            (str(channel.id), str(guild.id), channel.name, now)
            for guild in guilds
            for channel in getattr(guild, "text_channels", [])
            if getattr(guild, "me", None)
            and channel.permissions_for(guild.me).view_channel
            and channel.permissions_for(guild.me).send_messages
        ]
        category_records = [
            (str(category.id), str(guild.id), category.name, now)
            for guild in guilds
            for category in getattr(guild, "categories", [])
        ]
        role_records = [
            (
                str(role.id),
                str(guild.id),
                role.name,
                int(role.position),
                int(role.managed),
                f"#{int(getattr(getattr(role, 'colour', None), 'value', 0)):06X}",
                now,
            )
            for guild in guilds
            for role in getattr(guild, "roles", [])
            if not getattr(role, "is_default", lambda: False)()
        ]
        with self._connect() as connection:
            connection.execute("DELETE FROM bot_guilds")
            connection.execute("DELETE FROM bot_channels")
            connection.execute("DELETE FROM bot_categories")
            connection.execute("DELETE FROM bot_roles")
            connection.executemany(
                "INSERT INTO bot_guilds (guild_id, name, member_count, icon_url, banner_url, owner_id, owner_name, created_at, boost_level, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                records,
            )
            connection.executemany(
                "INSERT INTO bot_channels (channel_id, guild_id, name, last_seen_at) VALUES (?, ?, ?, ?)",
                channel_records,
            )
            connection.executemany(
                "INSERT INTO bot_categories (category_id, guild_id, name, last_seen_at) VALUES (?, ?, ?, ?)",
                category_records,
            )
            connection.executemany(
                "INSERT INTO bot_roles (role_id, guild_id, name, position, managed, color, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                role_records,
            )
            connection.execute(
                "INSERT INTO bot_state (key, value, updated_at) VALUES ('heartbeat', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (now, now),
            )
        self._invalidate_cache()

    def sync_bot_roles(self, guild: object) -> None:
        """Refresh one guild's role snapshot after a dashboard role change.

        The regular heartbeat refreshes every guild periodically, but role
        management should be reflected in the dashboard immediately after a
        create/edit/delete action without waiting for that heartbeat.
        """
        guild_id = str(getattr(guild, "id", ""))
        if not guild_id.isdigit():
            return
        now = utc_now()
        role_records = [
            (
                str(role.id),
                guild_id,
                role.name,
                int(role.position),
                int(role.managed),
                f"#{int(getattr(getattr(role, 'colour', None), 'value', 0)):06X}",
                now,
            )
            for role in getattr(guild, "roles", [])
            if not getattr(role, "is_default", lambda: False)()
        ]
        with self._connect() as connection:
            connection.execute("DELETE FROM bot_roles WHERE guild_id = ?", (guild_id,))
            connection.executemany(
                "INSERT INTO bot_roles (role_id, guild_id, name, position, managed, color, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                role_records,
            )
        self._invalidate_cache()

    def bot_is_online(self, max_age_seconds: int = 90) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM bot_state WHERE key = 'heartbeat'").fetchone()
        if not row:
            return False
        try:
            heartbeat = datetime.fromisoformat(row["value"])
        except ValueError:
            return False
        return (datetime.now(timezone.utc) - heartbeat).total_seconds() <= max_age_seconds

    def bot_guilds(self) -> dict[str, dict[str, object]]:
        cached = self._cache_get("bot_guilds")
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT guild_id, name, member_count, icon_url, banner_url, owner_id, owner_name, created_at, boost_level FROM bot_guilds ORDER BY lower(name)"
            ).fetchall()
        result = {
            row["guild_id"]: {
                "id": row["guild_id"],
                "name": row["name"],
                "members": row["member_count"],
                "icon_url": row["icon_url"],
                "banner_url": row["banner_url"],
                "owner_id": row["owner_id"],
                "owner_name": row["owner_name"],
                "created_at": row["created_at"],
                "boost_level": row["boost_level"],
            }
            for row in rows
        }
        return self._cache_set("bot_guilds", result, ttl=15.0)  # type: ignore[return-value]

    def bot_profile(self, guild_id: str) -> dict[str, object]:
        """Return the per-guild nickname/avatar configured for BirdBot."""
        key = f"bot_profile:{guild_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT nickname, avatar_path, avatar_url, updated_by, updated_at "
                "FROM bot_profiles WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        result: dict[str, object] = {
            "guild_id": str(guild_id),
            "nickname": row["nickname"] if row else "",
            "avatar_path": row["avatar_path"] if row else None,
            "avatar_url": row["avatar_url"] if row else None,
            "updated_by": row["updated_by"] if row else None,
            "updated_at": row["updated_at"] if row else None,
        }
        return self._cache_set(key, result, ttl=20.0)  # type: ignore[return-value]

    def save_bot_profile(
        self,
        guild_id: str,
        nickname: str,
        avatar_path: str | None,
        avatar_url: str | None,
        updated_by: str | None = None,
    ) -> dict[str, object]:
        """Persist the profile that was successfully applied to one guild."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO bot_profiles (guild_id, nickname, avatar_path, avatar_url, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
                "nickname = excluded.nickname, avatar_path = excluded.avatar_path, "
                "avatar_url = excluded.avatar_url, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (str(guild_id), nickname, avatar_path, avatar_url, updated_by, now),
            )
        self._invalidate_cache()
        return self.bot_profile(str(guild_id))

    @staticmethod
    def _default_command_config(guild_id: str, command_name: str) -> dict[str, object]:
        return {
            "guild_id": str(guild_id),
            "command_name": str(command_name),
            "enabled": True,
            "language": "en",
            "shortcuts": [],
            "updated_by": None,
            "updated_at": None,
        }

    def command_settings(self, guild_id: str) -> dict[str, object]:
        """Return the per-server prefix and command settings.

        Missing rows behave like the historical configuration: the environment
        prefix is enabled and every dashboard command is enabled.
        """
        guild_id = str(guild_id)
        key = f"command_settings:{guild_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            prefix_row = connection.execute(
                "SELECT prefix, prefix_enabled, updated_by, updated_at FROM guild_command_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT command_name, enabled, language, shortcuts, updated_by, updated_at FROM command_configs WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        prefix = str(prefix_row["prefix"] if prefix_row and prefix_row["prefix"] else COMMAND_PREFIX)
        prefix_enabled = bool(prefix_row["prefix_enabled"]) if prefix_row else True
        commands = {name: self._default_command_config(guild_id, name) for name in DASHBOARD_COMMAND_NAMES}
        for row in rows:
            name = str(row["command_name"])
            if name not in commands:
                continue
            try:
                shortcuts = json.loads(row["shortcuts"] or "[]")
            except (TypeError, ValueError):
                shortcuts = []
            if not isinstance(shortcuts, list):
                shortcuts = []
            commands[name] = {
                "guild_id": guild_id,
                "command_name": name,
                "enabled": bool(row["enabled"]),
                "language": str(row["language"] or "en") if row["language"] in {"en", "ar"} else "en",
                "shortcuts": [str(value) for value in shortcuts if isinstance(value, str)],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
            }
        result = {
            "guild_id": guild_id,
            "prefix": prefix,
            "prefix_enabled": prefix_enabled,
            "updated_by": prefix_row["updated_by"] if prefix_row else None,
            "updated_at": prefix_row["updated_at"] if prefix_row else None,
            "commands": commands,
        }
        # Prefix/shortcut changes are expected to be felt immediately by the
        # Discord gateway, so keep this cache deliberately short-lived.
        return self._cache_set(key, result, ttl=2.0)  # type: ignore[return-value]

    def command_config(self, guild_id: str, command_name: str) -> dict[str, object]:
        settings = self.command_settings(str(guild_id))
        commands = settings.get("commands")
        if isinstance(commands, dict) and command_name in commands:
            return commands[command_name]  # type: ignore[return-value]
        return self._default_command_config(str(guild_id), str(command_name))

    def command_for_shortcut(self, guild_id: str, shortcut: str) -> dict[str, object] | None:
        normalized = str(shortcut).casefold()
        settings = self.command_settings(str(guild_id))
        commands = settings.get("commands")
        if not isinstance(commands, dict):
            return None
        for config in commands.values():
            if not isinstance(config, dict) or not config.get("enabled"):
                continue
            shortcuts = config.get("shortcuts")
            if isinstance(shortcuts, list) and any(str(value).casefold() == normalized for value in shortcuts):
                return config
        return None

    def save_command_settings(
        self,
        guild_id: str,
        prefix: str,
        prefix_enabled: bool,
        commands: dict[str, dict[str, object]],
        updated_by: str | None = None,
    ) -> dict[str, object]:
        guild_id = str(guild_id)
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO guild_command_settings (guild_id, prefix, prefix_enabled, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET prefix = excluded.prefix, prefix_enabled = excluded.prefix_enabled, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (guild_id, prefix, int(bool(prefix_enabled)), updated_by, now),
            )
            for command_name, config in commands.items():
                if command_name not in DASHBOARD_COMMAND_NAMES:
                    continue
                shortcuts = config.get("shortcuts") if isinstance(config, dict) else []
                encoded_shortcuts = json.dumps(shortcuts if isinstance(shortcuts, list) else [], ensure_ascii=False)
                language = str(config.get("language") if isinstance(config, dict) else "en")
                if language not in {"en", "ar"}:
                    language = "en"
                connection.execute(
                    "INSERT INTO command_configs (guild_id, command_name, enabled, language, shortcuts, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, command_name) DO UPDATE SET enabled = excluded.enabled, language = excluded.language, shortcuts = excluded.shortcuts, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                    (guild_id, command_name, int(bool(config.get("enabled", True))), language, encoded_shortcuts, updated_by, now),
                )
        self._invalidate_cache()
        return self.command_settings(guild_id)

    def bot_text_channels(self, guild_id: str) -> list[dict[str, str]]:
        key = f"bot_text_channels:{guild_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT channel_id, name FROM bot_channels WHERE guild_id = ? ORDER BY lower(name)",
                (guild_id,),
            ).fetchall()
        return self._cache_set(key, [{"id": row["channel_id"], "name": row["name"]} for row in rows], ttl=20.0)  # type: ignore[return-value]

    def bot_categories(self, guild_id: str) -> list[dict[str, str]]:
        key = f"bot_categories:{guild_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT category_id, name FROM bot_categories WHERE guild_id = ? ORDER BY lower(name)",
                (guild_id,),
            ).fetchall()
        return self._cache_set(key, [{"id": row["category_id"], "name": row["name"]} for row in rows], ttl=20.0)  # type: ignore[return-value]

    def bot_roles(self, guild_id: str) -> list[dict[str, object]]:
        key = f"bot_roles:{guild_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT role_id, name, position, managed, color FROM bot_roles WHERE guild_id = ? ORDER BY position DESC, lower(name)",
                (guild_id,),
            ).fetchall()
        result = [
            {
                "id": row["role_id"],
                "name": row["name"],
                "position": row["position"],
                "managed": bool(row["managed"]),
                "color": row["color"] or "#000000",
            }
            for row in rows
        ]
        return self._cache_set(key, result, ttl=20.0)  # type: ignore[return-value]

    def ticket_config(self, guild_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT setup_channel_id, category_id, options, support_role_ids, panel_layout, priority, max_open_tickets, require_description, description_prompt, custom_icon_url, custom_icon_path, log_channel_id, updated_by, updated_at "
                "FROM ticket_configs WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if not row:
            return {
                "setup_channel_id": None,
                "category_id": None,
                "options": [],
                "support_role_ids": [],
                "panel_layout": "select_menu",
                "priority": "medium",
                "max_open_tickets": 1,
                "require_description": False,
                "description_prompt": "Please describe your request.",
                "custom_icon_url": None,
                "custom_icon_path": None,
                "log_channel_id": None,
                "updated_by": None,
                "updated_at": None,
            }
        try:
            options = json.loads(row["options"])
        except (TypeError, ValueError):
            options = []
        try:
            support_role_ids = json.loads(row["support_role_ids"])
        except (TypeError, ValueError):
            support_role_ids = []
        data = {**dict(row), "options": options, "support_role_ids": support_role_ids}
        data["require_description"] = bool(data["require_description"])
        try:
            data["max_open_tickets"] = max(1, min(int(data.get("max_open_tickets") or 1), 25))
        except (TypeError, ValueError):
            data["max_open_tickets"] = 1
        return data

    def save_ticket_config(
        self,
        guild_id: str,
        setup_channel_id: str,
        category_id: str | None,
        options: list[dict[str, object]],
        support_role_ids: list[str],
        priority: str,
        require_description: bool,
        description_prompt: str,
        custom_icon_url: str | None,
        custom_icon_path: str | None,
        updated_by: str,
        log_channel_id: str | None = None,
        panel_layout: str = "select_menu",
        max_open_tickets: int = 1,
    ) -> dict[str, object]:
        try:
            max_open_tickets = max(1, min(int(max_open_tickets), 25))
        except (TypeError, ValueError):
            max_open_tickets = 1
        updated_at = utc_now()
        encoded_options = json.dumps(options, separators=(",", ":"))
        encoded_support_roles = json.dumps(support_role_ids, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO ticket_configs (guild_id, setup_channel_id, category_id, options, support_role_ids, panel_layout, priority, max_open_tickets, require_description, description_prompt, custom_icon_url, custom_icon_path, log_channel_id, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET setup_channel_id = excluded.setup_channel_id, "
                "category_id = excluded.category_id, options = excluded.options, support_role_ids = excluded.support_role_ids, panel_layout = excluded.panel_layout, "
                "priority = excluded.priority, max_open_tickets = excluded.max_open_tickets, require_description = excluded.require_description, description_prompt = excluded.description_prompt, custom_icon_url = excluded.custom_icon_url, custom_icon_path = excluded.custom_icon_path, log_channel_id = excluded.log_channel_id, updated_by = excluded.updated_by, "
                "updated_at = excluded.updated_at",
                (guild_id, setup_channel_id, category_id, encoded_options, encoded_support_roles, panel_layout, priority, int(max_open_tickets), int(require_description), description_prompt, custom_icon_url, custom_icon_path, log_channel_id, updated_by, updated_at),
            )
        return {
            "setup_channel_id": setup_channel_id,
            "category_id": category_id,
            "options": options,
            "support_role_ids": support_role_ids,
            "panel_layout": panel_layout,
            "priority": priority,
            "max_open_tickets": int(max_open_tickets),
            "require_description": bool(require_description),
            "description_prompt": description_prompt,
            "custom_icon_url": custom_icon_url,
            "custom_icon_path": custom_icon_path,
            "log_channel_id": log_channel_id,
            "updated_by": updated_by,
            "updated_at": updated_at,
        }

    def active_ticket_count(self, guild_id: str, creator_id: str) -> int:
        """Count a user's open/claimed tickets for the per-user limit."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM tickets WHERE guild_id = ? AND creator_id = ? AND status IN ('open', 'claimed')",
                (str(guild_id), str(creator_id)),
            ).fetchone()
        return int(row["count"] if row else 0)

    def create_ticket(
        self,
        guild_id: str,
        channel_id: str,
        channel_name: str,
        creator_id: str,
        creator_name: str,
        option_label: str,
        category_id: str | None,
        category_name: str | None,
        priority: str,
    ) -> dict[str, object]:
        """Persist a ticket as soon as its Discord channel is created."""
        ticket_id = str(channel_id)
        created_at = utc_now()
        try:
            created_at_dt = datetime.fromisoformat(created_at)
        except ValueError:
            created_at_dt = datetime.now(timezone.utc)
        record = {
            "ticket_id": ticket_id,
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "channel_name": str(channel_name or f"ticket-{channel_id}"),
            "creator_id": str(creator_id),
            "creator_name": str(creator_name),
            "option_label": str(option_label or "Support"),
            "category_id": str(category_id) if category_id else None,
            "category_name": str(category_name) if category_name else None,
            "priority": str(priority or "medium"),
            "status": "open",
            "claimed_by": None,
            "claimed_by_name": None,
            "claimed_at": None,
            "created_at": created_at,
            "closed_at": None,
            "closed_by": None,
            "closed_by_name": None,
            "transcript_url": None,
            "unclaimed_until": (created_at_dt + timedelta(seconds=UNCLAIMED_TICKET_TIMEOUT_SECONDS)).isoformat(),
            "timeout_started_at": None,
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tickets (ticket_id, guild_id, channel_id, channel_name, creator_id, creator_name, option_label, category_id, category_name, priority, status, claimed_by, claimed_by_name, claimed_at, created_at, closed_at, closed_by, closed_by_name, transcript_url, unclaimed_until, timeout_started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticket_id) DO UPDATE SET channel_name = excluded.channel_name, creator_name = excluded.creator_name, option_label = excluded.option_label, category_id = excluded.category_id, category_name = excluded.category_name, priority = excluded.priority",
                tuple(record.values()),
            )
        self._invalidate_cache()
        return record

    def tickets(self, guild_id: str) -> list[dict[str, object]]:
        key = f"tickets:{guild_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT ticket_id, guild_id, channel_id, channel_name, creator_id, creator_name, option_label, category_id, category_name, priority, status, claimed_by, claimed_by_name, claimed_at, created_at, closed_at, closed_by, closed_by_name, transcript_url, unclaimed_until, timeout_started_at "
                "FROM tickets WHERE guild_id = ? ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'claimed' THEN 1 ELSE 2 END, created_at DESC",
                (str(guild_id),),
            ).fetchall()
        return self._cache_set(key, [dict(row) for row in rows], ttl=5.0)  # type: ignore[return-value]

    def ticket(self, guild_id: str, ticket_id: str) -> dict[str, object] | None:
        key = f"ticket:{guild_id}:{ticket_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT ticket_id, guild_id, channel_id, channel_name, creator_id, creator_name, option_label, category_id, category_name, priority, status, claimed_by, claimed_by_name, claimed_at, created_at, closed_at, closed_by, closed_by_name, transcript_url, unclaimed_until, timeout_started_at "
                "FROM tickets WHERE guild_id = ? AND ticket_id = ?",
                (str(guild_id), str(ticket_id)),
            ).fetchone()
        return self._cache_set(key, dict(row) if row else None, ttl=5.0)  # type: ignore[return-value]

    def claim_ticket(self, guild_id: str, ticket_id: str, user_id: str, user_name: str) -> dict[str, object] | None:
        claimed_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE tickets SET status = 'claimed', claimed_by = ?, claimed_by_name = ?, claimed_at = ?, unclaimed_until = NULL, timeout_started_at = NULL "
                "WHERE guild_id = ? AND ticket_id = ? AND status = 'open' AND claimed_by IS NULL AND timeout_started_at IS NULL",
                (str(user_id), str(user_name), claimed_at, str(guild_id), str(ticket_id)),
            )
        self._invalidate_cache()
        return self.ticket(guild_id, ticket_id)

    def close_ticket(
        self,
        guild_id: str,
        ticket_id: str,
        closed_by: str | None = None,
        closed_by_name: str | None = None,
        transcript_url: str | None = None,
    ) -> dict[str, object] | None:
        closed_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ?, closed_by_name = ?, transcript_url = ?, unclaimed_until = NULL, timeout_started_at = NULL "
                "WHERE guild_id = ? AND ticket_id = ? AND status != 'closed'",
                (closed_at, str(closed_by) if closed_by else None, str(closed_by_name) if closed_by_name else None,
                 transcript_url, str(guild_id), str(ticket_id)),
            )
        self._invalidate_cache()
        return self.ticket(guild_id, ticket_id)

    def close_unclaimed_ticket(
        self,
        guild_id: str,
        ticket_id: str,
        closed_by: str | None = None,
        closed_by_name: str | None = None,
        transcript_url: str | None = None,
    ) -> dict[str, object] | None:
        """Close a timeout-reserved ticket without racing a staff claim/close."""
        closed_at = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ?, closed_by_name = ?, transcript_url = ?, unclaimed_until = NULL, timeout_started_at = NULL "
                "WHERE guild_id = ? AND ticket_id = ? AND status = 'open' AND claimed_by IS NULL AND timeout_started_at IS NOT NULL",
                (closed_at, str(closed_by) if closed_by else None, str(closed_by_name) if closed_by_name else None,
                 transcript_url, str(guild_id), str(ticket_id)),
            )
            closed = cursor.rowcount > 0
        if not closed:
            return None
        self._invalidate_cache()
        return self.ticket(guild_id, ticket_id)

    def unclaimed_tickets_due(self, now: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        """Return open tickets whose five-minute unclaimed deadline has passed."""
        cutoff = now or utc_now()
        try:
            stale_before = (datetime.fromisoformat(cutoff) - timedelta(seconds=UNCLAIMED_TICKET_TIMEOUT_SECONDS)).isoformat()
        except ValueError:
            stale_before = cutoff
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT ticket_id, guild_id, channel_id, channel_name, creator_id, creator_name, option_label, category_id, category_name, priority, status, claimed_by, claimed_by_name, claimed_at, created_at, closed_at, closed_by, closed_by_name, transcript_url, unclaimed_until, timeout_started_at "
                "FROM tickets WHERE status = 'open' AND claimed_by IS NULL AND (timeout_started_at IS NULL OR timeout_started_at <= ?) AND unclaimed_until IS NOT NULL AND unclaimed_until <= ? "
                "ORDER BY unclaimed_until ASC LIMIT ?",
                (stale_before, cutoff, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve_unclaimed_timeout(self, guild_id: str, ticket_id: str, now: str | None = None) -> bool:
        """Atomically reserve a due ticket so claims cannot race its cleanup."""
        started_at = now or utc_now()
        try:
            stale_before = (datetime.fromisoformat(started_at) - timedelta(seconds=UNCLAIMED_TICKET_TIMEOUT_SECONDS)).isoformat()
        except ValueError:
            stale_before = started_at
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tickets SET timeout_started_at = ? "
                "WHERE guild_id = ? AND ticket_id = ? AND status = 'open' AND claimed_by IS NULL "
                "AND (timeout_started_at IS NULL OR timeout_started_at <= ?) AND unclaimed_until IS NOT NULL AND unclaimed_until <= ?",
                (started_at, str(guild_id), str(ticket_id), stale_before, started_at),
            )
            reserved = cursor.rowcount > 0
        if reserved:
            self._invalidate_cache()
        return reserved

    def clear_unclaimed_timeout(self, guild_id: str, ticket_id: str) -> None:
        """Release a timeout reservation when Discord deletion must be retried."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE tickets SET timeout_started_at = NULL "
                "WHERE guild_id = ? AND ticket_id = ? AND status = 'open'",
                (str(guild_id), str(ticket_id)),
            )
        self._invalidate_cache()

    def delete_ticket(self, guild_id: str, ticket_id: str) -> bool:
        """Permanently remove one closed ticket record, leaving audit logs intact."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM tickets WHERE guild_id = ? AND ticket_id = ? AND status = 'closed'",
                (str(guild_id), str(ticket_id)),
            )
            deleted = cursor.rowcount > 0
        if deleted:
            self._invalidate_cache()
        return deleted

    def delete_closed_tickets(self, guild_id: str) -> int:
        """Delete all closed ticket records without touching ticket_logs."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM tickets WHERE guild_id = ? AND status = 'closed'",
                (str(guild_id),),
            )
            deleted = max(0, int(cursor.rowcount))
        if deleted:
            self._invalidate_cache()
        return deleted

    def create_ticket_log(
        self,
        guild_id: str,
        event_type: str,
        *,
        ticket_id: str | None = None,
        actor_id: str | None = None,
        actor_name: str | None = None,
        creator_id: str | None = None,
        creator_name: str | None = None,
        channel_id: str | None = None,
        channel_name: str | None = None,
        priority: str | None = None,
        duration_seconds: int | None = None,
        transcript_url: str | None = None,
        dm_status: str | None = None,
        details: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, object]:
        timestamp = created_at or utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO ticket_logs (guild_id, ticket_id, event_type, actor_id, actor_name, creator_id, creator_name, channel_id, channel_name, priority, duration_seconds, transcript_url, dm_status, details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(guild_id), str(ticket_id) if ticket_id else None, str(event_type),
                 str(actor_id) if actor_id else None, str(actor_name) if actor_name else None,
                 str(creator_id) if creator_id else None, str(creator_name) if creator_name else None,
                 str(channel_id) if channel_id else None, str(channel_name) if channel_name else None,
                 str(priority) if priority else None, int(duration_seconds) if duration_seconds is not None else None,
                 transcript_url, dm_status, details, timestamp),
            )
            log_id = cursor.lastrowid
        self._invalidate_cache()
        return {
            "log_id": log_id, "guild_id": str(guild_id), "ticket_id": ticket_id,
            "event_type": event_type, "actor_id": actor_id, "actor_name": actor_name,
            "creator_id": creator_id, "creator_name": creator_name, "channel_id": channel_id,
            "channel_name": channel_name, "priority": priority, "duration_seconds": duration_seconds,
            "transcript_url": transcript_url, "dm_status": dm_status, "details": details, "created_at": timestamp,
        }

    def ticket_logs(self, guild_id: str, query: str = "", limit: int = 500) -> list[dict[str, object]]:
        normalized = query.strip().casefold()
        key = f"ticket_logs:{guild_id}:{normalized}:{int(limit)}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT log_id, guild_id, ticket_id, event_type, actor_id, actor_name, creator_id, creator_name, channel_id, channel_name, priority, duration_seconds, transcript_url, dm_status, details, created_at "
                "FROM ticket_logs WHERE guild_id = ? ORDER BY created_at DESC, log_id DESC LIMIT ?",
                (str(guild_id), max(1, min(int(limit), 2000))),
            ).fetchall()
        records = [dict(row) for row in rows]
        if not normalized:
            return self._cache_set(key, records, ttl=5.0)  # type: ignore[return-value]
        filtered = [
            record for record in records
            if normalized in " ".join(str(record.get(key) or "") for key in (
                "event_type", "actor_name", "actor_id", "creator_name", "creator_id",
                "channel_name", "channel_id", "priority", "dm_status", "details", "ticket_id",
            )).casefold()
        ]
        return self._cache_set(key, filtered, ttl=5.0)  # type: ignore[return-value]

    def delete_ticket_log(self, guild_id: str, log_id: int) -> bool:
        """Delete one audit-log row for a guild without touching ticket data."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM ticket_logs WHERE guild_id = ? AND log_id = ?",
                (str(guild_id), int(log_id)),
            )
            deleted = cursor.rowcount > 0
        if deleted:
            self._invalidate_cache()
        return deleted

    def delete_ticket_logs(self, guild_id: str) -> int:
        """Delete all audit-log rows for a guild, preserving tickets/transcripts."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM ticket_logs WHERE guild_id = ?",
                (str(guild_id),),
            )
            deleted = max(0, int(cursor.rowcount))
        if deleted:
            self._invalidate_cache()
        return deleted

    def create_spy_game_log(
        self,
        guild_id: str,
        secret: str,
        spy_id: str,
        spy_name: str,
        citizens: list[dict[str, str]],
        outcome: str,
        language: str = "en",
        match_at: str | None = None,
    ) -> dict[str, object]:
        """Persist one completed Spy Game match for the dashboard history."""
        created = match_at or utc_now()
        normalized_language = language if language in {"en", "ar"} else "en"
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO spy_game_logs (guild_id, match_at, secret, spy_id, spy_name, citizens, outcome, language) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(guild_id),
                    created,
                    str(secret),
                    str(spy_id),
                    str(spy_name),
                    json.dumps(citizens if isinstance(citizens, list) else [], separators=(",", ":")),
                    str(outcome),
                    normalized_language,
                ),
            )
            log_id = cursor.lastrowid
        self._invalidate_cache()
        return {
            "log_id": int(log_id or 0),
            "guild_id": str(guild_id),
            "match_at": created,
            "secret": str(secret),
            "spy_id": str(spy_id),
            "spy_name": str(spy_name),
            "citizens": citizens if isinstance(citizens, list) else [],
            "outcome": str(outcome),
            "language": normalized_language,
        }

    def spy_game_logs(self, guild_id: str, limit: int = 200) -> list[dict[str, object]]:
        """Return recent Spy Game matches in newest-first order."""
        safe_limit = max(1, min(int(limit), 1_000))
        key = f"spy_game_logs:{guild_id}:{safe_limit}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT log_id, guild_id, match_at, secret, spy_id, spy_name, citizens, outcome, language "
                "FROM spy_game_logs WHERE guild_id = ? ORDER BY match_at DESC, log_id DESC LIMIT ?",
                (str(guild_id), safe_limit),
            ).fetchall()
        logs: list[dict[str, object]] = []
        for row in rows:
            try:
                citizens = json.loads(row["citizens"] or "[]")
            except (TypeError, ValueError):
                citizens = []
            if not isinstance(citizens, list):
                citizens = []
            logs.append({
                "log_id": int(row["log_id"]),
                "guild_id": str(row["guild_id"]),
                "match_at": row["match_at"],
                "secret": row["secret"],
                "spy_id": row["spy_id"],
                "spy_name": row["spy_name"],
                "citizens": citizens,
                "outcome": row["outcome"],
                "language": row["language"],
            })
        return self._cache_set(key, logs, ttl=5.0)  # type: ignore[return-value]

    def spy_game_config(self, guild_id: str) -> dict[str, object]:
        """Return the persisted Spy Game limits for a guild.

        Values are clamped on read as a final guard so an older/manual database
        edit cannot make the lobby unusable.
        """
        defaults: dict[str, object] = {
            "min_players": 3,
            "max_players": 20,
            "question_timer_seconds": 30,
            "end_mode": "manual",
            "auto_end_rounds": 20,
            "enabled": True,
            "language": "en",
        }
        key = f"spy_game_config:{guild_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return dict(cached)  # type: ignore[arg-type]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT min_players, max_players, question_timer_seconds, end_mode, auto_end_rounds, enabled, language "
                "FROM spy_game_configs WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        if row is not None:
            try:
                minimum = max(3, min(50, int(row["min_players"])))
                maximum = max(3, min(50, int(row["max_players"])))
                timer = max(5, min(600, int(row["question_timer_seconds"])))
                end_mode = str(row["end_mode"] or "manual").lower()
                if end_mode not in {"manual", "auto"}:
                    end_mode = "manual"
                auto_end_rounds = max(1, min(1000, int(row["auto_end_rounds"] or 20)))
                if maximum < minimum:
                    maximum = minimum
                defaults = {
                    "min_players": minimum,
                    "max_players": maximum,
                    "question_timer_seconds": timer,
                    "end_mode": end_mode,
                    "auto_end_rounds": auto_end_rounds,
                    "enabled": bool(int(row["enabled"])),
                    "language": str(row["language"] or "en") if str(row["language"] or "en") in {"en", "ar"} else "en",
                }
            except (TypeError, ValueError):
                pass
        return self._cache_set(key, defaults, ttl=20.0)  # type: ignore[return-value]

    def save_spy_game_config(
        self,
        guild_id: str,
        min_players: int,
        max_players: int,
        question_timer_seconds: int,
        *,
        enabled: bool | None = None,
        language: str | None = None,
        end_mode: str | None = None,
        auto_end_rounds: int | None = None,
    ) -> dict[str, object]:
        """Persist validated Spy Game lobby settings and return their values."""
        minimum = max(3, min(50, int(min_players)))
        maximum = max(minimum, min(50, int(max_players)))
        timer = max(5, min(600, int(question_timer_seconds)))
        current = self.spy_game_config(guild_id)
        final_enabled = bool(current.get("enabled", True)) if enabled is None else bool(enabled)
        final_language = language if language in {"en", "ar"} else str(current.get("language") or "en")
        if final_language not in {"en", "ar"}:
            final_language = "en"
        final_end_mode = str(end_mode or current.get("end_mode") or "manual").lower()
        if final_end_mode not in {"manual", "auto"}:
            final_end_mode = "manual"
        final_auto_end_rounds = max(1, min(1000, int(auto_end_rounds if auto_end_rounds is not None else current.get("auto_end_rounds", 20))))
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO spy_game_configs "
                "(guild_id, min_players, max_players, question_timer_seconds, end_mode, auto_end_rounds, enabled, language, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "min_players = excluded.min_players, "
                "max_players = excluded.max_players, "
                "question_timer_seconds = excluded.question_timer_seconds, "
                "end_mode = excluded.end_mode, "
                "auto_end_rounds = excluded.auto_end_rounds, "
                "enabled = excluded.enabled, "
                "language = excluded.language, "
                "updated_at = excluded.updated_at",
                (str(guild_id), minimum, maximum, timer, final_end_mode, final_auto_end_rounds, int(final_enabled), final_language, now),
            )
        self._invalidate_cache()
        return {
            "min_players": minimum,
            "max_players": maximum,
            "question_timer_seconds": timer,
            "end_mode": final_end_mode,
            "auto_end_rounds": final_auto_end_rounds,
            "enabled": final_enabled,
            "language": final_language,
        }

    def roulette_game_config(self, guild_id: str) -> dict[str, object]:
        """Return persisted Roulette lobby limits with safe defaults."""
        defaults: dict[str, object] = {
            "min_players": 2,
            "max_players": 20,
            "enabled": True,
            "language": "en",
            "wheel_mode": "multi",
            "wheel_color": "#6B7280",
            "wheel_colors": [
                "#6B7280", "#9CA3AF", "#4B5563", "#374151",
                "#D1D5DB", "#818CF8", "#A78BFA",
            ],
            "turn_timer_seconds": 30,
        }
        key = f"roulette_game_config:{guild_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return dict(cached)  # type: ignore[arg-type]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT min_players, max_players, enabled, language, wheel_mode, wheel_color, wheel_colors, turn_timer_seconds "
                "FROM roulette_game_configs WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        if row is not None:
            try:
                minimum = max(2, min(50, int(row["min_players"])))
                maximum = max(minimum, min(50, int(row["max_players"])))
                mode = str(row["wheel_mode"] or "multi").lower()
                if mode not in {"multi", "single"}:
                    mode = "multi"
                language = str(row["language"] or "en").lower()
                if language not in {"en", "ar"}:
                    language = "en"
                color = str(row["wheel_color"] or "#6B7280").upper()
                if not (len(color) == 7 and color.startswith("#") and all(character in "0123456789ABCDEF" for character in color[1:])):
                    color = "#6B7280"
                try:
                    wheel_colors = json.loads(row["wheel_colors"] or "[]")
                except (TypeError, ValueError):
                    wheel_colors = []
                if not isinstance(wheel_colors, list):
                    wheel_colors = []
                wheel_colors = [str(value).upper() for value in wheel_colors[:7]]
                turn_timer_seconds = max(5, min(600, int(row["turn_timer_seconds"] or 30)))
                if len(wheel_colors) != 7 or any(
                    len(value) != 7
                    or not value.startswith("#")
                    or any(character not in "0123456789ABCDEF" for character in value[1:])
                    for value in wheel_colors
                ):
                    # Migrate older single-color settings into a useful
                    # seven-slice palette without changing their chosen base.
                    red, green, blue = (int(color[index : index + 2], 16) for index in (1, 3, 5))
                    wheel_colors = [
                        color,
                        f"#{min(255, red + 38):02X}{min(255, green + 38):02X}{min(255, blue + 38):02X}",
                        f"#{max(0, red - 38):02X}{max(0, green - 38):02X}{max(0, blue - 38):02X}",
                        f"#{blue:02X}{red:02X}{green:02X}",
                        f"#{min(255, red + 70):02X}{max(0, green - 20):02X}{min(255, blue + 20):02X}",
                        f"#{max(0, red - 20):02X}{min(255, green + 55):02X}{max(0, blue - 15):02X}",
                        f"#{min(255, red + 20):02X}{min(255, green + 15):02X}{max(0, blue - 45):02X}",
                    ]
                defaults = {
                    "min_players": minimum,
                    "max_players": maximum,
                    "enabled": bool(int(row["enabled"])),
                    "language": language,
                    "wheel_mode": mode,
                    "wheel_color": color,
                    "wheel_colors": wheel_colors,
                    "turn_timer_seconds": turn_timer_seconds,
                }
            except (TypeError, ValueError):
                pass
        return self._cache_set(key, defaults, ttl=20.0)  # type: ignore[return-value]

    def save_roulette_game_config(
        self,
        guild_id: str,
        min_players: int,
        max_players: int,
        *,
        enabled: bool | None = None,
        language: str | None = None,
        wheel_mode: str | None = None,
        wheel_color: str | None = None,
        wheel_colors: Iterable[str] | None = None,
        turn_timer_seconds: int | None = None,
    ) -> dict[str, object]:
        """Persist validated Roulette lobby capacity settings."""
        minimum = max(2, min(50, int(min_players)))
        maximum = max(minimum, min(50, int(max_players)))
        current = self.roulette_game_config(guild_id)
        final_enabled = bool(current.get("enabled", True)) if enabled is None else bool(enabled)
        final_language = language if language in {"en", "ar"} else str(current.get("language") or "en")
        if final_language not in {"en", "ar"}:
            final_language = "en"
        final_mode = str(wheel_mode or current.get("wheel_mode") or "multi").lower()
        if final_mode not in {"multi", "single"}:
            final_mode = "multi"
        final_color = str(wheel_color or current.get("wheel_color") or "#6B7280").upper()
        if not (len(final_color) == 7 and final_color.startswith("#") and all(character in "0123456789ABCDEF" for character in final_color[1:])):
            final_color = "#6B7280"
        raw_colors = list(wheel_colors) if wheel_colors is not None else current.get("wheel_colors", [])
        if not isinstance(raw_colors, list):
            raw_colors = []
        final_colors = [str(value).upper() for value in raw_colors[:7]]
        if len(final_colors) != 7 or any(
            len(value) != 7
            or not value.startswith("#")
            or any(character not in "0123456789ABCDEF" for character in value[1:])
            for value in final_colors
        ):
            final_colors = list(current.get("wheel_colors", [])) if isinstance(current.get("wheel_colors"), list) else []
        if len(final_colors) != 7:
            red, green, blue = (int(final_color[index : index + 2], 16) for index in (1, 3, 5))
            final_colors = [
                final_color,
                f"#{min(255, red + 38):02X}{min(255, green + 38):02X}{min(255, blue + 38):02X}",
                f"#{max(0, red - 38):02X}{max(0, green - 38):02X}{max(0, blue - 38):02X}",
                f"#{blue:02X}{red:02X}{green:02X}",
                f"#{min(255, red + 70):02X}{max(0, green - 20):02X}{min(255, blue + 20):02X}",
                f"#{max(0, red - 20):02X}{min(255, green + 55):02X}{max(0, blue - 15):02X}",
                f"#{min(255, red + 20):02X}{min(255, green + 15):02X}{max(0, blue - 45):02X}",
            ]
        final_turn_timer = max(5, min(600, int(turn_timer_seconds if turn_timer_seconds is not None else current.get("turn_timer_seconds", 30))))
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO roulette_game_configs "
                "(guild_id, min_players, max_players, enabled, language, wheel_mode, wheel_color, wheel_colors, turn_timer_seconds, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
                "min_players = excluded.min_players, max_players = excluded.max_players, "
                "enabled = excluded.enabled, language = excluded.language, "
                "wheel_mode = excluded.wheel_mode, wheel_color = excluded.wheel_color, wheel_colors = excluded.wheel_colors, turn_timer_seconds = excluded.turn_timer_seconds, "
                "updated_at = excluded.updated_at",
                (str(guild_id), minimum, maximum, int(final_enabled), final_language, final_mode, final_color, json.dumps(final_colors), final_turn_timer, now),
            )
        self._invalidate_cache()
        return {
            "min_players": minimum,
            "max_players": maximum,
            "enabled": final_enabled,
            "language": final_language,
            "wheel_mode": final_mode,
            "wheel_color": final_color,
            "wheel_colors": final_colors,
            "turn_timer_seconds": final_turn_timer,
        }

    def sync_bot_members(self, guilds: Iterable[object]) -> None:
        now = utc_now()
        records = [
            (
                str(member.id), str(guild.id), member.display_name, member.name,
                getattr(member, "global_name", None),
                str(member.display_avatar.url), member.joined_at.isoformat() if member.joined_at else None,
                json.dumps([role.name for role in member.roles[1:]]),
                json.dumps([str(role.id) for role in member.roles[1:]]),
                int(member.bot), now,
            )
            for guild in guilds
            for member in getattr(guild, "members", [])
        ]
        with self._connect() as connection:
            connection.execute("DELETE FROM bot_members")
            connection.executemany(
                "INSERT INTO bot_members (member_id, guild_id, display_name, username, global_name, avatar_url, joined_at, roles, role_ids, is_bot, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                records,
            )
        self._invalidate_cache()

    def bot_members(self, guild_id: str, query: str = "", limit: int = 10_000) -> list[dict[str, object]]:
        normalized = query.strip()
        key = f"bot_members:{guild_id}:{normalized.casefold()}:{int(limit)}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            member_columns = "member_id, display_name, username, global_name, avatar_url, joined_at, roles, role_ids, is_bot"
            if normalized:
                # Keep search work in SQLite instead of decoding every role
                # payload on every keystroke. Python filtering below remains a
                # final case-insensitive guard for Unicode display names.
                needle = f"%{normalized}%"
                rows = connection.execute(
                    f"SELECT {member_columns} FROM bot_members "
                    "WHERE guild_id = ? AND is_bot = 0 AND ("
                    "display_name LIKE ? COLLATE NOCASE OR username LIKE ? COLLATE NOCASE OR "
                    "global_name LIKE ? COLLATE NOCASE OR member_id LIKE ? COLLATE NOCASE) "
                    "ORDER BY display_name COLLATE NOCASE LIMIT ?",
                    (guild_id, needle, needle, needle, needle, max(1, min(int(limit), 10_000))),
                ).fetchall()
                # SQLite's NOCASE collation is ASCII-oriented. If a query
                # contains non-ASCII case variants and the indexed lookup has
                # no candidate, fall back once to the complete roster so the
                # dashboard never reports a false "no matching user" result.
                if not rows:
                    rows = connection.execute(
                        f"SELECT {member_columns} FROM bot_members WHERE guild_id = ? AND is_bot = 0 "
                        "ORDER BY display_name COLLATE NOCASE",
                        (guild_id,),
                    ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {member_columns} FROM bot_members "
                    "WHERE guild_id = ? AND is_bot = 0 ORDER BY display_name COLLATE NOCASE LIMIT ?",
                    (guild_id, max(1, min(int(limit), 10_000))),
                ).fetchall()
        members = [
            {
                **dict(row),
                "roles": json.loads(row["roles"] or "[]"),
                "role_ids": json.loads(row["role_ids"] or "[]"),
            }
            for row in rows
        ]
        if not normalized:
            return self._cache_set(key, members, ttl=15.0)  # type: ignore[return-value]
        needle = normalized.casefold()
        filtered = [
            member for member in members
            if any(needle in str(member.get(field) or "").casefold() for field in ("display_name", "username", "global_name", "member_id"))
        ][:limit]
        return self._cache_set(key, filtered, ttl=15.0)  # type: ignore[return-value]

    def bot_member(self, guild_id: str, member_id: str) -> dict[str, object] | None:
        key = f"bot_member:{guild_id}:{member_id}"
        cached = self._cache_get(key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT member_id, display_name, username, global_name, avatar_url, joined_at, roles, role_ids, is_bot FROM bot_members WHERE guild_id = ? AND member_id = ?",
                (guild_id, member_id),
            ).fetchone()
        result = {**dict(row), "roles": json.loads(row["roles"] or "[]"), "role_ids": json.loads(row["role_ids"] or "[]")} if row else None
        return self._cache_set(key, result, ttl=15.0)  # type: ignore[return-value]

    def queue_command(self, guild_id: str, channel_id: str, command_name: str, user_id: str, payload: dict[str, object] | None = None) -> str:
        request_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO command_requests (request_id, guild_id, channel_id, command_name, requested_by, status, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (request_id, guild_id, channel_id, command_name, user_id, json.dumps(payload or {}), utc_now()),
            )
        return request_id

    def claim_pending_commands(self, limit: int = 10) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id, guild_id, channel_id, command_name, requested_by, payload FROM command_requests "
                "WHERE status = 'pending' ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
            request_ids = [row["request_id"] for row in rows]
            if request_ids:
                connection.executemany(
                    "UPDATE command_requests SET status = 'running' WHERE request_id = ? AND status = 'pending'",
                    [(request_id,) for request_id in request_ids],
                )
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def complete_command(self, request_id: str, error: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE command_requests SET status = ?, error = ?, completed_at = ? WHERE request_id = ?",
                ("failed" if error else "complete", error, utc_now(), request_id),
            )

    def command_request_for_user(self, request_id: str, user_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_id, guild_id, channel_id, command_name, status, error FROM command_requests "
                "WHERE request_id = ? AND requested_by = ?",
                (request_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def sync_bans(self, guild_id: str, bans: Iterable[object]) -> None:
        records = [
            (str(ban.user.id), guild_id, str(ban.user), ban.reason, utc_now())
            for ban in bans
        ]
        with self._connect() as connection:
            connection.execute("DELETE FROM bot_bans WHERE guild_id = ?", (guild_id,))
            connection.executemany(
                "INSERT INTO bot_bans (user_id, guild_id, user_name, reason, banned_at) VALUES (?, ?, ?, ?, ?)",
                records,
            )

    def bot_bans(self, guild_id: str) -> list[dict[str, str | None]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT user_id, user_name, reason, banned_at FROM bot_bans WHERE guild_id = ? ORDER BY lower(user_name)",
                (guild_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def activate_guild(self, guild_id: str, user_id: str) -> dict[str, object]:
        activated_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO guild_activations (guild_id, activated, activated_by, activated_at) VALUES (?, 1, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET activated = 1, activated_by = excluded.activated_by, activated_at = excluded.activated_at",
                (guild_id, user_id, activated_at),
            )
        return {"guild_id": guild_id, "activated": True, "activated_by": user_id, "activated_at": activated_at}

    def disable_guild(self, guild_id: str, user_id: str) -> dict[str, object]:
        """Disable features for one guild without touching the global client."""
        changed_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO guild_activations (guild_id, activated, activated_by, activated_at) VALUES (?, 0, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET activated = 0, activated_by = excluded.activated_by, activated_at = excluded.activated_at",
                (guild_id, user_id, changed_at),
            )
        return {"guild_id": guild_id, "activated": False, "activated_by": user_id, "activated_at": changed_at}

    def is_guild_activated(self, guild_id: int | str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT activated FROM guild_activations WHERE guild_id = ?", (str(guild_id),)
            ).fetchone()
        return bool(row and row["activated"])

    def activation_for_guild(self, guild_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT guild_id, activated, activated_by, activated_at FROM guild_activations WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "guild_id": row["guild_id"],
            "activated": bool(row["activated"]),
            "activated_by": row["activated_by"],
            "activated_at": row["activated_at"],
        }

    def create_oauth_session(
        self,
        session_id: str,
        user_id: str,
        access_token: str,
        refresh_token: str | None,
        display_name: str,
        avatar_url: str | None,
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO oauth_sessions (session_id, user_id, access_token, refresh_token, display_name, avatar_url, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, access_token, refresh_token, display_name, avatar_url, now, now),
            )

    def oauth_tokens(self, session_id: str | None, user_id: str) -> dict[str, str | None] | None:
        if not session_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT access_token, refresh_token FROM oauth_sessions WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
        if not row:
            return None
        return {"access_token": row["access_token"], "refresh_token": row["refresh_token"]}

    def update_oauth_tokens(self, session_id: str, user_id: str, access_token: str, refresh_token: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE oauth_sessions SET access_token = ?, refresh_token = COALESCE(?, refresh_token), updated_at = ? "
                "WHERE session_id = ? AND user_id = ?",
                (access_token, refresh_token, utc_now(), session_id, user_id),
            )

    def delete_oauth_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM oauth_sessions WHERE session_id = ?", (session_id,))

    def spotify_account(self, user_id: str) -> dict[str, object] | None:
        """Return a linked Spotify account; access tokens never leave the backend."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id, spotify_user_id, display_name, access_token, refresh_token, expires_at, scope, created_at, updated_at "
                "FROM spotify_accounts WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
        return dict(row) if row else None

    def save_spotify_account(
        self,
        user_id: str,
        spotify_user_id: str,
        display_name: str | None,
        access_token: str,
        refresh_token: str | None,
        expires_at: float,
        scope: str | None = None,
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO spotify_accounts (user_id, spotify_user_id, display_name, access_token, refresh_token, expires_at, scope, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET spotify_user_id = excluded.spotify_user_id, display_name = excluded.display_name, "
                "access_token = excluded.access_token, refresh_token = COALESCE(excluded.refresh_token, spotify_accounts.refresh_token), "
                "expires_at = excluded.expires_at, scope = excluded.scope, updated_at = excluded.updated_at",
                (str(user_id), str(spotify_user_id), display_name, access_token, refresh_token, float(expires_at), scope, now, now),
            )

    def update_spotify_access_token(
        self,
        user_id: str,
        access_token: str,
        expires_at: float,
        refresh_token: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE spotify_accounts SET access_token = ?, refresh_token = COALESCE(?, refresh_token), expires_at = ?, updated_at = ? WHERE user_id = ?",
                (access_token, refresh_token, float(expires_at), utc_now(), str(user_id)),
            )

    def delete_spotify_account(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM spotify_accounts WHERE user_id = ?", (str(user_id),))

    def music_state(self, guild_id: str) -> dict[str, object]:
        """Read the latest lightweight player state shared with web clients."""
        cache_key = f"music_state:{guild_id}"
        cached = self._cache_get(cache_key)
        if cached is not self._CACHE_MISS:
            return cached  # type: ignore[return-value]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM music_sessions WHERE guild_id = ?", (str(guild_id),)
            ).fetchone()
        if not row:
            return self._cache_set(cache_key, {
                "connected": False,
                "connection_state": "disconnected",
                "voice_channel_id": None,
                "voice_channel_name": None,
                "queue": [],
                "current": None,
                "paused": True,
                "loop_enabled": False,
                "shuffle_enabled": False,
                "queue_finished": False,
                "last_error": None,
                "volume": 1.0,
                "position": 0.0,
                "duration": 0.0,
                "updated_at": None,
            }, ttl=0.5)  # type: ignore[return-value]
        try:
            state = json.loads(row["state"] or "{}")
        except (TypeError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        if "connection_state" not in state:
            state["connection_state"] = "ready" if state.get("connected") else "disconnected"
        return self._cache_set(cache_key, state, ttl=0.5)  # type: ignore[return-value]

    def save_music_state(self, guild_id: str, state: dict[str, object], *, persist: bool = True) -> None:
        now = utc_now()
        payload = {**state, "updated_at": now}
        # Fast websocket heartbeats can publish to the process cache without
        # opening a SQLite transaction on every frame.  The player promotes
        # an update to a durable write at a bounded cadence and on important
        # lifecycle changes.
        if not persist:
            self._cache_set(f"music_state:{guild_id}", payload, ttl=0.5)
            return
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO music_sessions (guild_id, state, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
                (str(guild_id), json.dumps(payload, separators=(",", ":")), now),
            )
        # The bot tick and websocket reader run at sub-second cadence. Keep
        # the latest serialized state in the process cache so readers do not
        # open a new SQLite connection for every frame.
        self._cache_set(f"music_state:{guild_id}", payload, ttl=0.5)

    def reset_music_states(self) -> None:
        """Clear runtime voice state left by a previous bot process."""
        with self._connect() as connection:
            rows = connection.execute("SELECT guild_id, state FROM music_sessions").fetchall()
            for row in rows:
                try:
                    state = json.loads(row["state"] or "{}")
                except (TypeError, ValueError):
                    state = {}
                if not isinstance(state, dict):
                    state = {}
                state.update({"connected": False, "connection_state": "disconnected", "voice_channel_id": None, "voice_channel_name": None, "current": None, "queue": [], "paused": True, "loop_enabled": False, "shuffle_enabled": False, "queue_finished": False, "last_error": None, "position": 0.0, "duration": 0.0})
                now = utc_now()
                connection.execute(
                    "UPDATE music_sessions SET state = ?, updated_at = ? WHERE guild_id = ?",
                    (json.dumps({**state, "updated_at": now}, separators=(",", ":")), now, row["guild_id"]),
                )
        # A bot reconnect can happen in the same process. Do not let a
        # websocket read the previous process' cached voice state.
        self._invalidate_cache()


store = BirdBotStore()
