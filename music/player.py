"""A small per-guild voice player used by the dashboard music controls.

Spotify supplies metadata and playlists, not playable audio streams.  Tracks
are therefore resolved to an audio source with yt-dlp and decoded by FFmpeg.
The player intentionally lives in the bot process so the web app never opens
or duplicates a Discord voice connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import random
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

import discord
from discord import opus

from storage import store

try:
    import yt_dlp
except ImportError:  # pragma: no cover - dependency is optional until music is used
    yt_dlp = None

try:
    import imageio_ffmpeg
except ImportError:  # pragma: no cover - requirements install this for music playback
    imageio_ffmpeg = None


class _TrackedAudioSource(discord.AudioSource):
    """Track the first decoded frame without changing Discord's audio API.

    ``VoiceClient.is_playing()`` only reports that the player thread is alive;
    it does not guarantee that FFmpeg is producing audio.  Wrapping the source
    lets the watchdog distinguish a healthy stream from a process that started
    but never yielded a frame (expired CDN URLs are a common cause).
    """

    def __init__(self, source: discord.AudioSource) -> None:
        self.source = source
        self.created_monotonic = time.monotonic()
        self.last_frame_monotonic: float | None = None
        self.frame_count = 0

    def read(self) -> bytes:
        data = self.source.read()
        if data:
            self.last_frame_monotonic = time.monotonic()
            self.frame_count += 1
        return data

    def is_opus(self) -> bool:
        return bool(self.source.is_opus())

    def cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.source.cleanup()

    def __getattr__(self, name: str) -> Any:
        # Preserve useful FFmpeg attributes (notably ``process``) for the
        # watchdog and diagnostics while keeping this wrapper transparent to
        # discord.py.
        return getattr(self.source, name)


class MusicPlayer:
    # YouTube stream URLs are short-lived. Keep them in memory long enough for
    # seeks/replays to be immediate, then resolve a fresh URL periodically.
    _STREAM_CACHE_SECONDS = 600.0
    _STREAM_RESOLVE_TIMEOUT_SECONDS = 20.0
    _VOICE_CONNECT_TIMEOUT_SECONDS = 20.0
    _SPOTIFY_PREVIEW_SECONDS = 30.0
    _IDLE_DISCONNECT_SECONDS = 180.0
    _AUDIO_START_TIMEOUT_SECONDS = 5.0

    def __init__(self, bot: Any, guild: discord.Guild) -> None:
        self.bot = bot
        self.guild = guild
        self.voice_client: discord.VoiceClient | None = guild.voice_client
        self.queue: list[dict[str, object]] = []
        self.history: list[dict[str, object]] = []
        self.current: dict[str, object] | None = None
        self.paused = True
        self.volume = 1.0
        self.position = 0.0
        self.duration = 0.0
        self.started_monotonic: float | None = None
        self.controller_id: int | None = None
        self.loop_enabled = False
        self.shuffle_enabled = False
        self.queue_finished = False
        self.last_error: str | None = None
        self._user_absent_since: float | None = None
        self._disconnect_task: asyncio.Task[None] | None = None
        # A Discord voice connection can disappear independently of the web
        # request that started playback.  Keep one bounded reconnect worker so
        # a transient gateway/UDP failure does not leave the player silently
        # stuck while never creating a second voice client.
        self._reconnect_task: asyncio.Task[None] | None = None
        self._resume_after_reconnect = False
        self._idle_since: float | None = None
        self._idle_disconnect_task: asyncio.Task[None] | None = None
        # FFmpeg can spawn successfully and then exit before Discord receives
        # an audio frame (expired CDN URL, blocked format, etc.).  This task
        # is a bounded five-second watchdog for that otherwise-silent failure.
        self._audio_watchdog_task: asyncio.Task[None] | None = None
        # URL, duration, cache timestamp, and the extractor's HTTP headers.
        # YouTube/CDN URLs can reject FFmpeg unless it sends the same headers
        # yt-dlp used while resolving the source.
        self._stream_cache: dict[str, tuple[str, float, float, dict[str, str]]] = {}
        # A CDN URL can occasionally expire or be closed before FFmpeg has
        # decoded the first packet. Keep a small per-track retry budget so a
        # transient failure does not immediately skip a perfectly playable
        # song (and never retry forever for a genuinely bad source).
        self._stream_retry_count: dict[str, int] = {}
        self._using_opus_source = False
        self._prefetch_tasks: dict[str, asyncio.Task[None]] = {}
        # Incremented whenever a source is intentionally replaced/stopped.
        # Discord invokes ``after`` from another thread, so a boolean flag can
        # race with Skip/Seek and let the old callback overwrite new state.
        self._playback_generation = 0
        # Unlike playback generation, this only changes for user actions that
        # replace a source. It lets a naturally finished callback detect that
        # Skip/Seek happened while it was awaiting the next stream lookup.
        self._command_generation = 0
        self._play_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._storage_error_logged = False
        self._last_state_persisted_at = 0.0
        self._save(force=True)

    def _state(self) -> dict[str, object]:
        connected = bool(self.voice_client and self.voice_client.is_connected())
        channel = getattr(self.voice_client, "channel", None) if connected and self.voice_client else None
        connection_state = "reconnecting" if self._reconnect_task else ("ready" if connected else "disconnected")
        return {
            "connected": connected,
            "connection_state": connection_state,
            "voice_channel_id": str(channel.id) if channel else None,
            "voice_channel_name": channel.name if channel else None,
            # Keep the complete metadata queue available to dashboard
            # clients; it contains no stream URLs or other large payloads.
            "queue": self.queue[:],
            "current": self.current,
            "paused": self.paused,
            "loop_enabled": self.loop_enabled,
            "shuffle_enabled": self.shuffle_enabled,
            "queue_finished": self.queue_finished,
            "last_error": self.last_error,
            "volume": round(self.volume, 3),
            "position": round(self.current_position(), 1),
            "duration": round(self.duration, 1),
        }

    def _save(self, *, force: bool = True) -> None:
        # SQLite serialization is deliberately limited to metadata.  Runtime
        # voice clients and stream URLs are never persisted.
        try:
            now = time.monotonic()
            persist = force or now - self._last_state_persisted_at >= 1.0
            store.save_music_state(str(self.guild.id), self._state(), persist=persist)
            if persist:
                self._last_state_persisted_at = now
            self._storage_error_logged = False
        except Exception as error:  # pragma: no cover - depends on local SQLite/runtime failures
            # A storage hiccup must not take down Discord playback or the
            # dashboard worker.  The next state tick retries persistence.
            if not self._storage_error_logged:
                print(f"Music state could not be saved for guild {self.guild.id}: {error}")
                self._storage_error_logged = True

    def current_position(self) -> float:
        if self.current and self.started_monotonic is not None and not self.paused:
            return min(self.duration or float("inf"), self.position + (time.monotonic() - self.started_monotonic))
        return self.position

    async def connect(self, member: discord.Member) -> discord.VoiceChannel:
        """Connect/move the single guild voice client without races."""
        async with self._connection_lock:
            channel = await self._connect(member)
            if self._resume_after_reconnect and self.current and self.voice_client and self.voice_client.is_connected():
                if not self.voice_client.is_playing() and not self.voice_client.is_paused():
                    await self._play_current(self.position)
                self._resume_after_reconnect = False
            return channel

    async def _connect(self, member: discord.Member) -> discord.VoiceChannel:
        if not member.voice or not member.voice.channel:
            raise ValueError("Error: You must be in a Voice Channel to start the bot.")
        channel = member.voice.channel
        previous_controller_id = self.controller_id
        # A voice connection may have been created outside this player (for
        # example after a reconnect). Reuse the guild's live client first.
        self._clear_disconnected_voice()
        if self.voice_client is None:
            self.voice_client = self.guild.voice_client
        # Import the same encryption dependencies used by Discord's voice
        # handshake. `import nacl` alone can succeed while voice support is
        # still incomplete (current discord.py also requires davey).
        try:
            from nacl.secret import SecretBox  # noqa: F401
            import davey  # noqa: F401
        except ImportError as error:
            raise ValueError(
                "Discord voice support is incomplete. Install the voice dependencies with "
                "`python -m pip install -U \"discord.py[voice]\" davey`, then restart BirdBot."
            ) from error
        bot_member = self.guild.me
        if bot_member is None and getattr(self.bot, "user", None) is not None:
            bot_member = self.guild.get_member(self.bot.user.id)
        if bot_member is None:
            raise ValueError("BirdBot is still loading its server permissions. Please try again in a moment.")
        permissions = channel.permissions_for(bot_member)
        if not permissions.view_channel:
            raise ValueError("BirdBot cannot see that Voice Channel. Allow View Channel for the bot in that voice channel.")
        if not permissions.connect:
            raise ValueError("BirdBot cannot join that Voice Channel. Allow Connect for the bot in that voice channel.")
        if not permissions.speak:
            raise ValueError("BirdBot can join that Voice Channel but cannot speak. Allow Speak for the bot in that voice channel.")
        user_limit = int(getattr(channel, "user_limit", 0) or 0)
        channel_members = list(getattr(channel, "members", []) or [])
        already_in_channel = any(getattr(member, "id", None) == getattr(bot_member, "id", None) for member in channel_members)
        if user_limit and len(channel_members) >= user_limit and not already_in_channel:
            # Discord only bypasses a hard user limit for administrators; the
            # Move Members permission alone is not enough to enter a full VC.
            if not bool(getattr(permissions, "administrator", False)):
                raise ValueError("That Voice Channel is full. Free a slot or allow BirdBot to bypass the user limit.")
        try:
            if self.voice_client and self.voice_client.is_connected():
                current_channel = getattr(self.voice_client, "channel", None)
                if not current_channel or current_channel.id != channel.id:
                    await asyncio.wait_for(
                        self.voice_client.move_to(channel),
                        timeout=self._VOICE_CONNECT_TIMEOUT_SECONDS,
                    )
            else:
                self.voice_client = await asyncio.wait_for(
                    channel.connect(),
                    timeout=self._VOICE_CONNECT_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError as error:
            self.controller_id = previous_controller_id
            raise ValueError("Discord voice connection timed out. Check the channel permissions and try again.") from error
        except discord.Forbidden as error:
            self.controller_id = previous_controller_id
            raise ValueError("Discord denied access to that Voice Channel. Check Connect, Speak, and View Channel on the voice-channel permissions.") from error
        except discord.ClientException as error:
            self.controller_id = previous_controller_id
            message = str(error).casefold()
            if "already connected" in message or "already playing" in message:
                raise ValueError("BirdBot is already connected to another Voice Channel in this server.") from error
            raise ValueError("BirdBot could not create a voice connection. Please disconnect it from Discord and try again.") from error
        except RuntimeError as error:
            self.controller_id = previous_controller_id
            message = str(error).casefold()
            if "pynacl" in message or "davey" in message:
                raise ValueError(
                    "Discord voice support is incomplete. Install the voice dependencies with "
                    "`python -m pip install -U \"discord.py[voice]\" davey`, then restart BirdBot."
                ) from error
            raise ValueError(f"Discord voice connection failed: {error}") from error
        except discord.HTTPException as error:
            self.controller_id = previous_controller_id
            raise ValueError(f"Discord could not join that Voice Channel (error {getattr(error, 'code', 'unknown')}). Try again shortly.") from error
        except discord.DiscordException as error:
            self.controller_id = previous_controller_id
            raise ValueError("Discord could not connect to that Voice Channel. Try again shortly.") from error
        if not self.voice_client or not self.voice_client.is_connected():
            self.controller_id = previous_controller_id
            raise ValueError("BirdBot did not reach the voice channel ready state. Please try again shortly.")
        connected_channel = getattr(self.voice_client, "channel", None)
        if connected_channel is not None and getattr(connected_channel, "id", None) != channel.id:
            self.controller_id = previous_controller_id
            raise ValueError("BirdBot connected to a different Voice Channel. Please try again.")
        self.controller_id = member.id
        self._user_absent_since = None
        self._idle_since = None
        if self._idle_disconnect_task and self._idle_disconnect_task is not asyncio.current_task():
            self._idle_disconnect_task.cancel()
            self._idle_disconnect_task = None
        if self._reconnect_task and self._reconnect_task is not asyncio.current_task():
            self._reconnect_task.cancel()
        self._save()
        return channel

    async def ensure_connected(self, member: discord.Member) -> discord.VoiceChannel:
        """Reuse or establish the guild's single voice connection.

        Dashboard play/queue actions use this pre-flight instead of assuming
        that the earlier Start request is still connected.  It intentionally
        delegates to :meth:`connect`, so all permission and DAVE/voice
        dependency checks remain in one place.
        """
        self._clear_disconnected_voice()
        if self.voice_client and self.voice_client.is_connected():
            channel = getattr(self.voice_client, "channel", None)
            requested = getattr(getattr(member, "voice", None), "channel", None)
            if channel and requested and channel.id == requested.id:
                return channel
        return await self.connect(member)

    async def ensure_playback_ready(self, member: discord.Member) -> discord.VoiceChannel:
        """Run playback pre-flight without moving the bot between channels."""
        if not member.voice or not member.voice.channel:
            raise ValueError("Error: You must be in a Voice Channel to manage playback.")
        self._clear_disconnected_voice()
        if self.voice_client and self.voice_client.is_connected():
            bot_channel = getattr(self.voice_client, "channel", None)
            if not bot_channel or bot_channel.id != member.voice.channel.id:
                raise ValueError("Error: You must be in the same voice channel as the bot to manage playback.")
            return bot_channel
        return await self.connect(member)

    def _clear_disconnected_voice(self) -> None:
        """Drop a stale client while retaining enough state to resume audio."""
        if self.voice_client and not self.voice_client.is_connected():
            self._resume_after_reconnect = self._resume_after_reconnect or bool(self.current and not self.paused)
            self.voice_client = None
            self._using_opus_source = False

    def _cancel_audio_watchdog(self) -> None:
        task = self._audio_watchdog_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._audio_watchdog_task = None

    def _consume_background_error(self, task: asyncio.Task[Any], label: str) -> None:
        """Consume task failures so one voice recovery cannot poison the loop."""
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception:
            return
        if error:
            print(f"Music {label} failed in guild {self.guild.id}: {error}")

    async def _watch_audio_start(self, generation: int, track_key: str) -> None:
        """Recover a source that never begins producing playable audio."""
        try:
            await asyncio.sleep(self._AUDIO_START_TIMEOUT_SECONDS)
            if generation != self._playback_generation or self.paused:
                return
            if not self.current or self._track_key(self.current) != track_key:
                return
            voice_client = self.voice_client
            if not voice_client or not voice_client.is_connected():
                return
            if voice_client.is_paused():
                return
            source = getattr(voice_client, "source", None)
            # ``is_playing`` can remain true while FFmpeg has produced zero
            # frames.  The tracked wrapper is authoritative when present.
            if isinstance(source, _TrackedAudioSource):
                if source.frame_count > 0:
                    return
            elif voice_client.is_playing():
                # Older/custom sources cannot expose frame counts.  Keep the
                # previous process check for those sources.
                process = getattr(source, "process", None)
                if process is None or process.poll() is None:
                    return
            process = getattr(source, "process", None)
            if process is not None and process.poll() is None and not isinstance(source, _TrackedAudioSource):
                return
            self._stream_cache.pop(track_key, None)
            self.last_error = "Error: Failed to stream track. Skipping to next..."
            await self._track_finished(
                RuntimeError("audio source produced no frames"),
                generation,
                self._command_generation,
            )
        except asyncio.CancelledError:
            return
        except Exception as error:  # pragma: no cover - defensive watchdog
            print(f"Music audio watchdog failed in guild {self.guild.id}: {error}")
        finally:
            if self._audio_watchdog_task is asyncio.current_task():
                self._audio_watchdog_task = None

    async def _reconnect_voice(self) -> None:
        """Recover an unexpected voice disconnect without another Start click.

        A transient gateway/UDP failure is not a user action, so the session
        remains active while the controller is still in (or can rejoin) voice.
        Retry with a capped backoff.  If the controller never returns for the
        normal idle window, stop the session and expose a fresh Start state.
        """
        retry_delay = 1.0
        disconnected_at = time.monotonic()
        try:
            while self.controller_id:
                await asyncio.sleep(retry_delay)
                member = self.guild.get_member(self.controller_id)
                channel = getattr(getattr(member, "voice", None), "channel", None) if member else None
                if channel is None:
                    # Do not spin while the person is offline.  Give them the
                    # same three-minute idle grace period as a connected
                    # player, then clear the session cleanly.
                    self.last_error = "BirdBot is waiting for you to rejoin your voice channel."
                    self._save()
                    if time.monotonic() - disconnected_at >= self._IDLE_DISCONNECT_SECONDS:
                        await self.stop()
                        return
                    retry_delay = min(10.0, retry_delay * 1.5)
                    continue
                try:
                    await self.ensure_connected(member)
                    if self.voice_client and self.voice_client.is_connected():
                        if self.current and self.voice_client.is_playing():
                            # ``move_to`` can preserve an active source.  Do
                            # not call play() a second time on that client.
                            pass
                        elif self.current and self._resume_after_reconnect:
                            await self._play_current(self.position)
                        elif self.current and self.paused:
                            # Preserve an intentional pause across a channel
                            # move/reconnect; the next dashboard Resume action
                            # will start the source again.
                            pass
                        elif self.current:
                            await self._play_current(self.position)
                        elif self.queue:
                            await self.play_next()
                        self._resume_after_reconnect = False
                        self.last_error = None
                        self._save()
                        return
                except (ValueError, discord.DiscordException, OSError, RuntimeError) as error:
                    self.last_error = "Voice connection interrupted. Reconnecting..."
                    self._save()
                    print(f"Music voice reconnect failed in guild {self.guild.id}: {error}")
                retry_delay = min(15.0, retry_delay * 1.5)
        finally:
            self._reconnect_task = None

    def _idle_conditions_met(self) -> bool:
        voice_client = self.voice_client
        if not voice_client or not voice_client.is_connected():
            return False
        channel = getattr(voice_client, "channel", None)
        if channel is None:
            return False
        humans = [member for member in (getattr(channel, "members", []) or []) if not getattr(member, "bot", False)]
        return not humans or (self.current is None and not self.queue)

    async def _disconnect_after_idle(self) -> None:
        """Disconnect after three quiet minutes and reset the Start state."""
        try:
            await asyncio.sleep(self._IDLE_DISCONNECT_SECONDS)
            if self._idle_conditions_met():
                self.last_error = "BirdBot disconnected after 3 minutes of inactivity."
                await self.stop()
        except asyncio.CancelledError:
            return
        finally:
            if self._idle_disconnect_task is asyncio.current_task():
                self._idle_disconnect_task = None

    @staticmethod
    def _extract_audio(query: str) -> tuple[str, float, dict[str, str]]:
        # Direct public audio files do not need an extractor. This also makes
        # links from smaller providers work when they expose an ordinary
        # MP3/M4A/OGG/WAV stream rather than a yt-dlp-specific page.
        try:
            parsed = urlparse(query)
            extension = parsed.path.casefold().rsplit(".", 1)[-1] if "." in parsed.path else ""
            if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname and extension in {"aac", "flac", "m4a", "mp3", "oga", "ogg", "opus", "wav", "webm"}:
                return query, 0.0, {}
        except ValueError:
            pass
        if yt_dlp is None:
            raise ValueError("Music playback needs yt-dlp and FFmpeg installed on the server.")
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "default_search": "ytsearch",
            # Prefer an audio-only stream. Some YouTube clients return a
            # video-first format for ``bestaudio/best`` which FFmpeg cannot
            # open reliably when the signed URL expires.
            "format": "bestaudio[acodec!=none]/bestaudio/best",
            "skip_download": True,
            "cachedir": False,
            "socket_timeout": 12,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
            "concurrent_fragment_downloads": 2,
            "geo_bypass": True,
            # Current YouTube extraction requires yt-dlp's EJS challenge
            # solver. Prefer Deno when available, then fall back to Node in
            # PATH (Windows users commonly already have Node installed).
            "js_runtimes": {"deno": {}, "node": {}},
            # The PyPI extra normally bundles EJS; allowing the GitHub source
            # keeps older installations recoverable after an upgrade.
            "remote_components": {"ejs:github"},
        }
        runtime_path = os.getenv("YTDLP_JS_RUNTIME_PATH", "").strip()
        if runtime_path:
            # yt-dlp accepts an explicit runtime path through the Python API.
            options["js_runtimes"] = {"node": {"path": runtime_path}}
        cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
        temporary_cookie_file: str | None = None
        if cookies_file:
            # Render mounts secret files read-only.  yt-dlp may refresh the
            # Netscape cookie jar while extracting, so pass it a private,
            # writable copy instead of the mounted `/etc/secrets` path.
            try:
                with open(cookies_file, "rb") as source:
                    with tempfile.NamedTemporaryFile(
                        prefix="birdbot-ytdlp-",
                        suffix=".cookies.txt",
                        delete=False,
                    ) as destination:
                        destination.write(source.read())
                        temporary_cookie_file = destination.name
                options["cookiefile"] = temporary_cookie_file
            except OSError as error:
                raise ValueError(
                    "The configured YouTube cookies file could not be read. "
                    "Check YTDLP_COOKIES_FILE and the Render secret file."
                ) from error
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(query, download=False)
                if info and info.get("entries"):
                    info = next((entry for entry in info["entries"] if entry), None)
                if not info or not info.get("url"):
                    raise ValueError("No playable audio was found for that track.")
                raw_headers = info.get("http_headers")
                headers = {
                    str(key): str(value)
                    for key, value in raw_headers.items()
                    if key and value is not None
                } if isinstance(raw_headers, dict) else {}
                return str(info["url"]), float(info.get("duration") or 0), headers
        except ValueError:
            raise
        except Exception as error:
            detail = str(error)
            lowered = detail.casefold()
            # YouTube may challenge traffic from hosted/datacenter IPs even
            # when yt-dlp's JavaScript challenge runtime is installed. Keep
            # this distinct from a missing track so the dashboard can tell the
            # operator which server-side credential/configuration is needed.
            if "sign in to confirm" in lowered or "not a bot" in lowered:
                print(
                    "Music source lookup blocked by YouTube anti-bot check. "
                    "Configure YTDLP_COOKIES_FILE (a private Render secret file) "
                    "or use another audio provider."
                )
                raise ValueError(
                    "YouTube blocked playback from this server. Add a private "
                    "YTDLP_COOKIES_FILE secret in Render, then redeploy."
                ) from error
            print(f"Music source lookup failed for {query!r}: {error}")
            raise ValueError("The track could not be resolved for playback.") from error
        finally:
            if temporary_cookie_file:
                with contextlib.suppress(OSError):
                    os.remove(temporary_cookie_file)

    @staticmethod
    def _track_key(track: dict[str, object]) -> str:
        artist = str(track.get("artist") or "Unknown artist")
        title = str(track.get("name") or "Unknown track")
        source_url = str(track.get("source_url") or track.get("url") or "").strip()
        return str(track.get("id") or source_url or f"{artist}\x00{title}")

    async def _resolve_track_audio(self, track: dict[str, object]) -> tuple[str, float, dict[str, str]]:
        """Resolve a fresh stream URL for Spotify metadata or a public URL.

        Spotify metadata occasionally points at a track whose YouTube source
        is temporarily unavailable (age restrictions, a provider outage, or
        an expired CDN signature). A short Spotify preview is still a valid
        audio source when one is supplied, so the player remains responsive
        instead of reporting a false queue-wide failure.
        """
        artist = str(track.get("artist") or "Unknown artist")
        title = str(track.get("name") or "Unknown track")
        last_error: Exception | None = None
        source_url = str(track.get("source_url") or track.get("url") or "").strip()
        if source_url:
            # The dashboard validates this as an HTTP(S) public URL. Pass it
            # directly to yt-dlp so YouTube and every provider supported by
            # the installed extractor keeps the exact URL the user entered.
            if not source_url.casefold().startswith(("http://", "https://")):
                raise ValueError("The audio URL must use HTTP or HTTPS.")
            return await asyncio.wait_for(
                asyncio.to_thread(self._extract_audio, source_url),
                timeout=self._STREAM_RESOLVE_TIMEOUT_SECONDS,
            )
        # Put the title first. A hyphen between artist and title can be
        # interpreted as an exclusion operator by search providers, causing
        # valid tracks to return no result. SoundCloud is a lightweight
        # fallback for tracks that are not available through YouTube.
        queries = (f"ytsearch1:{title} {artist}", f"scsearch1:{title} {artist}")
        for index, query in enumerate(queries):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._extract_audio, query),
                    timeout=self._STREAM_RESOLVE_TIMEOUT_SECONDS if index == 0 else 10.0,
                )
            except asyncio.TimeoutError as error:
                last_error = error
                break
            except (ValueError, OSError, RuntimeError, discord.DiscordException) as error:
                last_error = error
                if index == 0:
                    continue
                break
        preview_url = str(track.get("preview_url") or "").strip()
        if preview_url.startswith("https://"):
            metadata_duration = float(track.get("duration_ms") or 0) / 1000
            duration = min(
                self._SPOTIFY_PREVIEW_SECONDS,
                metadata_duration or self._SPOTIFY_PREVIEW_SECONDS,
            )
            return preview_url, duration, {}
        if last_error:
            raise last_error
        raise ValueError("No playable audio was found for that track.")

    def _cached_stream(self, key: str) -> tuple[str, float, dict[str, str]] | None:
        entry = self._stream_cache.get(key)
        if entry is None:
            return None
        stream_url, duration, cached_at, headers = entry
        if time.monotonic() - cached_at >= self._STREAM_CACHE_SECONDS:
            self._stream_cache.pop(key, None)
            return None
        return stream_url, duration, headers

    async def _prefetch_stream(self, track: dict[str, object]) -> None:
        key = self._track_key(track)
        try:
            if self._cached_stream(key) is not None:
                return
            stream_url, duration, headers = await self._resolve_track_audio(track)
            self._stream_cache[key] = (stream_url, float(duration or 0), time.monotonic(), headers)
        except (ValueError, OSError, RuntimeError, asyncio.TimeoutError):
            # Prefetch is an optimization. Playback performs a normal resolve
            # and reports a useful error if the source cannot be found.
            return
        except Exception as error:  # pragma: no cover - defensive guard for third-party extractors
            print(f"Music prefetch failed for guild {self.guild.id}: {error}")
            return
        finally:
            self._prefetch_tasks.pop(key, None)

    def _schedule_prefetch(self, track: dict[str, object]) -> None:
        key = self._track_key(track)
        if self._cached_stream(key) is not None or key in self._prefetch_tasks:
            return
        try:
            self._prefetch_tasks[key] = asyncio.create_task(self._prefetch_stream(track))
        except RuntimeError:
            # The event loop may be closing while the bot shuts down.
            self._prefetch_tasks.pop(key, None)

    async def _play_current(
        self,
        start_seconds: float = 0.0,
        *,
        expected_command_generation: int | None = None,
    ) -> None:
        if not self.current or not self.voice_client or not self.voice_client.is_connected():
            self.paused = True
            self._save()
            return
        # A stream lookup runs in a worker thread. If the user presses
        # Skip/Stop while it is resolving, abandon this stale lookup instead
        # of starting the old track after the newer command has taken effect.
        if expected_command_generation is not None and expected_command_generation != self._command_generation:
            return
        self._cancel_audio_watchdog()
        track = self.current
        track_key = self._track_key(track)
        cached = self._cached_stream(track_key)
        if cached is None:
            # Queueing a track starts a background lookup. Re-use that work
            # instead of starting a second yt-dlp request when the track is
            # skipped to.
            prefetch = self._prefetch_tasks.get(track_key)
            if prefetch is not None:
                await prefetch
                if expected_command_generation is not None and expected_command_generation != self._command_generation:
                    return
                cached = self._cached_stream(track_key)
        if cached is None:
            try:
                stream_url, duration, headers = await self._resolve_track_audio(track)
                if expected_command_generation is not None and expected_command_generation != self._command_generation:
                    return
            except asyncio.TimeoutError as error:
                self.paused = True
                self.started_monotonic = None
                self.last_error = "Error: Track lookup timed out. Skipping to next..."
                self._stream_cache.pop(track_key, None)
                self._save()
                raise ValueError("The track lookup timed out. Try again shortly.") from error
            except ValueError as error:
                self.paused = True
                self.started_monotonic = None
                self.last_error = "Error: Failed to stream track. Skipping to next..."
                print(f"Music track resolution failed in guild {self.guild.id} for {track.get('name')!r}: {error}")
                self._save()
                # Keep the dashboard and Discord-facing error consistent;
                # the queue worker will skip this source and try the next one.
                raise ValueError("Error: Failed to stream track. Skipping to next...") from error
            duration = float(duration or 0)
            self._stream_cache[track_key] = (stream_url, duration, time.monotonic(), headers)
        else:
            stream_url, duration, headers = cached
        self.duration = max(0.0, duration or float(track.get("duration_ms") or 0) / 1000)
        self.position = max(0.0, min(float(start_seconds), self.duration or float(start_seconds)))
        before_options = (
            f"-ss {self.position:.2f} -reconnect 1 -reconnect_streamed 1 "
            "-reconnect_at_eof 1 -reconnect_on_network_error 1 -reconnect_delay_max 5"
        )
        if headers:
            # Preserve only the protocol headers FFmpeg needs for signed CDN
            # URLs. Passing every browser header (especially cookies and
            # fetch metadata) can make FFmpeg reject the option string on
            # Windows before it even opens the stream.
            def safe_option(value: object) -> str:
                return str(value).replace(chr(13), "").replace(chr(10), "").replace('"', "").strip()

            user_agent = next((value for key, value in headers.items() if key.casefold() == "user-agent"), None)
            referer = next((value for key, value in headers.items() if key.casefold() == "referer"), None)
            if user_agent:
                before_options += f' -user_agent "{safe_option(user_agent)}"'
            if referer:
                before_options += f' -referer "{safe_option(referer)}"'
        try:
            if expected_command_generation is not None and expected_command_generation != self._command_generation:
                return
            # discord.py does not ship an FFmpeg binary.  Render's native
            # runtimes provide a system FFmpeg, which is preferred there
            # because it matches the host's libraries.  The bundled
            # imageio-ffmpeg binary remains a portable fallback for hosts
            # (especially Windows) where system FFmpeg is unavailable.
            executables: list[str] = []
            configured = os.getenv("FFMPEG_PATH", "").strip()
            if configured:
                executables.append(configured)
            executables.append("ffmpeg")
            if imageio_ffmpeg is not None:
                with contextlib.suppress(Exception):
                    bundled = imageio_ffmpeg.get_ffmpeg_exe()
                    if bundled:
                        executables.append(str(bundled))
            # Preserve order while avoiding duplicate paths (for example when
            # FFMPEG_PATH points at the imageio binary).
            executables = list(dict.fromkeys(executables))
            source = None
            using_opus_source = False
            source_error: Exception | None = None
            # FFmpegPCMAudio requires a locally loaded libopus encoder in
            # discord.py. Windows installations often have PyNaCl/davey but
            # no opus DLL, which makes playback fail even though voice
            # connection succeeds. FFmpegOpusAudio encodes Opus itself and is
            # a reliable fallback in that situation.
            source_factories = []
            if opus.is_loaded():
                source_factories.append((discord.FFmpegPCMAudio, False))
            source_factories.append((discord.FFmpegOpusAudio, True))
            for executable in executables:
                for factory, opus_source in source_factories:
                    candidate = None
                    try:
                        candidate = factory(
                            stream_url,
                            executable=executable,
                            before_options=before_options,
                            options=(
                                "-vn -sn -dn -nostdin -loglevel warning"
                                + (f' -af "volume={self.volume:.3f}"' if opus_source else "")
                            ),
                            **({"bitrate": 128} if opus_source else {}),
                        )
                        if not opus_source:
                            candidate = discord.PCMVolumeTransformer(candidate, volume=self.volume)
                        source = candidate
                        using_opus_source = opus_source
                        break
                    except (discord.ClientException, OSError, RuntimeError, TypeError, ValueError) as error:
                        source_error = error
                if source is not None:
                    break
            if source is None:
                print(f"Music FFmpeg startup failed in guild {self.guild.id}: {source_error}")
                raise source_error or RuntimeError("FFmpeg could not be started")
            # Keep a tiny amount of per-source telemetry so a subprocess that
            # starts but never emits a decodable frame can be retried instead
            # of leaving the player silently frozen.
            source = _TrackedAudioSource(source)
            self._using_opus_source = using_opus_source
            loop = self.bot.loop
            self._playback_generation += 1
            playback_generation = self._playback_generation
            command_generation = self._command_generation

            def consume_finished(future: Any) -> None:
                # Reading the result prevents "Task exception was never
                # retrieved" noise when Discord ends a source during shutdown.
                with contextlib.suppress(Exception):
                    future.result()

            def finished(error: Exception | None) -> None:
                if playback_generation != self._playback_generation:
                    return
                if loop.is_closed():
                    return
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._track_finished(error, playback_generation, command_generation),
                        loop,
                    )
                    future.add_done_callback(consume_finished)
                except (RuntimeError, TypeError):
                    # The loop can close between the check and scheduling.
                    return

            self.voice_client.play(source, after=finished)
        except (discord.ClientException, opus.OpusNotLoaded, RuntimeError, OSError, TypeError, ValueError) as error:
            message = str(error).casefold()
            if "ffmpeg" in message or isinstance(error, (RuntimeError, OSError)):
                playback_error = ValueError(
                    "FFmpeg could not start playback. BirdBot now uses the bundled "
                    "imageio-ffmpeg binary; reinstall requirements and restart BirdBot."
                )
            else:
                playback_error = ValueError("FFmpeg could not start playback.")
            self.paused = True
            self.started_monotonic = None
            self.last_error = "Error: Failed to stream track. Skipping to next..."
            # FFmpeg failures commonly mean the short-lived source URL has
            # expired. Remove it so a later retry performs a fresh lookup.
            self._stream_cache.pop(track_key, None)
            self._save()
            raise playback_error from error
        self.paused = False
        self.started_monotonic = time.monotonic()
        self.last_error = None
        self.queue_finished = False
        self._save()
        # Keep a lightweight guard against FFmpeg processes that exit cleanly
        # before Discord receives any audio.  Normal playback and all source
        # callbacks invalidate this task through the generation check.
        self._audio_watchdog_task = asyncio.create_task(
            self._watch_audio_start(playback_generation, track_key)
        )
        self._audio_watchdog_task.add_done_callback(
            lambda task: self._consume_background_error(task, "audio watchdog")
        )

    async def _track_finished(
        self,
        error: Exception | None = None,
        playback_generation: int | None = None,
        command_generation: int | None = None,
    ) -> None:
        # The callback can be queued just before Skip/Seek increments the
        # generation. Ignore that stale callback before it touches player
        # state or advances the queue.
        if playback_generation is not None and playback_generation != self._playback_generation:
            return
        if command_generation is not None and command_generation != self._command_generation:
            return
        self._cancel_audio_watchdog()
        was_playing = bool(self.current and not self.paused)
        if error:
            # Keep the player usable while exposing the failure through state.
            # The first failures are usually short-lived CDN URLs or a
            # transient network drop. Resolve a fresh source and retry the
            # same track before advancing the queue.
            print(f"Music stream ended with an error in guild {self.guild.id}: {error}")
            self.paused = True
            retry_key = self._track_key(self.current) if self.current else None
            if retry_key:
                self._stream_cache.pop(retry_key, None)
                retries = self._stream_retry_count.get(retry_key, 0)
                if retries < 2 and self.voice_client and self.voice_client.is_connected():
                    self._stream_retry_count[retry_key] = retries + 1
                    try:
                        self.position = 0.0
                        self.started_monotonic = None
                        await asyncio.sleep(0.25)
                        # Skip/stop may have replaced this source while the
                        # retry was waiting. Do not let a stale callback take
                        # over the new track.
                        if playback_generation is not None and playback_generation != self._playback_generation:
                            return
                        if command_generation is not None and command_generation != self._command_generation:
                            return
                        if not self.current or self._track_key(self.current) != retry_key:
                            return
                        await self._play_current(expected_command_generation=command_generation)
                        return
                    except Exception as retry_error:
                        print(f"Music stream retry failed in guild {self.guild.id}: {retry_error}")
                self._stream_retry_count.pop(retry_key, None)
            self.last_error = "Error: Failed to stream track. Skipping to next..."
        self.position = 0.0
        self.started_monotonic = None
        # A manually skipped/stopped track invalidates its callback generation,
        # so reaching this method means the source ended naturally (or failed).
        # Only repeat after a clean completion; retrying a failed source would
        # otherwise create a tight error loop.
        if not error and self.loop_enabled and self.current and self.voice_client and self.voice_client.is_connected():
            if playback_generation is not None and playback_generation != self._playback_generation:
                return
            try:
                await self._play_current(expected_command_generation=command_generation)
                if command_generation is not None and command_generation != self._command_generation:
                    return
                return
            except Exception as loop_error:
                # A looped source can expire or become unavailable. Continue
                # through the queue rather than leaving the player stuck.
                print(f"Music loop playback failed in guild {self.guild.id}: {loop_error}")
                error = RuntimeError("stream failed")
        if self.queue and self.voice_client and self.voice_client.is_connected():
            if playback_generation is not None and playback_generation != self._playback_generation:
                return
            try:
                await self.play_next()
                if command_generation is not None and command_generation != self._command_generation:
                    return
                return
            except (ValueError, discord.DiscordException, OSError, RuntimeError) as next_error:
                self.last_error = "Error: Failed to stream track. Skipping to next..."
                print(f"Music could not advance in guild {self.guild.id}: {next_error}")
        # Keep the current track when the gateway/voice transport vanished.
        # The reconnect worker (or the next dashboard action) can then resume
        # it instead of silently discarding the queue while offline.
        if not self.voice_client or not self.voice_client.is_connected():
            self._resume_after_reconnect = self._resume_after_reconnect or was_playing
            self.paused = True
            self.started_monotonic = None
            self.last_error = "BirdBot lost the voice connection. Reconnecting..."
            self._save()
            return
        self.current = None
        self.paused = True
        self.queue_finished = not bool(self.queue)
        self._save()

    async def play_next(self) -> None:
        async with self._play_lock:
            if not self.queue:
                self.current = None
                self.paused = True
                self.queue_finished = True
                self.position = 0.0
                self.started_monotonic = None
                self._save()
                return
            if self.current:
                self._stream_retry_count.pop(self._track_key(self.current), None)
                self.history.append(self.current)
                self.history = self.history[-20:]
            while self.queue:
                index = random.randrange(len(self.queue)) if self.shuffle_enabled and len(self.queue) > 1 else 0
                self.current = self.queue.pop(index)
                track_key = self._track_key(self.current)
                self._stream_retry_count.pop(track_key, None)
                # A synchronous FFmpeg startup failure can happen before the
                # ``after`` callback is installed. Give the fresh URL one
                # immediate retry, then move on so one bad track never blocks
                # the rest of the queue.
                started = False
                for attempt in range(2):
                    try:
                        await self._play_current()
                        started = True
                        break
                    except ValueError:
                        if attempt == 0:
                            self._stream_cache.pop(track_key, None)
                            await asyncio.sleep(0.15)
                if not started:
                    # A bad/age-restricted source should not stop the whole
                    # queue. Move on to the next item automatically.
                    self.current = None
                    self.paused = True
                    self.position = 0.0
                    self.started_monotonic = None
                    self.last_error = "Error: Failed to stream track. Skipping to next..."
                    self._stream_retry_count.pop(track_key, None)
                    if not self.queue:
                        self.queue_finished = True
                        self._save()
                        raise ValueError("Error: Failed to stream track. Skipping to next...")
                    continue
                self.queue_finished = False
                # A playable replacement has started successfully. Clear any
                # previous track's error so the dashboard does not show a
                # stale failure while audio is currently playing.
                self.last_error = None
                self._save()
                return
            self.current = None
            self.paused = True
            self.queue_finished = True
            self.position = 0.0
            self.started_monotonic = None
            self._save()

    async def enqueue(self, track: dict[str, object]) -> None:
        key = self._track_key(track)
        if any(self._track_key(queued) == key for queued in self.queue):
            raise ValueError("That track is already in the queue.")
        self.queue.append(track)
        self.queue_finished = False
        self.last_error = None
        self._schedule_prefetch(track)
        if not self.current:
            await self.play_next()
        else:
            self._save()

    def toggle_shuffle(self) -> bool:
        """Toggle random selection for the next queued track."""
        self.shuffle_enabled = not self.shuffle_enabled
        self._save()
        return self.shuffle_enabled

    def shuffle(self) -> bool:
        """Backward-compatible alias for the dashboard action."""
        return self.toggle_shuffle()

    def toggle_loop(self) -> bool:
        """Toggle repeating the current track when it reaches its end."""
        self.loop_enabled = not self.loop_enabled
        self._save()
        return self.loop_enabled

    async def play_now(self, track: dict[str, object]) -> None:
        """Start a selected track immediately, retaining the old track in history."""
        key = self._track_key(track)
        if any(self._track_key(queued) == key for queued in self.queue):
            raise ValueError("That track is already in the queue.")
        self.queue.insert(0, track)
        self._stream_retry_count.pop(key, None)
        self.queue_finished = False
        self.last_error = None
        self._schedule_prefetch(track)
        self._command_generation += 1
        self._playback_generation += 1
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        await self.play_next()

    async def pause(self) -> None:
        self._cancel_audio_watchdog()
        if self.voice_client and self.voice_client.is_playing():
            self.position = self.current_position()
            self.voice_client.pause()
        self.paused = True
        self.started_monotonic = None
        self._save()

    async def resume(self) -> None:
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            self.paused = False
            self.started_monotonic = time.monotonic()
        elif self.current and self.voice_client and not self.voice_client.is_playing():
            await self._play_current(self.position)
        self._save()

    async def skip(self) -> None:
        self._command_generation += 1
        self._playback_generation += 1
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        self.position = 0.0
        self.started_monotonic = None
        self.last_error = None
        await self.play_next()

    async def previous(self) -> None:
        self._command_generation += 1
        self._playback_generation += 1
        if self.history:
            if self.current:
                self.queue.insert(0, self.current)
            self.current = self.history.pop()
            self._stream_retry_count.pop(self._track_key(self.current), None)
            if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
                self.voice_client.stop()
            await self._play_current()
        elif self.current:
            await self.seek(-self.current_position())

    async def seek(self, seconds: float) -> None:
        if not self.current:
            return
        self._command_generation += 1
        self._playback_generation += 1
        target = max(0.0, self.current_position() + float(seconds))
        if self.duration:
            target = min(target, self.duration)
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        await self._play_current(target)

    def set_volume(self, volume: float) -> None:
        try:
            parsed = float(volume)
        except (TypeError, ValueError) as error:
            raise ValueError("Volume must be a number between 0 and 100.") from error
        if not math.isfinite(parsed):
            raise ValueError("Volume must be a number between 0 and 100.")
        self.volume = max(0.0, min(1.0, parsed))
        source = getattr(self.voice_client, "source", None) if self.voice_client else None
        # Playback sources are wrapped for first-frame telemetry.  Unwrap
        # only for mutable PCM volume control; the wrapper remains installed
        # so the watchdog continues to observe the same source.
        volume_source = source.source if isinstance(source, _TrackedAudioSource) else source
        if isinstance(volume_source, discord.PCMVolumeTransformer):
            volume_source.volume = self.volume
        elif self._using_opus_source and self.current and self.voice_client and self.voice_client.is_connected():
            # FFmpegOpusAudio has no PCM transformer. Restart the current
            # source at the current position so the new filter takes effect
            # without requiring the user to press Play again.
            position = self.current_position()
            self._command_generation += 1
            self._playback_generation += 1
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()
            with contextlib.suppress(RuntimeError):
                task = asyncio.create_task(self._play_current(position))
                task.add_done_callback(
                    lambda completed: self._consume_background_error(completed, "volume restart")
                )
        self._save()

    async def stop(self) -> None:
        self._command_generation += 1
        self._playback_generation += 1
        self._cancel_audio_watchdog()
        # Serialize explicit termination with a reconnect attempt.  Without
        # this lock, a delayed connect could finish after Stop and resurrect
        # a session the user intentionally ended.
        async with self._connection_lock:
            if self.voice_client and self.voice_client.is_connected():
                with contextlib.suppress(discord.DiscordException, asyncio.TimeoutError):
                    await asyncio.wait_for(self.voice_client.disconnect(force=True), timeout=10)
            self.voice_client = None
            self.controller_id = None
        self.queue.clear()
        self.current = None
        self.history.clear()
        self._resume_after_reconnect = False
        self._user_absent_since = None
        self._idle_since = None
        if self._disconnect_task and self._disconnect_task is not asyncio.current_task():
            self._disconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._disconnect_task
            self._disconnect_task = None
        if self._reconnect_task and self._reconnect_task is not asyncio.current_task():
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        if self._idle_disconnect_task and self._idle_disconnect_task is not asyncio.current_task():
            self._idle_disconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_disconnect_task
            self._idle_disconnect_task = None
        prefetch_tasks = tuple(self._prefetch_tasks.values())
        for task in prefetch_tasks:
            task.cancel()
        if prefetch_tasks:
            await asyncio.gather(*prefetch_tasks, return_exceptions=True)
        self._prefetch_tasks.clear()
        self._stream_cache.clear()
        self._stream_retry_count.clear()
        self._using_opus_source = False
        self.paused = True
        self.position = 0.0
        self.duration = 0.0
        self.started_monotonic = None
        self.queue_finished = False
        self.last_error = None
        self._save()

    def tick(self) -> None:
        if self.voice_client and not self.voice_client.is_connected():
            self._resume_after_reconnect = bool(self.current and not self.paused)
            self.voice_client = None
            self._using_opus_source = False
            self.paused = True
            self.started_monotonic = None
            self.last_error = "BirdBot was disconnected from the voice channel."
            # ``controller_id`` marks an intentionally active session.  Keep
            # reconnecting even when the queue is empty so a ready Start
            # session does not unexpectedly require another click after a
            # transient voice-server disconnect.
            if self.controller_id and self._reconnect_task is None:
                self._reconnect_task = asyncio.create_task(self._reconnect_voice())
                self._reconnect_task.add_done_callback(
                    lambda task: self._consume_background_error(task, "voice reconnect")
                )
        elif self.voice_client and self.voice_client.is_connected() and self.controller_id and self._reconnect_task is None:
            # A voice server move can leave the client technically connected
            # while the bot is no longer beside the dashboard controller.
            # Treat that as an interruption and move the same client back;
            # never create a second VoiceClient for the guild.
            controller = self.guild.get_member(self.controller_id)
            requested_channel = getattr(getattr(controller, "voice", None), "channel", None)
            connected_channel = getattr(self.voice_client, "channel", None)
            if requested_channel and (not connected_channel or requested_channel.id != connected_channel.id):
                self.last_error = "Voice connection interrupted. Reconnecting..."
                self._reconnect_task = asyncio.create_task(self._reconnect_voice())
                self._reconnect_task.add_done_callback(
                    lambda task: self._consume_background_error(task, "voice reconnect")
                )
        if self._idle_conditions_met():
            if self._idle_since is None:
                self._idle_since = time.monotonic()
            if self._idle_disconnect_task is None:
                self._idle_disconnect_task = asyncio.create_task(self._disconnect_after_idle())
                self._idle_disconnect_task.add_done_callback(
                    lambda task: self._consume_background_error(task, "idle cleanup")
                )
        else:
            self._idle_since = None
            if self._idle_disconnect_task and self._idle_disconnect_task is not asyncio.current_task():
                self._idle_disconnect_task.cancel()
                self._idle_disconnect_task = None
        if self.current and not self.paused and self.voice_client and self.controller_id:
            controller = self.guild.get_member(self.controller_id)
            connected_channel = getattr(self.voice_client, "channel", None)
            controller_channel = getattr(getattr(controller, "voice", None), "channel", None)
            if not controller or not controller_channel or not connected_channel or controller_channel.id != connected_channel.id:
                if self._user_absent_since is None:
                    self._user_absent_since = time.monotonic()
                elif time.monotonic() - self._user_absent_since >= 15 and self._disconnect_task is None:
                    self._disconnect_task = asyncio.create_task(self._pause_after_user_disconnect())
                    self._disconnect_task.add_done_callback(
                        lambda task: self._consume_background_error(task, "user disconnect pause")
                    )
            else:
                self._user_absent_since = None
        if self.current and not self.paused and self.duration and self.current_position() >= self.duration:
            self.position = self.duration
        self._save(force=False)

    async def _pause_after_user_disconnect(self) -> None:
        """Pause playback if the dashboard controller leaves the voice channel."""
        try:
            controller = self.guild.get_member(self.controller_id) if self.controller_id else None
            connected_channel = getattr(self.voice_client, "channel", None)
            controller_channel = getattr(getattr(controller, "voice", None), "channel", None)
            if self.current and not self.paused and (not controller_channel or not connected_channel or controller_channel.id != connected_channel.id):
                await self.pause()
                self.last_error = "Playback paused because you left the voice channel."
                self._save()
        finally:
            self._disconnect_task = None
