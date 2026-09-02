"""Guess the Number Discord game.

Players join a lobby, then submit one private guess per round.  The target is
kept in memory and is never included in the public round message until the
match is over.  When a round has no winner, the bot narrows the public range
with a higher/lower (or between) hint.  A tie creates a fresh round zero with
only the tied players still in the match.
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from discord_members import resolve_guild_member
from storage import store


DEFAULT_MIN_PLAYERS = 2
DEFAULT_MAX_PLAYERS = 20
DEFAULT_NUMBER_MIN = 1
DEFAULT_NUMBER_MAX = 100
LOBBY_TIMEOUT_SECONDS = 120
MESSAGE_COLOUR = discord.Colour.from_rgb(55, 65, 81)


def _text(game: "GuessNumberGame", english: str, arabic: str) -> str:
    return arabic if game.language == "ar" else english


def _mention(user_id: int) -> str:
    return f"<@{user_id}>"


@dataclass
class GuessNumberGame:
    guild: discord.Guild
    channel: discord.TextChannel
    host_id: int
    minimum_players: int = DEFAULT_MIN_PLAYERS
    maximum_players: int = DEFAULT_MAX_PLAYERS
    number_minimum: int = DEFAULT_NUMBER_MIN
    number_maximum: int = DEFAULT_NUMBER_MAX
    language: str = "en"
    players: dict[int, str] = field(default_factory=dict)
    active_players: set[int] = field(default_factory=set)
    guesses: dict[int, int] = field(default_factory=dict)
    lower_bound: int = DEFAULT_NUMBER_MIN
    upper_bound: int = DEFAULT_NUMBER_MAX
    target: int | None = None
    round_number: int = 1
    hint: str = ""
    started: bool = False
    finished: bool = False
    cancelled: bool = False
    winner_id: int | None = None
    winner_ids: tuple[int, ...] = field(default_factory=tuple)
    message: discord.Message | None = field(default=None, repr=False)
    view: "GuessNumberView | None" = field(default=None, repr=False)
    round_message: discord.Message | None = field(default=None, repr=False)
    round_view: "GuessNumberView | None" = field(default=None, repr=False)
    timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def reset_number(self, *, round_number: int | None = None) -> None:
        self.lower_bound = self.number_minimum
        self.upper_bound = self.number_maximum
        self.target = random.randint(self.number_minimum, self.number_maximum)
        self.guesses.clear()
        self.hint = ""
        if round_number is not None:
            self.round_number = round_number


ACTIVE_GUESS_NUMBER_GAMES: dict[tuple[int, int], GuessNumberGame] = {}


def _key(guild_id: int, channel_id: int) -> tuple[int, int]:
    return int(guild_id), int(channel_id)


def active_game(guild_id: int, channel_id: int) -> GuessNumberGame | None:
    game = ACTIVE_GUESS_NUMBER_GAMES.get(_key(guild_id, channel_id))
    if game and not game.finished and not game.cancelled:
        return game
    return None


def register_game(game: GuessNumberGame) -> None:
    ACTIVE_GUESS_NUMBER_GAMES[_key(game.guild.id, game.channel.id)] = game


def unregister_game(game: GuessNumberGame) -> None:
    key = _key(game.guild.id, game.channel.id)
    if ACTIVE_GUESS_NUMBER_GAMES.get(key) is game:
        ACTIVE_GUESS_NUMBER_GAMES.pop(key, None)


def game_embed(game: GuessNumberGame) -> discord.Embed:
    arabic = game.language == "ar"
    title = "خمن الرقم" if arabic else "Guess the Number"
    if game.cancelled:
        description = (
            (
                "أُلغيت اللعبة قبل تسجيل فائز."
                if game.started
                else "انتهت هذه اللعبة قبل أن تبدأ."
            )
            if arabic
            else (
                "This game was cancelled before a winner was recorded."
                if game.started
                else "This game ended before it started."
            )
        )
    elif game.finished:
        winners = game.winner_ids or ((game.winner_id,) if game.winner_id else ())
        winner = ", ".join(_mention(player_id) for player_id in winners) or "—"
        description = (
            f"الفائزون: {winner}\nالرقم كان **{game.target}**."
            if arabic
            else f"Winner{'s' if len(winners) != 1 else ''}: {winner}\nThe number was **{game.target}**."
        )
    elif not game.started:
        description = _text(
            game,
            f"Join the lobby, then {_mention(game.host_id)} can start it. Each player gets one guess per round.",
            f"انضم إلى الرَدْهة، ثم يمكن لـ {_mention(game.host_id)} بدء اللعبة. يحصل كل لاعب على تخمين واحد في كل جولة.",
        )
    else:
        pending = len(game.active_players) - len(game.guesses)
        description = _text(
            game,
            f"The round card below has the Guess number button. Waiting for **{pending}** player(s).",
            f"أرسل تخمينًا واحدًا من الزر أدناه. ننتظر **{pending}** لاعبًا.",
        )
        if game.hint:
            description += f"\n\n{game.hint}"
    embed = discord.Embed(title=title, description=description, colour=MESSAGE_COLOUR)
    if game.finished:
        embed.add_field(
            name="الرقم السري" if arabic else "Secret number",
            value=str(game.target if game.target is not None else "—"),
            inline=True,
        )
    elif game.started:
        embed.add_field(name="الجولة" if arabic else "Round", value=str(game.round_number), inline=True)
        embed.add_field(
            name="النطاق" if arabic else "Range",
            value=f"{game.lower_bound} – {game.upper_bound}",
            inline=True,
        )
        embed.add_field(
            name="التخمينات" if arabic else "Guesses",
            value=f"{len(game.guesses)} / {len(game.active_players)}",
            inline=True,
        )
    embed.add_field(
        name="اللاعبون" if arabic else "Players",
        value=f"{len(game.players)} / {game.maximum_players}",
        inline=True,
    )
    if game.players:
        names = "\n".join(f"{index}. {_mention(player_id)}" for index, player_id in enumerate(game.players, 1))
        if len(names) <= 1024:
            embed.add_field(name="المشاركون" if arabic else "Participants", value=names, inline=False)
    embed.set_footer(text="BirdBot · خمن الرقم" if arabic else "BirdBot · Guess the Number")
    return embed


def _round_hint(game: GuessNumberGame) -> str:
    """Create a useful hint without revealing the hidden target."""
    guesses = list(game.guesses.values())
    if not guesses or game.target is None:
        return ""
    minimum_guess, maximum_guess = min(guesses), max(guesses)
    if maximum_guess < game.target:
        game.lower_bound = max(game.lower_bound, maximum_guess + 1)
        return _text(
            game,
            f"Hint: the number is higher than **{maximum_guess}**.",
            f"تلميح: الرقم أكبر من **{maximum_guess}**.",
        )
    if minimum_guess > game.target:
        game.upper_bound = min(game.upper_bound, minimum_guess - 1)
        return _text(
            game,
            f"Hint: the number is lower than **{minimum_guess}**.",
            f"تلميح: الرقم أصغر من **{minimum_guess}**.",
        )
    # Guesses straddle the answer.  The target must be between the nearest
    # lower and upper guesses; this is more informative than a generic hint.
    lower = max(value for value in guesses if value < game.target)
    upper = min(value for value in guesses if value > game.target)
    game.lower_bound = max(game.lower_bound, lower + 1)
    game.upper_bound = min(game.upper_bound, upper - 1)
    return _text(
        game,
        f"Hint: the number is between **{lower}** and **{upper}**.",
        f"تلميح: الرقم بين **{lower}** و **{upper}**.",
    )


def round_embed(game: GuessNumberGame) -> discord.Embed:
    """Build the separate round card that owns the Guess number button."""
    arabic = game.language == "ar"
    title = "خمن الرقم" if arabic else "Guess the number"
    description = _text(
        game,
        f"Round **{game.round_number}** is open. Each active player may submit one guess.",
        f"الجولة **{game.round_number}** مفتوحة. يمكن لكل لاعب نشط إرسال تخمين واحد.",
    )
    if game.hint:
        description += f"\n\n{game.hint}"
    embed = discord.Embed(title=title, description=description, colour=MESSAGE_COLOUR)
    embed.add_field(
        name="Range" if not arabic else "النطاق",
        value=f"{game.lower_bound} – {game.upper_bound}",
        inline=True,
    )
    embed.add_field(
        name="Finished" if not arabic else "اكتمل التخمين",
        value=f"{len(game.guesses)} / {len(game.active_players)}",
        inline=True,
    )
    embed.set_footer(text="BirdBot · Guess the number" if not arabic else "BirdBot · خمن الرقم")
    return embed


def guess_progress_text(game: GuessNumberGame, member_id: int, remaining: int) -> str:
    mention = _mention(member_id)
    return _text(
        game,
        f"{mention} has finished guessing. Remaining players: {remaining}.",
        f"أنهى {mention} التخمين. اللاعبون المتبقون: {remaining}.",
    )


def round_transition_text(game: GuessNumberGame, finished_round: int, next_round: int) -> str:
    return _text(
        game,
        f"Round {finished_round} finished. Round {next_round} begins.",
        f"انتهت الجولة {finished_round}. تبدأ الجولة {next_round}.",
    )


def _legacy_collapsed_range_text(game: GuessNumberGame, winners: list[int]) -> str:
    """Legacy alias retained for compatibility with older imports."""
    mentions = ", ".join(_mention(player_id) for player_id in winners) or "—"
    number = game.target if game.target is not None else game.lower_bound
    return _text(
        game,
        f"The range narrowed to **{number}**, so the number was **{number}**. Range winners: {mentions}. They advance to the next round.",
        f"Ø§Ù†Ø­ØµØ± Ø§Ù„Ù†Ø·Ø§Ù‚ ÙÙŠ **{number}**ØŒ Ù„Ø°Ù„Ùƒ ÙƒØ§Ù† Ø§Ù„Ø±Ù‚Ù… **{number}**. Ø§Ù„ÙØ§Ø¦Ø²ÙˆÙ†: {mentions}.",
    )


def range_is_collapsed(game: GuessNumberGame) -> bool:
    """Return true only when the narrowed range proves the hidden answer."""
    return (
        game.target is not None
        and game.lower_bound == game.upper_bound
        and game.lower_bound == game.target
    )


def collapsed_range_text(game: GuessNumberGame, winners: list[int]) -> str:
    """Return the clear final announcement for a one-number range."""
    mentions = ", ".join(_mention(player_id) for player_id in winners) or "—"
    number = game.target if game.target is not None else game.lower_bound
    return _text(
        game,
        f"The range narrowed to **{number}**, so the number was **{number}**. Range winners: {mentions}. They advance to the next round.",
        f"\u0627\u0646\u062d\u0635\u0631 \u0627\u0644\u0646\u0637\u0627\u0642 \u0641\u064a **{number}**\u060c \u0644\u0630\u0644\u0643 \u0643\u0627\u0646 \u0627\u0644\u0631\u0642\u0645 **{number}**. \u0641\u0627\u0626\u0632\u0648 \u0627\u0644\u0646\u0637\u0627\u0642: {mentions}. \u064a\u0646\u062a\u0642\u0644\u0648\u0646 \u0625\u0644\u0649 \u0627\u0644\u062c\u0648\u0644\u0629 \u0627\u0644\u062a\u0627\u0644\u064a\u0629.",
    )


async def post_round_message(game: GuessNumberGame) -> None:
    """Post a fresh round card instead of reusing the lobby message."""
    if game.finished or game.cancelled:
        return
    view = GuessNumberView(game, mode="round")
    try:
        game.round_message = await game.channel.send(embed=round_embed(game), view=view)
        game.round_view = view
    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
        view.stop()
        if not game.finished:
            await finish_game(game, cancelled=True)
        raise


async def close_round_message(game: GuessNumberGame) -> None:
    """Disable the previous round button before the next round is posted."""
    if game.round_view:
        game.round_view.stop()
    game.round_view = None
    if game.round_message is not None:
        try:
            await game.round_message.edit(view=None)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass


async def refresh_game(game: GuessNumberGame, *, remove_view: bool = False) -> None:
    if game.message is None:
        return
    # The lobby controls are only shown before Start. Once the host starts,
    # the lobby card is finalized and a separate round card owns guessing.
    view: discord.ui.View | None = None if remove_view or game.started else GuessNumberView(game, mode="lobby")
    try:
        await game.message.edit(embed=game_embed(game), view=view)
    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
        return
    if isinstance(view, GuessNumberView) and view.mode == "lobby":
        game.view = view


async def finish_game(
    game: GuessNumberGame,
    winner_id: int | None = None,
    *,
    winner_ids: list[int] | tuple[int, ...] | None = None,
    cancelled: bool = False,
) -> None:
    if game.finished:
        return
    game.finished = True
    game.cancelled = cancelled
    winner_source = winner_ids if winner_ids is not None else (() if winner_id is None else (winner_id,))
    resolved_winners = tuple(dict.fromkeys(int(player_id) for player_id in winner_source))
    game.winner_ids = resolved_winners
    game.winner_id = resolved_winners[0] if len(resolved_winners) == 1 else winner_id
    if game.timeout_task and not game.timeout_task.done() and game.timeout_task is not asyncio.current_task():
        game.timeout_task.cancel()
    game.timeout_task = None
    unregister_game(game)
    if game.view:
        game.view.stop()
    game.view = None
    if game.round_view:
        game.round_view.stop()
    game.round_view = None
    if game.round_message is not None:
        try:
            await game.round_message.edit(embed=game_embed(game), view=None)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass
    await refresh_game(game, remove_view=True)


async def lobby_timeout(game: GuessNumberGame) -> None:
    try:
        await asyncio.sleep(LOBBY_TIMEOUT_SECONDS)
        if not game.started and not game.finished:
            await finish_game(game, cancelled=True)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"Guess the Number lobby timeout failed ({type(error).__name__}): {error}")


async def resolve_round(game: GuessNumberGame) -> None:
    correct = [player_id for player_id, guess in game.guesses.items() if guess == game.target]
    if len(correct) == 1:
        await finish_game(game, correct[0])
        return
    if len(correct) > 1:
        # A tie eliminates every other participant and starts a completely
        # fresh number at round zero, as requested.
        game.active_players = set(correct)
        game.players = {player_id: game.players[player_id] for player_id in correct if player_id in game.players}
        finished_round = game.round_number
        game.reset_number(round_number=0)
        game.hint = _text(
            game,
            f"Tie! {', '.join(_mention(player_id) for player_id in correct)} continue. A new number starts at Round 0.",
            f"تعادل! يستمر {', '.join(_mention(player_id) for player_id in correct)}. يبدأ رقم جديد من الجولة 0.",
        )
        await close_round_message(game)
        await game.channel.send(
            _text(
                game,
                f"Round {finished_round} finished. Tie between {', '.join(_mention(player_id) for player_id in correct)}. Round 0 begins.",
                f"انتهت الجولة {finished_round}. حدث تعادل بين {', '.join(_mention(player_id) for player_id in correct)}. تبدأ الجولة 0.",
            )
        )
        await post_round_message(game)
        await refresh_game(game, remove_view=True)
        return
    finished_round = game.round_number
    game.hint = _round_hint(game)
    if range_is_collapsed(game) and game.active_players:
        # A final clue can prove a single answer (for example 56–56) even
        # when nobody entered that value in the preceding round.  Let the
        # remaining players advance after an explicit announcement instead of
        # opening a broken or unwinnable round.
        advancing_players = sorted(game.active_players)
        await close_round_message(game)
        try:
            await game.channel.send(collapsed_range_text(game, advancing_players))
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass
        next_round = finished_round + 1
        game.reset_number(round_number=next_round)
        game.hint = _text(
            game,
            "The previous number was confirmed. Everyone continues with a fresh number.",
            "\u062a\u0645 \u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0631\u0642\u0645 \u0627\u0644\u0633\u0627\u0628\u0642. \u064a\u0633\u062a\u0645\u0631 \u0627\u0644\u062c\u0645\u064a\u0639 \u0645\u0639 \u0631\u0642\u0645 \u062c\u062f\u064a\u062f.",
        )
        try:
            await game.channel.send(round_transition_text(game, finished_round, next_round))
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            await finish_game(game, cancelled=True)
            return
        await post_round_message(game)
        await refresh_game(game, remove_view=True)
        return
    game.round_number += 1
    game.guesses.clear()
    await close_round_message(game)
    await game.channel.send(round_transition_text(game, finished_round, game.round_number))
    await post_round_message(game)
    await refresh_game(game, remove_view=True)


async def submit_guess(game: GuessNumberGame, member_id: int, value: int) -> str:
    async with game.lock:
        if game.finished or game.cancelled:
            return _text(game, "This game has finished.", "انتهت هذه اللعبة.")
        if not game.started:
            return _text(game, "The host has not started the game yet.", "لم يبدأ المضيف اللعبة بعد.")
        if member_id not in game.active_players:
            return _text(game, "You are not an active player in this round.", "أنت لست لاعبًا نشطًا في هذه الجولة.")
        if member_id in game.guesses:
            return _text(game, "You already guessed this round.", "لقد أرسلت تخمينك في هذه الجولة بالفعل.")
        if value < game.lower_bound or value > game.upper_bound:
            return _text(
                game,
                f"Choose a number from {game.lower_bound} to {game.upper_bound}.",
                f"اختر رقمًا من {game.lower_bound} إلى {game.upper_bound}.",
            )
        game.guesses[member_id] = value
        remaining = len(game.active_players) - len(game.guesses)
        if game.round_message is not None:
            try:
                await game.round_message.edit(embed=round_embed(game), view=game.round_view)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        try:
            await game.channel.send(guess_progress_text(game, member_id, remaining))
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            # The private interaction still receives the result if the public
            # progress message cannot be posted.
            pass
        if remaining <= 0:
            await resolve_round(game)
            if game.finished:
                return _text(game, "Your guess was correct — you won!", "كان تخمينك صحيحًا — لقد فزت!")
            return _text(game, "Round complete. The public game message has been updated.", "اكتملت الجولة وتم تحديث رسالة اللعبة.")
        return _text(game, f"Your guess was recorded. Waiting for {remaining} player(s).", f"تم تسجيل تخمينك. ننتظر {remaining} لاعبًا.")


class GuessNumberModal(discord.ui.Modal):
    def __init__(self, game: GuessNumberGame) -> None:
        super().__init__(title="Guess the Number" if game.language != "ar" else "خمن الرقم")
        self.game = game
        self.guess_input = discord.ui.TextInput(
            label="Your guess" if game.language != "ar" else "تخمينك",
            placeholder=f"{game.lower_bound}–{game.upper_bound}",
            min_length=1,
            max_length=12,
            required=True,
        )
        self.add_item(self.guess_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = int(str(self.guess_input.value).strip())
        except (TypeError, ValueError):
            await interaction.response.send_message(
                "Enter a whole number." if self.game.language != "ar" else "أدخل رقمًا صحيحًا.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await submit_guess(self.game, interaction.user.id, value)
            await interaction.followup.send(result, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            return
        except Exception as error:
            print(f"Guess the Number submission failed ({type(error).__name__}): {error}")
            await interaction.followup.send(
                "The guess could not be submitted. Try again." if self.game.language != "ar" else "تعذر إرسال التخمين. حاول مرة أخرى.",
                ephemeral=True,
            )


class GuessNumberView(discord.ui.View):
    def __init__(self, game: GuessNumberGame, *, mode: str = "lobby") -> None:
        super().__init__(timeout=None)
        self.game = game
        self.mode = mode
        arabic = game.language == "ar"
        if mode == "round":
            guess = discord.ui.Button(
                label="خمن الرقم" if arabic else "Guess number",
                style=discord.ButtonStyle.primary,
                custom_id=f"birdbot:guess_number:guess:{game.guild.id}:{game.channel.id}",
            )
            guess.callback = self.guess
            self.add_item(guess)
            game.round_view = self
            return

        game.view = self
        join = discord.ui.Button(
            label="انضمام" if arabic else "Join",
            style=discord.ButtonStyle.secondary,
            custom_id=f"birdbot:guess_number:join:{game.guild.id}:{game.channel.id}",
        )
        leave = discord.ui.Button(
            label="مغادرة" if arabic else "Leave",
            style=discord.ButtonStyle.secondary,
            custom_id=f"birdbot:guess_number:leave:{game.guild.id}:{game.channel.id}",
        )
        start = discord.ui.Button(
            label="بدء" if arabic else "Start",
            style=discord.ButtonStyle.secondary,
            custom_id=f"birdbot:guess_number:start:{game.guild.id}:{game.channel.id}",
        )
        join.callback = self.join
        leave.callback = self.leave
        start.callback = self.start
        self.add_item(join)
        self.add_item(leave)
        self.add_item(start)
        if game.started or game.finished:
            join.disabled = True
            leave.disabled = True
            start.disabled = True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: object) -> None:
        print(f"Guess the Number interaction failed ({type(error).__name__}): {error}")
        try:
            message = "The game could not process that action. Please try again." if self.game.language != "ar" else "تعذر تنفيذ هذا الإجراء في اللعبة. حاول مرة أخرى."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass

    async def _reply(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def join(self, interaction: discord.Interaction) -> None:
        game = self.game
        if game.started or game.finished:
            await self._reply(interaction, _text(game, "The game has already started.", "بدأت اللعبة بالفعل."))
            return
        if interaction.user.id in game.players:
            await self._reply(interaction, _text(game, "You are already in the lobby.", "أنت موجود في الرَدْهة بالفعل."))
            return
        if len(game.players) >= game.maximum_players:
            await self._reply(interaction, _text(game, "The lobby is full.", "الرَدْهة ممتلئة."))
            return
        member = await resolve_guild_member(game.guild, interaction.user.id)
        if member is None:
            await self._reply(interaction, _text(game, "Your membership is still syncing.", "لا تزال عضويتك قيد المزامنة."))
            return
        game.players[member.id] = member.display_name
        await interaction.response.edit_message(embed=game_embed(game), view=GuessNumberView(game, mode="lobby"))

    async def leave(self, interaction: discord.Interaction) -> None:
        game = self.game
        if game.started or game.finished:
            await self._reply(interaction, _text(game, "You cannot leave after the game starts.", "لا يمكنك المغادرة بعد بدء اللعبة."))
            return
        if interaction.user.id not in game.players:
            await self._reply(interaction, _text(game, "You are not in this lobby.", "أنت لست في هذه الرَدْهة."))
            return
        game.players.pop(interaction.user.id, None)
        if interaction.user.id == game.host_id:
            if game.players:
                game.host_id = next(iter(game.players))
            else:
                await finish_game(game, cancelled=True)
                await interaction.response.edit_message(embed=game_embed(game), view=None)
                return
        await interaction.response.edit_message(embed=game_embed(game), view=GuessNumberView(game, mode="lobby"))

    async def start(self, interaction: discord.Interaction) -> None:
        game = self.game
        if interaction.user.id != game.host_id:
            await self._reply(interaction, _text(game, "Only the host can start the game.", "يمكن للمضيف فقط بدء اللعبة."))
            return
        if game.started or game.finished:
            await self._reply(interaction, _text(game, "This game has already started.", "بدأت هذه اللعبة بالفعل."))
            return
        if len(game.players) < game.minimum_players:
            await self._reply(interaction, _text(game, f"At least {game.minimum_players} players are required.", f"يلزم وجود {game.minimum_players} لاعبين على الأقل."))
            return
        if game.timeout_task and not game.timeout_task.done():
            game.timeout_task.cancel()
        game.timeout_task = None
        game.started = True
        game.active_players = set(game.players)
        game.reset_number(round_number=1)
        if game.view:
            game.view.stop()
        game.view = None
        await interaction.response.edit_message(embed=game_embed(game), view=None)
        try:
            await post_round_message(game)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            await finish_game(game, cancelled=True)
            raise

    async def guess(self, interaction: discord.Interaction) -> None:
        game = self.game
        if not game.started or game.finished:
            await self._reply(interaction, _text(game, "The game has not started yet.", "لم تبدأ اللعبة بعد."))
            return
        if interaction.user.id not in game.active_players:
            await self._reply(interaction, _text(game, "You are not active in this round.", "أنت لست لاعبًا نشطًا في هذه الجولة."))
            return
        if interaction.user.id in game.guesses:
            await self._reply(interaction, _text(game, "You already guessed this round.", "لقد أرسلت تخمينك بالفعل في هذه الجولة."))
            return
        await interaction.response.send_modal(GuessNumberModal(game))


class GuessNumber(commands.Cog):
    """Expose /guess-number, !guess-number, and dashboard-created lobbies."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def create_lobby(self, guild: discord.Guild, channel: discord.TextChannel, member: discord.Member) -> GuessNumberGame:
        config = store.guess_number_game_config(str(guild.id))
        if not bool(config.get("enabled", True)):
            raise ValueError("Guess the Number is disabled for this server. Enable it in Games settings first.")
        game = GuessNumberGame(
            guild=guild,
            channel=channel,
            host_id=member.id,
            minimum_players=int(config.get("min_players", DEFAULT_MIN_PLAYERS)),
            maximum_players=int(config.get("max_players", DEFAULT_MAX_PLAYERS)),
            number_minimum=int(config.get("number_min", DEFAULT_NUMBER_MIN)),
            number_maximum=int(config.get("number_max", DEFAULT_NUMBER_MAX)),
            language=str(config.get("language") or "en") if config.get("language") in {"en", "ar"} else "en",
        )
        game.players[member.id] = member.display_name
        register_game(game)
        game.message = await channel.send(embed=game_embed(game), view=GuessNumberView(game, mode="lobby"))
        game.timeout_task = asyncio.create_task(lobby_timeout(game))
        return game

    async def _interaction_message(self, interaction: discord.Interaction, message: str, *, ephemeral: bool = True) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)

    async def _command(self, interaction: discord.Interaction, guess: int | None = None) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await self._interaction_message(interaction, "Guess the Number can only be played in a server text channel.")
            return
        # A slash command may need to edit the public game message before it
        # can answer privately.  Defer first so a slow Discord API response
        # never makes the command interaction expire.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        if not store.is_guild_activated(interaction.guild.id):
            await self._interaction_message(interaction, "This server has not enabled BirdBot yet.")
            return
        game = active_game(interaction.guild.id, interaction.channel.id)
        if game and game.started and guess is not None:
            result = await submit_guess(game, interaction.user.id, guess)
            await self._interaction_message(interaction, result)
            return
        if game and game.started:
            await self._interaction_message(
                interaction,
                "The game is running. Use the Guess number button." if game.language != "ar" else "اللعبة جارية. استخدم زر خمن الرقم.",
            )
            return
        if game and not game.started:
            view = GuessNumberView(game, mode="lobby")
            if interaction.user.id in game.players:
                await self._interaction_message(interaction, "You are already in the lobby." if game.language != "ar" else "أنت موجود في الرَدْهة بالفعل.")
            elif len(game.players) >= game.maximum_players:
                await self._interaction_message(interaction, "The lobby is full." if game.language != "ar" else "الرَدْهة ممتلئة.")
            else:
                member = await resolve_guild_member(interaction.guild, interaction.user.id)
                if member is None:
                    await self._interaction_message(interaction, "Your membership is still syncing." if game.language != "ar" else "لا تزال عضويتك قيد المزامنة.")
                else:
                    game.players[member.id] = member.display_name
                    if interaction.message:
                        await interaction.response.edit_message(embed=game_embed(game), view=GuessNumberView(game, mode="lobby"))
                    else:
                        await self._interaction_message(interaction, "You joined the lobby." if game.language != "ar" else "انضممت إلى الرَدْهة.")
            return
        try:
            member = await resolve_guild_member(interaction.guild, interaction.user.id)
            if member is None:
                await self._interaction_message(interaction, "Your membership is still syncing.")
                return
            await self.create_lobby(interaction.guild, interaction.channel, member)
            await self._interaction_message(interaction, "Lobby created. Other players can join using the button.")
        except (ValueError, discord.Forbidden, discord.HTTPException) as error:
            await self._interaction_message(interaction, str(error) or "The game could not be started.")

    @app_commands.command(name="guess-number", description="Start or play a Guess the Number game.")
    @app_commands.describe(guess="Your whole-number guess when a game is already running")
    async def guess_number_slash(self, interaction: discord.Interaction, guess: int | None = None) -> None:
        await self._command(interaction, guess)

    @app_commands.command(name="guess", description="Start or play Guess the Number.")
    @app_commands.describe(guess="Your whole-number guess when a game is already running")
    async def guess_slash(self, interaction: discord.Interaction, guess: int | None = None) -> None:
        await self._command(interaction, guess)

    @commands.command(name="guess-number", aliases=("guess", "number"))
    async def guess_number_prefix(self, ctx: commands.Context[commands.Bot], guess: int | None = None) -> None:
        if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
            return
        if not store.is_guild_activated(ctx.guild.id):
            await ctx.send("This server has not enabled BirdBot yet.")
            return
        game = active_game(ctx.guild.id, ctx.channel.id)
        if game and game.started and guess is not None:
            await ctx.send(await submit_guess(game, ctx.author.id, guess))
            return
        if game:
            await ctx.send(embed=game_embed(game), view=GuessNumberView(game, mode="round" if game.started else "lobby"))
            return
        member = await resolve_guild_member(ctx.guild, ctx.author.id)
        if member is None:
            await ctx.send("Your server membership is still syncing. Try again shortly.")
            return
        try:
            await self.create_lobby(ctx.guild, ctx.channel, member)
        except (ValueError, discord.Forbidden, discord.HTTPException) as error:
            await ctx.send(str(error) or "The game could not be started.")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        for game in list(ACTIVE_GUESS_NUMBER_GAMES.values()):
            if game.guild.id != member.guild.id or member.id not in game.players:
                continue
            game.players.pop(member.id, None)
            game.active_players.discard(member.id)
            game.guesses.pop(member.id, None)
            if member.id == game.host_id and game.players:
                game.host_id = next(iter(game.players))
            if game.started and len(game.active_players) == 1:
                await finish_game(game, next(iter(game.active_players)))
            elif game.started and len(game.active_players) < 1:
                await finish_game(game, cancelled=True)
            elif not game.players:
                await finish_game(game, cancelled=True)
            else:
                await refresh_game(game)

    async def run_dashboard_lobby(self, guild: discord.Guild, channel: discord.TextChannel, requested_by: str, payload: dict[str, object] | None = None) -> None:
        me = guild.me
        permissions = channel.permissions_for(me) if me is not None else None
        if permissions is None or not permissions.view_channel or not permissions.send_messages:
            raise ValueError("BirdBot cannot send messages in that channel.")
        try:
            member_id = int(requested_by)
        except (TypeError, ValueError) as error:
            raise ValueError("The dashboard user is not a valid server member.") from error
        member = await resolve_guild_member(guild, member_id)
        if member is None:
            raise ValueError("Your server membership is still syncing. Try again shortly.")
        if active_game(guild.id, channel.id):
            raise ValueError("A Guess the Number game is already active in that channel.")
        await self.create_lobby(guild, channel, member)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GuessNumber(bot))
