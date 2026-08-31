"""Shared, private runtime settings for BirdBot and its local dashboard."""

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX") or "!"
GUILD_ID = os.getenv("GUILD_ID")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_OWNER_ID = os.getenv("DISCORD_OWNER_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET")
DATA_PATH = Path(os.getenv("BIRDBOT_DATA_PATH") or (BASE_DIR / "data" / "birdbot.sqlite3"))
SUPPORT_URL = os.getenv("SUPPORT_URL")
# Render exposes RENDER_EXTERNAL_URL automatically for web services. Use it as
# the hosted default so a separate .env file is not required on Render; an
# explicit DASHBOARD_PUBLIC_URL still wins for a custom domain.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
DASHBOARD_PUBLIC_URL = os.getenv("DASHBOARD_PUBLIC_URL") or RENDER_EXTERNAL_URL or "http://127.0.0.1:8000"
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI") or f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/auth/discord/callback"

# Spotify OAuth credentials are kept server-side.  The dashboard never receives
# either the client secret or a Spotify access/refresh token.
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI") or f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/auth/spotify/callback"
# Optional premium gate for Spotify linking.  Keep it disabled until the
# deployment has a real entitlement source (for example Stripe) or an
# operator-managed allowlist.  URL playback never depends on this setting.
SPOTIFY_PREMIUM_REQUIRED = os.getenv("SPOTIFY_PREMIUM_REQUIRED", "0").strip().casefold() in {"1", "true", "yes", "on"}
SPOTIFY_PREMIUM_USER_IDS = frozenset(
    value.strip() for value in os.getenv("SPOTIFY_PREMIUM_USER_IDS", "").split(",") if value.strip()
)
