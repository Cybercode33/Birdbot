"""BirdBot dashboard: Discord OAuth and secure per-server activation."""

from __future__ import annotations

import secrets
import sys
import re
import json
import time
import asyncio
import contextlib
import hashlib
import ipaddress
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from settings import (  # noqa: E402
    DISCORD_CLIENT_ID,
    DISCORD_CLIENT_SECRET,
    DISCORD_REDIRECT_URI,
    GROQ_API_KEY,
    SESSION_SECRET,
    SUPPORT_URL,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_PREMIUM_REQUIRED,
    SPOTIFY_PREMIUM_USER_IDS,
    VC_PREMIUM_REQUIRED,
    VC_PREMIUM_USER_IDS,
    VC_BOT_SLOT_COUNT,
    vc_bot_slot_configured,
)
from role_permissions import ROLE_PERMISSION_FIELDS, ROLE_PERMISSION_KEYS  # noqa: E402
from storage import DASHBOARD_COMMAND_NAMES, LOG_CATEGORIES, store, utc_now  # noqa: E402

DISCORD_API = "https://discord.com/api/v10"
ADMINISTRATOR = 0x8
SUPPORT_ROLE_ERROR = "Error: You do not have the required Support Role to claim or close tickets."
DISCORD_SNOWFLAKE = re.compile(r"^\d{17,20}$")
TIMEOUT_MAX_MINUTES = 28 * 24 * 60
TIMEOUT_UNIT_MINUTES = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 24 * 60, "day": 24 * 60, "days": 24 * 60,
    "w": 7 * 24 * 60, "wk": 7 * 24 * 60, "wks": 7 * 24 * 60, "week": 7 * 24 * 60, "weeks": 7 * 24 * 60,
}


def parse_timeout_duration(value: object) -> tuple[int, str] | None:
    """Normalize dashboard timeout input to Discord minutes and a label."""
    if isinstance(value, bool) or value is None:
        return None
    raw = str(value).strip().casefold()
    match = re.fullmatch(r"(\d+)\s*([a-z]+)?", raw)
    if not match:
        return None
    amount = int(match.group(1))
    multiplier = TIMEOUT_UNIT_MINUTES.get(match.group(2) or "m")
    if amount < 1 or multiplier is None:
        return None
    minutes = amount * multiplier
    if minutes > TIMEOUT_MAX_MINUTES:
        return None
    labels = {1: "minute", 60: "hour", 24 * 60: "day", 7 * 24 * 60: "week"}
    unit = labels.get(multiplier, "minute")
    return minutes, f"{amount} {unit}{'' if amount == 1 else 's'}"
DISCORD_MESSAGE_LINK = re.compile(
    r"^https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/\d{17,20}/\d{17,20}/\d{17,20}$",
    re.IGNORECASE,
)
ROLE_COLOR = re.compile(r"^#?[0-9a-fA-F]{6}$")
MAX_TICKET_ICON_BYTES = 5 * 1024 * 1024
TICKET_ICON_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
PROFILE_AVATAR_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
DM_MEDIA_TYPES = {
    **PROFILE_AVATAR_TYPES,
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}
MAX_PROFILE_AVATAR_BYTES = 8 * 1024 * 1024
MAX_DM_MEDIA_BYTES = 8 * 1024 * 1024
TICKET_ICON_DIR = store.path.parent / "ticket-icons"
TICKET_ICON_DIR.mkdir(parents=True, exist_ok=True)
BOT_PROFILE_AVATAR_DIR = store.path.parent / "bot-profile-avatars"
BOT_PROFILE_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
DM_MEDIA_DIR = store.path.parent / "dm-media"
DM_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
TRANSCRIPT_DIR = store.path.parent / "transcripts"
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
_OAUTH_GUILD_CACHE: dict[str, tuple[float, dict[str, dict[str, object]]]] = {}
_OAUTH_GUILD_CACHE_TTL = 10.0
# Several protected endpoints can load in parallel when a dashboard page
# opens.  Serialising OAuth guild refreshes per browser session avoids a
# refresh-token race and prevents a transient Discord response from making a
# real member look as if they left the server.
_OAUTH_GUILD_LOCKS: dict[str, asyncio.Lock] = {}
_OAUTH_GUILD_FRESH_REUSE_SECONDS = 2.0
SPOTIFY_ACCOUNTS_API = "https://accounts.spotify.com"
SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_SCOPES = "user-read-private user-read-email playlist-read-private playlist-read-collaborative user-library-read user-top-read"
SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
EXTERNAL_MUSIC_URL_MAX_LENGTH = 2048
_BLOCKED_EXTERNAL_HOSTS = {"localhost", "localhost.localdomain", "127.0.0.1", "::1"}
_SPOTIFY_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


def normalize_external_music_url(value: object) -> str:
    """Validate a user-supplied media URL before passing it to yt-dlp.

    The extractor can handle YouTube and many other providers, but accepting
    arbitrary schemes/hosts would also let a dashboard user make the bot
    fetch local services.  Keep the public URL feature limited to HTTP(S) and
    reject obvious loopback/private targets.
    """
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="Paste a valid YouTube or audio URL.")
    url = value.strip()
    if not url or len(url) > EXTERNAL_MUSIC_URL_MAX_LENGTH:
        raise HTTPException(status_code=400, detail="Paste a valid YouTube or audio URL.")
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        # Credentials are never needed for a public song URL and can hide the
        # actual destination from the user/operator.
        _ = parsed.port
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Paste a valid YouTube or audio URL.") from error
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Paste a valid YouTube or audio URL.")
    if hostname in _BLOCKED_EXTERNAL_HOSTS or hostname.endswith(".localhost"):
        raise HTTPException(status_code=400, detail="Local and private media URLs are not allowed.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        raise HTTPException(status_code=400, detail="Local and private media URLs are not allowed.")
    return url


def external_music_track(url: object) -> dict[str, object]:
    """Create the same metadata shape used by Spotify tracks for URL input."""
    source_url = normalize_external_music_url(url)
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "external source").removeprefix("www.")
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:24]
    return {
        "id": f"url:{digest}",
        "name": f"External audio ({hostname})",
        "artist": "Direct link",
        "album": "",
        "image_url": None,
        "duration_ms": 0,
        "uri": source_url,
        "source_url": source_url,
        "preview_url": "",
    }


def command_music_track(query: object) -> dict[str, object]:
    """Build safe music metadata for a Commands-tab play/queue request.

    URL requests reuse the same public-URL validation as the member music
    portal. Plain text is kept as a search query and resolved by the Discord
    music player, so no short-lived provider/CDN URL is stored in SQLite.
    """
    if not isinstance(query, str):
        raise HTTPException(status_code=400, detail="Enter a YouTube link or music name.")
    value = query.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Enter a YouTube link or music name.")
    if len(value) > 500:
        raise HTTPException(status_code=400, detail="That music request is too long (maximum 500 characters).")
    try:
        parsed = urlparse(value)
        is_url = parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        is_url = False
    if is_url:
        return external_music_track(value)
    digest = hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:24]
    return {
        "id": f"search:{digest}",
        "name": value,
        "artist": "",
        "album": "",
        "image_url": None,
        "duration_ms": 0,
        "uri": "",
        "source_url": "",
        "preview_url": "",
        "search_query": value,
    }


def spotify_refresh_lock(user_id: str) -> asyncio.Lock:
    """Return one refresh lock per account to avoid token-refresh races."""
    lock = _SPOTIFY_REFRESH_LOCKS.get(str(user_id))
    if lock is None:
        lock = asyncio.Lock()
        _SPOTIFY_REFRESH_LOCKS[str(user_id)] = lock
    return lock


def public_ticket_config(config: dict[str, object]) -> dict[str, object]:
    """Do not expose the server's local icon path to dashboard clients."""
    return {key: value for key, value in config.items() if key != "custom_icon_path"}


def public_bot_profile(profile: dict[str, object]) -> dict[str, object]:
    """Expose profile metadata without leaking the dashboard's local paths."""
    return {key: value for key, value in profile.items() if key != "avatar_path"}


def oauth_is_configured() -> bool:
    return all((DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI, SESSION_SECRET))


def logged_in_user(request: Request) -> dict[str, str]:
    user = request.session.get("discord_user")
    if not user:
        raise HTTPException(status_code=401, detail="Sign in with Discord first.")
    return user


def avatar_url(user: dict[str, object]) -> str | None:
    avatar = user.get("avatar")
    if not avatar:
        return None
    extension = "gif" if str(avatar).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{user['id']}/{avatar}.{extension}?size=128"


def guild_is_manageable(guild: dict[str, object]) -> bool:
    try:
        permissions = int(str(guild.get("permissions", "0")))
    except ValueError:
        permissions = 0
    return bool(guild.get("owner") or permissions & ADMINISTRATOR)


def member_has_support_role(guild_id: str, user_id: str) -> bool:
    """Check the bot-synchronised guild roles for a configured support role.

    OAuth's ``guilds`` scope exposes owner/admin permissions, but not the
    member's role IDs.  The single Discord bot client therefore synchronises
    role IDs into the shared store; the worker still performs the authoritative
    live check immediately before claiming or closing a ticket.
    """
    config = store.ticket_config(guild_id)
    raw_roles = config.get("support_role_ids")
    configured = {str(role_id) for role_id in raw_roles} if isinstance(raw_roles, list) else set()
    if not configured:
        return False
    member = store.bot_member(guild_id, user_id)
    raw_member_roles = (member or {}).get("role_ids")
    member_roles = {str(role_id) for role_id in raw_member_roles} if isinstance(raw_member_roles, list) else set()
    return bool(configured.intersection(member_roles))


def oauth_guild_lock(cache_key: str) -> asyncio.Lock:
    lock = _OAUTH_GUILD_LOCKS.get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        _OAUTH_GUILD_LOCKS[cache_key] = lock
    return lock


def cached_oauth_guilds(cache_key: str, *, fresh: bool) -> dict[str, dict[str, object]] | None:
    cached = _OAUTH_GUILD_CACHE.get(cache_key)
    if not cached:
        return None
    expires_at, guilds = cached
    now = time.monotonic()
    if expires_at <= now:
        _OAUTH_GUILD_CACHE.pop(cache_key, None)
        return None
    # A request that was verified a moment ago is fresh enough to coalesce
    # page-open requests, while later mutations still make a new Discord call.
    fetched_at = expires_at - _OAUTH_GUILD_CACHE_TTL
    if not fresh or now - fetched_at <= _OAUTH_GUILD_FRESH_REUSE_SECONDS:
        return guilds
    return None


async def discord_user_guilds(
    request: Request,
    user: dict[str, str],
    *,
    fresh: bool = False,
) -> dict[str, dict[str, object]]:
    """Read Discord guild permissions without concurrent token-refresh races."""
    oauth_session_id = request.session.get("oauth_session_id")
    cache_key = f"{oauth_session_id}:{user['id']}"
    cached = cached_oauth_guilds(cache_key, fresh=fresh)
    if cached is not None:
        return cached

    async with oauth_guild_lock(cache_key):
        cached = cached_oauth_guilds(cache_key, fresh=fresh)
        if cached is not None:
            return cached
        tokens = store.oauth_tokens(oauth_session_id, user["id"])
        if not tokens:
            request.session.clear()
            raise HTTPException(status_code=401, detail="Your Discord session has expired. Please sign in again.")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{DISCORD_API}/users/@me/guilds",
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                )
                if response.status_code in (401, 403) and tokens["refresh_token"]:
                    refresh_response = await client.post(
                        f"{DISCORD_API}/oauth2/token",
                        data={
                            "client_id": DISCORD_CLIENT_ID,
                            "client_secret": DISCORD_CLIENT_SECRET,
                            "grant_type": "refresh_token",
                            "refresh_token": tokens["refresh_token"],
                        },
                    )
                    refresh_response.raise_for_status()
                    refreshed = refresh_response.json()
                    store.update_oauth_tokens(
                        str(oauth_session_id),
                        user["id"],
                        refreshed["access_token"],
                        refreshed.get("refresh_token"),
                    )
                    response = await client.get(
                        f"{DISCORD_API}/users/@me/guilds",
                        headers={"Authorization": f"Bearer {refreshed['access_token']}"},
                    )
            if response.status_code in (401, 403):
                _OAUTH_GUILD_CACHE.pop(cache_key, None)
                store.delete_oauth_session(oauth_session_id)
                request.session.clear()
                raise HTTPException(status_code=401, detail="Your Discord session has expired. Please sign in again.")
            response.raise_for_status()
        except HTTPException:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=502, detail="Discord guild permissions could not be verified. Please try again.") from error

        try:
            guilds = response.json()
        except ValueError as error:
            raise HTTPException(status_code=502, detail="Discord returned an invalid guild response. Please try again.") from error
        if not isinstance(guilds, list):
            raise HTTPException(status_code=502, detail="Discord returned an unexpected guild response. Please try again.")
        result = {str(guild["id"]): guild for guild in guilds if isinstance(guild, dict) and guild.get("id")}
        _OAUTH_GUILD_CACHE[cache_key] = (time.monotonic() + _OAUTH_GUILD_CACHE_TTL, result)
        # Keep process-local state bounded when users sign in over time.
        if len(_OAUTH_GUILD_CACHE) > 512:
            now = time.monotonic()
            for key, (expires_at, _) in list(_OAUTH_GUILD_CACHE.items()):
                if expires_at <= now:
                    _OAUTH_GUILD_CACHE.pop(key, None)
            if len(_OAUTH_GUILD_LOCKS) > 512:
                for key in list(_OAUTH_GUILD_LOCKS):
                    if key not in _OAUTH_GUILD_CACHE and not _OAUTH_GUILD_LOCKS[key].locked():
                        _OAUTH_GUILD_LOCKS.pop(key, None)
        return result


async def authorized_bot_guilds(request: Request, user: dict[str, str]) -> list[dict[str, object]]:
    """Return only bot guilds manageable by the authenticated Owner/Admin."""
    user_guilds = await discord_user_guilds(request, user)
    if not store.bot_is_online():
        return []
    bot_guilds = store.bot_guilds()
    result: list[dict[str, object]] = []
    for guild_id, bot_guild in bot_guilds.items():
        discord_guild = user_guilds.get(guild_id)
        if not discord_guild:
            continue
        can_manage = guild_is_manageable(discord_guild)
        has_support_role = member_has_support_role(guild_id, user["id"])
        if not can_manage:
            continue
        activation = store.activation_for_guild(guild_id)
        result.append(
            {
                **bot_guild,
                "activated": bool(activation and activation["activated"]),
                "activated_at": activation["activated_at"] if activation else None,
                "can_configure": can_manage,
                "has_support_role": has_support_role,
            }
        )
    return result


app = FastAPI(title="BirdBot Dashboard")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET or "configure-a-real-session-secret-before-use",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=bool(DISCORD_REDIRECT_URI and DISCORD_REDIRECT_URI.startswith("https://")),
)
app.mount("/assets", StaticFiles(directory=ROOT_DIR / "website"), name="assets")
app.mount("/uploads/ticket-icons", StaticFiles(directory=TICKET_ICON_DIR), name="ticket-icons")
app.mount("/uploads/bot-profile-avatars", StaticFiles(directory=BOT_PROFILE_AVATAR_DIR), name="bot-profile-avatars")


@app.get("/uploads/transcripts/{filename}")
async def ticket_transcript(filename: str) -> FileResponse:
    """Serve a generated transcript from the host's writable data directory.

    A dedicated route avoids StaticFiles' directory-resolution differences
    between local Windows and Linux hosts and rejects path traversal attempts.
    Ticket transcript names are generated by the bot and contain only a guild
    ID, ticket ID, and a hexadecimal suffix.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.html", filename):
        raise HTTPException(status_code=404, detail="Transcript not found.")
    path = (TRANSCRIPT_DIR / filename).resolve()
    transcript_root = TRANSCRIPT_DIR.resolve()
    if path.parent != transcript_root or not path.is_file():
        raise HTTPException(status_code=404, detail="Transcript not found.")
    return FileResponse(path, media_type="text/html; charset=utf-8")


def local_transcript_url(request: Request, value: object) -> str | None:
    """Rewrite legacy transcript hosts to the host serving this response."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    match = re.fullmatch(r"/uploads/transcripts/([A-Za-z0-9._-]+\.html)", parsed.path)
    if not match:
        return raw
    return str(request.url_for("ticket_transcript", filename=match.group(1)))


@app.get("/")
async def home():
    return FileResponse(ROOT_DIR / "website" / "index.html")


@app.get("/how-to-use")
async def how_to_use():
    """Public bilingual guide for the dashboard and Discord bot."""
    return FileResponse(ROOT_DIR / "website" / "how-to-use.html")


@app.get("/dashboard")
async def dashboard_entry(request: Request):
    """Skip Discord's authorization screen when this browser already has a session."""
    user = request.session.get("discord_user")
    session_id = request.session.get("oauth_session_id")
    if user and store.oauth_tokens(session_id, str(user.get("id", ""))):
        return RedirectResponse("/?portal=1", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/add-bot")
async def add_bot():
    """Open Discord's official server picker to invite this application bot."""
    if not DISCORD_CLIENT_ID:
        raise HTTPException(status_code=503, detail="DISCORD_CLIENT_ID has not been configured.")
    parameters = urlencode(
        {
            "client_id": DISCORD_CLIENT_ID,
            "scope": "bot applications.commands",
            # Keep the dashboard permissions, include voice permissions, and
            # request only the explicit moderation permissions used by the
            # dashboard member actions. Existing installations must be
            # re-authorized once if their role lacks these permissions.
            "permissions": str(
                2147567616
                | (1 << 1) | (1 << 2) | (1 << 4) | (1 << 13)
                | (1 << 10) | (1 << 20) | (1 << 21) | (1 << 22) | (1 << 24)
                | (1 << 40)
            ),
        }
    )
    return RedirectResponse(f"https://discord.com/oauth2/authorize?{parameters}")


@app.get("/support")
async def support():
    if not SUPPORT_URL:
        raise HTTPException(status_code=503, detail="Support contact has not been configured yet.")
    return RedirectResponse(SUPPORT_URL)


@app.get("/login")
async def login(request: Request):
    if not oauth_is_configured():
        raise HTTPException(status_code=503, detail="Discord OAuth settings have not been configured on this server.")
    state = secrets.token_urlsafe(32)
    request.session.clear()
    request.session["oauth_state"] = state
    parameters = urlencode(
        {
            "client_id": DISCORD_CLIENT_ID,
            "redirect_uri": DISCORD_REDIRECT_URI,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        }
    )
    return RedirectResponse(f"https://discord.com/oauth2/authorize?{parameters}")


@app.get("/auth/discord/callback")
async def discord_callback(request: Request, code: str | None = None, state: str | None = None):
    expected_state = request.session.pop("oauth_state", None)
    if not code or not state or not isinstance(expected_state, str) or not secrets.compare_digest(state, expected_state):
        raise HTTPException(status_code=400, detail="Discord login expired or could not be verified. Please try again.")

    token_data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(f"{DISCORD_API}/oauth2/token", data=token_data)
            token_response.raise_for_status()
            access_token = token_response.json()["access_token"]
            user_response = await client.get(
                f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {access_token}"}
            )
            user_response.raise_for_status()
    except (httpx.HTTPError, KeyError) as error:
        raise HTTPException(status_code=502, detail="Discord login could not be completed. Please try again.") from error

    discord_user = user_response.json()
    user = {
        "id": str(discord_user["id"]),
        "name": str(discord_user.get("global_name") or discord_user["username"]),
        "avatar": avatar_url(discord_user),
    }
    oauth_session_id = secrets.token_urlsafe(32)
    store.create_oauth_session(
        oauth_session_id,
        user["id"],
        access_token,
        token_response.json().get("refresh_token"),
        user["name"],
        user["avatar"],
    )
    request.session["discord_user"] = user
    request.session["oauth_session_id"] = oauth_session_id
    # Let the signed-in user choose the management or member music portal.
    return RedirectResponse("/?portal=1", status_code=303)


def spotify_is_configured() -> bool:
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI)


def spotify_premium_enabled(user_id: str) -> bool:
    """Return whether this account may use the optional Spotify feature.

    Payment providers can populate ``SPOTIFY_PREMIUM_USER_IDS`` after a
    successful purchase.  Until then the switch remains opt-in so existing
    deployments are not unexpectedly locked out; direct URL playback is
    always available.
    """
    return not SPOTIFY_PREMIUM_REQUIRED or str(user_id) in SPOTIFY_PREMIUM_USER_IDS


def require_spotify_premium(user_id: str) -> None:
    if not spotify_premium_enabled(user_id):
        raise HTTPException(
            status_code=402,
            detail="Spotify linking is a premium feature. Paste a YouTube or audio URL to play without Spotify.",
        )


def vc_premium_enabled(user_id: str) -> bool:
    """Return whether this account may open the VC presence control panel."""
    return not VC_PREMIUM_REQUIRED or str(user_id) in VC_PREMIUM_USER_IDS


def require_vc_premium(user_id: str) -> None:
    if not vc_premium_enabled(user_id):
        raise HTTPException(
            status_code=402,
            detail="The VC presence control panel is a premium feature. Contact the server owner to enable access.",
        )


def spotify_link_error(response: httpx.Response, *, profile_request: bool = False) -> HTTPException:
    """Translate Spotify's OAuth/API failures into an actionable user error.

    Spotify development-mode apps allow the authorization screen to be shown to
    users who are not on the app allowlist, but the subsequent ``/me`` request
    then returns 403.  Previously that response was collapsed into a generic
    502, which made it look like Render had failed and gave the user no way to
    resolve the problem.
    """
    status = response.status_code
    if status == 403 and profile_request:
        return HTTPException(
            status_code=403,
            detail=(
                "Spotify rejected this account. If the app is in Development "
                "Mode, add this Spotify account in Spotify Developer Dashboard "
                "-> Settings -> Users Management, then link it again."
            ),
        )
    if status == 400:
        return HTTPException(
            status_code=400,
            detail=(
                "Spotify rejected the authorization code. Start linking again; "
                "also verify that the callback URL exactly matches the one in "
                "the Spotify Developer Dashboard."
            ),
        )
    if status in {401, 403}:
        return HTTPException(
            status_code=502,
            detail="Spotify credentials or permissions are not valid for this account.",
        )
    return HTTPException(
        status_code=502,
        detail="Spotify is temporarily unavailable. Please try linking again.",
    )


def spotify_return_path(value: str | None) -> str:
    """Keep OAuth redirects local to this dashboard (never accept an open URL)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/?portal=1"
    return value[:500]


@app.get("/music/spotify/login")
async def spotify_login(request: Request, guild: str | None = None):
    user = logged_in_user(request)
    require_spotify_premium(user["id"])
    if not spotify_is_configured():
        raise HTTPException(status_code=503, detail="Spotify integration is not configured on this server yet.")
    if guild:
        # Spotify is a member-facing feature. Any authenticated member of a
        # bot-connected guild may link their own account; admin permissions
        # are only required for server-management routes.
        await verified_music_guild(guild, request)
        # Return directly to the Music tab after Spotify grants access.
        return_path = f"/?guild={guild}&music=1"
    else:
        return_path = "/?portal=1"
    state = secrets.token_urlsafe(32)
    request.session["spotify_oauth_state"] = state
    request.session["spotify_return_to"] = spotify_return_path(return_path)
    parameters = urlencode(
        {
            "client_id": SPOTIFY_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "scope": SPOTIFY_SCOPES,
            "state": state,
            "show_dialog": "false",
        }
    )
    return RedirectResponse(f"{SPOTIFY_ACCOUNTS_API}/authorize?{parameters}")


@app.get("/auth/spotify/callback")
async def spotify_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    user = logged_in_user(request)
    require_spotify_premium(user["id"])
    expected_state = request.session.pop("spotify_oauth_state", None)
    return_path = spotify_return_path(request.session.pop("spotify_return_to", None))
    if error:
        raise HTTPException(status_code=400, detail="Spotify account linking was cancelled.")
    if not code or not state or not isinstance(expected_state, str) or not secrets.compare_digest(state, expected_state):
        raise HTTPException(status_code=400, detail="Spotify linking expired or could not be verified. Please try again.")
    if not spotify_is_configured():
        raise HTTPException(status_code=503, detail="Spotify integration is not configured on this server yet.")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(
                f"{SPOTIFY_ACCOUNTS_API}/api/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": SPOTIFY_REDIRECT_URI,
                },
                auth=httpx.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            )
            try:
                token_response.raise_for_status()
            except httpx.HTTPStatusError as error_detail:
                raise spotify_link_error(token_response) from error_detail
            token_data = token_response.json()
            if not isinstance(token_data, dict):
                raise ValueError("Invalid Spotify token response")
            access_token = token_data["access_token"]
            me_response = await client.get(
                f"{SPOTIFY_API}/me", headers={"Authorization": f"Bearer {access_token}"}
            )
            try:
                me_response.raise_for_status()
            except httpx.HTTPStatusError as error_detail:
                raise spotify_link_error(me_response, profile_request=True) from error_detail
            profile = me_response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error_detail:
        raise HTTPException(status_code=502, detail="Spotify account linking could not be completed. Please try again.") from error_detail
    if not isinstance(profile, dict):
        raise HTTPException(status_code=502, detail="Spotify returned an invalid profile response.")
    # Spotify now recommends account_id for account linking because it is
    # immutable. Keep the legacy id as a fallback for older responses.
    spotify_account_id = str(profile.get("account_id") or profile.get("id") or "")
    if not spotify_account_id:
        raise HTTPException(status_code=502, detail="Spotify returned an invalid profile response.")
    try:
        expires_in = float(token_data.get("expires_in") or 3_600)
    except (TypeError, ValueError):
        expires_in = 3_600
    store.save_spotify_account(
        user["id"],
        spotify_account_id,
        str(profile.get("display_name") or spotify_account_id or "Spotify user"),
        str(access_token),
        token_data.get("refresh_token"),
        time.time() + expires_in,
        str(token_data.get("scope") or SPOTIFY_SCOPES),
    )
    return RedirectResponse(return_path, status_code=303)


async def spotify_access_token(user_id: str, *, force_refresh: bool = False) -> str:
    account = store.spotify_account(user_id)
    if not account:
        raise HTTPException(status_code=409, detail="Link your Spotify account first.")
    access_token = str(account.get("access_token") or "")
    try:
        expires_at = float(account.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if access_token and not force_refresh and expires_at > time.time() + 60:
        return access_token
    token_before_lock = access_token
    refresh_token = str(account.get("refresh_token") or "")
    if not refresh_token:
        raise HTTPException(status_code=409, detail="Your Spotify link expired. Link your account again.")
    if not spotify_is_configured():
        raise HTTPException(status_code=503, detail="Spotify integration is not configured on this server yet.")
    async with spotify_refresh_lock(user_id):
        # Another concurrent request may have refreshed the account while we
        # waited for the lock. Re-read the row before making another request.
        account = store.spotify_account(user_id) or account
        access_token = str(account.get("access_token") or "")
        try:
            expires_at = float(account.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        # A forced refresh is still skipped when the token changed while this
        # coroutine waited for the per-user lock; otherwise three parallel
        # overview calls would rotate the same refresh token unnecessarily.
        if access_token and expires_at > time.time() + 60 and (
            not force_refresh or access_token != token_before_lock
        ):
            return access_token
        refresh_token = str(account.get("refresh_token") or refresh_token)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(
                        f"{SPOTIFY_ACCOUNTS_API}/api/token",
                        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                        auth=httpx.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
                    )
                    if response.status_code == 429 and attempt < 2:
                        raw_retry_after = response.headers.get("Retry-After", "")
                        try:
                            retry_after = float(raw_retry_after)
                        except (TypeError, ValueError):
                            retry_after = 0.5 * (2 ** attempt)
                        await asyncio.sleep(max(0.25, min(retry_after, 8.0)))
                        continue
                    if response.status_code >= 500 and attempt < 2:
                        await asyncio.sleep(0.35 * (2 ** attempt))
                        continue
                    response.raise_for_status()
                    data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Spotify returned an invalid token response.")
                refreshed_token = str(data.get("access_token") or "")
                if not refreshed_token:
                    raise ValueError("Spotify returned an invalid token response.")
                try:
                    expires_in = float(data.get("expires_in") or 3_600)
                except (TypeError, ValueError):
                    expires_in = 3_600
                store.update_spotify_access_token(
                    user_id,
                    refreshed_token,
                    time.time() + expires_in,
                    data.get("refresh_token"),
                )
                return refreshed_token
            except httpx.HTTPStatusError as error_detail:
                last_error = error_detail
                if error_detail.response.status_code in {400, 401, 403}:
                    break
            except (httpx.HTTPError, TypeError, ValueError, KeyError) as error_detail:
                last_error = error_detail
            if attempt < 2:
                await asyncio.sleep(0.35 * (2 ** attempt))
        raise HTTPException(status_code=502, detail="Spotify token refresh failed. Link your account again.") from last_error


async def spotify_request(user_id: str, path: str, params: dict[str, object] | None = None, *, retry: bool = True) -> dict[str, object]:
    """Call Spotify with token refresh and bounded 429/network backoff."""
    refreshed = False
    last_error: Exception | None = None
    for attempt in range(3):
        token = await spotify_access_token(user_id, force_refresh=refreshed)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{SPOTIFY_API}/{path.lstrip('/')}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 401 and retry and not refreshed:
                    refreshed = True
                    continue
                if response.status_code == 429 and attempt < 2:
                    raw_retry_after = response.headers.get("Retry-After", "")
                    try:
                        retry_after = float(raw_retry_after)
                    except (TypeError, ValueError):
                        retry_after = 0.5 * (2 ** attempt)
                    await asyncio.sleep(max(0.25, min(retry_after, 8.0)))
                    continue
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as error_detail:
            last_error = error_detail
            detail = "Spotify could not fulfil that request."
            status_code = error_detail.response.status_code
            if status_code == 403:
                detail = "Spotify denied access to that data. Check the linked account permissions."
            elif status_code == 429:
                retry_after = error_detail.response.headers.get("Retry-After")
                detail = "Spotify is temporarily rate limited. Please try again in a moment."
                if retry_after:
                    detail = f"Spotify is temporarily rate limited. Try again in about {retry_after} seconds."
            elif status_code in {404, 410}:
                detail = "That Spotify resource is no longer available."
            raise HTTPException(status_code=502, detail=detail) from error_detail
        except (httpx.HTTPError, ValueError) as error_detail:
            last_error = error_detail
            if attempt < 2:
                await asyncio.sleep(0.35 * (2 ** attempt))
                continue
            raise HTTPException(status_code=502, detail="Spotify is temporarily unavailable. Please try again.") from error_detail
        if not isinstance(data, dict):
            raise HTTPException(status_code=502, detail="Spotify returned an unexpected response.")
        return data
    raise HTTPException(status_code=502, detail="Spotify is temporarily unavailable. Please try again.") from last_error


def spotify_image(item: dict[str, object]) -> str | None:
    images = item.get("images")
    if isinstance(images, list) and images and isinstance(images[0], dict):
        url = images[0].get("url")
        return str(url) if url else None
    album = item.get("album")
    if isinstance(album, dict):
        return spotify_image(album)
    return None


def normalize_spotify_track(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    track = item.get("track") if isinstance(item.get("track"), dict) else item
    if not isinstance(track, dict) or not track.get("id"):
        return None
    artists = track.get("artists")
    artist_names = [str(artist.get("name")) for artist in artists if isinstance(artist, dict) and artist.get("name")] if isinstance(artists, list) else []
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    return {
        "id": str(track["id"]),
        "name": str(track.get("name") or "Unknown track"),
        "artist": ", ".join(artist_names) or "Unknown artist",
        "album": str(album.get("name") or ""),
        "image_url": spotify_image(track),
        "duration_ms": int(track.get("duration_ms") or 0),
        "uri": str(track.get("uri") or ""),
        # Spotify may expose a short preview when a full YouTube source is
        # unavailable. Keep this URL server-side in the queued metadata so
        # the player can use it as a safe, time-limited fallback.
        "preview_url": str(track.get("preview_url") or ""),
    }


def normalize_spotify_playlist(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict) or not item.get("id"):
        return None
    return {"id": str(item["id"]), "name": str(item.get("name") or "Untitled playlist"), "image_url": spotify_image(item), "track_count": int((item.get("tracks") or {}).get("total") or 0) if isinstance(item.get("tracks"), dict) else 0}


def normalize_spotify_album(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict) or not item.get("id"):
        item = item.get("album") if isinstance(item, dict) else None
    if not isinstance(item, dict) or not item.get("id"):
        return None
    artists = item.get("artists")
    artist = ", ".join(str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")) if isinstance(artists, list) else ""
    return {"id": str(item["id"]), "name": str(item.get("name") or "Untitled album"), "artist": artist, "image_url": spotify_image(item)}


async def spotify_playlist_items(user_id: str, playlist_id: str, *, max_tracks: int = 10_000) -> list[object]:
    """Fetch playlist pages so bulk play queues the complete playlist."""
    items: list[object] = []
    offset = 0
    while len(items) < max_tracks:
        page = await spotify_request(user_id, f"playlists/{playlist_id}/tracks", {"limit": 100, "offset": offset})
        page_items = page.get("items") if isinstance(page, dict) else []
        if not isinstance(page_items, list) or not page_items:
            break
        items.extend(page_items)
        total = int(page.get("total") or len(items)) if isinstance(page, dict) else len(items)
        offset += len(page_items)
        if offset >= total or len(page_items) < 100:
            break
    return items[:max_tracks]


async def verified_music_member(guild_id: str, request: Request, *, fresh: bool = False) -> tuple[dict[str, str], dict[str, object]]:
    """Authorize a member-facing music request without requiring admin rights."""
    if not DISCORD_SNOWFLAKE.fullmatch(guild_id):
        raise HTTPException(status_code=400, detail="The server identifier is invalid.")
    user = logged_in_user(request)
    user_guilds = await discord_user_guilds(request, user, fresh=fresh)
    if not store.bot_is_online():
        raise HTTPException(status_code=404, detail="BirdBot is not currently available in that server.")
    if guild_id not in store.bot_guilds():
        raise HTTPException(status_code=404, detail="BirdBot is not currently available in that server.")
    if guild_id not in user_guilds:
        raise HTTPException(status_code=403, detail="You must be a member of that server to use its Music system.")
    return user, store.bot_guilds()[guild_id]


async def verified_music_guild(guild_id: str, request: Request, *, fresh: bool = False) -> tuple[dict[str, str], dict[str, object]]:
    # Read-only playlists and member portal navigation are available to every
    # authenticated member. Player mutations still enforce activation below.
    return await verified_music_member(guild_id, request, fresh=fresh)


@app.post("/api/spotify/unlink")
async def unlink_spotify(request: Request) -> dict[str, bool]:
    user = logged_in_user(request)
    store.delete_spotify_account(user["id"])
    return {"ok": True}


@app.get("/api/guilds/{guild_id}/music")
async def music_overview(guild_id: str, request: Request) -> dict[str, object]:
    user, bot_guild = await verified_music_guild(guild_id, request)
    bot_guild = {**bot_guild, "activated": store.is_guild_activated(guild_id)}
    account = store.spotify_account(user["id"])
    state = store.music_state(guild_id)
    spotify_allowed = spotify_premium_enabled(user["id"])
    if not spotify_allowed:
        return {
            "linked": False,
            "spotify_configured": spotify_is_configured(),
            "spotify_premium": False,
            "spotify_premium_required": True,
            "guild": bot_guild,
            "state": state,
            "playlists": [],
            "albums": [],
            "top_tracks": [],
        }
    if not account:
        return {"linked": False, "spotify_configured": spotify_is_configured(), "spotify_premium": True, "spotify_premium_required": False, "guild": bot_guild, "state": state, "playlists": [], "albums": [], "top_tracks": []}
    results = await asyncio.gather(
        spotify_request(user["id"], "me/playlists", {"limit": 50}),
        spotify_request(user["id"], "me/albums", {"limit": 50}),
        spotify_request(user["id"], "me/top/tracks", {"limit": 20, "time_range": "medium_term"}),
        return_exceptions=True,
    )
    playlists_data, albums_data, top_tracks_data = results
    # Keep the overview usable when one Spotify endpoint is rate-limited or
    # temporarily unavailable.  The dashboard can show these warnings while
    # still rendering any sections that did load successfully.
    warnings: list[str] = []
    for label, result in (
        ("playlists", playlists_data),
        ("saved albums", albums_data),
        ("top tracks", top_tracks_data),
    ):
        if isinstance(result, Exception):
            detail = result.detail if isinstance(result, HTTPException) else None
            warnings.append(str(detail or f"Spotify {label} could not be loaded."))
    playlists = [value for item in (playlists_data.get("items", []) if isinstance(playlists_data, dict) else []) if (value := normalize_spotify_playlist(item))]
    albums = [value for item in (albums_data.get("items", []) if isinstance(albums_data, dict) else []) if (value := normalize_spotify_album(item))]
    top_tracks = [value for item in (top_tracks_data.get("items", []) if isinstance(top_tracks_data, dict) else []) if (value := normalize_spotify_track(item))]
    return {
        "linked": True,
        "spotify_configured": True,
        "spotify_premium": True,
        "spotify_premium_required": False,
        "guild": bot_guild,
        "spotify": {"id": str(account.get("spotify_user_id") or ""), "name": str(account.get("display_name") or "Spotify user")},
        "state": state,
        "warnings": warnings,
        "playlists": playlists,
        "albums": albums,
        "top_tracks": top_tracks,
    }


@app.get("/api/guilds/{guild_id}/music/playlists/{playlist_id}/tracks")
async def music_playlist_tracks(guild_id: str, playlist_id: str, request: Request) -> dict[str, object]:
    user, _ = await verified_music_guild(guild_id, request)
    require_spotify_premium(user["id"])
    if not SPOTIFY_ID.fullmatch(playlist_id):
        raise HTTPException(status_code=400, detail="The Spotify playlist identifier is invalid.")
    try:
        items = await asyncio.wait_for(spotify_playlist_items(user["id"], playlist_id), timeout=30)
    except asyncio.TimeoutError as error:
        raise HTTPException(status_code=504, detail="Spotify took too long to load this playlist. Try again shortly.") from error
    tracks = [value for item in items if (value := normalize_spotify_track(item))]
    return {"tracks": tracks}


@app.get("/api/guilds/{guild_id}/music/search")
async def music_search(guild_id: str, request: Request, q: str = "") -> dict[str, object]:
    user, _ = await verified_music_guild(guild_id, request)
    require_spotify_premium(user["id"])
    query = q.strip()[:100]
    if len(query) < 2:
        return {"tracks": []}
    data = await spotify_request(
        user["id"],
        "search",
        {"q": query, "type": "track", "limit": 20, "market": "from_token"},
    )
    tracks_data = data.get("tracks") if isinstance(data.get("tracks"), dict) else {}
    tracks = [value for item in (tracks_data.get("items", []) if isinstance(tracks_data, dict) else []) if (value := normalize_spotify_track(item))]
    return {"tracks": tracks}


@app.get("/api/guilds/{guild_id}/music/state")
async def music_state(guild_id: str, request: Request) -> dict[str, object]:
    await verified_music_guild(guild_id, request)
    return {"state": store.music_state(guild_id)}


@app.websocket("/ws/guilds/{guild_id}/music")
async def music_state_socket(websocket: WebSocket, guild_id: str) -> None:
    """Push live player state to a music dashboard without another bot client.

    The websocket reads the same SQLite-backed state that the one global bot
    worker publishes.  It therefore cannot create a duplicate Discord
    connection, and the short server-side cadence keeps progress/queue/voice
    state in sync while the HTTP state endpoint remains a fallback.
    """
    await websocket.accept()
    if not DISCORD_SNOWFLAKE.fullmatch(guild_id):
        await websocket.close(code=4400, reason="Invalid server identifier")
        return
    session = websocket.scope.get("session") or {}
    user = session.get("discord_user")
    if not isinstance(user, dict) or not user.get("id"):
        await websocket.close(code=4401, reason="Sign in with Discord first")
        return
    # Reuse the OAuth verifier used by normal HTTP routes.  Starlette's
    # SessionMiddleware places the same mutable session mapping on websocket
    # scopes, so this tiny adapter keeps token refresh/expiry behaviour
    # consistent without fabricating a second authentication mechanism.
    session_request = type("WebSocketSessionRequest", (), {"session": session})()
    try:
        user_guilds = await discord_user_guilds(session_request, user)
        if not store.bot_is_online() or guild_id not in store.bot_guilds():
            await websocket.close(code=4404, reason="BirdBot is not available in that server")
            return
        if guild_id not in user_guilds:
            await websocket.close(code=4403, reason="You must be a member of that server")
            return
        heartbeat_checked_at = 0.0
        bot_online = True
        while True:
            now = time.monotonic()
            if now - heartbeat_checked_at >= 2.0:
                bot_online = store.bot_is_online()
                heartbeat_checked_at = now
            state = store.music_state(guild_id)
            if not bot_online:
                state = {
                    **state,
                    "connected": False,
                    "connection_state": "reconnecting",
                    "voice_channel_id": None,
                    "voice_channel_name": None,
                    "last_error": "BirdBot is offline. Reconnecting...",
                }
            await websocket.send_json(
                {
                    "type": "music_state",
                    "state": state,
                    "server_time": time.time(),
                }
            )
            # Player state changes do not need four database reads per second;
            # a one-second cadence keeps controls responsive while reducing
            # per-connection CPU and SQLite work on small hosts.
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    except (HTTPException, httpx.HTTPError, RuntimeError, TypeError, ValueError):
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="Music state is temporarily unavailable")


@app.post("/api/guilds/{guild_id}/music/{action}")
async def music_action(guild_id: str, action: str, request: Request) -> dict[str, str]:
    user, _ = await verified_music_guild(guild_id, request, fresh=True)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before using the Music system.")
    allowed = {"start", "queue", "play", "playlist", "pause", "resume", "skip", "previous", "shuffle", "loop", "seek", "volume", "volume_up", "volume_down", "stop"}
    if action not in allowed:
        raise HTTPException(status_code=404, detail="That music action is not available.")
    try:
        payload = await request.json()
    except ValueError as error_detail:
        raise HTTPException(status_code=400, detail="Invalid music action data.") from error_detail
    if not isinstance(payload, dict):
        payload = {}
    if action in {"queue", "play"}:
        # URL playback deliberately does not require a linked Spotify
        # account.  yt-dlp resolves YouTube and its other supported public
        # providers inside the bot process, then the existing player controls
        # (pause, skip, shuffle, loop, seek, volume) apply unchanged.
        if "url" in payload or "source_url" in payload:
            track = external_music_track(payload.get("url") or payload.get("source_url"))
        else:
            require_spotify_premium(user["id"])
            track_id = payload.get("track_id")
            if not isinstance(track_id, str) or not SPOTIFY_ID.fullmatch(track_id):
                raise HTTPException(status_code=400, detail="Choose a valid Spotify track or paste an audio URL.")
            # Ask Spotify to resolve the track in the linked user's market.
            # This matters for search results outside their playlists: a track
            # can be globally searchable but unavailable in the default
            # market, which otherwise makes the dashboard report a misleading
            # playback error.
            track_data = await spotify_request(user["id"], f"tracks/{track_id}", {"market": "from_token"})
            track = normalize_spotify_track(track_data)
            if not track:
                raise HTTPException(status_code=404, detail="That Spotify track could not be found.")
        payload = {"track": track}
    elif action == "playlist":
        playlist_id = payload.get("playlist_id")
        if not isinstance(playlist_id, str) or not SPOTIFY_ID.fullmatch(playlist_id):
            raise HTTPException(status_code=400, detail="Choose a valid Spotify playlist.")
        try:
            playlist_items = await asyncio.wait_for(spotify_playlist_items(user["id"], playlist_id), timeout=30)
        except asyncio.TimeoutError as error:
            raise HTTPException(status_code=504, detail="Spotify took too long to load this playlist. Try again shortly.") from error
        tracks = [value for item in playlist_items if (value := normalize_spotify_track(item))]
        if not tracks:
            raise HTTPException(status_code=404, detail="That playlist has no playable tracks.")
        payload = {"tracks": tracks}
    elif action == "seek":
        try:
            seconds = float(payload.get("seconds", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Seek time must be a number of seconds.") from None
        if not -600 <= seconds <= 600:
            raise HTTPException(status_code=400, detail="Seek time must be between -600 and 600 seconds.")
        payload = {"seconds": seconds}
    elif action == "volume":
        try:
            volume = float(payload.get("volume", 1))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Volume must be between 0 and 100.") from None
        if not 0 <= volume <= 100:
            raise HTTPException(status_code=400, detail="Volume must be between 0 and 100.")
        payload = {"volume": volume / 100}
    request_id = store.queue_command(guild_id, "0", f"music_{action}", user["id"], payload)
    return {"request_id": request_id, "status": "pending"}


@app.get("/api/music/guilds")
async def music_guilds(request: Request) -> dict[str, object]:
    """List bot-connected guilds for any authenticated member-facing portal."""
    user = logged_in_user(request)
    user_guilds = await discord_user_guilds(request, user)
    if not store.bot_is_online():
        return {"guilds": [], "bot_online": False}
    result = []
    for guild_id, bot_guild in store.bot_guilds().items():
        if guild_id not in user_guilds:
            continue
        activation = store.activation_for_guild(guild_id)
        result.append({**bot_guild, "activated": bool(activation and activation["activated"])})
    return {"guilds": result, "bot_online": True}


@app.get("/api/session")
async def session_status(request: Request) -> dict[str, object]:
    user = request.session.get("discord_user")
    return {"authenticated": bool(user), "user": user}


@app.post("/logout")
async def logout(request: Request):
    session_id = request.session.get("oauth_session_id")
    user = request.session.get("discord_user") or {}
    _OAUTH_GUILD_CACHE.pop(f"{session_id}:{user.get('id', '')}", None)
    store.delete_oauth_session(session_id)
    request.session.clear()
    return {"ok": True}


@app.get("/api/dashboard")
async def dashboard(request: Request) -> dict[str, object]:
    user = logged_in_user(request)
    guilds = await authorized_bot_guilds(request, user)
    if not guilds:
        # There is no management view for a member-only account.  Check the
        # OAuth guild permissions even when the bot heartbeat is offline so a
        # direct ``?dashboard=1`` URL cannot bypass the portal choice screen.
        user_guilds = await discord_user_guilds(request, user)
        if not any(guild_is_manageable(guild) for guild in user_guilds.values()):
            raise HTTPException(status_code=403, detail="Access Denied: Administrator permissions required")
    return {"user": user, "bot_online": store.bot_is_online(), "guilds": guilds}


@app.get("/api/guilds/{guild_id}/manage")
async def manage_guild(guild_id: str, request: Request) -> dict[str, object]:
    """Return management data for the authenticated guild owner/admin only.

    The dashboard portal is deliberately separate from the member-facing
    Music portal.  Support-role staff can still use the Discord ticket
    controls, but they must not enter the administrative dashboard.
    """
    user, bot_guild = await verified_guild_manager(guild_id, request)
    activation = store.activation_for_guild(guild_id)
    if not activation or not activation["activated"]:
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing it.")
    commands = [
        {
            "name": "ping", "label": "/ping", "category": "General",
            "description": "Check BirdBot's connection and uptime.",
            "usage": "/ping", "details": "A quick health check that returns the bot's response time.",
            "options": ["No options required."], "requirements": "Available in any text channel.",
        },
        {
            "name": "server", "label": "/server", "category": "General",
            "description": "Show server information.",
            "usage": "/server", "details": "Displays this server's owner, member count, boost level, and other overview details.",
            "options": ["No options required."], "requirements": "Available in any text channel.",
        },
        {
            "name": "profile", "label": "/profile", "category": "General",
            "description": "Show a member profile.",
            "usage": "/profile <member>", "details": "Open a member summary with their ID, join date, roles, and profile information.",
            "options": ["Search by username or display name.", "Choose a member from the server list."],
            "requirements": "Requires a server member to be selected.",
        },
        {
            "name": "kick", "label": "/kick", "category": "Moderation",
            "description": "Remove a member from the server.",
            "usage": "/kick <member> [reason]", "details": "Kick a member while keeping their ban history clear so they can rejoin later.",
            "options": ["Target member is required.", "Reason is optional and included in the moderation log."],
            "requirements": "Requires Kick Members permission.",
        },
        {
            "name": "ban", "label": "/ban", "category": "Moderation",
            "description": "Ban a member from the server.",
            "usage": "/ban <member> [reason] [delete days]", "details": "Ban a member and optionally remove their recent message history.",
            "options": ["Target member is required.", "Choose 0–7 days of message history to remove.", "Reason is optional and included in the moderation log."],
            "requirements": "Requires Ban Members permission.",
        },
        {
            "name": "warning", "label": "/warning", "category": "Moderation",
            "description": "Give a numbered warning to a member.",
            "usage": "/warning <member> <reason>", "details": "Create a permanent warning record with a unique number and notify the member when possible.",
            "options": ["Target member is required.", "A clear reason is recommended."],
            "requirements": "Requires the server's warning moderation permission.",
        },
        {
            "name": "unwarning", "label": "/unwarning", "category": "Moderation",
            "description": "Remove a warning by its unique number.",
            "usage": "/unwarning <warning number>", "details": "Delete exactly one warning record by its numbered ID.",
            "options": ["Enter the warning number shown by /show warning."],
            "requirements": "The warning number must belong to this server.",
        },
        {
            "name": "show_warning", "label": "/show warning", "category": "Moderation",
            "description": "View a member's active warnings.",
            "usage": "/show warning <member>", "details": "Show an embed containing the warning count, numbers, reasons, and timestamps.",
            "options": ["Search and choose the member to inspect."],
            "requirements": "Requires a server member to be selected.",
        },
        {
            "name": "timeout", "label": "/timeout", "category": "Moderation",
            "description": "Temporarily timeout a member.",
            "usage": "/timeout <member> <duration> [reason]", "details": "Prevent a member from communicating for a chosen period without removing them.",
            "options": ["Use minutes, hours, days, or weeks (for example 10m, 2h, 1d, or 1w; maximum 28 days).", "Reason is optional and included in the moderation log."],
            "requirements": "Requires Moderate Members permission.",
        },
        {
            "name": "untimeout", "label": "/untimeout", "category": "Moderation",
            "description": "Remove a member's active timeout.",
            "usage": "/untimeout <member> [reason]", "details": "Restore a member's ability to communicate before their timeout expires.",
            "options": ["Choose a member with an active timeout.", "Reason is optional and included in the moderation log."],
            "requirements": "Requires Moderate Members permission.",
        },
        {
            "name": "mute", "label": "/mute", "category": "Moderation",
            "description": "Server-mute a member in voice.",
            "usage": "/mute <member> [reason]", "details": "Apply a server voice mute to the selected member.",
            "options": ["Target member is required.", "Reason is optional."],
            "requirements": "Requires Mute Members permission and a member in voice.",
        },
        {
            "name": "unmute", "label": "/unmute", "category": "Moderation",
            "description": "Remove a server voice mute.",
            "usage": "/unmute <member> [reason]", "details": "Remove BirdBot's server mute while leaving the member in their current voice channel.",
            "options": ["Choose a member who is currently server-muted in voice.", "Reason is optional and included in the moderation log."],
            "requirements": "Requires Mute Members permission and a member in voice.",
        },
        {
            "name": "lock", "label": "/lock", "category": "Moderation",
            "description": "Lock the selected channel for @everyone.",
            "usage": "/lock", "details": "Remove the send-message permission for @everyone in the selected text channel.",
            "options": ["Choose the text channel to lock."],
            "requirements": "Requires Manage Channels permission.",
        },
        {
            "name": "unlock", "label": "/unlock", "category": "Moderation",
            "description": "Unlock the selected channel for @everyone.",
            "usage": "/unlock", "details": "Restore the send-message permission for @everyone in the selected text channel.",
            "options": ["Choose the text channel to unlock."],
            "requirements": "Requires Manage Channels permission.",
        },
        {
            "name": "delete", "label": "/delete", "category": "Moderation",
            "description": "Delete recent messages from a channel.",
            "usage": "/delete <amount>", "details": "Bulk-delete recent messages from the selected text channel.",
            "options": ["Choose 1–100 messages."],
            "requirements": "Requires Manage Messages permission.",
        },
        {
            "name": "play", "label": "/play", "category": "Music",
            "description": "Play or queue a YouTube link or music name.",
            "usage": "/play <YouTube link or music name>", "details": "Start a track immediately when idle; when music is already playing, add it to the queue.",
            "options": ["Paste a YouTube link or type a search name.", "Join a voice channel before running the command."],
            "requirements": "Uses the requesting member's current voice channel.",
        },
        {
            "name": "q", "label": "/q", "category": "Music",
            "description": "Add a YouTube link or music name to the queue.",
            "usage": "/q <YouTube link or music name>", "details": "Resolve a track and place it after the currently playing music.",
            "options": ["Paste a YouTube link or type a search name.", "Join a voice channel before running the command."],
            "requirements": "Uses the requesting member's current voice channel.",
        },
        {
            "name": "pause", "label": "/pause", "category": "Music",
            "description": "Pause the currently playing track.",
            "usage": "/pause", "details": "Pause playback without clearing the current queue.",
            "options": ["Run again through the music controls to resume."],
            "requirements": "BirdBot must be connected to your voice channel.",
        },
        {
            "name": "skip", "label": "/skip", "category": "Music",
            "description": "Skip the currently playing track.",
            "usage": "/skip", "details": "Stop the current track and continue with the next queued track.",
            "options": ["If the queue is empty, playback ends."],
            "requirements": "BirdBot must be connected to your voice channel.",
        },
        {
            "name": "stop", "label": "/stop", "category": "Music",
            "description": "Stop music, clear the queue, and leave voice chat.",
            "usage": "/stop", "details": "End the current session and remove all tracks waiting in the queue.",
            "options": ["Use /play again to start a new session."],
            "requirements": "BirdBot must be connected to your voice channel.",
        },
    ]
    return {
        "guild": {**bot_guild, "activated": True},
        "profile": public_bot_profile(store.bot_profile(guild_id)),
        "channels": store.bot_text_channels(guild_id),
        "voice_channels": store.bot_voice_channels(guild_id),
        "categories": store.bot_categories(guild_id),
        "roles": store.bot_roles(guild_id),
        "role_permission_fields": [
            {"key": key, "label": label, "description": description}
            for key, label, description in ROLE_PERMISSION_FIELDS
        ],
        # Keep the initial management payload small. The member picker searches
        # the complete roster through /members/search when needed.
        "members": store.bot_members(guild_id, limit=100),
        "bans": store.bot_bans(guild_id),
        "commands": commands,
        "command_settings": store.command_settings(guild_id),
        "log_config": store.log_config(guild_id),
        "auto_reacts": store.auto_reacts(guild_id),
        "automod": store.automod_config(guild_id),
        "ai": store.ai_config(guild_id),
        # Expose readiness only; never return the provider key to the browser.
        "ai_available": bool(GROQ_API_KEY),
        "temp_vc": store.temp_vc_config(guild_id),
        "temp_vc_channels": store.temp_vc_channels(guild_id),
        # Placement rows contain no bot credentials. Live online/voice state
        # is fetched from the dedicated VC endpoint when the card opens.
        "vc_presence": store.vc_presence_configs(guild_id, VC_BOT_SLOT_COUNT),
        "vc_premium": vc_premium_enabled(user["id"]),
    }


def parse_command_settings(payload: object) -> tuple[str, bool, dict[str, dict[str, object]]]:
    """Validate and normalize the Commands tab payload before persistence."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid command settings.")
    raw_prefix = payload.get("prefix")
    if not isinstance(raw_prefix, str):
        raise HTTPException(status_code=400, detail="Enter a command prefix.")
    # Discord.py accepts arbitrary non-empty prefix strings, including spaces,
    # emoji, and punctuation. Preserve the exact text the manager entered so
    # the gateway and the dashboard display/use the same value.
    prefix = raw_prefix
    if not prefix:
        raise HTTPException(status_code=400, detail="Enter at least one character for the prefix.")
    prefix_enabled = bool(payload.get("prefix_enabled", True))
    raw_commands = payload.get("commands")
    if not isinstance(raw_commands, dict):
        raise HTTPException(status_code=400, detail="Command settings are missing.")
    normalized: dict[str, dict[str, object]] = {}
    seen_shortcuts: set[str] = set()
    command_names = set(DASHBOARD_COMMAND_NAMES)
    for command_name in DASHBOARD_COMMAND_NAMES:
        raw_config = raw_commands.get(command_name, {})
        if not isinstance(raw_config, dict):
            raw_config = {}
        language = str(raw_config.get("language") or "en").casefold()
        if language not in {"en", "ar"}:
            raise HTTPException(status_code=400, detail=f"Choose English or Arabic for {command_name}.")
        raw_shortcuts = raw_config.get("shortcuts", [])
        if isinstance(raw_shortcuts, str):
            raw_shortcuts = raw_shortcuts.split(",")
        if not isinstance(raw_shortcuts, list) or len(raw_shortcuts) > 10:
            raise HTTPException(status_code=400, detail=f"Use up to 10 shortcuts for {command_name}.")
        shortcuts: list[str] = []
        for raw_shortcut in raw_shortcuts:
            if not isinstance(raw_shortcut, str):
                raise HTTPException(status_code=400, detail=f"Shortcuts for {command_name} must be text.")
            shortcut = raw_shortcut.strip()
            if not shortcut:
                continue
            normalized_shortcut = shortcut.casefold()
            if not 1 <= len(shortcut) <= 32 or any(character.isspace() for character in shortcut):
                raise HTTPException(status_code=400, detail=f"Each {command_name} shortcut must be 1-32 characters, without spaces.")
            if normalized_shortcut in command_names or normalized_shortcut in seen_shortcuts:
                raise HTTPException(status_code=400, detail=f"The shortcut '{shortcut}' is already used by another command.")
            seen_shortcuts.add(normalized_shortcut)
            shortcuts.append(shortcut)
        normalized[command_name] = {
            "enabled": bool(raw_config.get("enabled", True)),
            "language": language,
            "shortcuts": shortcuts,
        }
    return prefix, prefix_enabled, normalized


@app.post("/api/guilds/{guild_id}/control/commands/settings")
async def save_command_settings(guild_id: str, request: Request) -> dict[str, object]:
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before changing command settings.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid command settings.") from error
    prefix, prefix_enabled, commands = parse_command_settings(payload)
    settings = store.save_command_settings(guild_id, prefix, prefix_enabled, commands, str(user["id"]))
    return {"command_settings": settings, "status": "saved"}


@app.get("/api/guilds/{guild_id}/logs")
async def guild_logs(guild_id: str, request: Request, q: str = "") -> dict[str, object]:
    """Return general server activity logs and the current log configuration."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing logs.")
    return {"logs": store.logs(guild_id, q[:120], limit=500), "log_config": store.log_config(guild_id)}


@app.post("/api/guilds/{guild_id}/control/logs")
async def save_guild_log_config(guild_id: str, request: Request) -> dict[str, object]:
    """Validate and persist the destination/categories for general logs."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before changing log settings.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid log settings.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid log settings.")
    current = store.log_config(guild_id)
    current_categories = current.get("categories") if isinstance(current.get("categories"), dict) else {}
    current_destinations = current.get("category_channels") if isinstance(current.get("category_channels"), dict) else {}
    raw_categories = payload.get("categories")
    if raw_categories is None:
        raw_categories = current_categories
    if not isinstance(raw_categories, dict):
        raise HTTPException(status_code=400, detail="Invalid log categories.")
    categories = {key: bool(raw_categories.get(key, current_categories.get(key, True))) for key in LOG_CATEGORIES}
    available_channels = {str(channel.get("id")) for channel in store.bot_text_channels(guild_id)}
    raw_category_channels = payload.get("category_channels")
    legacy_channel = payload.get("log_channel_id")
    if raw_category_channels is None:
        # Accept the previous single-channel payload while clients update.
        if legacy_channel not in (None, ""):
            raw_category_channels = {key: legacy_channel for key in LOG_CATEGORIES}
        else:
            raw_category_channels = current_destinations
    if not isinstance(raw_category_channels, dict):
        raise HTTPException(status_code=400, detail="Invalid log channel settings.")
    category_channels: dict[str, str | None] = {}
    for key in LOG_CATEGORIES:
        value = raw_category_channels.get(key)
        if value in (None, ""):
            category_channels[key] = None
        elif isinstance(value, str) and DISCORD_SNOWFLAKE.fullmatch(value) and value in available_channels:
            category_channels[key] = value
        elif isinstance(value, str) and DISCORD_SNOWFLAKE.fullmatch(value):
            raise HTTPException(status_code=400, detail="Choose a text channel BirdBot can access.")
        else:
            raise HTTPException(status_code=400, detail="Choose valid log channels.")
    if bool(payload.get("enabled", False)):
        missing = [key for key in LOG_CATEGORIES if categories[key] and not category_channels[key]]
        if missing:
            raise HTTPException(status_code=400, detail="Choose a channel for every enabled log type.")
    config = store.save_log_config(
        guild_id,
        bool(payload.get("enabled", False)),
        None,
        categories,
        str(user["id"]),
        category_channels=category_channels,
    )
    return {"log_config": config, "status": "saved"}


def spy_game_dashboard_config(guild_id: str | None = None) -> dict[str, object]:
    """Read the small, non-secret Spy Game branding/configuration manifest."""
    path = ROOT_DIR / "config" / "games" / "spy.config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # Keep local filesystem paths private while exposing browser-safe asset URLs.
    assets: dict[str, object] = {}
    for key, fallback in (("bannerPath", "/assets/games/spy_banner.svg"), ("iconPath", "/assets/games/spy_icon.svg")):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            relative = raw.replace("\\", "/").lstrip("./")
            if relative.startswith("assets/"):
                assets[key] = f"/assets/{relative.removeprefix('assets/')}"
            else:
                assets[key] = fallback
        else:
            assets[key] = fallback
    locations = payload.get("locations")
    if not isinstance(locations, list):
        locations = []
    try:
        minimum_players = max(3, min(int(payload.get("minimumPlayers") or 3), 50))
    except (TypeError, ValueError):
        minimum_players = 3
    try:
        maximum_players = max(minimum_players, min(int(payload.get("maximumPlayers") or 20), 50))
    except (TypeError, ValueError):
        maximum_players = 20
    try:
        question_timer_seconds = max(5, min(int(payload.get("questionTimerSeconds") or 30), 600))
    except (TypeError, ValueError):
        question_timer_seconds = 30
    end_mode = str(payload.get("endMode") or "manual").lower()
    if end_mode not in {"manual", "auto"}:
        end_mode = "manual"
    try:
        auto_end_rounds = max(1, min(int(payload.get("autoEndRounds") or 20), 1000))
    except (TypeError, ValueError):
        auto_end_rounds = 20
    enabled = True
    language = "en"
    if isinstance(payload.get("enabled"), bool):
        enabled = bool(payload["enabled"])
    if payload.get("language") in {"en", "ar"}:
        language = str(payload["language"])
    if guild_id:
        saved = store.spy_game_config(guild_id)
        minimum_players = saved["min_players"]
        maximum_players = saved["max_players"]
        question_timer_seconds = saved["question_timer_seconds"]
        end_mode = str(saved.get("end_mode") or "manual")
        auto_end_rounds = int(saved.get("auto_end_rounds") or 20)
        enabled = bool(saved.get("enabled", True))
        language = str(saved.get("language") or "en")
    return {
        "id": "spy",
        "name": str(payload.get("name") or "Spy Game"),
        "theme": str(payload.get("theme") or "#000000"),
        "bannerPath": assets["bannerPath"],
        "iconPath": assets["iconPath"],
        "minimumPlayers": minimum_players,
        "maximumPlayers": maximum_players,
        "questionTimerSeconds": question_timer_seconds,
        "endMode": end_mode,
        "autoEndRounds": auto_end_rounds,
        "enabled": enabled,
        "language": language,
        "locations": [str(value) for value in locations[:50]],
    }


def roulette_game_dashboard_config(guild_id: str | None = None) -> dict[str, object]:
    """Read Roulette's local banner and per-server lobby capacity settings."""
    path = ROOT_DIR / "config" / "games" / "roulette.config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    def asset_url(key: str, fallback: str) -> str:
        raw = payload.get(key)
        if not isinstance(raw, str) or not raw.strip():
            return fallback
        relative = raw.replace("\\", "/").lstrip("./")
        local = ROOT_DIR / "website" / relative
        if local.is_file() and relative.startswith(("games/", "assets/")):
            return f"/assets/{relative.removeprefix('assets/')}"
        return fallback

    minimum = 2
    maximum = 20
    enabled = True
    language = "en"
    wheel_mode = "multi"
    wheel_color = "#6B7280"
    turn_timer_seconds = 30
    wheel_colors = [
        "#6B7280", "#9CA3AF", "#4B5563", "#374151",
        "#D1D5DB", "#818CF8", "#A78BFA",
    ]
    if isinstance(payload.get("enabled"), bool):
        enabled = bool(payload["enabled"])
    if payload.get("language") in {"en", "ar"}:
        language = str(payload["language"])
    if payload.get("wheelMode") in {"multi", "single"}:
        wheel_mode = str(payload["wheelMode"])
    if isinstance(payload.get("wheelColor"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", payload["wheelColor"]):
        wheel_color = str(payload["wheelColor"]).upper()
        wheel_colors[0] = wheel_color
    try:
        turn_timer_seconds = max(5, min(600, int(payload.get("turnTimerSeconds") or 30)))
    except (TypeError, ValueError):
        turn_timer_seconds = 30
    configured_colors = payload.get("wheelColors")
    if isinstance(configured_colors, list) and len(configured_colors) == 7 and all(
        isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) for value in configured_colors
    ):
        wheel_colors = [str(value).upper() for value in configured_colors]
        wheel_color = wheel_colors[0]
    try:
        minimum = max(2, min(50, int(payload.get("minimumPlayers") or 2)))
    except (TypeError, ValueError):
        pass
    try:
        maximum = max(minimum, min(50, int(payload.get("maximumPlayers") or 20)))
    except (TypeError, ValueError):
        maximum = max(minimum, 20)
    if guild_id:
        saved = store.roulette_game_config(guild_id)
        minimum = saved["min_players"]
        maximum = saved["max_players"]
        enabled = bool(saved.get("enabled", True))
        language = str(saved.get("language") or "en")
        wheel_mode = str(saved.get("wheel_mode") or "multi")
        wheel_color = str(saved.get("wheel_color") or "#6B7280")
        turn_timer_seconds = int(saved.get("turn_timer_seconds") or 30)
        saved_colors = saved.get("wheel_colors")
        if isinstance(saved_colors, list) and len(saved_colors) == 7 and all(
            isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) for value in saved_colors
        ):
            wheel_colors = [str(value).upper() for value in saved_colors]
            wheel_color = wheel_colors[0]
        else:
            wheel_colors[0] = wheel_color
    return {
        "id": "roulette",
        "name": str(payload.get("name") or "Roulette"),
        "theme": str(payload.get("theme") or "#000000"),
        "bannerPath": asset_url("bannerPath", "/assets/games/roulette_banner.png"),
        "iconPath": asset_url("iconPath", "/assets/games/roulette_icon.svg"),
        "minimumPlayers": minimum,
        "maximumPlayers": maximum,
        "enabled": enabled,
        "language": language,
        "wheelMode": wheel_mode,
        "wheelColor": wheel_color,
        "wheelColors": wheel_colors,
        "turnTimerSeconds": turn_timer_seconds,
    }


def guess_number_game_dashboard_config(guild_id: str | None = None) -> dict[str, object]:
    """Read Guess the Number branding and per-server game settings."""
    path = ROOT_DIR / "config" / "games" / "guess-number.config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    minimum = 2
    maximum = 20
    number_minimum = 1
    number_maximum = 100
    enabled = bool(payload.get("enabled", True))
    language = str(payload.get("language") or "en") if payload.get("language") in {"en", "ar"} else "en"
    try:
        minimum = max(2, min(50, int(payload.get("minimumPlayers") or 2)))
        maximum = max(minimum, min(50, int(payload.get("maximumPlayers") or 20)))
        raw_minimum = payload.get("numberMinimum", 1)
        raw_maximum = payload.get("numberMaximum", 100)
        number_minimum = max(-1_000_000, min(1_000_000, int(1 if raw_minimum is None else raw_minimum)))
        number_maximum = max(-1_000_000, min(1_000_000, int(100 if raw_maximum is None else raw_maximum)))
    except (TypeError, ValueError):
        minimum, maximum, number_minimum, number_maximum = 2, 20, 1, 100
    if number_maximum <= number_minimum:
        number_minimum, number_maximum = 1, 100
    if guild_id:
        saved = store.guess_number_game_config(guild_id)
        minimum = int(saved["min_players"])
        maximum = int(saved["max_players"])
        number_minimum = int(saved["number_min"])
        number_maximum = int(saved["number_max"])
        enabled = bool(saved.get("enabled", True))
        language = str(saved.get("language") or "en")
    return {
        "id": "guess-number",
        "name": str(payload.get("name") or "Guess the Number"),
        "theme": str(payload.get("theme") or "#1f2937"),
        "bannerPath": "/assets/games/spy_banner.svg",
        "iconPath": "/assets/games/spy_icon.svg",
        "minimumPlayers": minimum,
        "maximumPlayers": maximum,
        "numberMinimum": number_minimum,
        "numberMaximum": number_maximum,
        "enabled": enabled,
        "language": language,
    }


@app.get("/api/guilds/{guild_id}/games")
async def games_catalog(guild_id: str, request: Request) -> dict[str, object]:
    """Return the mini-games available to an authorized dashboard manager."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing games.")
    return {
        "games": [
            spy_game_dashboard_config(guild_id),
            roulette_game_dashboard_config(guild_id),
            guess_number_game_dashboard_config(guild_id),
        ],
        "server_id": guild_id,
    }


@app.get("/api/guilds/{guild_id}/games/spy")
async def spy_game_overview(guild_id: str, request: Request) -> dict[str, object]:
    """Return Spy Game detail metadata and persistent match history."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing games.")
    return {
        "game": spy_game_dashboard_config(guild_id),
        "logs": store.spy_game_logs(guild_id),
        "server_id": guild_id,
    }


@app.get("/api/guilds/{guild_id}/games/spy/logs")
async def spy_game_logs(guild_id: str, request: Request) -> dict[str, object]:
    """Read completed Spy Game matches without exposing any bot credentials."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing games.")
    return {"logs": store.spy_game_logs(guild_id), "server_id": guild_id}


@app.get("/api/guilds/{guild_id}/games/roulette")
async def roulette_game_overview(guild_id: str, request: Request) -> dict[str, object]:
    """Return Roulette branding and the saved per-server lobby limits."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing games.")
    return {
        "game": roulette_game_dashboard_config(guild_id),
        "server_id": guild_id,
    }


@app.get("/api/guilds/{guild_id}/games/guess-number")
async def guess_number_game_overview(guild_id: str, request: Request) -> dict[str, object]:
    """Return Guess the Number details for the Games dashboard."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing games.")
    return {
        "game": guess_number_game_dashboard_config(guild_id),
        "server_id": guild_id,
    }


@app.get("/api/guilds/{guild_id}/games/guess-number/config")
async def guess_number_game_config(guild_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing games.")
    return {"config": guess_number_game_dashboard_config(guild_id), "server_id": guild_id}


@app.post("/api/guilds/{guild_id}/games/guess-number/config")
async def save_guess_number_game_config(guild_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before updating games.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid Guess the Number settings.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Guess the Number settings.")
    current = store.guess_number_game_config(guild_id)

    def integer(name: str, fallback: int) -> int:
        value = payload.get(name, fallback)
        if isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"{name} must be a number.")
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=f"{name} must be a number.") from error

    def boolean(name: str, fallback: bool) -> bool:
        value = payload.get(name, fallback)
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "on", "off"}:
            return value.strip().lower() in {"true", "1", "on"}
        raise HTTPException(status_code=400, detail=f"{name} must be true or false.")

    minimum = integer("minimum_players", int(current["min_players"]))
    maximum = integer("maximum_players", int(current["max_players"]))
    number_minimum = integer("number_minimum", int(current["number_min"]))
    number_maximum = integer("number_maximum", int(current["number_max"]))
    enabled = boolean("enabled", bool(current.get("enabled", True)))
    language = payload.get("language", current.get("language", "en"))
    if not isinstance(language, str) or language not in {"en", "ar"}:
        raise HTTPException(status_code=400, detail="Choose English or Arabic for Guess the Number.")
    if not 2 <= minimum <= 50:
        raise HTTPException(status_code=400, detail="Minimum players must be between 2 and 50.")
    if not minimum <= maximum <= 50:
        raise HTTPException(status_code=400, detail="Maximum players must be at least the minimum and no more than 50.")
    if not -1_000_000 <= number_minimum < number_maximum <= 1_000_000:
        raise HTTPException(status_code=400, detail="The number range must be valid and no wider than two million values.")
    try:
        store.save_guess_number_game_config(
            guild_id,
            minimum,
            maximum,
            number_minimum,
            number_maximum,
            enabled=enabled,
            language=language,
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid Guess the Number settings.") from error
    except Exception as error:
        print(f"Guess the Number settings save failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="Guess the Number settings are temporarily unavailable. Please try again.") from error
    return {"config": guess_number_game_dashboard_config(guild_id), "server_id": guild_id, "status": "saved"}


@app.get("/api/guilds/{guild_id}/games/roulette/config")
async def roulette_game_config(guild_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing games.")
    return {"config": roulette_game_dashboard_config(guild_id), "server_id": guild_id}


@app.post("/api/guilds/{guild_id}/games/roulette/config")
async def save_roulette_game_config(guild_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before updating games.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid Roulette settings.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Roulette settings.")

    def integer(name: str, fallback: int) -> int:
        value = payload.get(name, fallback)
        if isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"{name} must be a number.")
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=f"{name} must be a number.") from error

    def boolean(name: str, fallback: bool) -> bool:
        value = payload.get(name, fallback)
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "on", "off"}:
            return value.strip().lower() in {"true", "1", "on"}
        raise HTTPException(status_code=400, detail=f"{name} must be true or false.")

    current = store.roulette_game_config(guild_id)
    minimum = integer("minimum_players", current["min_players"])
    maximum = integer("maximum_players", current["max_players"])
    enabled = boolean("enabled", bool(current.get("enabled", True)))
    language = payload.get("language", current.get("language", "en"))
    if not isinstance(language, str) or language not in {"en", "ar"}:
        raise HTTPException(status_code=400, detail="Choose English or Arabic for Roulette.")
    wheel_mode = payload.get("wheel_mode", current.get("wheel_mode", "multi"))
    if not isinstance(wheel_mode, str) or wheel_mode not in {"multi", "single"}:
        raise HTTPException(status_code=400, detail="Wheel mode must be multi or single.")
    wheel_color = payload.get("wheel_color", current.get("wheel_color", "#6B7280"))
    if not isinstance(wheel_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", wheel_color):
        raise HTTPException(status_code=400, detail="Wheel color must be a valid hex color.")
    wheel_colors = payload.get("wheel_colors", current.get("wheel_colors", []))
    if not isinstance(wheel_colors, list) or len(wheel_colors) != 7 or any(
        not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value)
        for value in wheel_colors
    ):
        raise HTTPException(status_code=400, detail="Provide exactly seven valid wheel slice colors.")
    wheel_colors = [str(value).upper() for value in wheel_colors]
    turn_timer_seconds = integer("turn_timer_seconds", current.get("turn_timer_seconds", 30))
    if not 2 <= minimum <= 50:
        raise HTTPException(status_code=400, detail="Minimum players must be between 2 and 50.")
    if not minimum <= maximum <= 50:
        raise HTTPException(status_code=400, detail="Maximum players must be at least the minimum and no more than 50.")
    if not 5 <= turn_timer_seconds <= 600:
        raise HTTPException(status_code=400, detail="Turn timer must be between 5 and 600 seconds.")
    try:
        store.save_roulette_game_config(
            guild_id,
            minimum,
            maximum,
            enabled=enabled,
            language=language,
            wheel_mode=wheel_mode,
            wheel_color=wheel_color,
            wheel_colors=wheel_colors,
            turn_timer_seconds=turn_timer_seconds,
        )
    except Exception as error:
        print(f"Roulette settings save failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="Roulette settings are temporarily unavailable. Please try again.") from error
    return {"config": roulette_game_dashboard_config(guild_id), "server_id": guild_id, "status": "saved"}


@app.get("/api/guilds/{guild_id}/games/spy/config")
async def spy_game_config(guild_id: str, request: Request) -> dict[str, object]:
    """Read per-server Spy Game lobby limits for the dashboard settings card."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing games.")
    return {"config": spy_game_dashboard_config(guild_id), "server_id": guild_id}


@app.post("/api/guilds/{guild_id}/games/spy/config")
async def save_spy_game_config(guild_id: str, request: Request) -> dict[str, object]:
    """Validate and persist the Spy Game player/timer controls."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before updating games.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid Spy Game settings.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Spy Game settings.")

    def integer(name: str, fallback: int) -> int:
        value = payload.get(name, fallback)
        if isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"{name} must be a number.")
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=f"{name} must be a number.") from error

    def boolean(name: str, fallback: bool) -> bool:
        value = payload.get(name, fallback)
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "on", "off"}:
            return value.strip().lower() in {"true", "1", "on"}
        raise HTTPException(status_code=400, detail=f"{name} must be true or false.")

    current = store.spy_game_config(guild_id)
    minimum = integer("minimum_players", current["min_players"])
    maximum = integer("maximum_players", current["max_players"])
    timer = integer("question_timer_seconds", current["question_timer_seconds"])
    enabled = boolean("enabled", bool(current.get("enabled", True)))
    language = payload.get("language", current.get("language", "en"))
    end_mode = str(payload.get("end_mode", current.get("end_mode", "manual"))).lower()
    auto_end_rounds = integer("auto_end_rounds", int(current.get("auto_end_rounds", 20)))
    if not isinstance(language, str) or language not in {"en", "ar"}:
        raise HTTPException(status_code=400, detail="Choose English or Arabic for Spy Game.")
    if not 3 <= minimum <= 50:
        raise HTTPException(status_code=400, detail="Minimum players must be between 3 and 50.")
    if not minimum <= maximum <= 50:
        raise HTTPException(status_code=400, detail="Maximum players must be at least the minimum and no more than 50.")
    if not 5 <= timer <= 600:
        raise HTTPException(status_code=400, detail="Question timer must be between 5 and 600 seconds.")
    if end_mode not in {"manual", "auto"}:
        raise HTTPException(status_code=400, detail="End mode must be Manual End or Auto End.")
    if not 1 <= auto_end_rounds <= 1000:
        raise HTTPException(status_code=400, detail="Auto end rounds must be between 1 and 1000.")
    try:
        store.save_spy_game_config(
            guild_id,
            minimum,
            maximum,
            timer,
            enabled=enabled,
            language=language,
            end_mode=end_mode,
            auto_end_rounds=auto_end_rounds,
        )
    except Exception as error:
        print(f"Spy Game settings save failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="Spy Game settings are temporarily unavailable. Please try again.") from error
    return {"config": spy_game_dashboard_config(guild_id), "server_id": guild_id, "status": "saved"}


@app.post("/api/guilds/{guild_id}/games/spy/lobby")
async def create_spy_lobby(guild_id: str, request: Request) -> dict[str, str]:
    """Queue a Spy Game lobby for the one global Discord bot to post.

    The dashboard only enqueues the action.  The bot worker performs the
    final channel/member/permission checks immediately before sending, so a
    stale browser payload cannot post into another guild or channel.
    """
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before starting games.")
    game_config = store.spy_game_config(guild_id)
    if not bool(game_config.get("enabled", True)):
        raise HTTPException(status_code=409, detail="Spy Game is disabled for this server. Enable it in the Games settings first.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Choose a text channel for the Spy Game lobby.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Spy Game lobby data.")
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not DISCORD_SNOWFLAKE.fullmatch(channel_id):
        raise HTTPException(status_code=400, detail="Choose a valid text channel for the Spy Game lobby.")
    if channel_id not in {channel["id"] for channel in store.bot_text_channels(guild_id)}:
        raise HTTPException(status_code=404, detail="BirdBot cannot access that text channel.")
    try:
        request_id = store.queue_command(
            guild_id,
            channel_id,
            "spy_lobby",
            user["id"],
            # The bot reads the persisted language at execution time. Do not
            # let an old/crafted browser payload override the server setting.
            {},
        )
    except Exception as error:
        print(f"Spy Game lobby queue failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="The Spy Game queue is temporarily unavailable. Please try again.") from error
    return {"request_id": request_id, "status": "pending"}


@app.post("/api/guilds/{guild_id}/games/spy/start")
async def start_spy_lobby(guild_id: str, request: Request) -> dict[str, str]:
    """Compatibility alias matching the dashboard's Start action wording."""
    return await create_spy_lobby(guild_id, request)


@app.post("/api/guilds/{guild_id}/games/roulette/lobby")
async def create_roulette_lobby(guild_id: str, request: Request) -> dict[str, str]:
    """Queue a Roulette lobby for the one global Discord bot client."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before starting games.")
    game_config = store.roulette_game_config(guild_id)
    if not bool(game_config.get("enabled", True)):
        raise HTTPException(status_code=409, detail="Roulette is disabled for this server. Enable it in the Games settings first.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Choose a text channel for the Roulette lobby.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Roulette lobby data.")
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not DISCORD_SNOWFLAKE.fullmatch(channel_id):
        raise HTTPException(status_code=400, detail="Choose a valid text channel for the Roulette lobby.")
    if channel_id not in {channel["id"] for channel in store.bot_text_channels(guild_id)}:
        raise HTTPException(status_code=404, detail="BirdBot cannot access that text channel.")
    try:
        request_id = store.queue_command(guild_id, channel_id, "roulette_lobby", user["id"], {})
    except Exception as error:
        print(f"Roulette lobby queue failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="The Roulette queue is temporarily unavailable. Please try again.") from error
    return {"request_id": request_id, "status": "pending"}


@app.post("/api/guilds/{guild_id}/games/roulette/start")
async def start_roulette_lobby(guild_id: str, request: Request) -> dict[str, str]:
    """Compatibility alias matching the dashboard's Start action wording."""
    return await create_roulette_lobby(guild_id, request)


@app.post("/api/guilds/{guild_id}/games/guess-number/lobby")
async def create_guess_number_lobby(guild_id: str, request: Request) -> dict[str, str]:
    """Queue a Guess the Number lobby for the shared Discord client."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before starting games.")
    config = store.guess_number_game_config(guild_id)
    if not bool(config.get("enabled", True)):
        raise HTTPException(status_code=409, detail="Guess the Number is disabled for this server. Enable it in Games settings first.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Choose a text channel for the Guess the Number lobby.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Guess the Number lobby data.")
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not DISCORD_SNOWFLAKE.fullmatch(channel_id):
        raise HTTPException(status_code=400, detail="Choose a valid text channel for the Guess the Number lobby.")
    if channel_id not in {channel["id"] for channel in store.bot_text_channels(guild_id)}:
        raise HTTPException(status_code=404, detail="BirdBot cannot access that text channel.")
    try:
        request_id = store.queue_command(guild_id, channel_id, "guess_number_lobby", user["id"], {})
    except Exception as error:
        print(f"Guess the Number lobby queue failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="The Guess the Number queue is temporarily unavailable. Please try again.") from error
    return {"request_id": request_id, "status": "pending"}


@app.post("/api/guilds/{guild_id}/games/guess-number/start")
async def start_guess_number_lobby(guild_id: str, request: Request) -> dict[str, str]:
    """Compatibility alias matching the dashboard Start action wording."""
    return await create_guess_number_lobby(guild_id, request)


TICKET_PRIORITIES = {"low", "medium", "high"}
TICKET_PANEL_LAYOUTS = {"buttons", "select_menu"}
# Discord action buttons support four visual styles.  These are intentionally
# named styles rather than arbitrary hex values because Discord controls their
# actual rendered colors.
TICKET_BUTTON_STYLES = ("primary", "success", "danger", "secondary")
# Accept both Discord's API names and the color names used by older/cached
# dashboard builds.  Discord action buttons intentionally support only these
# four callback-capable styles; arbitrary hex colors are not available.
TICKET_BUTTON_STYLE_ALIASES = {
    "blue": "primary",
    "blurple": "primary",
    "green": "success",
    "red": "danger",
    "gray": "secondary",
    "grey": "secondary",
}
MIN_OPEN_TICKETS_PER_USER = 1
MAX_OPEN_TICKETS_PER_USER = 25


def validate_ticket_payload(guild_id: str, payload: dict[str, object]) -> dict[str, object]:
    """Normalize ticket settings and validate every ID against bot-synced guild state."""
    setup_channel_id = payload.get("setup_channel_id")
    if isinstance(setup_channel_id, (int, float)) and not isinstance(setup_channel_id, bool):
        setup_channel_id = str(setup_channel_id)
    if isinstance(setup_channel_id, str):
        setup_channel_id = setup_channel_id.strip()
    if not isinstance(setup_channel_id, str) or not DISCORD_SNOWFLAKE.fullmatch(setup_channel_id):
        raise HTTPException(status_code=400, detail="Choose a text channel for the ticket panel.")
    if setup_channel_id not in {channel["id"] for channel in store.bot_text_channels(guild_id)}:
        raise HTTPException(status_code=404, detail="BirdBot cannot access that ticket panel channel.")

    category_id = payload.get("category_id")
    if isinstance(category_id, (int, float)) and not isinstance(category_id, bool):
        category_id = str(category_id)
    if isinstance(category_id, str):
        category_id = category_id.strip()
    if category_id == "":
        category_id = None
    if category_id is not None:
        if not isinstance(category_id, str) or not DISCORD_SNOWFLAKE.fullmatch(category_id):
            raise HTTPException(status_code=400, detail="Choose a valid ticket category.")
        if category_id not in {category["id"] for category in store.bot_categories(guild_id)}:
            raise HTTPException(status_code=404, detail="That ticket category is no longer available.")

    log_channel_id = payload.get("log_channel_id")
    if isinstance(log_channel_id, (int, float)) and not isinstance(log_channel_id, bool):
        log_channel_id = str(log_channel_id)
    if isinstance(log_channel_id, str):
        log_channel_id = log_channel_id.strip()
    if log_channel_id == "":
        log_channel_id = None
    if log_channel_id is not None:
        if not isinstance(log_channel_id, str) or not DISCORD_SNOWFLAKE.fullmatch(log_channel_id):
            raise HTTPException(status_code=400, detail="Choose a valid ticket logs channel.")
        if log_channel_id not in {channel["id"] for channel in store.bot_text_channels(guild_id)}:
            raise HTTPException(status_code=404, detail="That ticket logs channel is no longer available.")

    raw_role_ids = payload.get("support_role_ids", [])
    if raw_role_ids is None:
        raw_role_ids = []
    if isinstance(raw_role_ids, (str, int)) and not isinstance(raw_role_ids, bool):
        raw_role_ids = [str(raw_role_ids)]
    if not isinstance(raw_role_ids, list) or len(raw_role_ids) > 25:
        raise HTTPException(status_code=400, detail="Choose up to 25 support roles.")
    available_roles = {role["id"]: role for role in store.bot_roles(guild_id)}
    support_role_ids: list[str] = []
    for role_id in raw_role_ids:
        if isinstance(role_id, (int, float)) and not isinstance(role_id, bool):
            role_id = str(role_id)
        if not isinstance(role_id, str) or not DISCORD_SNOWFLAKE.fullmatch(role_id.strip()):
            raise HTTPException(status_code=400, detail="One of the selected support roles is invalid.")
        role_id = role_id.strip()
        role = available_roles.get(role_id)
        if not role:
            raise HTTPException(status_code=404, detail="One of the selected support roles is no longer available.")
        if role_id not in support_role_ids:
            support_role_ids.append(role_id)

    priority = payload.get("priority", "medium")
    if not isinstance(priority, str) or priority.strip().casefold() not in TICKET_PRIORITIES:
        raise HTTPException(status_code=400, detail="Choose a Low, Medium, or High priority.")
    priority = priority.strip().casefold()

    raw_max_open_tickets = payload.get("max_open_tickets", payload.get("maxTicketsPerUser", 1))
    if isinstance(raw_max_open_tickets, bool):
        raise HTTPException(status_code=400, detail="Maximum open tickets must be a whole number from 1 to 25.")
    try:
        max_open_tickets = int(str(raw_max_open_tickets).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Maximum open tickets must be a whole number from 1 to 25.") from None
    if not MIN_OPEN_TICKETS_PER_USER <= max_open_tickets <= MAX_OPEN_TICKETS_PER_USER:
        raise HTTPException(status_code=400, detail="Maximum open tickets must be a whole number from 1 to 25.")

    panel_layout = payload.get("panel_layout", payload.get("panelLayout", "select_menu"))
    if not isinstance(panel_layout, str) or panel_layout.strip().casefold() not in TICKET_PANEL_LAYOUTS:
        raise HTTPException(status_code=400, detail="Choose Buttons or Select Menu ticket panel layout.")
    panel_layout = panel_layout.strip().casefold()
    require_description = payload.get("require_description", False)
    if isinstance(require_description, str) and require_description.casefold() in {"true", "false"}:
        require_description = require_description.casefold() == "true"
    if not isinstance(require_description, bool):
        raise HTTPException(status_code=400, detail="Description prompting must be enabled or disabled.")
    description_prompt = payload.get("description_prompt", "Please describe your request.")
    if description_prompt is None:
        description_prompt = "Please describe your request."
    if not isinstance(description_prompt, str):
        raise HTTPException(status_code=400, detail="Enter a description prompt of 1-200 characters.")
    description_prompt = description_prompt.strip() or "Please describe your request."
    if len(description_prompt) > 200:
        raise HTTPException(status_code=400, detail="Enter a description prompt of 1-200 characters.")

    remove_custom_icon = payload.get("remove_custom_icon", False)
    if isinstance(remove_custom_icon, str) and remove_custom_icon.casefold() in {"true", "false"}:
        remove_custom_icon = remove_custom_icon.casefold() == "true"
    if not isinstance(remove_custom_icon, bool):
        raise HTTPException(status_code=400, detail="Custom icon removal must be enabled or disabled.")

    raw_options = payload.get("options")
    if not isinstance(raw_options, list) or not 1 <= len(raw_options) <= 25:
        raise HTTPException(status_code=400, detail="Add between 1 and 25 ticket options.")
    options: list[dict[str, object]] = []
    values: set[str] = set()
    for index, raw_option in enumerate(raw_options, start=1):
        if not isinstance(raw_option, dict):
            raise HTTPException(status_code=400, detail=f"Ticket option {index} is invalid.")
        label = raw_option.get("label")
        value = raw_option.get("value")
        description = raw_option.get("description", "")
        emoji = raw_option.get("emoji", "")
        if description is None:
            description = ""
        if emoji is None:
            emoji = ""
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise HTTPException(status_code=400, detail=f"Ticket option {index} needs a label of 1-80 characters.")
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
            raise HTTPException(status_code=400, detail=f"Ticket option {index} needs a value of 1-100 characters.")
        label = label.strip()
        value = value.strip()
        if value in values:
            raise HTTPException(status_code=400, detail="Ticket option values must be unique.")
        values.add(value)
        if not isinstance(description, str) or len(description.strip()) > 100:
            raise HTTPException(status_code=400, detail=f"Ticket option {index} description must be 100 characters or fewer.")
        if not isinstance(emoji, str) or len(emoji.strip()) > 64:
            raise HTTPException(status_code=400, detail=f"Ticket option {index} emoji is too long.")
        raw_button_style = raw_option.get("button_style", raw_option.get("button_color", raw_option.get("color")))
        if raw_button_style is None or (isinstance(raw_button_style, str) and not raw_button_style.strip()):
            button_style = TICKET_BUTTON_STYLES[(index - 1) % len(TICKET_BUTTON_STYLES)]
        elif isinstance(raw_button_style, str):
            button_style = raw_button_style.strip().casefold()
            button_style = TICKET_BUTTON_STYLE_ALIASES.get(button_style, button_style)
        else:
            button_style = ""
        if button_style not in TICKET_BUTTON_STYLES:
            raise HTTPException(
                status_code=400,
                detail=f"Ticket option {index} has an invalid button color. Choose blue, green, red, or gray.",
            )
        options.append({
            "label": label,
            "value": value,
            "description": description.strip() or None,
            "emoji": emoji.strip() or None,
            "button_style": button_style,
        })

    return {
        "setup_channel_id": setup_channel_id,
        "category_id": category_id,
        "options": options,
        "support_role_ids": support_role_ids,
        "panel_layout": panel_layout,
        "priority": priority,
        "max_open_tickets": max_open_tickets,
        "require_description": require_description,
        "description_prompt": description_prompt,
        "log_channel_id": log_channel_id,
        "remove_custom_icon": remove_custom_icon,
    }


@app.get("/api/guilds/{guild_id}/tickets/config")
async def get_ticket_config(guild_id: str, request: Request) -> dict[str, object]:
    """Return ticket settings and the current channel/category/role choices."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before configuring tickets.")
    return {
        "config": public_ticket_config(store.ticket_config(guild_id)),
        "channels": store.bot_text_channels(guild_id),
        "categories": store.bot_categories(guild_id),
        "roles": store.bot_roles(guild_id),
    }


async def ticket_request_payload(request: Request) -> tuple[dict[str, object], UploadFile | None]:
    """Read JSON (legacy clients) or multipart form data (icon uploads)."""
    content_type = request.headers.get("content-type", "").casefold()
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail="The ticket form could not be read.") from error

        def form_value(name: str, default: str = "") -> str:
            value = form.get(name, default)
            if isinstance(value, bytes):
                return value.decode("utf-8", "replace")
            if isinstance(value, str):
                return value
            # Starlette may expose non-file form values as String-like
            # objects.  Never coerce an uploaded file into a field value.
            if value is None or hasattr(value, "read"):
                return default
            return str(value)

        raw_options = form_value("options", "[]")
        raw_role_ids = form_value("support_role_ids", "[]")
        try:
            options = json.loads(raw_options)
            support_role_ids = json.loads(raw_role_ids)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail="Ticket options or support roles are invalid.") from error
        upload = form.get("custom_icon")
        if upload is not None and not isinstance(upload, UploadFile) and not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="The custom panel icon upload is invalid.")
        # Accept the current field names and the short aliases used by older
        # dashboard builds so a cached tab can still be submitted safely.
        setup_channel_id = form_value("setup_channel_id") or form_value("channel_id")
        category_id = form_value("category_id") or form_value("ticket_category_id")
        parsed_payload: dict[str, object] = {
            "setup_channel_id": setup_channel_id,
            "category_id": category_id,
            "priority": form_value("priority", "medium"),
            "support_role_ids": support_role_ids,
            "require_description": form_value("require_description", "false"),
            "description_prompt": form_value("description_prompt", "Please describe your request."),
            "remove_custom_icon": form_value("remove_custom_icon", "false"),
            "options": options,
        }
        if form.get("max_open_tickets") is not None or form.get("maxTicketsPerUser") is not None:
            parsed_payload["max_open_tickets"] = form_value(
                "max_open_tickets", form_value("maxTicketsPerUser", "1")
            )
        # Older cached dashboard builds do not send a layout field. Leaving it
        # out lets the save endpoint preserve the currently configured mode.
        if form.get("panel_layout") is not None or form.get("panelLayout") is not None:
            parsed_payload["panel_layout"] = form_value(
                "panel_layout", form_value("panelLayout", "select_menu")
            )
        # An omitted logs field means an older cached client; preserve its
        # existing setting. An explicitly empty field intentionally clears it.
        if form.get("log_channel_id") is not None:
            parsed_payload["log_channel_id"] = form_value("log_channel_id")
        return parsed_payload, upload
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Enter ticket settings before saving.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid ticket settings.")
    return payload, None


def remove_ticket_icon(path_value: str | None) -> None:
    """Delete only files previously created in the ticket icon directory."""
    if not path_value:
        return
    try:
        candidate = Path(path_value).resolve()
        icon_root = TICKET_ICON_DIR.resolve()
        if candidate.parent == icon_root and candidate.is_file():
            candidate.unlink()
    except OSError:
        # A missing/locked old icon must not prevent saving the new settings.
        return


async def save_ticket_icon(
    guild_id: str,
    upload: UploadFile | None,
    existing_url: str | None,
    existing_path: str | None,
    remove: bool = False,
) -> tuple[str | None, str | None]:
    """Persist a bounded image upload, explicitly remove, or preserve the current icon."""
    if remove:
        remove_ticket_icon(existing_path)
        return None, None
    if upload is None or not upload.filename:
        return existing_url, existing_path
    content_type = (upload.content_type or "").casefold()
    extension = TICKET_ICON_TYPES.get(content_type)
    if not extension:
        raise HTTPException(status_code=400, detail="Custom Panel Icon must be PNG, JPEG, WebP, or GIF.")
    content = await upload.read(MAX_TICKET_ICON_BYTES + 1)
    if len(content) > MAX_TICKET_ICON_BYTES:
        raise HTTPException(status_code=413, detail="Custom Panel Icon must be 5 MB or smaller.")
    filename = f"{guild_id}-{secrets.token_hex(10)}{extension}"
    path = TICKET_ICON_DIR / filename
    path.write_bytes(content)
    remove_ticket_icon(existing_path)
    return f"/uploads/ticket-icons/{filename}", str(path)


def remove_managed_file(path_value: object, root: Path) -> None:
    """Delete only a file that was created inside one of our upload folders."""
    if not isinstance(path_value, str) or not path_value:
        return
    try:
        candidate = Path(path_value).resolve()
        if candidate.parent == root.resolve() and candidate.is_file():
            candidate.unlink()
    except OSError:
        return


async def save_profile_avatar(
    guild_id: str,
    upload: UploadFile | None,
) -> tuple[str, str]:
    """Store a bounded bot-profile avatar and return its public URL and path."""
    if upload is None or not upload.filename:
        raise HTTPException(status_code=400, detail="Choose an avatar image first.")
    content_type = (upload.content_type or "").casefold()
    extension = PROFILE_AVATAR_TYPES.get(content_type)
    if not extension:
        raise HTTPException(status_code=400, detail="Avatar must be PNG, JPEG, WebP, or GIF.")
    content = await upload.read(MAX_PROFILE_AVATAR_BYTES + 1)
    if len(content) > MAX_PROFILE_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="Avatar must be 8 MB or smaller.")
    filename = f"{guild_id}-{secrets.token_hex(10)}{extension}"
    path = BOT_PROFILE_AVATAR_DIR / filename
    path.write_bytes(content)
    return f"/uploads/bot-profile-avatars/{filename}", str(path)


async def save_dm_media(guild_id: str, upload: UploadFile | None) -> tuple[str, str, str]:
    """Store one bounded image/video attachment for a queued DM."""
    if upload is None or not upload.filename:
        raise HTTPException(status_code=400, detail="Choose an image or video attachment first.")
    content_type = (upload.content_type or "").casefold()
    extension = DM_MEDIA_TYPES.get(content_type)
    if not extension:
        raise HTTPException(status_code=400, detail="Attachment must be an image or MP4/WebM/MOV video.")
    content = await upload.read(MAX_DM_MEDIA_BYTES + 1)
    if len(content) > MAX_DM_MEDIA_BYTES:
        raise HTTPException(status_code=413, detail="Attachments must be 8 MB or smaller.")
    filename = f"{guild_id}-{secrets.token_hex(10)}{extension}"
    path = DM_MEDIA_DIR / filename
    path.write_bytes(content)
    return str(path), filename, content_type


@app.post("/api/guilds/{guild_id}/tickets/config")
async def save_ticket_config(guild_id: str, request: Request) -> dict[str, object]:
    """Validate and persist ticket panel configuration for an authorized guild manager."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before configuring tickets.")
    payload, upload = await ticket_request_payload(request)
    normalized = validate_ticket_payload(guild_id, payload)
    existing = store.ticket_config(guild_id)
    if "panel_layout" not in payload and "panelLayout" not in payload:
        normalized["panel_layout"] = existing.get("panel_layout") or "select_menu"
    if "max_open_tickets" not in payload and "maxTicketsPerUser" not in payload:
        normalized["max_open_tickets"] = existing.get("max_open_tickets") or 1
    if "log_channel_id" not in payload:
        normalized["log_channel_id"] = existing.get("log_channel_id")
    normalized["custom_icon_url"], normalized["custom_icon_path"] = await save_ticket_icon(
        guild_id,
        upload,
        str(existing.get("custom_icon_url") or "") or None,
        str(existing.get("custom_icon_path") or "") or None,
        bool(normalized.get("remove_custom_icon")),
    )
    config = store.save_ticket_config(
        guild_id,
        normalized["setup_channel_id"],
        normalized["category_id"],
        normalized["options"],
        normalized["support_role_ids"],
        normalized["priority"],
        normalized["require_description"],
        normalized["description_prompt"],
        normalized["custom_icon_url"],
        normalized["custom_icon_path"],
        user["id"],
        normalized["log_channel_id"],
        normalized["panel_layout"],
        max_open_tickets=normalized["max_open_tickets"],
    )
    return {"config": public_ticket_config(config), "message": "Ticket system configuration saved."}


@app.post("/api/guilds/{guild_id}/tickets/post")
async def post_ticket_panel(guild_id: str, request: Request) -> dict[str, object]:
    """Save settings and queue the single global bot to post the interactive panel."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before posting tickets.")
    payload, upload = await ticket_request_payload(request)
    normalized = validate_ticket_payload(guild_id, payload)
    existing = store.ticket_config(guild_id)
    if "panel_layout" not in payload and "panelLayout" not in payload:
        normalized["panel_layout"] = existing.get("panel_layout") or "select_menu"
    if "max_open_tickets" not in payload and "maxTicketsPerUser" not in payload:
        normalized["max_open_tickets"] = existing.get("max_open_tickets") or 1
    if "log_channel_id" not in payload:
        normalized["log_channel_id"] = existing.get("log_channel_id")
    normalized["custom_icon_url"], normalized["custom_icon_path"] = await save_ticket_icon(
        guild_id,
        upload,
        str(existing.get("custom_icon_url") or "") or None,
        str(existing.get("custom_icon_path") or "") or None,
        bool(normalized.get("remove_custom_icon")),
    )
    config = store.save_ticket_config(
        guild_id,
        normalized["setup_channel_id"],
        normalized["category_id"],
        normalized["options"],
        normalized["support_role_ids"],
        normalized["priority"],
        normalized["require_description"],
        normalized["description_prompt"],
        normalized["custom_icon_url"],
        normalized["custom_icon_path"],
        user["id"],
        normalized["log_channel_id"],
        normalized["panel_layout"],
        max_open_tickets=normalized["max_open_tickets"],
    )
    request_id = store.queue_command(guild_id, normalized["setup_channel_id"], "ticket_post", user["id"], normalized)
    return {"request_id": request_id, "status": "pending", "config": public_ticket_config(config), "message": "Ticket panel saved and queued for posting."}


@app.get("/api/guilds/{guild_id}/tickets")
async def list_tickets(guild_id: str, request: Request) -> dict[str, object]:
    """List persistent ticket records, including closed tickets and deleted channels."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing tickets.")
    channel_names = {channel["id"]: channel["name"] for channel in store.bot_text_channels(guild_id)}
    tickets = []
    for ticket in store.tickets(guild_id):
        # Newly-created ticket channels may not be present in the 30-second
        # channel snapshot yet. Active records are therefore actionable; the
        # bot worker performs the authoritative live-channel check.
        channel_available = ticket["status"] != "closed" or ticket["channel_id"] in channel_names
        tickets.append(
            {
                **ticket,
                "transcript_url": local_transcript_url(request, ticket.get("transcript_url")),
                "channel_name": channel_names.get(ticket["channel_id"], ticket.get("channel_name") or f"ticket-{ticket['channel_id']}"),
                "channel_available": channel_available,
            }
        )
    # The client uses this timestamp to compensate for local clock skew while
    # rendering countdowns.  Ticket deadlines themselves remain authoritative
    # in SQLite and are evaluated by the Discord bot worker.
    return {"tickets": tickets, "server_time": utc_now()}


async def queue_ticket_action(guild_id: str, ticket_id: str, action: str, request: Request) -> dict[str, str]:
    # Web dashboard routes are admin/owner-only.  The Discord bot performs the
    # authoritative configured Support Role check before executing the action.
    user, _ = await verified_guild_manager(guild_id, request, fresh=True)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing tickets.")
    if not DISCORD_SNOWFLAKE.fullmatch(ticket_id):
        raise HTTPException(status_code=400, detail="The ticket identifier is invalid.")
    ticket = store.ticket(guild_id, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="That ticket could not be found.")
    if action == "claim" and ticket.get("status") == "closed":
        raise HTTPException(status_code=409, detail="Closed tickets cannot be claimed.")
    if action == "claim" and ticket.get("status") == "claimed":
        raise HTTPException(status_code=409, detail=f"That ticket is already claimed by {ticket.get('claimed_by_name') or 'another staff member'}.")
    if action == "close" and ticket.get("status") == "closed":
        raise HTTPException(status_code=409, detail="That ticket is already closed.")
    request_id = store.queue_command(
        guild_id,
        str(ticket["channel_id"]),
        f"ticket_{action}",
        user["id"],
        {"ticket_id": ticket_id},
    )
    return {"request_id": request_id, "status": "pending"}


@app.post("/api/guilds/{guild_id}/tickets/{ticket_id}/claim")
async def claim_ticket(guild_id: str, ticket_id: str, request: Request) -> dict[str, str]:
    return await queue_ticket_action(guild_id, ticket_id, "claim", request)


@app.post("/api/guilds/{guild_id}/tickets/{ticket_id}/close")
async def close_ticket(guild_id: str, ticket_id: str, request: Request) -> dict[str, str]:
    return await queue_ticket_action(guild_id, ticket_id, "close", request)


TICKET_DELETE_CLOSED_ERROR = "Error: You must close the ticket first before deleting it."


@app.delete("/api/guilds/{guild_id}/tickets/{ticket_id}")
async def delete_closed_ticket(guild_id: str, ticket_id: str, request: Request) -> dict[str, object]:
    """Delete one ticket metadata row only after it has been closed."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing tickets.")
    if not DISCORD_SNOWFLAKE.fullmatch(ticket_id):
        raise HTTPException(status_code=400, detail="The ticket identifier is invalid.")
    ticket = store.ticket(guild_id, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="That ticket could not be found.")
    if ticket.get("status") != "closed":
        raise HTTPException(status_code=409, detail=TICKET_DELETE_CLOSED_ERROR)
    if not store.delete_ticket(guild_id, ticket_id):
        raise HTTPException(status_code=409, detail="That ticket could not be deleted. Refresh and try again.")
    return {"deleted": 1, "ticket_id": ticket_id, "message": "Closed ticket deleted."}


@app.post("/api/guilds/{guild_id}/ticket-records/{ticket_id}/delete")
async def delete_closed_ticket_action(guild_id: str, ticket_id: str, request: Request) -> dict[str, object]:
    """POST alias for clients that do not issue DELETE requests."""
    return await delete_closed_ticket(guild_id, ticket_id, request)


@app.post("/api/guilds/{guild_id}/tickets/closed/delete")
async def delete_all_closed_tickets(guild_id: str, request: Request) -> dict[str, object]:
    """Bulk-delete closed ticket rows while preserving the permanent audit log."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing tickets.")
    deleted = store.delete_closed_tickets(guild_id)
    return {"deleted": deleted, "message": f"Deleted {deleted} closed ticket record{'s' if deleted != 1 else ''}."}


@app.post("/api/guilds/{guild_id}/tickets/delete-closed")
async def delete_all_closed_ticket_records(guild_id: str, request: Request) -> dict[str, object]:
    """Compatibility alias for the bulk closed-ticket deletion action."""
    return await delete_all_closed_tickets(guild_id, request)


async def queue_ticket_member_action(guild_id: str, ticket_id: str, action: str, request: Request) -> dict[str, str]:
    user, _ = await verified_guild_manager(guild_id, request, fresh=True)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing tickets.")
    if not DISCORD_SNOWFLAKE.fullmatch(ticket_id):
        raise HTTPException(status_code=400, detail="The ticket identifier is invalid.")
    ticket = store.ticket(guild_id, ticket_id)
    if not ticket or ticket.get("status") == "closed":
        raise HTTPException(status_code=404, detail="That ticket is not available.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Choose a member first.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid member data.")
    member_id = payload.get("member_id")
    if not isinstance(member_id, str) or not DISCORD_SNOWFLAKE.fullmatch(member_id):
        raise HTTPException(status_code=400, detail="Choose a valid server member.")
    # The roster snapshot is a fast search aid, not an authorization source.
    # A member can join between snapshots or be absent from Discord's local
    # cache. The single live bot resolves this ID again immediately before it
    # changes ticket permissions.
    request_id = store.queue_command(
        guild_id, str(ticket["channel_id"]), f"ticket_{action}", user["id"],
        {"ticket_id": ticket_id, "member_id": member_id},
    )
    return {"request_id": request_id, "status": "pending"}


@app.post("/api/guilds/{guild_id}/tickets/{ticket_id}/members/add")
async def add_ticket_member(guild_id: str, ticket_id: str, request: Request) -> dict[str, str]:
    return await queue_ticket_member_action(guild_id, ticket_id, "add_member", request)


@app.post("/api/guilds/{guild_id}/tickets/{ticket_id}/members/remove")
async def remove_ticket_member(guild_id: str, ticket_id: str, request: Request) -> dict[str, str]:
    return await queue_ticket_member_action(guild_id, ticket_id, "remove_member", request)


@app.get("/api/guilds/{guild_id}/tickets/logs")
async def ticket_logs(guild_id: str, request: Request, q: str = "") -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing ticket logs.")
    logs = []
    for log in store.ticket_logs(guild_id, q[:120], limit=1_000):
        logs.append({**log, "transcript_url": local_transcript_url(request, log.get("transcript_url"))})
    return {"logs": logs}


def parse_ticket_log_id(log_id: str) -> int:
    try:
        value = int(log_id)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="The log identifier is invalid.") from error
    if value < 1 or value > 9_223_372_036_854_775_807:
        raise HTTPException(status_code=400, detail="The log identifier is invalid.")
    return value


@app.delete("/api/guilds/{guild_id}/tickets/logs/{log_id}")
async def delete_ticket_log(guild_id: str, log_id: str, request: Request) -> dict[str, object]:
    """Delete one ticket-log row; ticket records and transcripts are untouched."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing ticket logs.")
    value = parse_ticket_log_id(log_id)
    if not store.delete_ticket_log(guild_id, value):
        raise HTTPException(status_code=404, detail="That ticket log could not be found.")
    return {"deleted": 1, "log_id": value, "message": "Ticket log deleted."}


@app.post("/api/guilds/{guild_id}/tickets/logs/delete-all")
async def delete_all_ticket_logs(guild_id: str, request: Request) -> dict[str, object]:
    """Delete every ticket-log row for the guild without deleting tickets."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing ticket logs.")
    deleted = store.delete_ticket_logs(guild_id)
    return {"deleted": deleted, "message": f"Deleted {deleted} ticket log{'s' if deleted != 1 else ''}."}


async def queue_dashboard_command(guild_id: str, command_name: str, request: Request) -> dict[str, str]:
    """Validate a dashboard command before the global bot executes it."""
    music_command_names = {"play", "q", "pause", "skip", "stop"}
    is_music_command = command_name in music_command_names
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before using commands.")
    if command_name in DASHBOARD_COMMAND_NAMES and not store.command_config(guild_id, command_name).get("enabled", True):
        raise HTTPException(status_code=409, detail="That command is disabled for this server. Enable it in the Commands tab first.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Choose a text channel first.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid command data.")
    # Member-management actions from the Show Members panel can suppress a
    # confirmation message in the selected channel. The Discord worker still
    # records the action in the configured moderation logs.
    if "silent" in payload and not isinstance(payload["silent"], bool):
        raise HTTPException(status_code=400, detail="The silent option must be a boolean.")
    # Music playback uses the requesting member's current voice channel, so
    # it does not need a text-channel destination. Keep the neutral channel
    # ID used by the member Music portal for the shared command queue.
    if is_music_command:
        channel_id = "0"
        payload["command_surface"] = "commands"
    else:
        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, str) or not DISCORD_SNOWFLAKE.fullmatch(channel_id):
            raise HTTPException(status_code=400, detail="Choose a valid text channel.")
        if channel_id not in {channel["id"] for channel in store.bot_text_channels(guild_id)}:
            raise HTTPException(status_code=404, detail="BirdBot cannot access that text channel.")
    if command_name in {"play", "q"}:
        payload["track"] = command_music_track(payload.get("query"))
    member_id = payload.get("member_id")
    if command_name in {"profile", "kick", "ban", "warning", "show_warning", "timeout", "untimeout", "mute", "unmute"}:
        if not isinstance(member_id, str) or not DISCORD_SNOWFLAKE.fullmatch(member_id):
            raise HTTPException(status_code=400, detail="Choose a valid server member.")
        # Do not reject a valid ID merely because the dashboard's synchronised
        # member snapshot is briefly behind Discord. The bot worker performs
        # the authoritative live guild-member lookup before it sends a
        # profile, kick, or ban request.
    if command_name == "unban":
        if not isinstance(member_id, str) or not DISCORD_SNOWFLAKE.fullmatch(member_id) or member_id not in {ban["user_id"] for ban in store.bot_bans(guild_id)}:
            raise HTTPException(status_code=404, detail="Choose a current ban entry.")
    if command_name in {"kick", "ban", "warning", "timeout", "untimeout", "mute", "unmute"}:
        reason = payload.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 512:
            raise HTTPException(status_code=400, detail="The reason must be 512 characters or fewer.")
    if command_name == "ban":
        delete_days = payload.get("delete_message_days", 0)
        if not isinstance(delete_days, int) or not 0 <= delete_days <= 7:
            raise HTTPException(status_code=400, detail="Message deletion days must be between 0 and 7.")
        payload["delete_message_seconds"] = delete_days * 86_400
    if command_name == "unwarning":
        warning_id = payload.get("warning_id")
        if isinstance(warning_id, str) and warning_id.isdigit():
            warning_id = int(warning_id)
        if isinstance(warning_id, bool) or not isinstance(warning_id, int) or warning_id < 1:
            raise HTTPException(status_code=400, detail="Enter a valid warning number.")
        payload["warning_id"] = warning_id
    if command_name == "timeout":
        # The dashboard sends a friendly amount/unit pair. Accept the old
        # minute-only field too so saved clients and API users remain valid.
        amount = payload.get("duration_amount")
        unit = str(payload.get("duration_unit") or "m").strip().casefold()
        raw_duration = payload.get("duration_minutes", 10) if amount in (None, "") else f"{amount}{unit}"
        parsed_duration = parse_timeout_duration(raw_duration)
        if not parsed_duration:
            raise HTTPException(status_code=400, detail="Use a valid timeout such as 10m, 2h, 1d, or 1w (maximum 28 days).")
        payload["duration_minutes"], payload["duration_label"] = parsed_duration
    if command_name == "delete":
        amount = payload.get("amount", 10)
        if isinstance(amount, str) and amount.isdigit():
            amount = int(amount)
        if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 100:
            raise HTTPException(status_code=400, detail="Delete amount must be between 1 and 100 messages.")
        payload["amount"] = amount
    queue_name = f"music_{command_name}" if is_music_command else command_name
    request_id = store.queue_command(guild_id, channel_id, queue_name, user["id"], payload)
    return {"request_id": request_id, "status": "pending"}


@app.post("/api/guilds/{guild_id}/control/server-message")
async def queue_server_message(guild_id: str, request: Request) -> dict[str, str]:
    """Queue a normal or embed announcement for the global Discord bot.

    The dashboard never sends through a second Discord client.  It validates
    the selected channel here, then hands the small payload to BirdBot's
    low-latency command worker, which performs the final live permission check
    immediately before sending.
    """
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before sending messages.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Choose a message style and write a message first.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid server message data.")
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not DISCORD_SNOWFLAKE.fullmatch(channel_id):
        raise HTTPException(status_code=400, detail="Choose a valid text channel.")
    if channel_id not in {channel["id"] for channel in store.bot_text_channels(guild_id)}:
        raise HTTPException(status_code=404, detail="BirdBot cannot access that text channel.")

    raw_mentions = payload.get("mention_user_ids", [])
    if raw_mentions is None:
        raw_mentions = []
    if not isinstance(raw_mentions, list) or len(raw_mentions) > 25:
        raise HTTPException(status_code=400, detail="Choose up to 25 members to mention.")
    mention_user_ids: list[str] = []
    for member_id in raw_mentions:
        if not isinstance(member_id, str) or not DISCORD_SNOWFLAKE.fullmatch(member_id):
            raise HTTPException(status_code=400, detail="One of the selected members is invalid. Choose them again.")
        if member_id not in mention_user_ids:
            mention_user_ids.append(member_id)
    reply_to = payload.get("reply_to", "")
    if reply_to is None:
        reply_to = ""
    if not isinstance(reply_to, str) or len(reply_to.strip()) > 200:
        raise HTTPException(status_code=400, detail="The reply message link or ID is invalid.")
    reply_to = reply_to.strip()
    if reply_to and not (DISCORD_SNOWFLAKE.fullmatch(reply_to) or DISCORD_MESSAGE_LINK.fullmatch(reply_to)):
        raise HTTPException(status_code=400, detail="Paste a valid Discord message link or message ID to reply.")

    message_type = str(payload.get("message_type") or "normal").strip().lower()
    if message_type not in {"normal", "embed"}:
        raise HTTPException(status_code=400, detail="Choose either a normal message or an embed message.")
    normalized: dict[str, object] = {
        "message_type": message_type,
        "mention_user_ids": mention_user_ids,
        "reply_to": reply_to,
    }
    if message_type == "normal":
        content = payload.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="Write a message before sending it.")
        content = content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="Write a message before sending it.")
        if len(content) > 2_000:
            raise HTTPException(status_code=400, detail="Normal messages must be 2,000 characters or fewer.")
        normalized["content"] = content
    else:
        title = payload.get("title", "")
        description = payload.get("description")
        if not isinstance(title, str) or not isinstance(description, str):
            raise HTTPException(status_code=400, detail="Provide an embed description and optional title.")
        title = title.strip()
        description = description.strip()
        if not description:
            raise HTTPException(status_code=400, detail="Write an embed description before sending it.")
        if len(title) > 256:
            raise HTTPException(status_code=400, detail="Embed titles must be 256 characters or fewer.")
        if len(description) > 4_096:
            raise HTTPException(status_code=400, detail="Embed descriptions must be 4,096 characters or fewer.")
        normalized.update({"title": title, "description": description})
    try:
        request_id = store.queue_command(guild_id, channel_id, "server_message", user["id"], normalized)
    except Exception as error:
        print(f"Server message queue failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="The message queue is temporarily unavailable. Please try again.") from error
    return {"request_id": request_id, "status": "pending"}


def dashboard_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().casefold() in {"0", "false", "no", "off", ""}:
        return False
    raise HTTPException(status_code=400, detail="The checkbox value is invalid.")


def _dashboard_snowflake(value: object, *, allow_empty: bool = False) -> str | None:
    """Normalize an ID coming from a dashboard form without trusting it."""
    if value in (None, "") and allow_empty:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="Choose a valid Discord channel or member.")
    value = value.strip()
    if not DISCORD_SNOWFLAKE.fullmatch(value):
        raise HTTPException(status_code=400, detail="Choose a valid Discord channel or member.")
    return value


def parse_temp_vc_config(guild_id: str, payload: object) -> dict[str, object]:
    """Validate Temp VC settings against the bot's current Discord snapshot."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Temp VC settings.")
    enabled = dashboard_bool(payload.get("enabled"), default=False)
    lobby_channel_id = _dashboard_snowflake(payload.get("lobby_channel_id"), allow_empty=True)
    available_voice = {str(channel.get("id")): channel for channel in store.bot_voice_channels(guild_id)}
    if enabled and (not lobby_channel_id or lobby_channel_id not in available_voice):
        raise HTTPException(status_code=400, detail="Choose a voice channel as the Temp VC lobby.")
    if lobby_channel_id and lobby_channel_id not in available_voice:
        raise HTTPException(status_code=404, detail="That Temp VC lobby is no longer available.")
    category_id = _dashboard_snowflake(payload.get("category_id"), allow_empty=True)
    available_categories = {str(category.get("id")) for category in store.bot_categories(guild_id)}
    if category_id and category_id not in available_categories:
        raise HTTPException(status_code=404, detail="That Temp VC category is no longer available.")
    template = payload.get("channel_name_template", "{owner}'s room")
    if template is None:
        template = "{owner}'s room"
    if not isinstance(template, str):
        raise HTTPException(status_code=400, detail="The channel name template must be text.")
    template = template.strip() or "{owner}'s room"
    if len(template) > 100 or any(character in template for character in "\r\n"):
        raise HTTPException(status_code=400, detail="The channel name template must be 1-100 characters without line breaks.")
    raw_limit = payload.get("user_limit", 0)
    if isinstance(raw_limit, bool):
        raise HTTPException(status_code=400, detail="User limit must be a whole number from 0 to 99.")
    try:
        user_limit = int(str(raw_limit).strip())
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="User limit must be a whole number from 0 to 99.") from error
    if not 0 <= user_limit <= 99:
        raise HTTPException(status_code=400, detail="User limit must be a whole number from 0 to 99.")
    return {
        "enabled": enabled,
        "lobby_channel_id": lobby_channel_id,
        "category_id": category_id,
        "channel_name_template": template,
        "user_limit": user_limit,
    }


def parse_temp_vc_action(payload: object, guild_id: str) -> dict[str, object]:
    """Validate one live Temp VC management action before queueing it."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Temp VC action.")
    action = str(payload.get("action") or "").strip().casefold()
    allowed = {"rename", "lock", "unlock", "block", "unblock", "limit", "transfer", "kick"}
    if action not in allowed:
        raise HTTPException(status_code=400, detail="Choose a valid Temp VC action.")
    channel_id = _dashboard_snowflake(payload.get("channel_id"))
    if not store.temp_vc_channel(guild_id, str(channel_id)):
        raise HTTPException(status_code=404, detail="That temporary voice channel is no longer active.")
    normalized: dict[str, object] = {"action": action, "channel_id": str(channel_id)}
    if action in {"block", "unblock", "transfer", "kick"}:
        normalized["member_id"] = _dashboard_snowflake(payload.get("member_id"))
    if action == "rename":
        name = payload.get("name")
        if not isinstance(name, str):
            raise HTTPException(status_code=400, detail="Enter a channel name.")
        name = name.strip()
        if not 1 <= len(name) <= 100 or any(character in name for character in "\r\n"):
            raise HTTPException(status_code=400, detail="Channel names must be 1-100 characters without line breaks.")
        normalized["name"] = name
    if action == "limit":
        raw_limit = payload.get("user_limit", 0)
        if isinstance(raw_limit, bool):
            raise HTTPException(status_code=400, detail="User limit must be a whole number from 0 to 99.")
        try:
            user_limit = int(str(raw_limit).strip())
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail="User limit must be a whole number from 0 to 99.") from error
        if not 0 <= user_limit <= 99:
            raise HTTPException(status_code=400, detail="User limit must be a whole number from 0 to 99.")
        normalized["user_limit"] = user_limit
    return normalized


@app.get("/api/guilds/{guild_id}/control/temp-vc")
async def get_dashboard_temp_vc(guild_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing Temp VC.")
    return {
        "config": store.temp_vc_config(guild_id),
        "channels": store.temp_vc_channels(guild_id),
        "voice_channels": store.bot_voice_channels(guild_id),
        "categories": store.bot_categories(guild_id),
        "members": store.bot_members(guild_id, limit=100),
    }


@app.post("/api/guilds/{guild_id}/control/temp-vc")
async def save_dashboard_temp_vc(guild_id: str, request: Request) -> dict[str, object]:
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before changing Temp VC settings.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid Temp VC settings.") from error
    normalized = parse_temp_vc_config(guild_id, payload)
    config = store.save_temp_vc_config(guild_id, updated_by=str(user["id"]), **normalized)
    return {"config": config, "channels": store.temp_vc_channels(guild_id), "status": "saved"}


@app.post("/api/guilds/{guild_id}/control/temp-vc/action")
async def queue_dashboard_temp_vc_action(guild_id: str, request: Request) -> dict[str, object]:
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing Temp VC.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid Temp VC action.") from error
    normalized = parse_temp_vc_action(payload, guild_id)
    request_id = store.queue_command(
        guild_id,
        str(normalized["channel_id"]),
        "temp_vc_action",
        str(user["id"]),
        normalized,
    )
    return {"request_id": request_id, "status": "pending"}


def parse_vc_presence_config(guild_id: str, payload: object) -> dict[str, object]:
    """Validate one safe placement for a host-configured VC bot slot."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid VC bot settings.")
    raw_slot = payload.get("slot")
    try:
        slot = int(str(raw_slot).strip())
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Choose a valid VC bot slot.") from error
    if not 1 <= slot <= VC_BOT_SLOT_COUNT:
        raise HTTPException(status_code=400, detail=f"Choose a VC bot slot from 1 to {VC_BOT_SLOT_COUNT}.")
    enabled = dashboard_bool(payload.get("enabled"), default=False)
    channel_id = _dashboard_snowflake(payload.get("channel_id"), allow_empty=True)
    available_voice = {str(channel.get("id")) for channel in store.bot_voice_channels(guild_id)}
    if enabled and (not channel_id or channel_id not in available_voice):
        raise HTTPException(status_code=400, detail="Choose a voice channel for this VC bot.")
    if channel_id and channel_id not in available_voice:
        raise HTTPException(status_code=404, detail="That voice channel is no longer available.")
    return {"slot": slot, "enabled": enabled, "channel_id": channel_id}


@app.get("/api/guilds/{guild_id}/control/vc")
async def get_dashboard_vc_presence(guild_id: str, request: Request) -> dict[str, object]:
    user, _ = await verified_guild_manager(guild_id, request)
    require_vc_premium(user["id"])
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing VC bots.")
    configs = store.vc_presence_configs(guild_id, VC_BOT_SLOT_COUNT)
    manager = getattr(request.app.state, "vc_presence", None)
    if manager is not None:
        statuses = manager.status_for_guild(guild_id)
    else:
        # The web process can remain available while the Discord worker is
        # restarting. Keep this response token-free and useful in that state.
        by_slot = {int(item["slot"]): item for item in configs}
        statuses = [
            {
                "slot": slot,
                "configured": vc_bot_slot_configured(slot),
                "online": False,
                "bot_name": None,
                "bot_id": None,
                "guild_count": 0,
                "enabled": bool(by_slot.get(slot, {}).get("enabled")),
                "channel_id": by_slot.get(slot, {}).get("channel_id"),
                "channel_name": None,
                "connected": False,
                "error": "Voice bot worker is reconnecting.",
            }
            for slot in range(1, VC_BOT_SLOT_COUNT + 1)
        ]
    return {
        "slots": statuses,
        "configs": configs,
        "voice_channels": store.bot_voice_channels(guild_id),
        "status": "ok",
    }


@app.post("/api/guilds/{guild_id}/control/vc")
async def save_dashboard_vc_presence(guild_id: str, request: Request) -> dict[str, object]:
    user, _ = await verified_guild_manager(guild_id, request)
    require_vc_premium(user["id"])
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before changing VC bots.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid VC bot settings.") from error
    normalized = parse_vc_presence_config(guild_id, payload)
    slot = int(normalized["slot"])
    manager = getattr(request.app.state, "vc_presence", None)
    configured = manager.slot_configured(slot) if manager is not None else vc_bot_slot_configured(slot)
    if not configured:
        raise HTTPException(
            status_code=409,
            detail=f"VC bot slot {slot} is not configured on the host. Add VC_BOT_{slot}_TOKEN as a private secret, then restart the worker.",
        )
    config = store.save_vc_presence_config(
        guild_id,
        slot,
        channel_id=normalized.get("channel_id"),
        enabled=bool(normalized.get("enabled")),
        updated_by=str(user["id"]),
    )
    request_id = store.queue_command(
        guild_id,
        str(normalized.get("channel_id") or "0"),
        "vc_presence_action",
        str(user["id"]),
        normalized,
    )
    return {"config": config, "request_id": request_id, "status": "pending"}


async def dashboard_multipart_payload(request: Request) -> tuple[dict[str, object], UploadFile | None]:
    """Read the JSON payload and optional upload used by profile/DM forms."""
    content_type = (request.headers.get("content-type") or "").casefold()
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail="The dashboard upload form could not be read.") from error
        raw_payload = form.get("payload")
        if raw_payload is None:
            raw_payload = "{}"
        if not isinstance(raw_payload, str):
            raise HTTPException(status_code=400, detail="Invalid dashboard form data.")
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail="Invalid dashboard form data.") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid dashboard form data.")
        upload = form.get("media") or form.get("avatar")
        if upload is not None and not isinstance(upload, UploadFile) and not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="The uploaded file is invalid.")
        return payload, upload if upload is not None else None
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Enter the settings before saving.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid dashboard data.")
    return payload, None


@app.get("/api/guilds/{guild_id}/control/profile")
async def get_dashboard_profile(guild_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing its profile.")
    return {"profile": public_bot_profile(store.bot_profile(guild_id))}


@app.post("/api/guilds/{guild_id}/control/profile")
async def save_dashboard_profile(guild_id: str, request: Request) -> dict[str, object]:
    """Queue a per-server nickname/avatar update for the one Discord bot."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing its profile.")
    payload, upload = await dashboard_multipart_payload(request)
    nickname = payload.get("nickname", "")
    if not isinstance(nickname, str):
        raise HTTPException(status_code=400, detail="The bot nickname must be text.")
    nickname = nickname.strip()
    if len(nickname) > 32:
        raise HTTPException(status_code=400, detail="The bot nickname must be 32 characters or fewer.")
    remove_avatar = dashboard_bool(payload.get("remove_avatar"), default=False)
    existing = store.bot_profile(guild_id)
    avatar_action = "keep"
    avatar_path: str | None = str(existing.get("avatar_path") or "") or None
    avatar_preview_url: str | None = str(existing.get("avatar_url") or "") or None
    new_avatar_path: str | None = None
    if upload is not None and upload.filename:
        if remove_avatar:
            raise HTTPException(status_code=400, detail="Choose an avatar image or remove the current avatar, not both.")
        avatar_preview_url, new_avatar_path = await save_profile_avatar(guild_id, upload)
        avatar_path = new_avatar_path
        avatar_action = "set"
    elif remove_avatar:
        avatar_action = "remove"
        avatar_path = None
        avatar_preview_url = None
    command_payload = {
        "nickname": nickname,
        "avatar_action": avatar_action,
        "avatar_path": avatar_path,
        "avatar_preview_url": avatar_preview_url,
        "previous_avatar_url": str(existing.get("avatar_url") or "") or None,
        "previous_avatar_path": str(existing.get("avatar_path") or "") or None,
        "updated_by": user["id"],
    }
    try:
        request_id = store.queue_command(guild_id, "0", "bot_profile", user["id"], command_payload)
    except Exception as error:
        remove_managed_file(new_avatar_path, BOT_PROFILE_AVATAR_DIR)
        print(f"Bot profile queue failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="The bot profile queue is temporarily unavailable. Please try again.") from error
    return {
        "request_id": request_id,
        "status": "pending",
        "profile": public_bot_profile({
            **existing,
            "guild_id": guild_id,
            "nickname": nickname,
            "avatar_url": avatar_preview_url,
            "avatar_path": avatar_path,
        }),
    }


def parse_auto_react_payload(payload: object) -> tuple[str | None, str, str, bool]:
    """Validate one channel/emoji rule from the Bot Settings panel."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid auto-react settings.")
    rule_id = payload.get("rule_id")
    if rule_id in (None, ""):
        normalized_rule_id = None
    elif isinstance(rule_id, str) and re.fullmatch(r"[a-fA-F0-9]{32}", rule_id):
        normalized_rule_id = rule_id
    else:
        raise HTTPException(status_code=400, detail="The auto-react rule identifier is invalid.")
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not DISCORD_SNOWFLAKE.fullmatch(channel_id):
        raise HTTPException(status_code=400, detail="Choose a valid text channel for the auto-react rule.")
    emoji = payload.get("emoji")
    if not isinstance(emoji, str):
        raise HTTPException(status_code=400, detail="Enter an emoji for the auto-react rule.")
    emoji = emoji.strip()
    if not emoji or len(emoji) > 100 or any(character in emoji for character in "\r\n"):
        raise HTTPException(status_code=400, detail="Use one emoji or custom emoji (up to 100 characters).")
    enabled = dashboard_bool(payload.get("enabled"), default=True)
    return normalized_rule_id, channel_id, emoji, enabled


def parse_ai_payload(payload: object, current: dict[str, object]) -> dict[str, object]:
    """Validate the per-server AI toggle and its response channel."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid AI assistant settings.")
    enabled = dashboard_bool(payload.get("enabled"), default=bool(current.get("enabled", False)))
    channel_value = payload.get("channel_id", current.get("channel_id"))
    channel_id = _dashboard_snowflake(channel_value, allow_empty=True)
    if enabled and not channel_id:
        raise HTTPException(status_code=400, detail="Choose a text channel for the AI assistant.")
    return {"enabled": enabled, "channel_id": channel_id}


def parse_automod_payload(payload: object, current: dict[str, object]) -> dict[str, object]:
    """Validate the Bot Settings Automod form and keep moderation conservative."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid Automod settings.")

    def boolean(name: str, fallback: bool) -> bool:
        value = payload.get(name, fallback)
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str) and value.strip().casefold() in {"true", "false", "1", "0", "on", "off"}:
            return value.strip().casefold() in {"true", "1", "on"}
        raise HTTPException(status_code=400, detail=f"{name} must be true or false.")

    def integer(name: str, fallback: int, minimum: int, maximum: int) -> int:
        value = payload.get(name, fallback)
        if isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"{name} must be a number.")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=f"{name} must be a number.") from error
        if not minimum <= normalized <= maximum:
            raise HTTPException(status_code=400, detail=f"{name} must be between {minimum} and {maximum}.")
        return normalized

    raw_words = payload.get("banned_words_list", current.get("banned_words_list", []))
    if isinstance(raw_words, str):
        raw_words = raw_words.replace("\r", "\n").split("\n")
    if not isinstance(raw_words, list) or len(raw_words) > 100:
        raise HTTPException(status_code=400, detail="Enter up to 100 banned words or phrases.")
    words: list[str] = []
    for raw_word in raw_words:
        if not isinstance(raw_word, str):
            raise HTTPException(status_code=400, detail="Each banned word must be text.")
        word = " ".join(raw_word.split()).strip()
        if not word:
            continue
        if len(word) > 64:
            raise HTTPException(status_code=400, detail="Each banned word or phrase must be 64 characters or fewer.")
        if word.casefold() not in {item.casefold() for item in words}:
            words.append(word)

    timeout_amount = payload.get("auto_timeout_amount")
    if timeout_amount not in (None, ""):
        parsed_auto_timeout = parse_timeout_duration(f"{timeout_amount}{str(payload.get('auto_timeout_unit') or 'm').strip().casefold()}")
        if not parsed_auto_timeout:
            raise HTTPException(status_code=400, detail="Use a valid Auto-timeout duration such as 10m, 2h, 1d, or 1w (maximum 28 days).")
        auto_timeout_minutes = parsed_auto_timeout[0]
    else:
        auto_timeout_minutes = integer("auto_timeout_minutes", int(current.get("auto_timeout_minutes", 10)), 1, TIMEOUT_MAX_MINUTES)

    return {
        "enabled": boolean("enabled", bool(current.get("enabled", False))),
        "anti_link": boolean("anti_link", bool(current.get("anti_link", False))),
        "anti_spam": boolean("anti_spam", bool(current.get("anti_spam", False))),
        "banned_words": boolean("banned_words", bool(current.get("banned_words", False))),
        "raid_protection": boolean("raid_protection", bool(current.get("raid_protection", False))),
        "auto_warning": boolean("auto_warning", bool(current.get("auto_warning", False))),
        "auto_timeout": boolean("auto_timeout", bool(current.get("auto_timeout", False))),
        "banned_words_list": words,
        "spam_message_limit": integer("spam_message_limit", int(current.get("spam_message_limit", 5)), 3, 30),
        "spam_window_seconds": integer("spam_window_seconds", int(current.get("spam_window_seconds", 8)), 3, 60),
        "raid_join_limit": integer("raid_join_limit", int(current.get("raid_join_limit", 8)), 3, 50),
        "raid_window_seconds": integer("raid_window_seconds", int(current.get("raid_window_seconds", 10)), 5, 120),
        "auto_timeout_minutes": auto_timeout_minutes,
    }


@app.get("/api/guilds/{guild_id}/control/bot-settings")
async def get_dashboard_bot_settings(guild_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing Bot Settings.")
    return {
        "auto_reacts": store.auto_reacts(guild_id),
        "automod": store.automod_config(guild_id),
        "ai": store.ai_config(guild_id),
        "ai_available": bool(GROQ_API_KEY),
    }


@app.get("/api/guilds/{guild_id}/control/bot-settings/ai")
async def get_dashboard_ai(guild_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing Bot Settings.")
    return {"ai": store.ai_config(guild_id), "available": bool(GROQ_API_KEY)}


@app.post("/api/guilds/{guild_id}/control/bot-settings/ai")
async def save_dashboard_ai(guild_id: str, request: Request) -> dict[str, object]:
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before changing Bot Settings.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid AI assistant settings.") from error
    current = store.ai_config(guild_id)
    normalized = parse_ai_payload(payload, current)
    channel_id = normalized.get("channel_id")
    if channel_id and channel_id not in {str(channel.get("id")) for channel in store.bot_text_channels(guild_id)}:
        raise HTTPException(status_code=404, detail="BirdBot cannot access that AI text channel.")
    config = store.save_ai_config(
        guild_id,
        enabled=bool(normalized["enabled"]),
        channel_id=str(channel_id) if channel_id else None,
        updated_by=str(user["id"]),
    )
    return {"ai": config, "available": bool(GROQ_API_KEY), "status": "saved"}


@app.post("/api/guilds/{guild_id}/control/bot-settings/automod")
async def save_dashboard_automod(guild_id: str, request: Request) -> dict[str, object]:
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before changing Bot Settings.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid Automod settings.") from error
    current = store.automod_config(guild_id)
    normalized = parse_automod_payload(payload, current)
    config = store.save_automod_config(guild_id, updated_by=str(user["id"]), **normalized)
    return {"automod": config, "status": "saved"}


@app.post("/api/guilds/{guild_id}/control/bot-settings/auto-react")
async def save_dashboard_auto_react(guild_id: str, request: Request) -> dict[str, object]:
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before changing Bot Settings.")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid auto-react settings.") from error
    rule_id, channel_id, emoji, enabled = parse_auto_react_payload(payload)
    if channel_id not in {str(channel.get("id")) for channel in store.bot_text_channels(guild_id)}:
        raise HTTPException(status_code=404, detail="BirdBot cannot access that text channel.")
    if rule_id and not any(str(rule.get("rule_id")) == rule_id for rule in store.auto_reacts(guild_id)):
        raise HTTPException(status_code=404, detail="That auto-react rule no longer exists.")
    store.save_auto_react(guild_id, channel_id, emoji, enabled, str(user["id"]), rule_id)
    return {"auto_reacts": store.auto_reacts(guild_id), "status": "saved"}


@app.delete("/api/guilds/{guild_id}/control/bot-settings/auto-react/{rule_id}")
async def delete_dashboard_auto_react(guild_id: str, rule_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before changing Bot Settings.")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", rule_id):
        raise HTTPException(status_code=400, detail="The auto-react rule identifier is invalid.")
    if not store.delete_auto_react(guild_id, rule_id):
        raise HTTPException(status_code=404, detail="That auto-react rule no longer exists.")
    return {"auto_reacts": store.auto_reacts(guild_id), "status": "deleted"}


@app.post("/api/guilds/{guild_id}/control/dm-message")
async def queue_dashboard_dm_message(guild_id: str, request: Request) -> dict[str, object]:
    """Queue a private message to one member or every human server member."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before sending DMs.")
    payload, upload = await dashboard_multipart_payload(request)
    recipient_mode = str(payload.get("recipient_mode") or "member").strip().casefold()
    if recipient_mode not in {"member", "everyone"}:
        raise HTTPException(status_code=400, detail="Choose one member or everyone in the server.")
    member_id = payload.get("member_id")
    if recipient_mode == "member" and (not isinstance(member_id, str) or not DISCORD_SNOWFLAKE.fullmatch(member_id)):
        raise HTTPException(status_code=400, detail="Choose a valid server member.")
    raw_mentions = payload.get("mention_user_ids", [])
    if raw_mentions is None:
        raw_mentions = []
    if not isinstance(raw_mentions, list) or len(raw_mentions) > 25:
        raise HTTPException(status_code=400, detail="Choose up to 25 members to mention.")
    mentions: list[str] = []
    for mention_id in raw_mentions:
        if not isinstance(mention_id, str) or not DISCORD_SNOWFLAKE.fullmatch(mention_id):
            raise HTTPException(status_code=400, detail="One of the selected members is invalid.")
        if mention_id not in mentions:
            mentions.append(mention_id)
    message_type = str(payload.get("message_type") or "normal").strip().casefold()
    if message_type not in {"normal", "embed"}:
        raise HTTPException(status_code=400, detail="Choose either a normal message or an embed message.")
    normalized: dict[str, object] = {
        "recipient_mode": recipient_mode,
        "member_id": member_id if recipient_mode == "member" else None,
        "message_type": message_type,
        "mention_user_ids": mentions,
    }
    if message_type == "normal":
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(status_code=400, detail="Write a private message before sending it.")
        content = content.strip()
        if len(content) > 2_000:
            raise HTTPException(status_code=400, detail="Normal messages must be 2,000 characters or fewer.")
        normalized["content"] = content
    else:
        title = payload.get("title", "")
        description = payload.get("description")
        if not isinstance(title, str) or not isinstance(description, str) or not description.strip():
            raise HTTPException(status_code=400, detail="Write an embed description before sending it.")
        title, description = title.strip(), description.strip()
        if len(title) > 256 or len(description) > 4_096:
            raise HTTPException(status_code=400, detail="The embed is longer than Discord allows.")
        normalized.update({"title": title, "description": description})
    saved_media_path: str | None = None
    if upload is not None and upload.filename:
        saved_media_path, media_filename, media_content_type = await save_dm_media(guild_id, upload)
        normalized.update({"media_path": saved_media_path, "media_filename": media_filename, "media_content_type": media_content_type})
    try:
        request_id = store.queue_command(guild_id, "0", "dm_message", user["id"], normalized)
    except Exception as error:
        remove_managed_file(saved_media_path, DM_MEDIA_DIR)
        print(f"DM message queue failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="The DM message queue is temporarily unavailable. Please try again.") from error
    return {"request_id": request_id, "status": "pending"}


def parse_role_payload(payload: object, *, require_both: bool = True) -> dict[str, object]:
    """Validate the small role payload shared by create and edit routes."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid role data.")
    name = payload.get("name")
    color = payload.get("color")
    if require_both and (not isinstance(name, str) or not isinstance(color, str)):
        raise HTTPException(status_code=400, detail="Provide a role name and a six-digit color.")
    if name is not None:
        if not isinstance(name, str):
            raise HTTPException(status_code=400, detail="Role names must be text.")
        name = name.strip()
        if not 1 <= len(name) <= 100 or name.casefold() == "@everyone":
            raise HTTPException(status_code=400, detail="Role names must be 1–100 characters and cannot be @everyone.")
    else:
        name = ""
    if color is not None:
        if not isinstance(color, str) or not ROLE_COLOR.fullmatch(color.strip()):
            raise HTTPException(status_code=400, detail="Choose a valid six-digit hexadecimal color.")
        color = color.strip().upper()
        if not color.startswith("#"):
            color = f"#{color}"
    else:
        color = ""
    normalized: dict[str, object] = {"name": name, "color": color}
    if "permissions" in payload:
        raw_permissions = payload.get("permissions")
        if not isinstance(raw_permissions, dict):
            raise HTTPException(status_code=400, detail="Role permissions must be an object.")
        unknown = sorted(set(raw_permissions) - set(ROLE_PERMISSION_KEYS))
        if unknown:
            raise HTTPException(status_code=400, detail="The role permissions contain an unknown option.")
        normalized["permissions"] = {
            key: bool(raw_permissions.get(key, False))
            for key in ROLE_PERMISSION_KEYS
        }
    return normalized


@app.post("/api/guilds/{guild_id}/control/roles")
async def create_dashboard_role(guild_id: str, request: Request) -> dict[str, str]:
    """Queue a role creation after dashboard authorization checks."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing roles.")
    try:
        payload = parse_role_payload(await request.json())
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Provide a role name and color first.") from error
    try:
        request_id = store.queue_command(guild_id, "0", "role_create", user["id"], payload)
    except Exception as error:
        print(f"Role create queue failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="The role queue is temporarily unavailable. Please try again.") from error
    return {"request_id": request_id, "status": "pending"}


@app.patch("/api/guilds/{guild_id}/control/roles/{role_id}")
async def edit_dashboard_role(guild_id: str, role_id: str, request: Request) -> dict[str, str]:
    """Queue a role name, color, and optional permissions update."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing roles.")
    if not DISCORD_SNOWFLAKE.fullmatch(role_id):
        raise HTTPException(status_code=400, detail="The role identifier is invalid.")
    if role_id not in {str(role["id"]) for role in store.bot_roles(guild_id)}:
        raise HTTPException(status_code=404, detail="That role is no longer available.")
    try:
        payload = parse_role_payload(await request.json())
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Provide a role name and color first.") from error
    payload["role_id"] = role_id
    try:
        request_id = store.queue_command(guild_id, "0", "role_edit", user["id"], payload)
    except Exception as error:
        print(f"Role edit queue failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="The role queue is temporarily unavailable. Please try again.") from error
    return {"request_id": request_id, "status": "pending"}


@app.delete("/api/guilds/{guild_id}/control/roles/{role_id}")
async def delete_dashboard_role(guild_id: str, role_id: str, request: Request) -> dict[str, str]:
    """Queue deletion of a manageable guild role."""
    user, _ = await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before managing roles.")
    if not DISCORD_SNOWFLAKE.fullmatch(role_id):
        raise HTTPException(status_code=400, detail="The role identifier is invalid.")
    if role_id not in {str(role["id"]) for role in store.bot_roles(guild_id)}:
        raise HTTPException(status_code=404, detail="That role is no longer available.")
    try:
        request_id = store.queue_command(guild_id, "0", "role_delete", user["id"], {"role_id": role_id})
    except Exception as error:
        print(f"Role delete queue failed for {guild_id}: {error}")
        raise HTTPException(status_code=503, detail="The role queue is temporarily unavailable. Please try again.") from error
    return {"request_id": request_id, "status": "pending"}


@app.post("/api/guilds/{guild_id}/commands/{command_name}")
async def queue_command_endpoint(guild_id: str, command_name: str, request: Request) -> dict[str, str]:
    if command_name not in set(DASHBOARD_COMMAND_NAMES) | {"unban"}:
        raise HTTPException(status_code=404, detail="That command is not available.")
    return await queue_dashboard_command(guild_id, command_name, request)


@app.get("/api/guilds/{guild_id}/members/search")
async def search_guild_members(guild_id: str, request: Request, q: str = "") -> dict[str, object]:
    """Search the complete bot-synced roster by ID, username, global name, or nickname."""
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing members.")
    query = q.strip()[:80]
    if not query:
        return {"members": []}
    return {"members": store.bot_members(guild_id, query, limit=250)}


@app.get("/api/guilds/{guild_id}/members")
async def guild_members(guild_id: str, request: Request, query: str = "") -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing members.")
    # The roster viewer is an administrative feature and should show every
    # account in the server, including bot accounts.  Target selectors keep
    # their safer human-only default via ``search_guild_members``.
    return {"members": store.bot_members(guild_id, query[:80], limit=10_000, include_bots=True)}


@app.get("/api/guilds/{guild_id}/bans")
async def guild_bans(guild_id: str, request: Request) -> dict[str, object]:
    await verified_guild_manager(guild_id, request)
    if not store.is_guild_activated(guild_id):
        raise HTTPException(status_code=409, detail="Enable BirdBot for this server before viewing bans.")
    return {"bans": store.bot_bans(guild_id)}


@app.get("/api/command-requests/{request_id}")
async def command_request_status(request_id: str, request: Request) -> dict[str, object]:
    user = logged_in_user(request)
    command_request = store.command_request_for_user(request_id, user["id"])
    if not command_request:
        raise HTTPException(status_code=404, detail="The command request was not found.")
    return command_request


@app.post("/api/guilds/{guild_id}/activate")
async def activate_guild(guild_id: str, request: Request) -> dict[str, object]:
    """Enable one guild after four server-side authorization checks."""
    user, bot_guild = await verified_guild_manager(guild_id, request)
    activation = store.activate_guild(guild_id, user["id"])
    return {"guild": {**bot_guild, **activation}}


async def verified_guild_manager(
    guild_id: str,
    request: Request,
    *,
    fresh: bool = True,
) -> tuple[dict[str, str], dict[str, object]]:
    """Apply the same four authorization checks to every guild state change."""
    if not DISCORD_SNOWFLAKE.fullmatch(guild_id):
        raise HTTPException(status_code=400, detail="The server identifier is invalid.")
    user = logged_in_user(request)
    user_guilds = await discord_user_guilds(request, user, fresh=fresh)
    user_guild = user_guilds.get(guild_id)
    bot_guilds = store.bot_guilds()

    # 1: a fresh heartbeat and the bot's own published guild list prove membership.
    if not store.bot_is_online() or guild_id not in bot_guilds:
        raise HTTPException(status_code=404, detail="BirdBot is not currently available in that server.")
    # 2 and 4: this guild must be returned by Discord for this authenticated user.
    if not user_guild:
        raise HTTPException(status_code=403, detail="You are not a member of that server.")
    # 3: Discord's current OAuth guild data must say Owner or Administrator.
    if not guild_is_manageable(user_guild):
        raise HTTPException(status_code=403, detail="Owner or Administrator permission is required for that server.")
    return user, bot_guilds[guild_id]


async def verified_ticket_actor(
    guild_id: str,
    request: Request,
    require_support_role: bool = False,
    fresh: bool = False,
) -> tuple[dict[str, str], dict[str, object]]:
    """Authorize ticket actions for an owner/admin or configured support staff.

    The live Discord worker performs the final role and hierarchy checks. This
    gate only decides whether the dashboard may enqueue an action at all.
    """
    if not DISCORD_SNOWFLAKE.fullmatch(guild_id):
        raise HTTPException(status_code=400, detail="The server identifier is invalid.")
    user = logged_in_user(request)
    user_guilds = await discord_user_guilds(request, user, fresh=fresh)
    user_guild = user_guilds.get(guild_id)
    bot_guilds = store.bot_guilds()
    if not store.bot_is_online() or guild_id not in bot_guilds:
        raise HTTPException(status_code=404, detail="BirdBot is not currently available in that server.")
    if not user_guild:
        raise HTTPException(status_code=403, detail="You are not a member of that server.")
    member = store.bot_member(guild_id, user["id"])
    raw_roles = store.ticket_config(guild_id).get("support_role_ids")
    configured = {str(role_id) for role_id in raw_roles} if isinstance(raw_roles, list) else set()
    raw_member_roles = (member or {}).get("role_ids")
    member_roles = {str(role_id) for role_id in raw_member_roles} if isinstance(raw_member_roles, list) else set()
    has_support_role = bool(configured and configured.intersection(member_roles))
    if require_support_role and not has_support_role:
        raise HTTPException(status_code=403, detail=SUPPORT_ROLE_ERROR)
    can_configure = guild_is_manageable(user_guild)
    if not can_configure and not has_support_role:
        raise HTTPException(status_code=403, detail="A configured support role or Administrator permission is required.")
    return user, {
        **bot_guilds[guild_id],
        "can_configure": can_configure,
        "has_support_role": has_support_role,
    }


@app.post("/api/guilds/{guild_id}/disable")
async def disable_guild(guild_id: str, request: Request) -> dict[str, object]:
    """Disable features only in the selected, authorized guild."""
    user, bot_guild = await verified_guild_manager(guild_id, request)
    activation = store.disable_guild(guild_id, user["id"])
    return {"guild": {**bot_guild, **activation}}
