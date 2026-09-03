"""The Discord client used by both the terminal bot and local dashboard."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from command_messages import command_message
from settings import COMMAND_PREFIX, GUILD_ID, VC_BOT_TOKENS
from storage import DASHBOARD_COMMAND_NAMES, store
from vc_presence import VCPresenceManager


DEFAULT_PRESENCES: list[tuple[discord.ActivityType, str]] = [
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

PRESENCE_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "bot.config.json"
_ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}
_STATUS_TYPES = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "do_not_disturb": discord.Status.dnd,
    "invisible": discord.Status.invisible,
    # Discord represents an intentionally hidden bot as ``invisible``;
    # accepting ``offline`` in the config keeps the file friendly without
    # sending an unsupported gateway status.
    "offline": discord.Status.invisible,
}


def load_presence_config() -> tuple[discord.Status, list[tuple[discord.ActivityType, str]], int]:
    """Load the easy-to-edit bot status/activity configuration file."""
    status = discord.Status.online
    activities = list(DEFAULT_PRESENCES)
    interval_minutes = 2
    try:
        raw = json.loads(PRESENCE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    if isinstance(raw, dict):
        configured_status = str(raw.get("status") or "online").strip().casefold()
        status = _STATUS_TYPES.get(configured_status, status)
        configured_activities = raw.get("activities")
        if isinstance(configured_activities, list):
            parsed: list[tuple[discord.ActivityType, str]] = []
            for item in configured_activities[:20]:
                if not isinstance(item, dict):
                    continue
                activity_type = _ACTIVITY_TYPES.get(str(item.get("type") or "playing").strip().casefold())
                name = str(item.get("name") or "").strip()
                if activity_type is not None and name:
                    parsed.append((activity_type, name[:128]))
            if parsed:
                activities = parsed
        try:
            interval_minutes = max(1, min(1440, int(raw.get("rotate_every_minutes") or 2)))
        except (TypeError, ValueError):
            interval_minutes = 2
    return status, activities, interval_minutes


PRESENCE_STATUS, PRESENCES, PRESENCE_INTERVAL_MINUTES = load_presence_config()


class EyooBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix=self.get_dynamic_prefix,
            intents=intents,
            help_command=None,
            chunk_guilds_at_startup=True,
        )
        self.presence_index = 0
        self.presences = list(PRESENCES)
        self.presence_status = PRESENCE_STATUS
        self.started_at: datetime | None = None
        self.members_loaded = False
        # Optional secondary voice-presence clients use private host secrets
        # and never expose their tokens to the dashboard or shared storage.
        self.vc_presence = VCPresenceManager(VC_BOT_TOKENS, store)
        self.rotate_presence.change_interval(minutes=PRESENCE_INTERVAL_MINUTES)

    @staticmethod
    def get_dynamic_prefix(_: commands.Bot, message: discord.Message) -> str:
        """Return the prefix configured for the message's server.

        A non-printing sentinel is used while prefixed commands are disabled
        so Discord.py still receives a valid prefix callable. Configured
        shortcuts are dispatched separately and can remain standalone.
        """
        if not message.guild:
            return COMMAND_PREFIX
        settings = store.command_settings(str(message.guild.id))
        if not settings.get("prefix_enabled", True):
            return "\x00"
        return str(settings.get("prefix") or COMMAND_PREFIX)

    async def on_message(self, message: discord.Message) -> None:
        """Dispatch configured per-server shortcuts through their command."""
        if message.author.bot or not message.guild:
            await self.process_commands(message)
            return
        settings = store.command_settings(str(message.guild.id))
        prefix = str(settings.get("prefix") or COMMAND_PREFIX)
        content = str(message.content or "")
        # A shortcut is an alias, not another prefixed command: accept it both
        # as ``!alias`` and as a standalone ``alias``.  Splitting on any
        # whitespace also keeps aliases usable when users paste tabs or line
        # breaks after them.
        candidates = []
        if settings.get("prefix_enabled", True) and content.startswith(prefix):
            candidates.append(content[len(prefix):])
        # Keep the full content as a fallback for prefixes that are also valid
        # username/shortcut text (for example prefix ``p`` and shortcut
        # ``pong``). Standalone aliases remain available even when prefixed
        # commands have been switched off.
        if content not in candidates:
            candidates.append(content)
        for remainder in candidates:
            shortcut_parts = remainder.split(None, 1)
            shortcut = shortcut_parts[0] if shortcut_parts else ""
            arguments = shortcut_parts[1] if len(shortcut_parts) > 1 else ""
            config = store.command_for_shortcut(str(message.guild.id), shortcut)
            if not (config and config.get("enabled")):
                continue
            command_name = str(config.get("command_name") or "")
            # Dashboard settings use an underscore-safe key for the nested
            # ``show warning`` command. Discord.py resolves the actual prefix
            # command by its space-separated qualified name.
            dispatched_name = "show warning" if command_name == "show_warning" else command_name
            command = self.get_command(dispatched_name) or self.get_command(command_name)
            if command:
                proxy = copy.copy(message)
                # Use the same non-printing sentinel as the dynamic prefix
                # when the manager disabled prefixed commands. This keeps a
                # standalone shortcut usable without adding custom attributes
                # to Discord's slotted Message object.
                dispatch_prefix = prefix if settings.get("prefix_enabled", True) else "\x00"
                proxy.content = f"{dispatch_prefix}{dispatched_name}{(' ' + arguments) if arguments else ''}"
                await self.process_commands(proxy)
                return
        await self.process_commands(message)

    async def setup_hook(self) -> None:
        # Voice clients and stream URLs are process-local; never present a
        # stale connected/playing state after a restart.
        store.reset_music_states()
        await self.load_extension("cogs.general")
        # Mini-games share this same global Discord client; the website only
        # reads persisted results and never creates another bot connection.
        await self.load_extension("games.spy.game")
        await self.load_extension("games.roulette.game")
        await self.load_extension("games.guess_number.game")
        # Music is a cog on this same global client.  The dashboard only queues
        # actions; it never creates another Discord connection or bot process.
        await self.load_extension("music.cog")
        await self.vc_presence.start()
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
        print(f"Prefix commands are ready. Default prefix: {COMMAND_PREFIX} (server settings may override it)")
        store.sync_bot_guilds(self.guilds)
        await self.vc_presence.reconcile_all()
        if not self.members_loaded:
            await self.load_all_members()
            self.members_loaded = True
        else:
            store.sync_bot_members(self.guilds)
        general = self.get_cog("General")
        if general and hasattr(general, "register_ticket_views"):
            await general.register_ticket_views()
        if general and hasattr(general, "reconcile_temp_vcs"):
            await general.reconcile_temp_vcs()

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
        command_name = str(ctx.command.qualified_name if ctx.command else "").replace(" ", "_")
        if command_name == "show_warnings":
            command_name = "show_warning"
        arabic = bool(ctx.guild and store.command_config(str(ctx.guild.id), command_name).get("language") == "ar") if command_name else False
        if isinstance(error, commands.CheckFailure):
            if ctx.guild and command_name in DASHBOARD_COMMAND_NAMES and not store.command_config(str(ctx.guild.id), command_name).get("enabled", True):
                await ctx.send(command_message("common", "ar" if arabic else "en", "disabled"))
                return
            await ctx.send(command_message("common", "ar" if arabic else "en", "not_enabled"))
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(command_message("common", "ar" if arabic else "en", "bad_argument"))
            return
        print(f"Prefix command error: {error}")
        await ctx.send(command_message("common", "ar" if arabic else "en", "failed"))

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        command_name = str(getattr(interaction.command, "qualified_name", "") or getattr(interaction.command, "name", "")).replace(" ", "_")
        if command_name == "show_warnings":
            command_name = "show_warning"
        arabic = bool(interaction.guild and store.command_config(str(interaction.guild.id), command_name).get("language") == "ar") if command_name else False
        if isinstance(error, app_commands.CommandOnCooldown):
            message = command_message("common", "ar" if arabic else "en", "cooldown")
        elif isinstance(error, app_commands.TransformerError):
            message = command_message("common", "ar" if arabic else "en", "slash_option")
        elif isinstance(error, app_commands.MissingPermissions):
            message = command_message("common", "ar" if arabic else "en", "permission")
        else:
            print(f"Slash command error: {error}")
            message = command_message("common", "ar" if arabic else "en", "failed")
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @tasks.loop(minutes=2)
    async def rotate_presence(self) -> None:
        if not self.presences:
            await self.change_presence(status=self.presence_status, activity=None)
            return
        activity_type, name = self.presences[self.presence_index]
        self.presence_index = (self.presence_index + 1) % len(self.presences)
        await self.change_presence(
            status=self.presence_status,
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
                command_result: object | None = None
                if not store.is_guild_activated(request["guild_id"]):
                    raise ValueError("This server no longer has the command available.")
                guild = self.get_guild(int(request["guild_id"]))
                if not guild:
                    raise ValueError("BirdBot is no longer a member of that server.")
                command_name = str(request["command_name"])
                if command_name == "vc_presence_action":
                    await self.vc_presence.apply_action(int(guild.id), request.get("payload") or {})
                    command_result = self.vc_presence.status_for_guild(str(guild.id))
                elif command_name.startswith("music_"):
                    music = self.get_cog("Music")
                    if music is None:
                        raise ValueError("The Music system is not ready yet. Please try again.")
                    music_payload = dict(request["payload"])
                    configured_command = command_name.removeprefix("music_")
                    # A Commands-tab ``/play`` follows slash-command
                    # semantics: append to an active queue. The member Music
                    # portal's Play button intentionally remains an immediate
                    # replacement, so give this surface its own action name.
                    music_payload["command_name"] = configured_command
                    is_commands_tab = music_payload.get("command_surface") == "commands"
                    music_payload["action"] = (
                        "command_play"
                        if configured_command == "play" and is_commands_tab
                        else "queue"
                        if configured_command == "q" and is_commands_tab
                        else configured_command
                    )
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
                elif command_name == "guess_number_lobby":
                    guess_number = self.get_cog("GuessNumber")
                    channel = guild.get_channel(int(request["channel_id"]))
                    if guess_number is None:
                        raise ValueError("Guess the Number is not ready yet. Please try again.")
                    if not isinstance(channel, discord.TextChannel):
                        raise ValueError("The selected text channel is no longer available.")
                    await guess_number.run_dashboard_lobby(
                        guild, channel, request["requested_by"], request.get("payload")
                    )
                elif command_name == "server_message":
                    channel = guild.get_channel(int(request["channel_id"]))
                    general = self.get_cog("General")
                    if not isinstance(channel, discord.TextChannel) or general is None:
                        raise ValueError("The selected text channel is no longer available.")
                    await general.run_dashboard_server_message(guild, channel, request.get("payload") or {})
                elif command_name == "bot_profile":
                    general = self.get_cog("General")
                    if general is None:
                        raise ValueError("The Control Panel is not ready yet. Please try again.")
                    await general.run_dashboard_bot_profile(guild, request.get("payload") or {})
                elif command_name == "dm_message":
                    general = self.get_cog("General")
                    if general is None:
                        raise ValueError("The Control Panel is not ready yet. Please try again.")
                    command_result = await general.run_dashboard_dm_message(guild, request.get("payload") or {})
                elif command_name == "temp_vc_action":
                    general = self.get_cog("General")
                    if general is None:
                        raise ValueError("The Temp VC system is not ready yet. Please try again.")
                    await general.run_dashboard_temp_vc(guild, request["requested_by"], request.get("payload") or {})
                elif command_name in {"role_create", "role_edit", "role_delete", "role_permissions"}:
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
                store.complete_command(request["request_id"], result=command_result)
            except discord.Forbidden as error:
                if getattr(error, "code", None) == 50001:
                    store.complete_command(request["request_id"], "BirdBot cannot access the selected channel. Grant View Channel and Send Messages permissions, then try again.")
                elif getattr(error, "code", None) == 50278:
                    store.complete_command(
                        request["request_id"],
                        "Discord could not open a DM with that member. Make sure BirdBot is in the selected server and the member allows direct messages from server members.",
                    )
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

    async def close(self) -> None:
        general = self.get_cog("General")
        if general and hasattr(general, "close_ai"):
            await general.close_ai()
        await self.vc_presence.close()
        await super().close()


def create_bot() -> EyooBot:
    """Return a fresh client; a fresh client is required after stopping one."""
    return EyooBot()
