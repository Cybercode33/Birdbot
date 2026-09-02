"""Dashboard and Discord slash-command music actions for BirdBot's client."""

from __future__ import annotations

import contextlib
import math
from typing import Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks

from command_messages import command_message
from discord_members import resolve_guild_member
from storage import store
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

    @staticmethod
    def _language(guild: discord.Guild, command_name: str) -> str:
        language = store.command_config(str(guild.id), command_name).get("language")
        return str(language) if language in {"en", "ar"} else "en"

    async def cog_check(self, ctx: commands.Context[commands.Bot]) -> bool:
        """Apply the Commands-tab activation and enable switch to prefixes."""
        if not ctx.guild or not store.is_guild_activated(ctx.guild.id):
            return False
        command_name = str(ctx.command.qualified_name if ctx.command else "").split(" ", 1)[0]
        if command_name in {"play", "q", "pause", "skip", "stop"}:
            return bool(store.command_config(str(ctx.guild.id), command_name).get("enabled", True))
        return True

    async def _slash_command_ready(self, interaction: discord.Interaction, command_name: str) -> bool:
        guild = interaction.guild
        if guild is None:
            await self._send_slash_error(interaction, "Music commands can only be used inside a server.")
            return False
        if not store.is_guild_activated(guild.id):
            await self._send_slash_error(interaction, command_message("common", "en", "not_enabled"))
            return False
        language = self._language(guild, command_name)
        if not store.command_config(str(guild.id), command_name).get("enabled", True):
            await self._send_slash_error(interaction, command_message("common", language, "disabled"))
            return False
        return True

    @staticmethod
    def _track_from_query(query: str) -> dict[str, object]:
        """Build the lightweight metadata object expected by ``MusicPlayer``.

        The player deliberately resolves a fresh stream URL only when a track
        is about to play.  Keeping the user-entered URL/query here means slash
        commands can queue quickly and avoids storing short-lived CDN URLs.
        """
        value = str(query or "").strip()
        if not value:
            raise ValueError("Enter a YouTube link or a music name.")
        if len(value) > 500:
            raise ValueError("That music request is too long (maximum 500 characters).")
        try:
            parsed = urlparse(value)
            is_url = parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)
        except ValueError:
            is_url = False
        if is_url:
            # The source URL is resolved by yt-dlp (or passed directly to
            # FFmpeg for public audio files) when playback starts.
            return {
                "id": f"url:{value}",
                "name": value,
                "artist": "Direct link",
                "source_url": value,
            }
        # A plain name is resolved through the same YouTube/SoundCloud search
        # fallback used for dashboard tracks with Spotify metadata.
        return {
            "id": f"search:{value.casefold()}",
            "name": value,
            "artist": "",
            "search_query": value,
        }

    async def _music_member(self, interaction: discord.Interaction) -> discord.Member:
        guild = interaction.guild
        if guild is None:
            raise ValueError("Music commands can only be used inside a server.")
        user = interaction.user
        if isinstance(user, discord.Member):
            return user
        return await self.member_for(guild, str(user.id))

    @staticmethod
    async def _send_slash_error(interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _enqueue_for_member(
        self,
        guild: discord.Guild,
        member: discord.Member,
        query: str,
    ) -> tuple[MusicPlayer, dict[str, object]]:
        player = self.player_for(guild)
        await player.ensure_playback_ready(member)
        track = self._track_from_query(query)
        await player.enqueue(track)
        return player, track

    async def _queue_slash_track(self, interaction: discord.Interaction, query: str, command_name: str) -> None:
        """Resolve and enqueue a slash-command request.

        Both ``/play`` and ``/q`` intentionally call ``enqueue``.  ``enqueue``
        starts an idle player, while an active player keeps its current track
        and places the new request at the end of the queue.
        """
        if not await self._slash_command_ready(interaction, command_name):
            return
        await interaction.response.defer(thinking=True)
        try:
            member = await self._music_member(interaction)
            assert interaction.guild is not None
            player, track = await self._enqueue_for_member(interaction.guild, member, query)
        except (ValueError, discord.DiscordException, OSError, RuntimeError) as error:
            message = str(error).strip() or "That track could not be queued. Try again shortly."
            await self._send_slash_error(interaction, message)
            return
        position = len(player.queue) + (1 if player.current else 0)
        language = self._language(interaction.guild, command_name)
        if player.current and player.current.get("id") != track.get("id"):
            message = command_message("music", language, "queued", track=query.strip(), position=position)
        else:
            message = command_message("music", language, "playing", track=query.strip())
        await interaction.followup.send(message)

    @app_commands.command(name="play", description="Play a YouTube link or search for a music name.")
    @app_commands.describe(query="YouTube link or music name")
    async def slash_play(self, interaction: discord.Interaction, query: str) -> None:
        await self._queue_slash_track(interaction, query, "play")

    @app_commands.command(name="q", description="Add a YouTube link or music name to the queue.")
    @app_commands.describe(query="YouTube link or music name")
    async def slash_queue(self, interaction: discord.Interaction, query: str) -> None:
        await self._queue_slash_track(interaction, query, "q")

    @app_commands.command(name="stop", description="Stop music, clear the queue, and leave voice chat.")
    async def slash_stop(self, interaction: discord.Interaction) -> None:
        if not await self._slash_command_ready(interaction, "stop"):
            return
        assert interaction.guild is not None
        await interaction.response.defer(thinking=True)
        player = self.player_for(interaction.guild)
        await player.stop()
        await interaction.followup.send(command_message("music", self._language(interaction.guild, "stop"), "stopped"))

    @app_commands.command(name="pause", description="Pause the currently playing track.")
    async def slash_pause(self, interaction: discord.Interaction) -> None:
        if not await self._slash_command_ready(interaction, "pause"):
            return
        assert interaction.guild is not None
        await interaction.response.defer(thinking=True)
        try:
            member = await self._music_member(interaction)
            player = self.player_for(interaction.guild)
            if not player.current:
                raise ValueError("Nothing is currently playing.")
            await player.ensure_playback_ready(member)
            if player.paused:
                raise ValueError("Playback is already paused.")
            await player.pause()
        except (ValueError, discord.DiscordException, OSError, RuntimeError) as error:
            await self._send_slash_error(interaction, str(error) or "Playback could not be paused.")
            return
        await interaction.followup.send(command_message("music", self._language(interaction.guild, "pause"), "paused"))

    @app_commands.command(name="skip", description="Skip the currently playing track.")
    async def slash_skip(self, interaction: discord.Interaction) -> None:
        if not await self._slash_command_ready(interaction, "skip"):
            return
        assert interaction.guild is not None
        await interaction.response.defer(thinking=True)
        try:
            member = await self._music_member(interaction)
            player = self.player_for(interaction.guild)
            if not player.current:
                raise ValueError("There is no track to skip.")
            await player.ensure_playback_ready(member)
            await player.skip()
        except (ValueError, discord.DiscordException, OSError, RuntimeError) as error:
            await self._send_slash_error(interaction, str(error) or "The track could not be skipped.")
            return
        language = self._language(interaction.guild, "skip")
        if player.current:
            await interaction.followup.send(command_message("music", language, "skipped", track=player.current.get("name", "the next track")))
        else:
            await interaction.followup.send(command_message("music", language, "queue_empty"))

    async def _queue_prefix_track(self, ctx: commands.Context[commands.Bot], query: str, command_name: str) -> None:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return
        try:
            player, track = await self._enqueue_for_member(ctx.guild, ctx.author, query)
        except (ValueError, discord.DiscordException, OSError, RuntimeError) as error:
            await ctx.send(str(error).strip() or "That track could not be queued. Try again shortly.")
            return
        language = self._language(ctx.guild, command_name)
        position = len(player.queue) + (1 if player.current else 0)
        if player.current and player.current.get("id") != track.get("id"):
            await ctx.send(command_message("music", language, "queued", track=query.strip(), position=position))
        else:
            await ctx.send(command_message("music", language, "playing", track=query.strip()))

    @commands.command(name="play")
    async def play_prefix(self, ctx: commands.Context[commands.Bot], *, query: str) -> None:
        await self._queue_prefix_track(ctx, query, "play")

    @commands.command(name="q")
    async def queue_prefix(self, ctx: commands.Context[commands.Bot], *, query: str) -> None:
        await self._queue_prefix_track(ctx, query, "q")

    @commands.command(name="pause")
    async def pause_prefix(self, ctx: commands.Context[commands.Bot]) -> None:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return
        player = self.player_for(ctx.guild)
        try:
            if not player.current:
                raise ValueError("Nothing is currently playing.")
            await player.ensure_playback_ready(ctx.author)
            if player.paused:
                raise ValueError("Playback is already paused.")
            await player.pause()
        except (ValueError, discord.DiscordException, OSError, RuntimeError) as error:
            await ctx.send(str(error).strip() or "Playback could not be paused.")
            return
        await ctx.send(command_message("music", self._language(ctx.guild, "pause"), "paused"))

    @commands.command(name="skip")
    async def skip_prefix(self, ctx: commands.Context[commands.Bot]) -> None:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return
        player = self.player_for(ctx.guild)
        try:
            if not player.current:
                raise ValueError("There is no track to skip.")
            await player.ensure_playback_ready(ctx.author)
            await player.skip()
        except (ValueError, discord.DiscordException, OSError, RuntimeError) as error:
            await ctx.send(str(error).strip() or "The track could not be skipped.")
            return
        language = self._language(ctx.guild, "skip")
        await ctx.send(
            command_message("music", language, "skipped", track=player.current.get("name", "the next track"))
            if player.current
            else command_message("music", language, "queue_empty")
        )

    @commands.command(name="stop")
    async def stop_prefix(self, ctx: commands.Context[commands.Bot]) -> None:
        if not ctx.guild:
            return
        player = self.player_for(ctx.guild)
        await player.stop()
        await ctx.send(command_message("music", self._language(ctx.guild, "stop"), "stopped"))

    async def run_dashboard_command(self, guild: discord.Guild, requested_by: str, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("The music action data is invalid.")
        action = str(payload.get("action") or "").strip().casefold()
        # The queue stores music_start, music_queue, ... as command names.
        if not action:
            raise ValueError("The music action is missing.")
        configured_command = str(payload.get("command_name") or "").strip().casefold()
        if configured_command in {"play", "q", "pause", "skip", "stop"}:
            if not store.command_config(str(guild.id), configured_command).get("enabled", True):
                raise ValueError("That command is disabled for this server. Enable it in the Commands tab first.")
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
        if action in {"queue", "play", "command_play"}:
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

    @tasks.loop(seconds=1.0)
    async def publish_state(self) -> None:
        for player in tuple(self.players.values()):
            with contextlib.suppress(Exception):
                player.tick()

    @publish_state.before_loop
    async def before_publish_state(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
