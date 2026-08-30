"""The Discord client used by both the terminal bot and local dashboard."""

from __future__ import annotations

import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from settings import COMMAND_PREFIX, GUILD_ID
from storage import store


PRESENCES: list[tuple[discord.ActivityType, str]] = [
    (discord.ActivityType.playing, "with !ping"),
    (discord.ActivityType.watching, "over this server"),
    (discord.ActivityType.listening, "your ideas"),
    (discord.ActivityType.playing, "Python from zero"),
    (discord.ActivityType.watching, "the command list"),
    (discord.ActivityType.listening, "lo-fi beats"),
    (discord.ActivityType.playing, "with new features"),
    (discord.ActivityType.watching, "for new members"),
    (discord.ActivityType.playing, "a helpful assistant"),
    (discord.ActivityType.listening, "feedback"),
]


class EyooBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix=COMMAND_PREFIX,
            intents=intents,
            help_command=None,
            chunk_guilds_at_startup=True,
        )
        self.presence_index = 0
        self.started_at: datetime | None = None
        self.members_loaded = False

    async def setup_hook(self) -> None:
        # Voice clients and stream URLs are process-local; never present a
        # stale connected/playing state after a restart.
        store.reset_music_states()
        await self.load_extension("cogs.general")
        # Mini-games share this same global Discord client; the website only
        # reads persisted results and never creates another bot connection.
        await self.load_extension("games.spy.game")
        await self.load_extension("games.roulette.game")
        # Music is a cog on this same global client.  The dashboard only queues
        # actions; it never creates another Discord connection or bot process.
        await self.load_extension("music.cog")
        if GUILD_ID and GUILD_ID.isdigit():
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash commands to test guild {GUILD_ID}.")
        else:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} global slash commands.")
        self.rotate_presence.start()
        self.publish_guild_state.start()
        self.process_dashboard_commands.start()
        self.publish_ban_state.start()
        self.expire_unclaimed_tickets.start()

    async def on_ready(self) -> None:
        assert self.user is not None
        if self.started_at is None:
            self.started_at = discord.utils.utcnow()
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Prefix commands are ready. Prefix: {COMMAND_PREFIX}")
        store.sync_bot_guilds(self.guilds)
        if not self.members_loaded:
            await self.load_all_members()
            self.members_loaded = True
        else:
            store.sync_bot_members(self.guilds)
        general = self.get_cog("General")
        if general and hasattr(general, "register_ticket_views"):
            await general.register_ticket_views()

    async def on_guild_join(self, _: discord.Guild) -> None:
        store.sync_bot_guilds(self.guilds)
        await self.load_all_members()

    async def on_guild_remove(self, _: discord.Guild) -> None:
        store.sync_bot_guilds(self.guilds)
        store.sync_bot_members(self.guilds)

    async def on_member_join(self, _: discord.Member) -> None:
        store.sync_bot_members(self.guilds)

    async def on_member_remove(self, _: discord.Member) -> None:
        store.sync_bot_members(self.guilds)

    async def load_all_members(self) -> None:
        """Chunk every guild so the dashboard database contains the complete member list."""
        total = 0
        for guild in self.guilds:
            try:
                expected = guild.member_count or 0
                if not guild.chunked or (expected and len(guild.members) < expected):
                    await guild.chunk(cache=True)
                loaded = len(guild.members)
                total += loaded
                if expected and loaded < expected:
                    print(f"Member cache for {guild.name} is incomplete ({loaded}/{expected}); enable Server Members Intent.")
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"Could not fetch all members for {guild.name}: {error}")
        store.sync_bot_members(self.guilds)
        print(f"Cached {total} guild members for the dashboard.")

    async def on_command_error(self, ctx: commands.Context[commands.Bot], error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CheckFailure):
            await ctx.send("This server has not enabled BirdBot yet.")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send("I could not understand that command. Check the member and options, then try again.")
            return
        print(f"Prefix command error: {error}")
        await ctx.send("BirdBot could not complete that command. Please try again.")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = "Please wait before using that command again."
        elif isinstance(error, app_commands.TransformerError):
            message = "I could not understand that command option."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You do not have permission to use that command."
        else:
            print(f"Slash command error: {error}")
            message = "BirdBot could not complete that command. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @tasks.loop(minutes=2)
    async def rotate_presence(self) -> None:
        activity_type, name = PRESENCES[self.presence_index]
        self.presence_index = (self.presence_index + 1) % len(PRESENCES)
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=activity_type, name=name),
        )

    @rotate_presence.before_loop
    async def before_rotate_presence(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=30)
    async def publish_guild_state(self) -> None:
        """Let the separate web app verify membership without creating another client."""
        store.sync_bot_guilds(self.guilds)
        store.sync_bot_members(self.guilds)

    @publish_guild_state.before_loop
    async def before_publish_guild_state(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=0.1)
    async def process_dashboard_commands(self) -> None:
        """Execute validated website requests using this one global Discord client."""
        for request in store.claim_pending_commands():
            try:
                if not store.is_guild_activated(request["guild_id"]):
                    raise ValueError("This server no longer has the command available.")
                guild = self.get_guild(int(request["guild_id"]))
                if not guild:
                    raise ValueError("BirdBot is no longer a member of that server.")
                command_name = str(request["command_name"])
                if command_name.startswith("music_"):
                    music = self.get_cog("Music")
                    if music is None:
                        raise ValueError("The Music system is not ready yet. Please try again.")
                    music_payload = dict(request["payload"])
                    music_payload["action"] = command_name.removeprefix("music_")
                    await music.run_dashboard_command(guild, request["requested_by"], music_payload)
                elif command_name == "spy_lobby":
                    spy_game = self.get_cog("SpyGame")
                    channel = guild.get_channel(int(request["channel_id"]))
                    if spy_game is None:
                        raise ValueError("The Spy Game system is not ready yet. Please try again.")
                    if not isinstance(channel, discord.TextChannel):
                        raise ValueError("The selected text channel is no longer available.")
                    await spy_game.run_dashboard_lobby(
                        guild, channel, request["requested_by"], request["payload"]
                    )
                elif command_name == "roulette_lobby":
                    roulette = self.get_cog("Roulette")
                    channel = guild.get_channel(int(request["channel_id"]))
                    if roulette is None:
                        raise ValueError("The Roulette system is not ready yet. Please try again.")
                    if not isinstance(channel, discord.TextChannel):
                        raise ValueError("The selected text channel is no longer available.")
                    await roulette.run_dashboard_lobby(
                        guild, channel, request["requested_by"], request.get("payload")
                    )
                elif command_name == "server_message":
                    channel = guild.get_channel(int(request["channel_id"]))
                    general = self.get_cog("General")
                    if not isinstance(channel, discord.TextChannel) or general is None:
                        raise ValueError("The selected text channel is no longer available.")
                    await general.run_dashboard_server_message(guild, channel, request.get("payload") or {})
                elif command_name in {"role_create", "role_edit", "role_delete"}:
                    general = self.get_cog("General")
                    if general is None:
                        raise ValueError("The Control Panel is not ready yet. Please try again.")
                    await general.run_dashboard_role_command(
                        guild, command_name.removeprefix("role_"), request.get("payload") or {}
                    )
                else:
                    channel = guild.get_channel(int(request["channel_id"]))
                    general = self.get_cog("General")
                    if not isinstance(channel, discord.TextChannel) or general is None:
                        raise ValueError("The selected text channel is no longer available.")
                    await general.run_dashboard_command(
                        guild, channel, command_name, request["requested_by"], request["payload"]
                    )
                store.complete_command(request["request_id"])
            except discord.Forbidden as error:
                if getattr(error, "code", None) == 50001:
                    store.complete_command(request["request_id"], "BirdBot cannot access the selected channel. Grant View Channel and Send Messages permissions, then try again.")
                else:
                    store.complete_command(request["request_id"], str(error))
            except asyncio.TimeoutError:
                store.complete_command(request["request_id"], "BirdBot took too long to complete that music action. Please try again.")
            except (discord.HTTPException, ValueError, TypeError, OSError, RuntimeError) as error:
                store.complete_command(request["request_id"], str(error))
            except Exception as error:
                print(f"Dashboard command failed: {error}")
                store.complete_command(request["request_id"], "BirdBot could not run that command.")

    @process_dashboard_commands.before_loop
    async def before_process_dashboard_commands(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def publish_ban_state(self) -> None:
        general = self.get_cog("General")
        if general:
            for guild in self.guilds:
                if store.is_guild_activated(guild.id):
                    await general.refresh_bans(guild)

    @publish_ban_state.before_loop
    async def before_publish_ban_state(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=5)
    async def expire_unclaimed_tickets(self) -> None:
        """Delete open tickets that have remained unclaimed for five minutes."""
        general = self.get_cog("General")
        if general:
            await general.expire_unclaimed_tickets()

    @expire_unclaimed_tickets.before_loop
    async def before_expire_unclaimed_tickets(self) -> None:
        await self.wait_until_ready()


def create_bot() -> EyooBot:
    """Return a fresh client; a fresh client is required after stopping one."""
    return EyooBot()
