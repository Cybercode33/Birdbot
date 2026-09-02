"""Discord Spy Game lobby and match foundation.

The game uses the bot's single global Discord client. A lobby is kept alive by
its Discord view, while completed matches are persisted for the dashboard.
Role delivery prefers a DM. Discord only permits ephemeral messages as a
response to that user's interaction, so the last interaction from each player
is retained as a safe private fallback when their DMs are closed.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from discord_members import resolve_guild_member
from settings import DASHBOARD_PUBLIC_URL
from storage import store


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT_DIR / "config" / "games" / "spy.config.json"
WORDS_PATH = ROOT_DIR / "config" / "games" / "spy_words.json"
BLACK = discord.Colour.from_rgb(0, 0, 0)
WHITE = discord.Colour.from_rgb(255, 255, 255)
MINIMUM_PLAYERS = 3
DEFAULT_MAXIMUM_PLAYERS = 20
DEFAULT_QUESTION_TIMER_SECONDS = 30
LOBBY_TIMEOUT_SECONDS = 60
VOTE_TIMEOUT_SECONDS = 30
DEFAULT_AUTO_END_ROUNDS = 20

ENGLISH_RULES = (
    "Citizens receive the same secret. One player is secretly the Spy. "
    "Citizens ask careful questions to find the Spy, while the Spy blends in "
    "and tries to discover the secret without being caught."
)
ARABIC_RULES = (
    "يحصل المواطنون على نفس السر، بينما يتم اختيار لاعب واحد سراً ليكون الجاسوس. "
    "يطرح المواطنون أسئلة ذكية لاكتشاف الجاسوس، ويحاول الجاسوس الاندماج ومعرفة السر دون أن ينكشف."
)


def load_config() -> dict[str, Any]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_vocabulary() -> list[dict[str, str]]:
    """Load bilingual places/items and tolerate older string-only configs."""
    try:
        value = json.loads(WORDS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {}
    entries: list[dict[str, str]] = []
    if isinstance(value, dict):
        for category in ("places", "items"):
            raw_items = value.get(category, [])
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if isinstance(item, dict):
                    en = str(item.get("en") or item.get("english") or "").strip()
                    ar = str(item.get("ar") or item.get("arabic") or en).strip()
                    if en:
                        entries.append({"en": en, "ar": ar or en, "type": "object" if category == "items" else "location"})
    if entries:
        return entries
    raw_locations = load_config().get("locations", [])
    for item in raw_locations if isinstance(raw_locations, list) else []:
        if isinstance(item, dict):
            en = str(item.get("en") or item.get("english") or "").strip()
            ar = str(item.get("ar") or item.get("arabic") or en).strip()
        else:
            en, ar = str(item).strip(), str(item).strip()
        if en:
            entries.append({"en": en, "ar": ar or en, "type": "location"})
    return entries or [{"en": "Airport", "ar": "مطار"}]


def configured_asset_url(key: str) -> str | None:
    config = load_config()
    raw_path = config.get(key)
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    relative = raw_path.replace("\\", "/").lstrip("./")
    local_path = ROOT_DIR / "website" / relative
    if not local_path.is_file():
        return None
    public_origin = DASHBOARD_PUBLIC_URL.rstrip("/").casefold()
    if public_origin.startswith(("http://127.0.0.1", "http://localhost", "http://0.0.0.0")):
        return None
    return f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/assets/{relative.removeprefix('assets/')}"


def spy_embed(guild: discord.Guild, *, title: str, description: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, colour=BLACK)
    icon_url = configured_asset_url("iconPath") or (guild.icon.url if guild.icon else None)
    banner_url = configured_asset_url("bannerPath") or (guild.banner.url if guild.banner else None)
    if icon_url:
        embed.set_thumbnail(url=icon_url)
    if banner_url:
        embed.set_image(url=banner_url)
    embed.set_footer(text="BirdBot · Spy Game")
    return embed


def secret_kind(lobby: SpyLobby) -> str:
    """Return the selected vocabulary category."""
    return "object" if lobby.secret_type == "object" else "location"


def secret_kind_label(lobby: SpyLobby) -> str:
    if lobby.language == "ar":
        return "الغرض" if secret_kind(lobby) == "object" else "الموقع"
    return "Object" if secret_kind(lobby) == "object" else "Location"


def result_embed(guild: discord.Guild, lobby: SpyLobby, *, spy_won: bool) -> discord.Embed:
    """Render the final result with a prominent winner and typed secret."""
    if lobby.language == "ar":
        title = "فاز الجاسوس" if spy_won else "فاز المواطنون"
        description = (
            f"**الجاسوس:** {lobby.spy_name or 'غير معروف'}\n\n"
            f"**{secret_kind_label(lobby)}:** {lobby.secret or 'غير معروف'}"
        )
        footer = "انتهت لعبة الجاسوس."
    else:
        title = "The Spy Won" if spy_won else "The Citizens Won"
        description = (
            f"**Spy:** {lobby.spy_name or 'Unknown'}\n\n"
            f"**{secret_kind_label(lobby)}:** {lobby.secret or 'Unknown'}"
        )
        footer = "Spy Game finished."
    embed = spy_embed(guild, title=title, description=description)
    embed.set_footer(text=footer)
    return embed


def game_choice_embed(guild: discord.Guild) -> discord.Embed:
    return spy_embed(
        guild,
        title="Choose Your Game",
        description="Select a mini-game to start a lobby. Spy Game is available in English and Arabic; Roulette spins a fair player wheel; Guess the Number gives everyone a turn to find a hidden number.",
    )


def language_embed(guild: discord.Guild) -> discord.Embed:
    return spy_embed(
        guild,
        title="Choose your language / اختر لغتك",
        description="Select English or Arabic for this lobby.\nاختر اللغة الإنجليزية أو العربية لهذه الردهة.",
    )


def configured_button_emoji(name: str) -> discord.PartialEmoji | str | None:
    """Read an optional Unicode/custom emoji without making invalid buttons.

    Unicode characters can be placed directly in the environment. For a
    server emoji use Discord's copied markup (``<:name:id>``); the bot must be
    in that server and have access to the emoji.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    if raw.startswith("<"):
        try:
            return discord.PartialEmoji.from_str(raw)
        except (TypeError, ValueError):
            print(f"Ignoring invalid {name}; expected Unicode or <:name:id>.")
            return None
    # Discord rejects colon aliases such as :sparkles: in component payloads;
    # require a real Unicode value instead of allowing a bad request to fail.
    if raw.startswith(":") and raw.endswith(":"):
        print(f"Ignoring invalid {name}; use the emoji character or <:name:id>.")
        return None
    return raw[:32]


@dataclass
class SpyLobby:
    guild_id: int
    host_id: int
    language: str = "en"
    players: dict[int, str] = field(default_factory=dict)
    channel_id: int | None = None
    channel: discord.TextChannel | None = field(default=None, repr=False)
    guild: discord.Guild | None = field(default=None, repr=False)
    minimum_players: int = MINIMUM_PLAYERS
    maximum_players: int = DEFAULT_MAXIMUM_PLAYERS
    question_timer_seconds: int = DEFAULT_QUESTION_TIMER_SECONDS
    end_mode: str = "manual"
    auto_end_rounds: int = DEFAULT_AUTO_END_ROUNDS
    started: bool = False
    finished: bool = False
    cancelled: bool = False
    secret: str | None = None
    secret_type: str = "location"
    spy_id: int | None = None
    spy_name: str | None = None
    result_text: str | None = None
    # Interaction tokens are short-lived, but retaining the object lets us
    # send a private ephemeral fallback for closed DMs after /start.
    player_interactions: dict[int, discord.Interaction] = field(default_factory=dict, repr=False)
    pending_roles: dict[int, str] = field(default_factory=dict, repr=False)
    asker_id: int | None = None
    answerer_id: int | None = None
    turn_number: int = 0
    turn_deadline: float | None = None
    turn_message: discord.Message | None = field(default=None, repr=False)
    turn_task: asyncio.Task[None] | None = field(default=None, repr=False)
    turn_skip: asyncio.Event | None = field(default=None, repr=False)
    turn_generation: int = 0
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Prevent two rapid clicks on the host's Start button from launching two
    # role assignments/turn loops at the same time.
    starting: bool = False
    start_task: asyncio.Task[None] | None = field(default=None, repr=False)
    lobby_message: discord.Message | None = field(default=None, repr=False)
    lobby_deadline: float | None = None
    lobby_timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    reveal_requests: set[int] = field(default_factory=set, repr=False)
    vote_started: bool = False
    vote_choices: dict[int, int] = field(default_factory=dict, repr=False)
    vote_deadline: float | None = None
    vote_message: discord.Message | None = field(default=None, repr=False)
    vote_task: asyncio.Task[None] | None = field(default=None, repr=False)
    vote_event: asyncio.Event | None = field(default=None, repr=False)
    vote_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Keep one private role message per player and edit it when their role is
    # refreshed. Role content must never be shared in one public message.
    role_messages: dict[int, discord.Message] = field(default_factory=dict, repr=False)


# Views are kept by discord.py for component dispatch, but the view store is
# intentionally private.  Keep a small registry so gateway member-removal
# events can also update a lobby when its host leaves without clicking a
# button. Entries remain registered through the discussion and vote so stale
# component interactions can be recovered, then are removed when finished.
ACTIVE_SPY_LOBBIES: dict[int, SpyLobby] = {}
_SPY_INTERACTIONS_IN_FLIGHT: set[int] = set()


def register_spy_lobby(lobby: SpyLobby) -> None:
    ACTIVE_SPY_LOBBIES[id(lobby)] = lobby


def unregister_spy_lobby(lobby: SpyLobby) -> None:
    ACTIVE_SPY_LOBBIES.pop(id(lobby), None)


def lobby_embed(guild: discord.Guild, lobby: SpyLobby) -> discord.Embed:
    language = "English" if lobby.language == "en" else "العربية"
    arabic = lobby.language == "ar"
    if lobby.finished:
        description = lobby.result_text or ("انتهت هذه الجولة." if arabic else "This match has finished.")
        title = (
            "لعبة الجاسوس · انتهت مهلة الردهة" if arabic and lobby.cancelled
            else "Spy Game · Lobby timed out" if lobby.cancelled
            else "لعبة الجاسوس · انتهت الجولة" if arabic
            else "Spy Game · Match finished"
        )
    elif lobby.started and lobby.vote_started:
        description = (
            "The discussion is complete. Vote for the player you believe is the Spy."
            if lobby.language == "en"
            else "انتهى النقاش. صوّت للاعب الذي تعتقد أنه الجاسوس."
        )
        title = "Spy Game · Vote" if not arabic else "لعبة الجاسوس · التصويت"
    elif lobby.started:
        description = (
            "The match has started. Roles were sent privately to each player.\n"
            "The host can reveal the results when the round is finished."
            if lobby.language == "en"
            else "بدأت الجولة. تم إرسال الأدوار سراً لكل لاعب.\nيمكن للمضيف كشف النتائج عند انتهاء الجولة."
        )
        title = "لعبة الجاسوس · الجولة جارية" if arabic else "Spy Game · Match in progress"
    else:
        description = (
            f"Join the lobby, then the host can start when at least {lobby.minimum_players} players are ready.\n"
            f"Language: {language}"
            if lobby.language == "en"
            else f"انضم إلى الردهة، ويمكن للمضيف البدء عند جاهزية {lobby.minimum_players} لاعبين على الأقل.\nاللغة: {language}"
        )
        title = "لعبة الجاسوس · الردهة" if arabic else "Spy Game · Lobby"
    embed = spy_embed(guild, title=title, description=description)
    embed.add_field(name="اللاعبون" if arabic else "Players", value=f"{len(lobby.players)} / {lobby.maximum_players}", inline=True)
    embed.add_field(name="الحد الأدنى" if arabic else "Minimum", value=str(lobby.minimum_players), inline=True)
    if lobby.started and not lobby.vote_started:
        mode_text = (
            (f"إنهاء تلقائي · {lobby.auto_end_rounds} جولة" if arabic else f"Auto End · {lobby.auto_end_rounds} rounds")
            if lobby.end_mode == "auto"
            else (f"إنهاء يدوي · {len(lobby.reveal_requests)} / {len(lobby.players)} طلب كشف" if arabic else f"Manual End · {len(lobby.reveal_requests)} / {len(lobby.players)} reveal requests")
        )
        embed.add_field(name="وضع النهاية" if arabic else "End mode", value=mode_text, inline=True)
    if not lobby.started and not lobby.finished and lobby.lobby_deadline is not None:
        remaining = max(0, math.ceil(lobby.lobby_deadline - asyncio.get_running_loop().time()))
        timeout_text = f"{remaining // 60:02d}:{remaining % 60:02d}"
        embed.add_field(
            name="تنتهي الردهة خلال" if arabic else "Lobby timeout",
            value=timeout_text,
            inline=True,
        )
    if lobby.vote_started and not lobby.finished and lobby.vote_deadline is not None:
        remaining = max(0, math.ceil(lobby.vote_deadline - asyncio.get_running_loop().time()))
        clock = f"{remaining // 60:02d}:{remaining % 60:02d}"
        embed.add_field(
            name="Vote timeout" if not arabic else "ينتهي التصويت خلال",
            value=clock,
            inline=True,
        )
        embed.add_field(
            name="Votes" if not arabic else "الأصوات",
            value=f"{len(lobby.vote_choices)} / {len(lobby.players)}",
            inline=True,
        )
    elif lobby.started and not lobby.finished and lobby.end_mode == "manual":
        embed.add_field(
            name="Reveal requests" if not arabic else "طلبات الكشف",
            value=f"{len(lobby.reveal_requests)} / {len(lobby.players)}",
            inline=True,
        )
    if lobby.finished and lobby.secret:
        embed.add_field(name=secret_kind_label(lobby), value=lobby.secret, inline=False)
        embed.add_field(name="الجاسوس" if arabic else "Spy", value=lobby.spy_name or ("غير معروف" if arabic else "Unknown"), inline=True)
    # Once a language is selected, keep the lobby completely in that language
    # instead of mixing both copies of the rules into every embed.
    if lobby.language == "ar":
        embed.add_field(name="القواعد", value=ARABIC_RULES, inline=False)
        embed.set_footer(text="BirdBot · لعبة الجاسوس")
    else:
        embed.add_field(name="Rules", value=ENGLISH_RULES, inline=False)
    return embed


async def refresh_spy_lobby_message(lobby: SpyLobby, *, remove_view: bool = False) -> None:
    """Refresh a lobby after a gateway event without an interaction token."""
    if lobby.lobby_message is None or lobby.guild is None:
        return
    view = None if remove_view else SpyLobbyView(lobby)
    try:
        message = await lobby.lobby_message.edit(embed=lobby_embed(lobby.guild, lobby), view=view)
        lobby.lobby_message = message or lobby.lobby_message
    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
        pass


class SafeView(discord.ui.View):
    """Central error boundary for all Spy Game component interactions."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        # discord.Interaction uses slots and deliberately has no __dict__, so
        # deferred-response state must live on the view instead of being
        # attached to the interaction object.
        self._deferred_ephemeral: dict[int, bool] = {}

    async def defer_component(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        """Acknowledge a component before doing cache/Discord work.

        ``thinking=True`` gives private buttons (for example Show Players) an
        ephemeral deferred response. Mutating buttons use Discord's deferred
        message-update response so the public lobby can be edited afterwards.
        """
        interaction_id = int(getattr(interaction, "id", id(interaction)))
        if interaction.response.is_done():
            self._deferred_ephemeral[id(interaction)] = ephemeral
            return
        try:
            await interaction.response.defer(ephemeral=ephemeral, thinking=ephemeral)
        except Exception:
            # Do not leave the stale-interaction marker behind when Discord
            # rejects an acknowledgement.  The global fallback listener can
            # then provide the best available response instead of silently
            # allowing the component to time out.
            _SPY_INTERACTIONS_IN_FLIGHT.discard(interaction_id)
            raise
        _SPY_INTERACTIONS_IN_FLIGHT.add(interaction_id)
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(10, _SPY_INTERACTIONS_IN_FLIGHT.discard, interaction_id)
        except RuntimeError:
            _SPY_INTERACTIONS_IN_FLIGHT.discard(interaction_id)
        self._deferred_ephemeral[id(interaction)] = ephemeral

    async def private_response(self, interaction: discord.Interaction, message: str) -> None:
        """Send an ephemeral result whether the interaction was deferred or not."""
        try:
            deferred_ephemeral = self._deferred_ephemeral.pop(id(interaction), False)
            if deferred_ephemeral:
                await interaction.edit_original_response(content=message)
            elif interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            # The interaction token can expire while a network retry is in
            # flight. Reporting the error must never raise a second failure.
            pass

    async def edit_lobby(self, interaction: discord.Interaction) -> None:
        """Replace the public lobby view after a deferred button action."""
        lobby = getattr(self, "lobby", None)
        if not isinstance(lobby, SpyLobby) or not interaction.response.is_done():
            return
        view = SpyLobbyView(lobby)
        message = await interaction.edit_original_response(embed=lobby_embed(lobby.guild, lobby), view=view)
        lobby.lobby_message = message or interaction.message
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: object) -> None:
        lobby = getattr(self, "lobby", None)
        arabic = bool(getattr(lobby, "language", "en") == "ar")
        print(f"Spy Game interaction failed ({type(error).__name__}): {error}")
        message = "Something went wrong. Please try again." if not arabic else "حدث خطأ ما. حاول مرة أخرى."
        try:
            deferred_ephemeral = self._deferred_ephemeral.pop(id(interaction), False)
            if deferred_ephemeral:
                await interaction.edit_original_response(content=message)
            elif interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            # The interaction token may have expired; never let error
            # reporting create a second unhandled exception.
            pass


class GameChooserView(SafeView):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        select = discord.ui.Select(
            placeholder="Choose Your Game",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Spy Game", value="spy", description="Find the hidden Spy"),
                discord.SelectOption(label="Roulette", value="roulette", description="Spin the wheel with your friends"),
                discord.SelectOption(label="Guess the Number", value="guess-number", description="Take turns finding the hidden number"),
            ],
            custom_id=f"birdbot:games:choose:{owner_id}"[:100],
        )
        select.callback = self.choose
        self.add_item(select)

    async def choose(self, interaction: discord.Interaction) -> None:
        await self.defer_component(interaction)
        if interaction.user.id != self.owner_id:
            await self.private_response(interaction, "Only the person who ran /start can choose the game.")
            return
        if not interaction.guild:
            await self.private_response(interaction, "Games can only be started inside a server.")
            return
        selected = str((interaction.data or {}).get("values", ["spy"])[0])
        if selected == "roulette":
            roulette = interaction.client.get_cog("Roulette")
            if roulette is None or not hasattr(roulette, "start_interaction_lobby"):
                await self.private_response(interaction, "Roulette is still loading. Please try again in a moment.")
                return
            if not bool(store.roulette_game_config(str(interaction.guild.id)).get("enabled", True)):
                await self.private_response(interaction, "Roulette is disabled for this server. An administrator can enable it in the Games settings.")
                return
            created = await roulette.start_interaction_lobby(interaction, deferred=True)
            if created is not False:
                self.stop()
            return
        if selected == "guess-number":
            guess_number = interaction.client.get_cog("GuessNumber")
            if guess_number is None or not hasattr(guess_number, "_command"):
                await self.private_response(interaction, "Guess the Number is still loading. Please try again in a moment.")
                return
            if not bool(store.guess_number_game_config(str(interaction.guild.id)).get("enabled", True)):
                await self.private_response(interaction, "Guess the Number is disabled for this server. Enable it in the Games settings.")
                return
            await guess_number._command(interaction)
            self.stop()
            return
        spy = interaction.client.get_cog("SpyGame")
        if spy is None or not hasattr(spy, "start_interaction_lobby"):
            await self.private_response(interaction, "Spy Game is still loading. Please try again in a moment.")
            return
        if not bool(store.spy_game_config(str(interaction.guild.id)).get("enabled", True)):
            await self.private_response(interaction, "Spy Game is disabled for this server. An administrator can enable it in the Games settings.")
            return
        created = await spy.start_interaction_lobby(interaction, deferred=True)
        if created is not False:
            self.stop()


class LanguageView(SafeView):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self._selected = False
        for label, language, flag in (("EN", "en", "🇺🇸"), ("AR", "ar", "🇦🇪")):
            button = discord.ui.Button(
                label=label,
                emoji=flag,
                style=discord.ButtonStyle.secondary,
                custom_id=f"birdbot:games:language:{language}:{owner_id}"[:100],
            )

            async def callback(interaction: discord.Interaction, selected: str = language) -> None:
                await self.select_language(interaction, selected)

            button.callback = callback
            self.add_item(button)

    async def select_language(self, interaction: discord.Interaction, language: str) -> None:
        await self.defer_component(interaction)
        if interaction.user.id != self.owner_id:
            await self.private_response(interaction, "Only the lobby host can choose the language.")
            return
        if not interaction.guild:
            await self.private_response(interaction, "Games can only be started inside a server.")
            return
        if self._selected:
            await self.private_response(interaction, "A language has already been selected for this lobby.")
            return
        self._selected = True
        # The language is now configured per server in the web dashboard. This
        # compatibility view ignores the old button value and starts using the
        # saved setting instead of exposing a second Discord language picker.
        spy = interaction.client.get_cog("SpyGame")
        if spy is None or not hasattr(spy, "start_interaction_lobby"):
            await self.private_response(interaction, "Spy Game is still loading. Please try again in a moment.")
            return
        await spy.start_interaction_lobby(interaction, deferred=True)
        self.stop()


def turn_embed(guild: discord.Guild, lobby: SpyLobby, remaining: int) -> discord.Embed:
    """Build the live white interrogation card shown for the current turn."""
    arabic = lobby.language == "ar"
    asker = lobby.players.get(lobby.asker_id or 0, "Unknown")
    answerer = lobby.players.get(lobby.answerer_id or 0, "Unknown")
    seconds = max(0, int(remaining))
    clock = f"{seconds // 60:02d}:{seconds % 60:02d}"
    if arabic:
        title = f"جولة الأسئلة · الدور {lobby.turn_number}"
        description = (
            f"**{asker}** يسأل **{answerer}**.\n"
            f"الوقت المتبقي: **{clock}**\n"
            "يمكن للسائل فقط الضغط على تخطي للانتقال للدور التالي."
        )
        footer = "BirdBot · لعبة الجاسوس"
    else:
        title = f"Question turn · Round {lobby.turn_number}"
        description = (
            f"**{asker}** asks **{answerer}**.\n"
            f"Time remaining: **{clock}**\n"
            "Only the asker can press Skip to move to the next turn."
        )
        footer = "BirdBot · Spy Game"
    embed = discord.Embed(title=title, description=description, colour=WHITE)
    icon_url = configured_asset_url("iconPath") or (guild.icon.url if guild.icon else None)
    banner_url = configured_asset_url("bannerPath") or (guild.banner.url if guild.banner else None)
    if icon_url:
        embed.set_thumbnail(url=icon_url)
    if banner_url:
        embed.set_image(url=banner_url)
    embed.set_footer(text=footer)
    return embed


class TurnView(SafeView):
    """A short-lived Skip control for the currently announced pair."""

    def __init__(self, lobby: SpyLobby, generation: int, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.lobby = lobby
        self.generation = generation
        label = "تخطي" if lobby.language == "ar" else "Skip"
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            emoji=configured_button_emoji("SPY_SKIP_EMOJI"),
            disabled=disabled,
            custom_id=f"birdbot:games:skip:{lobby.guild_id}:{lobby.host_id}:{generation}"[:100],
        )
        button.callback = self.skip
        self.add_item(button)

    async def skip(self, interaction: discord.Interaction) -> None:
        lobby = self.lobby
        arabic = lobby.language == "ar"
        await self.defer_component(interaction)
        if not interaction.guild or interaction.guild.id != lobby.guild_id:
            await self.private_response(interaction, "This turn is no longer available here." if not arabic else "هذا الدور غير متاح هنا.")
            return
        if lobby.turn_generation != self.generation:
            await self.private_response(interaction, "This turn has already ended." if not arabic else "انتهى هذا الدور بالفعل.")
            return
        if interaction.user.id != lobby.asker_id:
            await self.private_response(
                interaction,
                "Only the asker can skip this turn." if not arabic else "يمكن للسائل فقط تخطي هذا الدور.",
            )
            return
        if lobby.turn_skip:
            lobby.turn_skip.set()


def vote_embed(guild: discord.Guild, lobby: SpyLobby, remaining: int) -> discord.Embed:
    """Render the timed vote that follows Auto End or unanimous reveal."""
    seconds = max(0, int(remaining))
    clock = f"{seconds // 60:02d}:{seconds % 60:02d}"
    votes = len(lobby.vote_choices)
    total = len(lobby.players)
    if lobby.language == "ar":
        title = "لعبة الجاسوس · التصويت"
        description = (
            "اختر اللاعب الذي تعتقد أنه الجاسوس.\n"
            f"الوقت المتبقي: **{clock}**\n"
            f"الأصوات: **{votes} / {total}**"
        )
        footer = "يحتاج كل لاعب إلى التصويت مرة واحدة."
    else:
        title = "Spy Game · Vote"
        description = (
            "Choose the player you believe is the Spy.\n"
            f"Time remaining: **{clock}**\n"
            f"Votes: **{votes} / {total}**"
        )
        footer = "Each player can vote once."
    embed = discord.Embed(title=title, description=description, colour=WHITE)
    icon_url = configured_asset_url("iconPath") or (guild.icon.url if guild.icon else None)
    banner_url = configured_asset_url("bannerPath") or (guild.banner.url if guild.banner else None)
    if icon_url:
        embed.set_thumbnail(url=icon_url)
    if banner_url:
        embed.set_image(url=banner_url)
    embed.set_footer(text=footer)
    return embed


class VoteView(SafeView):
    """One-vote-per-player select menu for the final Spy reveal."""

    def __init__(self, lobby: SpyLobby) -> None:
        super().__init__(timeout=None)
        self.lobby = lobby
        players = list(lobby.players.items())
        # Discord allows 25 options per select. Spy supports up to 50 lobby
        # players, so split a large vote across two menus.
        for chunk_index in range(0, max(1, len(players)), 25):
            options = [
                discord.SelectOption(label=str(name)[:100], value=str(player_id))
                for player_id, name in players[chunk_index:chunk_index + 25]
            ]
            select = discord.ui.Select(
                placeholder=("Vote for the Spy" if lobby.language != "ar" else "صوّت للجاسوس")
                + (f" ({chunk_index // 25 + 1})" if len(players) > 25 else ""),
                min_values=1,
                max_values=1,
                options=options or [discord.SelectOption(label="No players", value="none")],
                custom_id=f"birdbot:games:vote:{lobby.guild_id}:{lobby.host_id}:{lobby.turn_generation}:{chunk_index // 25}"[:100],
            )
            select.callback = self.vote
            self.add_item(select)

    async def vote(self, interaction: discord.Interaction) -> None:
        await self.defer_component(interaction, ephemeral=True)
        lobby = self.lobby
        if not interaction.guild or interaction.guild.id != lobby.guild_id:
            await self.private_response(interaction, "This vote is no longer available here.")
            return
        if lobby.finished or not lobby.vote_started:
            await self.private_response(interaction, "This vote has already ended.")
            return
        if interaction.user.id not in lobby.players:
            await self.private_response(
                interaction,
                "Only players in this Spy Game can vote." if lobby.language != "ar" else "يمكن للاعبي لعبة الجاسوس فقط التصويت.",
            )
            return
        if interaction.user.id in lobby.vote_choices:
            await self.private_response(
                interaction,
                "You have already voted." if lobby.language != "ar" else "لقد صوّت بالفعل.",
            )
            return
        values = (interaction.data or {}).get("values", [])
        try:
            target_id = int(values[0])
        except (TypeError, ValueError, IndexError):
            await self.private_response(interaction, "Choose a valid player.")
            return
        if target_id not in lobby.players:
            await self.private_response(interaction, "That player is no longer in the match.")
            return
        lobby.vote_choices[interaction.user.id] = target_id
        await self.private_response(
            interaction,
            "Vote recorded." if lobby.language != "ar" else "تم تسجيل تصويتك.",
        )
        if len(lobby.vote_choices) >= len(lobby.players) and lobby.vote_event:
            lobby.vote_event.set()


class SpyLobbyView(SafeView):
    def __init__(self, lobby: SpyLobby) -> None:
        super().__init__(timeout=1_800)
        self.lobby = lobby
        # Discord cannot render an arbitrary Icons8 URL as a component emoji;
        # remote raster icons are used by the web controls instead. Keep these
        # Discord labels monochrome and bilingual so they render consistently
        # on every client without Windows emoji substitutions.
        labels = ("دخول", "خروج", "اللاعبون", "بدء اللعبة") if lobby.language == "ar" else ("Join", "Leave", "Players", "Start Game")
        self.join_button = discord.ui.Button(label=labels[0], style=discord.ButtonStyle.secondary, emoji=configured_button_emoji("SPY_JOIN_EMOJI"), custom_id=f"birdbot:games:join:{lobby.guild_id}:{lobby.host_id}"[:100])
        self.leave_button = discord.ui.Button(label=labels[1], style=discord.ButtonStyle.secondary, emoji=configured_button_emoji("SPY_LEAVE_EMOJI"), custom_id=f"birdbot:games:leave:{lobby.guild_id}:{lobby.host_id}"[:100])
        self.players_button = discord.ui.Button(label=labels[2], style=discord.ButtonStyle.secondary, emoji=configured_button_emoji("SPY_PLAYERS_EMOJI"), custom_id=f"birdbot:games:players:{lobby.guild_id}:{lobby.host_id}"[:100])
        self.start_button = discord.ui.Button(label=labels[3], style=discord.ButtonStyle.secondary, emoji=configured_button_emoji("SPY_START_EMOJI"), custom_id=f"birdbot:games:start:{lobby.guild_id}:{lobby.host_id}"[:100])
        self.join_button.callback = self.join
        self.leave_button.callback = self.leave
        self.players_button.callback = self.show_players
        self.start_button.callback = self.start
        self.add_item(self.join_button)
        self.add_item(self.leave_button)
        self.add_item(self.players_button)
        self.add_item(self.start_button)
        if lobby.started and not lobby.finished:
            self.join_button.disabled = True
            self.leave_button.disabled = True
            self.start_button.disabled = True
            reveal = discord.ui.Button(label="كشف النتائج" if lobby.language == "ar" else "Reveal Results", style=discord.ButtonStyle.secondary, custom_id=f"birdbot:games:reveal:{lobby.guild_id}:{lobby.host_id}"[:100])
            # Manual End uses this as a unanimous reveal request. Auto End
            # transitions automatically after the configured round count.
            reveal.disabled = lobby.end_mode == "auto" or lobby.vote_started
            reveal.callback = self.reveal
            self.add_item(reveal)
            role_button = discord.ui.Button(label="اعرض دوري" if lobby.language == "ar" else "Get My Role", style=discord.ButtonStyle.secondary, custom_id=f"birdbot:games:role:{lobby.guild_id}:{lobby.host_id}"[:100])
            role_button.callback = self.get_role
            self.add_item(role_button)
        elif lobby.finished:
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
        else:
            self.start_button.disabled = len(lobby.players) < lobby.minimum_players

    def start_lobby_timeout(self, *, reset: bool = False) -> None:
        """Start/restart the shared one-minute lobby readiness countdown."""
        lobby = self.lobby
        if lobby.started or lobby.finished:
            return
        existing = lobby.lobby_timeout_task
        if existing and not existing.done():
            if not reset:
                return
            existing.cancel()
        lobby.lobby_deadline = asyncio.get_running_loop().time() + LOBBY_TIMEOUT_SECONDS
        lobby.lobby_timeout_task = asyncio.create_task(self._run_lobby_timeout())

    async def cancel_lobby_timeout(self) -> None:
        task = self.lobby.lobby_timeout_task
        self.lobby.lobby_timeout_task = None
        self.lobby.lobby_deadline = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _run_lobby_timeout(self) -> None:
        lobby = self.lobby
        try:
            while not lobby.started and not lobby.finished and lobby.lobby_deadline is not None:
                if len(lobby.players) >= lobby.minimum_players:
                    lobby.lobby_deadline = None
                    return
                remaining = max(0, math.ceil(lobby.lobby_deadline - asyncio.get_running_loop().time()))
                if lobby.lobby_message:
                    try:
                        if lobby.guild is not None:
                            await lobby.lobby_message.edit(embed=lobby_embed(lobby.guild, lobby), view=SpyLobbyView(lobby))
                    except (discord.HTTPException, discord.NotFound, AttributeError):
                        # Keep the timer alive if a single edit races with a
                        # user interaction or the original message vanished.
                        pass
                if remaining <= 0:
                    break
                await asyncio.sleep(1)
            if lobby.started or lobby.finished or len(lobby.players) >= lobby.minimum_players:
                return
            lobby.cancelled = True
            lobby.finished = True
            lobby.lobby_deadline = None
            unregister_spy_lobby(lobby)
            lobby.result_text = (
                "انتهت مهلة الردهة لعدم اكتمال عدد اللاعبين المطلوب."
                if lobby.language == "ar"
                else f"Lobby cancelled: the minimum of {lobby.minimum_players} players was not reached in one minute."
            )
            if lobby.lobby_message:
                try:
                    if lobby.guild is not None:
                        await lobby.lobby_message.edit(embed=lobby_embed(lobby.guild, lobby), view=SpyLobbyView(lobby))
                except (discord.HTTPException, discord.NotFound, AttributeError):
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"Spy lobby timeout failed: {error}")
        finally:
            if lobby.lobby_timeout_task is asyncio.current_task():
                lobby.lobby_timeout_task = None

    def _text(self, english: str, arabic: str) -> str:
        return arabic if self.lobby.language == "ar" else english

    async def _check_guild(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.lobby.guild_id:
            await self.private_response(interaction, self._text("This game lobby is no longer available here.", "هذه الردهة غير متاحة هنا."))
            return False
        return True

    async def _deliver_pending(self, interaction: discord.Interaction) -> None:
        pending = self.lobby.pending_roles.pop(interaction.user.id, None)
        if pending:
            try:
                await interaction.followup.send(pending, ephemeral=True)
            except (discord.HTTPException, discord.Forbidden):
                self.lobby.pending_roles[interaction.user.id] = pending

    def _role_message(self, player_id: int) -> str | None:
        if not self.lobby.started or player_id not in self.lobby.players:
            return None
        kind = secret_kind(self.lobby)
        label = secret_kind_label(self.lobby)
        if player_id == self.lobby.spy_id:
            return (
                f"أنت الجاسوس! حاول معرفة {label} دون أن تنكشف."
                if self.lobby.language == "ar"
                else f"You are the Spy! Blend in and discover the secret {kind}."
            )
        return (
            f"أنت مواطن. {label} السري هو: **{self.lobby.secret}**"
            if self.lobby.language == "ar"
            else f"You are a Citizen. The secret {kind} is: **{self.lobby.secret}**"
        )

    async def get_role(self, interaction: discord.Interaction) -> None:
        await self.defer_component(interaction, ephemeral=True)
        if not await self._check_guild(interaction):
            return
        message = self._role_message(interaction.user.id)
        if not message:
            await self.private_response(interaction, self._text("You are not a player in this match.", "أنت لست لاعباً في هذه الجولة."))
            return
        self.lobby.player_interactions[interaction.user.id] = interaction
        await interaction.edit_original_response(content=message)

    async def join(self, interaction: discord.Interaction) -> None:
        await self.defer_component(interaction)
        if not await self._check_guild(interaction):
            return
        self.lobby.player_interactions[interaction.user.id] = interaction
        if self.lobby.started or self.lobby.finished:
            await self.private_response(interaction, "Error: Game has already started. You cannot join or leave mid-game.")
            return
        if interaction.user.id in self.lobby.players:
            await self.private_response(interaction, "Error: You are already in the lobby!")
            return
        if len(self.lobby.players) >= self.lobby.maximum_players:
            await self.private_response(interaction, "Error: This game lobby is currently full!")
            return
        if not isinstance(interaction.user, discord.Member):
            await self.private_response(interaction, self._text("You must be a server member to join.", "يجب أن تكون عضواً في الخادم للانضمام."))
            return
        self.lobby.players[interaction.user.id] = interaction.user.display_name
        if len(self.lobby.players) >= self.lobby.minimum_players:
            await self.cancel_lobby_timeout()
        await self.edit_lobby(interaction)
        await self._deliver_pending(interaction)

    async def leave(self, interaction: discord.Interaction) -> None:
        await self.defer_component(interaction)
        if not await self._check_guild(interaction):
            return
        self.lobby.player_interactions[interaction.user.id] = interaction
        if self.lobby.started or self.lobby.finished:
            await self.private_response(interaction, "Error: Game has already started. You cannot join or leave mid-game.")
            return
        if interaction.user.id not in self.lobby.players:
            await self.private_response(interaction, "Error: You are not currently in this game lobby.")
            return
        self.lobby.players.pop(interaction.user.id, None)
        if interaction.user.id == self.lobby.host_id:
            if self.lobby.players:
                self.lobby.host_id = next(iter(self.lobby.players))
            else:
                self.lobby.cancelled = True
                self.lobby.finished = True
                self.lobby.lobby_deadline = None
                await self.cancel_lobby_timeout()
                message = await interaction.edit_original_response(embed=lobby_embed(interaction.guild, self.lobby), view=None)
                self.lobby.lobby_message = message or interaction.message
                self.stop()
                return
        if len(self.lobby.players) < self.lobby.minimum_players:
            self.start_lobby_timeout(reset=True)
        await self.edit_lobby(interaction)
        await self._deliver_pending(interaction)

    async def show_players(self, interaction: discord.Interaction) -> None:
        await self.defer_component(interaction, ephemeral=True)
        if not await self._check_guild(interaction):
            return
        self.lobby.player_interactions[interaction.user.id] = interaction
        names = [f"{index}. {name}" for index, name in enumerate(self.lobby.players.values(), start=1)]
        await interaction.edit_original_response(content="\n".join(names) or self._text("No players have joined yet.", "لم ينضم أي لاعب بعد."))
        await self._deliver_pending(interaction)

    async def _start_after_ack(self, interaction: discord.Interaction) -> None:
        if not await self._check_guild(interaction):
            return
        if not bool(store.spy_game_config(str(self.lobby.guild_id)).get("enabled", True)):
            await self.private_response(
                interaction,
                self._text(
                    "Spy Game is disabled for this server. Enable it in the Games settings first.",
                    "تم تعطيل لعبة الجاسوس لهذا الخادم. فعّلها من إعدادات الألعاب أولاً.",
                ),
            )
            return
        self.lobby.player_interactions[interaction.user.id] = interaction
        if interaction.user.id != self.lobby.host_id:
            await self.private_response(interaction, self._text("Only the lobby host can start the game.", "يمكن للمضيف فقط بدء اللعبة."))
            return
        if self.lobby.finished:
            await self.private_response(interaction, self._text("This match has already finished.", "انتهت هذه الجولة بالفعل."))
            return
        if self.lobby.started:
            await self.private_response(interaction, self._text("This match has already started.", "بدأت هذه الجولة بالفعل."))
            return
        if len(self.lobby.players) < self.lobby.minimum_players:
            await self.private_response(
                interaction,
                self._text(
                    f"At least {self.lobby.minimum_players} players are required to start.",
                    f"يلزم وجود {self.lobby.minimum_players} لاعبين على الأقل للبدء.",
                ),
            )
            return
        await self.cancel_lobby_timeout()
        self.lobby.started = True
        choice = random.choice(load_vocabulary())
        self.lobby.secret = choice.get(self.lobby.language, choice.get("en", "Airport"))
        self.lobby.secret_type = "object" if choice.get("type") == "object" else "location"
        self.lobby.spy_id = random.choice(list(self.lobby.players))
        self.lobby.spy_name = self.lobby.players.get(self.lobby.spy_id, "Unknown")
        # Keep the match registered so stale vote/turn components can be
        # recovered by the interaction fallback until the match ends.
        await self.edit_lobby(interaction)
        await self._send_private_roles(interaction.guild)
        await self._begin_turn(interaction.guild, interaction.channel)

    async def _start_worker(self, interaction: discord.Interaction) -> None:
        lobby = self.lobby
        try:
            await self._start_after_ack(interaction)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"Spy Game start failed ({type(error).__name__}): {error}")
            if not lobby.finished:
                lobby.started = False
                try:
                    if interaction.guild:
                        view = SpyLobbyView(lobby)
                        register_spy_lobby(lobby)
                        await interaction.edit_original_response(
                            embed=lobby_embed(interaction.guild, lobby),
                            view=view,
                        )
                        view.start_lobby_timeout(reset=True)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass
            await self.private_response(
                interaction,
                self._text(
                    "Spy Game could not start right now. Please try again.",
                    "تعذر بدء لعبة الجاسوس الآن. حاول مرة أخرى.",
                ),
            )
        finally:
            lobby.starting = False
            if lobby.start_task is asyncio.current_task():
                lobby.start_task = None

    async def start(self, interaction: discord.Interaction) -> None:
        # Acknowledge synchronously before storage, member, DM, and Discord
        # message work. This prevents the three-second interaction timeout.
        try:
            await self.defer_component(interaction)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound) as error:
            print(f"Spy Game start acknowledgement failed: {error}")
            return
        except Exception as error:
            print(f"Spy Game start acknowledgement failed ({type(error).__name__}): {error}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Spy Game could not acknowledge that action. Please try again.",
                        ephemeral=True,
                    )
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
            return
        if self.lobby.starting:
            await self.private_response(interaction, self._text("The game is already starting.", "بدأت اللعبة بالفعل."))
            return
        self.lobby.starting = True
        self.lobby.start_task = asyncio.create_task(self._start_worker(interaction))

    async def _send_private_roles(self, guild: discord.Guild) -> None:
        for player_id in self.lobby.players:
            member = await resolve_guild_member(guild, player_id)
            if not member:
                continue
            message = self._role_message(player_id)
            if not message:
                continue
            previous = self.lobby.role_messages.get(player_id)
            try:
                if previous:
                    await previous.edit(content=message)
                else:
                    self.lobby.role_messages[player_id] = await member.send(message)
                continue
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                self.lobby.role_messages.pop(player_id, None)
                pass
            sent_ephemeral = False
            player_interaction = self.lobby.player_interactions.get(player_id)
            if player_interaction:
                try:
                    await player_interaction.followup.send(message, ephemeral=True)
                    sent_ephemeral = True
                except (discord.Forbidden, discord.HTTPException):
                    pass
            if not sent_ephemeral:
                self.lobby.pending_roles[player_id] = message

    async def _begin_turn(self, guild: discord.Guild, channel: discord.TextChannel | None = None) -> None:
        """Select a fresh asker/answerer and start its countdown task."""
        lobby = self.lobby
        if channel is not None:
            lobby.channel = channel
            lobby.channel_id = channel.id
        target = lobby.channel
        if not lobby.started or lobby.finished or target is None or len(lobby.players) < 2:
            return
        async with lobby.turn_lock:
            if lobby.finished or not lobby.started:
                return
            current_task = asyncio.current_task()
            previous = lobby.turn_task
            if previous and not previous.done() and previous is not current_task:
                previous.cancel()
            if lobby.turn_message:
                try:
                    await lobby.turn_message.edit(view=None)
                except (discord.HTTPException, discord.NotFound):
                    pass
            ids = list(lobby.players)
            asker_id, answerer_id = random.sample(ids, 2)
            lobby.asker_id = asker_id
            lobby.answerer_id = answerer_id
            lobby.turn_number += 1
            lobby.turn_generation += 1
            generation = lobby.turn_generation
            duration = max(5, min(600, int(lobby.question_timer_seconds)))
            lobby.turn_deadline = asyncio.get_running_loop().time() + duration
            lobby.turn_skip = asyncio.Event()
            try:
                message = await target.send(
                    embed=turn_embed(guild, lobby, duration),
                    view=TurnView(lobby, generation),
                )
            except (discord.Forbidden, discord.HTTPException) as error:
                # The lobby remains usable (the host can reveal it), but a
                # missing channel permission must not leave an unhandled task.
                print(f"Spy turn announcement failed: {error}")
                lobby.turn_message = None
                lobby.turn_task = None
                return
            lobby.turn_message = message
            lobby.turn_task = asyncio.create_task(self._run_turn(guild, generation, message, lobby.turn_skip))

    async def _run_turn(
        self,
        guild: discord.Guild,
        generation: int,
        message: discord.Message,
        skip_event: asyncio.Event,
    ) -> None:
        lobby = self.lobby
        cancelled = False
        try:
            while (
                not lobby.finished
                and lobby.started
                and lobby.turn_generation == generation
                and lobby.turn_deadline is not None
            ):
                remaining = max(0, math.ceil(lobby.turn_deadline - asyncio.get_running_loop().time()))
                try:
                    await message.edit(embed=turn_embed(guild, lobby, remaining), view=TurnView(lobby, generation))
                except (discord.HTTPException, discord.NotFound):
                    # Continue timing even if Discord rejects an update.
                    pass
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(skip_event.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as error:
            print(f"Spy turn loop failed: {error}")
        finally:
            if lobby.turn_generation == generation:
                lobby.turn_task = None
                lobby.turn_skip = None
                if not cancelled and not lobby.finished and lobby.started:
                    if lobby.end_mode == "auto" and lobby.turn_number >= lobby.auto_end_rounds:
                        await self._begin_vote(guild)
                    else:
                        await self._begin_turn(guild)

    async def _begin_vote(self, guild: discord.Guild, interaction: discord.Interaction | None = None) -> None:
        """Stop questioning and publish the single timed Spy vote."""
        lobby = self.lobby
        if lobby.finished or lobby.vote_started or len(lobby.players) < 2:
            return
        async with lobby.vote_lock:
            if lobby.finished or lobby.vote_started or len(lobby.players) < 2:
                return
            lobby.vote_started = True
            lobby.vote_choices.clear()
            lobby.vote_deadline = asyncio.get_running_loop().time() + VOTE_TIMEOUT_SECONDS
            lobby.vote_event = asyncio.Event()
            if lobby.turn_skip:
                lobby.turn_skip.set()
            if lobby.turn_task and not lobby.turn_task.done() and lobby.turn_task is not asyncio.current_task():
                lobby.turn_task.cancel()
            lobby.turn_task = None
            lobby.turn_deadline = None
            if lobby.turn_message:
                try:
                    await lobby.turn_message.edit(view=None)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass
            if interaction is not None and interaction.response.is_done():
                try:
                    lobby.lobby_message = await interaction.edit_original_response(
                        embed=lobby_embed(guild, lobby),
                        view=None,
                    )
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass
            elif lobby.lobby_message:
                try:
                    await lobby.lobby_message.edit(embed=lobby_embed(guild, lobby), view=None)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass
            try:
                lobby.vote_message = await lobby.channel.send(
                    embed=vote_embed(guild, lobby, VOTE_TIMEOUT_SECONDS),
                    view=VoteView(lobby),
                ) if lobby.channel is not None else None
            except (discord.HTTPException, discord.Forbidden, discord.NotFound) as error:
                print(f"Spy vote announcement failed: {error}")
                lobby.vote_started = False
                lobby.vote_deadline = None
                lobby.vote_event = None
                await self._finish_match(guild, outcome_error=True)
                return
            lobby.vote_task = asyncio.create_task(self._run_vote(guild))

    async def _run_vote(self, guild: discord.Guild) -> None:
        lobby = self.lobby
        event = lobby.vote_event
        try:
            while not lobby.finished and lobby.vote_started and lobby.vote_deadline is not None:
                remaining = max(0, math.ceil(lobby.vote_deadline - asyncio.get_running_loop().time()))
                if lobby.vote_message:
                    try:
                        await lobby.vote_message.edit(embed=vote_embed(guild, lobby, remaining), view=VoteView(lobby))
                    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                        pass
                if remaining <= 0:
                    break
                if event is not None:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=1.0)
                        break
                    except asyncio.TimeoutError:
                        continue
                await asyncio.sleep(1)
            if lobby.finished or not lobby.vote_started:
                return
            await self._finish_match(guild)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"Spy vote loop failed ({type(error).__name__}): {error}")
            await self._finish_match(guild, outcome_error=True)
        finally:
            if lobby.vote_task is asyncio.current_task():
                lobby.vote_task = None

    async def _finish_match(self, guild: discord.Guild, *, outcome_error: bool = False) -> None:
        """Resolve the vote, persist the match, and remove vote controls."""
        lobby = self.lobby
        if lobby.finished:
            return
        lobby.finished = True
        lobby.vote_started = False
        lobby.vote_deadline = None
        if lobby.vote_event:
            lobby.vote_event.set()
        task = lobby.vote_task
        lobby.vote_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if lobby.turn_task and not lobby.turn_task.done() and lobby.turn_task is not asyncio.current_task():
            lobby.turn_task.cancel()
        lobby.turn_task = None
        if outcome_error:
            spy_won = False
            lobby.result_text = (
                "تعذر إكمال التصويت، لذلك انتهت الجولة دون نتيجة."
                if lobby.language == "ar"
                else "The vote could not be completed, so the match ended without a result."
            )
        else:
            counts: dict[int, int] = {}
            for target_id in lobby.vote_choices.values():
                counts[target_id] = counts.get(target_id, 0) + 1
            voted_id = max(counts, key=counts.get) if counts else None
            spy_won = voted_id != lobby.spy_id
            if voted_id == lobby.spy_id:
                lobby.result_text = (
                    f"تم العثور على الجاسوس: {lobby.spy_name or 'غير معروف'}. فاز المواطنون!"
                    if lobby.language == "ar"
                    else f"The players found the Spy: {lobby.spy_name or 'Unknown'}. Citizens win!"
                )
            else:
                lobby.result_text = (
                    f"لم يتم اختيار الجاسوس. يفوز الجاسوس: {lobby.spy_name or 'غير معروف'}."
                    if lobby.language == "ar"
                    else f"The Spy was not chosen. Spy wins: {lobby.spy_name or 'Unknown'}."
                )
        citizens = [
            {"id": str(player_id), "name": player_name}
            for player_id, player_name in lobby.players.items()
            if player_id != lobby.spy_id
        ]
        try:
            store.create_spy_game_log(
                str(lobby.guild_id),
                lobby.secret or "Unknown",
                str(lobby.spy_id or ""),
                lobby.spy_name or "Unknown",
                citizens,
                lobby.result_text or "Match finished",
                lobby.language,
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as error:
            print(f"Spy Game result log failed: {error}")
        unregister_spy_lobby(lobby)
        if lobby.vote_message:
            try:
                final_embed = result_embed(guild, lobby, spy_won=spy_won)
                await lobby.vote_message.edit(embed=final_embed, view=None)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        if lobby.lobby_message:
            try:
                await lobby.lobby_message.edit(embed=lobby_embed(guild, lobby), view=SpyLobbyView(lobby))
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass

    async def reveal(self, interaction: discord.Interaction) -> None:
        """Collect unanimous Manual End requests, then start the vote."""
        await self.defer_component(interaction)
        lobby = self.lobby
        if not await self._check_guild(interaction):
            return
        if not lobby.started or lobby.finished:
            await self.private_response(
                interaction,
                self._text("This match is not accepting reveal requests.", "هذه الجولة لا تقبل طلبات الكشف."),
            )
            return
        if lobby.end_mode == "auto":
            await self.private_response(
                interaction,
                self._text(
                    f"Auto End will start the vote after {lobby.auto_end_rounds} rounds.",
                    f"سيبدأ التصويت تلقائياً بعد {lobby.auto_end_rounds} جولة.",
                ),
            )
            return
        if interaction.user.id not in lobby.players:
            await self.private_response(
                interaction,
                self._text("Only players in this match can request a reveal.", "يمكن للاعبي هذه الجولة فقط طلب الكشف."),
            )
            return
        if interaction.user.id in lobby.reveal_requests:
            await self.private_response(
                interaction,
                self._text("You already requested a reveal.", "لقد طلبت الكشف بالفعل."),
            )
            return
        lobby.reveal_requests.add(interaction.user.id)
        remaining = max(0, len(lobby.players) - len(lobby.reveal_requests))
        if remaining:
            await self.edit_lobby(interaction)
            await self.private_response(
                interaction,
                self._text(
                    f"Reveal request recorded. Waiting for {remaining} more player(s).",
                    f"تم تسجيل طلب الكشف. ننتظر موافقة {remaining} من اللاعبين.",
                ),
            )
            return
        await self._begin_vote(interaction.guild, interaction)

class SpyGame(commands.Cog):
    """Expose /start, !start, and website-triggered Spy Game lobbies."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def _lobby_for_component(interaction: discord.Interaction, host_id: int | None = None) -> SpyLobby | None:
        """Resolve a lobby when discord.py's in-memory ViewStore is stale."""
        if interaction.guild is None:
            return None
        candidates = [
            lobby
            for lobby in ACTIVE_SPY_LOBBIES.values()
            if lobby.guild_id == interaction.guild.id
            and not lobby.finished
            and not lobby.cancelled
            and (host_id is None or lobby.host_id == host_id or host_id in lobby.players)
        ]
        return candidates[-1] if candidates else None

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Recover clicks from lobby messages created before a reload/edit.

        A component whose view is no longer registered would otherwise never
        call ``defer_component`` and Discord displays its generic timeout
        toast. Normal callbacks acknowledge first, so this listener only
        handles interactions that are still unacknowledged after a short
        grace period.
        """
        if interaction.type is not discord.InteractionType.component:
            return
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        if not isinstance(custom_id, str) or not custom_id.startswith("birdbot:games:"):
            return
        await asyncio.sleep(0.35)
        interaction_id = int(getattr(interaction, "id", id(interaction)))
        if interaction_id in _SPY_INTERACTIONS_IN_FLIGHT or interaction.response.is_done():
            return
        parts = custom_id.split(":")
        action = parts[2] if len(parts) > 2 else ""
        if action not in {"join", "leave", "players", "start", "reveal", "role", "skip", "vote"}:
            return
        host_id: int | None = None
        try:
            if len(parts) >= 5:
                host_id = int(parts[4])
        except (TypeError, ValueError):
            host_id = None
        lobby = self._lobby_for_component(interaction, host_id)
        try:
            if lobby is not None and action == "vote":
                view = VoteView(lobby)
                await view.vote(interaction)
                return
            if lobby is not None and action == "skip":
                generation = int(parts[5]) if len(parts) > 5 else lobby.turn_generation
                view = TurnView(lobby, generation)
                await view.skip(interaction)
                return
            if lobby is not None:
                view = SpyLobbyView(lobby)
                callback = {
                    "join": view.join,
                    "leave": view.leave,
                    "players": view.show_players,
                    "start": view.start,
                    "reveal": view.reveal,
                    "role": view.get_role,
                }[action]
                await callback(interaction)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.edit_original_response(
                content="This Spy Game lobby is no longer active. Please run /start to create a new one."
            )
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            return
        except Exception as error:
            print(f"Spy Game fallback interaction failed ({type(error).__name__}): {error}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Spy Game could not process that action. Please run /start again.",
                        ephemeral=True,
                    )
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Keep pre-game lobbies valid when a participant leaves the guild."""
        for lobby in list(ACTIVE_SPY_LOBBIES.values()):
            if lobby.guild_id != member.guild.id or lobby.finished or member.id not in lobby.players:
                continue
            was_host = member.id == lobby.host_id
            lobby.players.pop(member.id, None)
            if lobby.started:
                lobby.reveal_requests.discard(member.id)
                if len(lobby.players) < 2 and lobby.guild is not None:
                    await self._finish_match(lobby.guild, outcome_error=True)
                    continue
                if lobby.vote_started:
                    lobby.vote_choices.pop(member.id, None)
                    if len(lobby.players) < 2 and lobby.guild is not None:
                        await self._finish_match(lobby.guild, outcome_error=True)
                    elif len(lobby.vote_choices) >= len(lobby.players) and lobby.vote_event:
                        lobby.vote_event.set()
                elif member.id in {lobby.asker_id, lobby.answerer_id} and lobby.turn_skip:
                    # End a turn whose participant left; the next pair is
                    # selected by the normal turn-loop finalizer.
                    lobby.turn_skip.set()
                continue
            if not lobby.players:
                lobby.cancelled = True
                lobby.finished = True
                lobby.lobby_deadline = None
                task = lobby.lobby_timeout_task
                lobby.lobby_timeout_task = None
                if task and not task.done():
                    task.cancel()
                unregister_spy_lobby(lobby)
                await refresh_spy_lobby_message(lobby, remove_view=True)
                continue
            if was_host:
                lobby.host_id = next(iter(lobby.players))
            if len(lobby.players) < lobby.minimum_players:
                SpyLobbyView(lobby).start_lobby_timeout(reset=True)
            await refresh_spy_lobby_message(lobby)

    @app_commands.command(name="start", description="Start a BirdBot mini-game lobby.")
    async def start_game(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Games can only be started inside a server.", ephemeral=True)
            return
        if not store.is_guild_activated(interaction.guild.id):
            await interaction.response.send_message("This server has not enabled BirdBot yet.", ephemeral=True)
            return
        await interaction.response.send_message(embed=game_choice_embed(interaction.guild), view=GameChooserView(interaction.user.id))

    @commands.command(name="start")
    async def start_prefix(self, ctx: commands.Context[commands.Bot]) -> None:
        if not ctx.guild:
            return
        if not store.is_guild_activated(ctx.guild.id):
            await ctx.send("This server has not enabled BirdBot yet.")
            return
        await ctx.send(embed=game_choice_embed(ctx.guild), view=GameChooserView(ctx.author.id))

    async def start_interaction_lobby(self, interaction: discord.Interaction, *, deferred: bool = False) -> bool:
        """Create a Spy lobby directly using the server's saved web settings."""
        async def private_error(message: str) -> None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass

        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await private_error("Spy Game can only be started in a text channel.")
            return False
        config = store.spy_game_config(str(interaction.guild.id))
        if not bool(config.get("enabled", True)):
            await private_error("Spy Game is disabled for this server. An administrator can enable it in the Games settings.")
            return False
        host = await resolve_guild_member(interaction.guild, interaction.user.id)
        if host is None:
            await private_error("Your server membership is still syncing. Try again in a moment.")
            return False
        language = str(config.get("language") or "en")
        if language not in {"en", "ar"}:
            language = "en"
        lobby = SpyLobby(
            guild_id=interaction.guild.id,
            host_id=host.id,
            guild=interaction.guild,
            language=language,
            channel_id=interaction.channel.id,
            channel=interaction.channel,
            minimum_players=int(config["min_players"]),
            maximum_players=int(config["max_players"]),
            question_timer_seconds=int(config["question_timer_seconds"]),
            end_mode=str(config.get("end_mode") or "manual"),
            auto_end_rounds=int(config.get("auto_end_rounds") or DEFAULT_AUTO_END_ROUNDS),
        )
        lobby.players[host.id] = host.display_name
        lobby.player_interactions[host.id] = interaction
        lobby.lobby_deadline = asyncio.get_running_loop().time() + LOBBY_TIMEOUT_SECONDS
        register_spy_lobby(lobby)
        view = SpyLobbyView(lobby)
        try:
            if deferred:
                lobby.lobby_message = await interaction.edit_original_response(embed=lobby_embed(interaction.guild, lobby), view=view)
            else:
                await interaction.response.edit_message(embed=lobby_embed(interaction.guild, lobby), view=view)
                lobby.lobby_message = interaction.message
            lobby.lobby_message = lobby.lobby_message or interaction.message
            view.start_lobby_timeout()
            return True
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            unregister_spy_lobby(lobby)
            raise

    async def run_dashboard_lobby(self, guild: discord.Guild, channel: discord.TextChannel, requested_by: str, payload: dict[str, object] | None = None) -> None:
        """Post a lobby requested by an authorized dashboard manager."""
        me = guild.me
        permissions = channel.permissions_for(me) if me is not None else None
        if permissions is None or not permissions.view_channel or not permissions.send_messages:
            raise ValueError("BirdBot cannot send messages in that channel.")
        try:
            host_id = int(requested_by)
        except (TypeError, ValueError) as error:
            raise ValueError("The dashboard user is not a valid server member.") from error
        host = await resolve_guild_member(guild, host_id)
        if host is None:
            raise ValueError("Your server membership is still syncing. Wait a moment, then create the lobby again.")
        config = store.spy_game_config(str(guild.id))
        if not bool(config.get("enabled", True)):
            raise ValueError("Spy Game is disabled for this server. Enable it in the Games settings first.")
        language = str(config.get("language") or "en") if config.get("language") in {"en", "ar"} else "en"
        lobby = SpyLobby(
            guild_id=guild.id,
            host_id=host.id,
            guild=guild,
            language=language,
            channel_id=channel.id,
            channel=channel,
            minimum_players=config["min_players"],
            maximum_players=config["max_players"],
            question_timer_seconds=config["question_timer_seconds"],
            end_mode=str(config.get("end_mode") or "manual"),
            auto_end_rounds=int(config.get("auto_end_rounds") or DEFAULT_AUTO_END_ROUNDS),
        )
        lobby.players[host.id] = host.display_name
        lobby.lobby_deadline = asyncio.get_running_loop().time() + LOBBY_TIMEOUT_SECONDS
        register_spy_lobby(lobby)
        view = SpyLobbyView(lobby)
        lobby.lobby_message = await channel.send(embed=lobby_embed(guild, lobby), view=view)
        view.start_lobby_timeout()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SpyGame(bot))
