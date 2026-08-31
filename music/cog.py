"""Dashboard-facing music actions for BirdBot's one global Discord client."""

from __future__ import annotations

import contextlib
import math
from typing import Any

import discord
from discord.ext import commands, tasks

from discord_members import resolve_guild_member
from .player import MusicPlayer


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}
        self.publish_state.start()

    def cog_unload(self) -> None:
        self.publish_state.cancel()

    def player_for(self, guild: discord.Guild) -> MusicPlayer:
        player = self.players.get(guild.id)
        if player is None:
            player = MusicPlayer(self.bot, guild)
            self.players[guild.id] = player
        return player

    async def member_for(self, guild: discord.Guild, user_id: str) -> discord.Member:
        member = await resolve_guild_member(guild, user_id)
        if member is None:
            raise ValueError("Your server membership is still syncing. Wait a moment, then try again.")
        return member

    async def run_dashboard_command(self, guild: discord.Guild, requested_by: str, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("The music action data is invalid.")
        action = str(payload.get("action") or "").strip().casefold()
        # The queue stores music_start, music_queue, ... as command names.
        if not action:
            raise ValueError("The music action is missing.")
        actor = await self.member_for(guild, requested_by)
        player = self.player_for(guild)

        async def ensure_control_ready() -> None:
            # Read-only mode toggles (Shuffle/Loop) can be changed while the
            # player is idle, but once a session exists every playback
            # mutation must come from the user's current voice channel.
            if player.current or player.queue or (player.voice_client and player.voice_client.is_connected()):
                await player.ensure_playback_ready(actor)

        if action == "start":
            await player.connect(actor)
            return
        if action in {"queue", "play"}:
            track = payload.get("track")
            source_url = track.get("source_url") if isinstance(track, dict) else None
            has_source_url = isinstance(source_url, str) and source_url.startswith(("http://", "https://"))
            has_track_id = isinstance(track.get("id"), str) and bool(track.get("id")) if isinstance(track, dict) else False
            if (
                not isinstance(track, dict)
                or not (has_track_id or has_source_url)
                or not isinstance(track.get("name"), str)
                or not isinstance(track.get("artist"), str)
            ):
                raise ValueError("Choose a valid Spotify track or audio URL first.")
            # Voice state can change between the Start request and a later
            # play/queue click.  Reuse the same per-guild player and perform a
            # bounded pre-flight reconnect when the dashboard actor is still
            # in voice, instead of leaving a silent/stale player behind.
            await player.ensure_playback_ready(actor)
            if action == "play":
                await player.play_now(track)
            else:
                await player.enqueue(track)
            return
        if action == "playlist":
            tracks = payload.get("tracks")
            if not isinstance(tracks, list) or not tracks:
                raise ValueError("Choose a playlist with playable tracks first.")
            await player.ensure_playback_ready(actor)
            added = 0
            duplicates = 0
            for track in tracks:
                if not isinstance(track, dict) or not track.get("id"):
                    continue
                try:
                    await player.enqueue(track)
                except ValueError as error:
                    # Duplicate entries are intentionally skipped while the
                    # rest of a bulk playlist continues to queue.
                    message = str(error).casefold()
                    if "already in the queue" in message:
                        duplicates += 1
                        continue
                    if "failed to stream" in message or "ffmpeg" in message or "playback" in message:
                        continue
                    raise
                added += 1
            if not added:
                raise ValueError("Every track in that playlist is already in the queue." if duplicates else "No playable tracks could be added to the queue.")
            return
        if action == "pause":
            if not player.current:
                raise ValueError("Nothing is currently playing.")
            await player.ensure_playback_ready(actor)
            if not player.voice_client or not player.voice_client.is_connected():
                raise ValueError("Nothing is currently playing.")
            if player.paused:
                raise ValueError("Playback is already paused.")
            await player.pause(); return
        if action == "resume":
            if not player.current:
                raise ValueError("Nothing is queued to play. Start the bot and choose a track first.")
            await player.ensure_playback_ready(actor)
            if not player.paused and player.voice_client.is_playing():
                raise ValueError("Playback is already running.")
            await player.resume(); return
        if action == "skip":
            if not player.current:
                raise ValueError("There is no track to skip.")
            await player.ensure_playback_ready(actor)
            await player.skip(); return
        if action == "previous":
            if not player.current and not player.history:
                raise ValueError("There is no previous track.")
            await player.ensure_playback_ready(actor)
            await player.previous(); return
        if action == "shuffle":
            await ensure_control_ready()
            player.toggle_shuffle(); return
        if action == "loop":
            await ensure_control_ready()
            player.toggle_loop(); return
        if action == "seek":
            try:
                seconds = float(payload.get("seconds", 0))
            except (TypeError, ValueError) as error:
                raise ValueError("Seek time must be a number of seconds.") from error
            if not math.isfinite(seconds) or not -600 <= seconds <= 600:
                raise ValueError("Seek time must be between -600 and 600 seconds.")
            if not player.current:
                raise ValueError("There is no track to seek.")
            await player.ensure_playback_ready(actor)
            await player.seek(seconds); return
        if action == "volume":
            try:
                volume = float(payload.get("volume", 0))
            except (TypeError, ValueError) as error:
                raise ValueError("Volume must be a number between 0 and 100.") from error
            if not math.isfinite(volume) or not 0 <= volume <= 1:
                raise ValueError("Volume must be a number between 0 and 100.")
            await ensure_control_ready()
            player.set_volume(volume); return
        if action == "volume_up":
            await ensure_control_ready()
            player.set_volume(player.volume + 0.1); return
        if action == "volume_down":
            await ensure_control_ready()
            player.set_volume(player.volume - 0.1); return
        if action == "stop":
            # Stopping is the manual escape hatch after a network disconnect;
            # it must remain available even when the user has already left VC
            # or the stale client cannot be pre-flighted.
            await player.stop(); return
        raise ValueError("That music action is not available.")

    @tasks.loop(seconds=0.25)
    async def publish_state(self) -> None:
        for player in tuple(self.players.values()):
            with contextlib.suppress(Exception):
                player.tick()

    @publish_state.before_loop
    async def before_publish_state(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
