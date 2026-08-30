"""Shared, private runtime settings for BirdBot and its local dashboard."""

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
GUILD_ID = os.getenv("GUILD_ID")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_OWNER_ID = os.getenv("DISCORD_OWNER_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET")
DATA_PATH = Path(os.getenv("BIRDBOT_DATA_PATH", BASE_DIR / "data" / "birdbot.sqlite3"))
SUPPORT_URL = os.getenv("SUPPORT_URL")
# Base URL used in transcript links shared with Discord log messages.
DASHBOARD_PUBLIC_URL = os.getenv("DASHBOARD_PUBLIC_URL") or "http://127.0.0.1:8000"

# Spotify OAuth credentials are kept server-side.  The dashboard never receives
# either the client secret or a Spotify access/refresh token.
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI") or f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/auth/spotify/callback"
