"""Roulette lobby for BirdBot's single global Discord client.

Roulette intentionally follows the same lightweight lobby architecture as Spy
Game: one in-memory lobby per dashboard/Discord start action.  The lobby
banner is a local asset uploaded with the message so it also works when the
dashboard is running on localhost. Once started, a short wheel animation and
public elimination-turn messages drive the match until one player remains.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from discord_members import resolve_guild_member
from games.spy.game import configured_button_emoji
from storage import store

try:  # Pillow is used lazily for the animated Discord wheel.
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Keep the bot usable until dependencies are installed.
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT_DIR / "config" / "games" / "roulette.config.json"
DEFAULT_MIN_PLAYERS = 2
DEFAULT_MAX_PLAYERS = 20
LOBBY_FILL_TIMEOUT_SECONDS = 30
LOBBY_READY_TIMEOUT_SECONDS = 60
DEFAULT_TURN_TIMER_SECONDS = 30
SPIN_STEPS = 14
DEFAULT_WHEEL_COLORS = (
    "#6B7280", "#9CA3AF", "#4B5563", "#374151",
    "#D1D5DB", "#818CF8", "#A78BFA",
)


def load_config() -> dict[str, Any]:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def banner_path() -> Path | None:
    raw = load_config().get("bannerPath")
    if not isinstance(raw, str) or not raw.strip():
        return None
    relative = raw.replace("\\", "/").lstrip("./")
    # Assets configured as ./games/... are served from website/games.  Also
    # accept a root-relative path for operators with a custom asset folder.
    candidates = (ROOT_DIR / "website" / relative, ROOT_DIR / relative)
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def configured_roulette_emoji(name: str, fallback: str) -> discord.PartialEmoji | str | None:
    return configured_button_emoji(name) or configured_button_emoji(fallback)


def _roulette_font(size: int, *, bold: bool = False) -> Any:
    """Load a portable font for wheel labels, including common Linux hosts."""
    if ImageFont is None:
        return None
    windows_fonts = Path("C:/Windows/Fonts")
    candidates = [
        windows_fonts / ("segoeuib.ttf" if bold else "segoeui.ttf"),
        windows_fonts / ("arialbd.ttf" if bold else "arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu") / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2") / ("LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"),
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), size=size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _short_wheel_name(value: str, limit: int = 15) -> str:
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[: max(1, limit - 1)] + "…"


def _wheel_palette(lobby: RouletteLobby) -> list[tuple[int, int, int]]:
    """Build the seven-color palette saved in the web dashboard.

    A lobby created before palette support may not have ``wheel_colors``.  In
    that case retain backwards compatibility by deriving seven shades from
    its previous single base color.
    """
    configured = getattr(lobby, "wheel_colors", None)
    if isinstance(configured, (list, tuple)) and len(configured) == 7:
        parsed: list[tuple[int, int, int]] = []
        for value in configured:
            raw_value = str(value or "").lstrip("#")
            if len(raw_value) != 6:
                parsed = []
                break
            try:
                parsed.append(tuple(int(raw_value[index : index + 2], 16) for index in (0, 2, 4)))
            except (TypeError, ValueError):
                parsed = []
                break
        if len(parsed) == 7:
            return parsed
    raw = str(getattr(lobby, "wheel_color", "#6B7280") or "#6B7280").lstrip("#")
    try:
        base = tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))
        if len(base) != 3:
            raise ValueError
    except (TypeError, ValueError):
        base = (107, 114, 128)
    red, green, blue = base
    return [
        base,
        (min(255, red + 38), min(255, green + 38), min(255, blue + 38)),
        (max(0, red - 38), max(0, green - 38), max(0, blue - 38)),
        (blue, red, green),
        (min(255, red + 70), max(0, green - 20), min(255, blue + 20)),
        (max(0, red - 20), min(255, green + 55), max(0, blue - 15)),
        (min(255, red + 20), min(255, green + 15), max(0, blue - 45)),
    ]


def _draw_wheel_frame(
    lobby: RouletteLobby,
    rotation: float,
    player_items: list[tuple[int, str]] | None = None,
) -> Any:
    """Render one high-resolution wheel frame with equal player slices.

    The pointer is fixed at the top while the wheel rotates underneath it,
    which makes the selection easy to follow in Discord's image preview.
    """
    if Image is None or ImageDraw is None:
        return None
    size = 720
    center = size // 2
    radius = 306
    image = Image.new("RGB", (size, size), (9, 9, 11))
    draw = ImageDraw.Draw(image)
    player_items = player_items if player_items is not None else list(lobby.players.items())
    count = max(1, len(player_items))
    slice_degrees = 360.0 / count
    palette = _wheel_palette(lobby)
    bounds = (center - radius, center - radius, center + radius, center + radius)

    for index, (_, name) in enumerate(player_items):
        start = rotation + index * slice_degrees
        end = start + slice_degrees
        # Give the slice under the pointer a subtle highlight.
        pointer_angle = (-90.0 - rotation) % 360.0
        local_angle = pointer_angle % 360.0
        pointed_index = min(count - 1, int(local_angle / slice_degrees))
        colour = palette[(index + (1 if index == pointed_index else 0)) % len(palette)]
        draw.pieslice(bounds, start=start, end=end, fill=colour, outline=(10, 10, 12), width=5)

        midpoint = math.radians(start + slice_degrees / 2.0)
        label_radius = radius * (0.63 if count <= 8 else 0.70)
        x = center + math.cos(midpoint) * label_radius
        y = center + math.sin(midpoint) * label_radius
        label = _short_wheel_name(name, 18 if count <= 8 else 11)
        font_size = 25 if count <= 6 else 19 if count <= 10 else 14
        font = _roulette_font(font_size, bold=True)
        # Rotate labels tangentially, matching the physical-wheel appearance.
        try:
            label_box = draw.textbbox((0, 0), label, font=font)
            label_image = Image.new(
                "RGBA",
                (max(2, label_box[2] - label_box[0] + 18), max(2, label_box[3] - label_box[1] + 12)),
                (0, 0, 0, 0),
            )
            label_draw = ImageDraw.Draw(label_image)
            label_draw.text((label_image.width // 2, label_image.height // 2), label, font=font, fill=(245, 245, 245), anchor="mm")
            angle = math.degrees(midpoint) + 90
            label_image = label_image.rotate(-angle, resample=getattr(getattr(Image, "Resampling", Image), "BICUBIC", 3), expand=True)
            image.paste(label_image, (int(x - label_image.width / 2), int(y - label_image.height / 2)), label_image)
        except (AttributeError, OSError, ValueError, TypeError):
            # A font/rendering issue should never prevent the game itself.
            draw.text((x, y), label, font=font, fill=(245, 245, 245), anchor="mm")

    draw.ellipse(bounds, outline=(3, 3, 4), width=9)
    inner_radius = 82
    draw.ellipse(
        (center - inner_radius, center - inner_radius, center + inner_radius, center + inner_radius),
        fill=(20, 20, 22),
        outline=(232, 232, 232),
        width=5,
    )
    center_font = _roulette_font(20, bold=True)
    draw.text((center, center), "ROULETTE", font=center_font, fill=(245, 245, 245), anchor="mm")
    # Fixed pointer: the slice beneath this triangle is the current choice.
    # The tip points down into the wheel (the previous triangle pointed up).
    pointer = [(center, 60), (center - 22, 18), (center + 22, 18)]
    draw.polygon(pointer, fill=(245, 245, 245), outline=(0, 0, 0))
    return image


def build_roulette_spin_gif(
    lobby: RouletteLobby,
    selected_id: int,
    player_items: list[tuple[int, str]] | None = None,
) -> tuple[bytes, int] | None:
    """Build a one-shot, easing animated GIF for Discord's native preview."""
    if Image is None or ImageDraw is None or ImageFont is None:
        return None
    player_items = player_items if player_items is not None else list(lobby.players.items())
    if len(player_items) < 2 or selected_id not in lobby.players:
        return None
    selected_index = next(index for index, (player_id, _) in enumerate(player_items) if player_id == selected_id)
    slice_degrees = 360.0 / len(player_items)
    start_rotation = random.uniform(0.0, 360.0)
    target_mod = (-90.0 - (selected_index + 0.5) * slice_degrees) % 360.0
    delta = (target_mod - start_rotation) % 360.0
    target_rotation = start_rotation + (360.0 * 5.0) + delta
    frame_count = 22
    frames: list[Any] = []
    durations: list[int] = []
    for index in range(frame_count):
        progress = index / (frame_count - 1)
        eased = 1.0 - (1.0 - progress) ** 3
        frames.append(
            _draw_wheel_frame(
                lobby,
                start_rotation + (target_rotation - start_rotation) * eased,
                player_items,
            )
        )
        durations.append(65 + int(progress * progress * 260))
    output = io.BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        disposal=2,
        optimize=False,
    )
    return output.getvalue(), sum(durations)


@dataclass
class RouletteLobby:
    guild: discord.Guild
    host_id: int
    channel: discord.TextChannel
    minimum_players: int
    maximum_players: int
    language: str = "en"
    wheel_mode: str = "multi"
    wheel_color: str = "#6B7280"
    wheel_colors: list[str] = field(default_factory=lambda: list(DEFAULT_WHEEL_COLORS))
    turn_timer_seconds: int = DEFAULT_TURN_TIMER_SECONDS
    players: dict[int, str] = field(default_factory=dict)
    started: bool = False
    finished: bool = False
    cancelled: bool = False
    winner_id: int | None = None
    winner_name: str | None = None
    message: discord.Message | None = field(default=None, repr=False)
    timeout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    banner_filename: str | None = None
    # The currently attached view.  Keeping this pointer lets us ignore an
    # older view's timeout after a lobby message has been refreshed.
    view: Any | None = field(default=None, repr=False)
    lobby_deadline: float | None = None
    spin_message: discord.Message | None = field(default=None, repr=False)
    spin_task: asyncio.Task[None] | None = field(default=None, repr=False)
    elimination_message: discord.Message | None = field(default=None, repr=False)
    elimination_view: Any | None = field(default=None, repr=False)
    active_picker_id: int | None = None
    turn_deadline: float | None = None
    turn_task: asyncio.Task[None] | None = field(default=None, repr=False)
    elimination_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def wheel_slices(self) -> int:
        return max(1, len(self.players))


def roulette_message_content(lobby: RouletteLobby) -> str:
    """Build the text for the image-first lobby message.

    Roulette intentionally posts the banner as a normal Discord attachment,
    rather than placing it inside an embed.  Discord then renders the image
    followed by this text and the component buttons in the usual message
    layout.
    """
    arabic = lobby.language == "ar"
    game_name = "روليت" if arabic else "Roulette"
    if lobby.cancelled:
        description = (
            "أُغلقت الردهة لأن الحد الأدنى من اللاعبين لم ينضم في الوقت المحدد."
            if arabic and len(lobby.players) < lobby.minimum_players
            else "انتهت الردهة قبل أن يبدأ المضيف الدوران." if arabic
            else "This lobby closed because the minimum number of players did not join in time."
            if len(lobby.players) < lobby.minimum_players
            else "This lobby expired before the host started the spin."
        )
        heading = f"{game_name} · انتهت الردهة" if arabic else "Roulette · Lobby expired"
    elif lobby.finished:
        heading = f"{game_name} · انتهت اللعبة" if arabic else "Roulette · Game complete"
        description = (
            f"اختارت العجلة **{lobby.winner_name or 'لاعباً غير معروف'}** ليكون الفائز."
            if arabic
            else f"The wheel selected **{lobby.winner_name or 'Unknown player'}** as the winner."
        )
    elif lobby.started:
        heading = f"{game_name} · اللعبة جارية" if arabic else "Roulette · Game in progress"
        description = "تدور العجلة وتستمر جولات الإقصاء…" if arabic else "The wheel is spinning and elimination rounds are continuing…"
    else:
        heading = f"{game_name} · الردهة" if arabic else "Roulette · Lobby"
        description = (
            f"انضم إلى الردهة، ويمكن للمضيف البدء عند جاهزية {lobby.minimum_players} لاعبين على الأقل.\n"
            "يحصل كل لاعب على قسم متساوٍ في العجلة."
            if arabic
            else f"Join the lobby, then the host can spin once at least {lobby.minimum_players} players are ready.\n"
            "Each joined player receives one equal wheel slice."
        )
    lines = [
        f"**{heading}**",
        description,
        (
            f"اللاعبون: **{len(lobby.players)} / {lobby.maximum_players}** · الحد الأدنى: **{lobby.minimum_players}**"
            if arabic
            else f"Players: **{len(lobby.players)} / {lobby.maximum_players}** · Minimum: **{lobby.minimum_players}**"
        ),
    ]
    if lobby.finished and lobby.winner_id:
        lines.append(
            f"الفائز: **{lobby.winner_name or 'لاعب غير معروف'}** <@{lobby.winner_id}>"
            if arabic
            else f"Winner: **{lobby.winner_name or 'Unknown player'}** <@{lobby.winner_id}>"
        )
    if not lobby.started and not lobby.finished and not lobby.cancelled and lobby.lobby_deadline is not None:
        remaining = max(0, int(lobby.lobby_deadline - asyncio.get_running_loop().time() + 0.999))
        clock = f"{remaining // 60:02d}:{remaining % 60:02d}"
        lines.append(f"المهلة المتبقية: **{clock}**" if arabic else f"Lobby timeout: **{clock}**")
    return "\n".join(lines)


def roulette_spin_content(lobby: RouletteLobby, player_id: int | None = None) -> str:
    name = lobby.players.get(player_id or 0, "—")
    entries = " · ".join(str(player_name)[:40] for player_name in lobby.players.values())
    if len(entries) > 900:
        entries = entries[:897] + "..."
    if lobby.language == "ar":
        return f"**روليت · دوران العجلة**\nتدور العجلة…\n\nالاختيار الحالي: **{name}**\nالأسماء: {entries}"
    return f"**Roulette · Wheel spinning**\nThe wheel is spinning…\n\nCurrent selection: **{name}**\nWheel entries: {entries}"


def roulette_elimination_prompt(lobby: RouletteLobby) -> str:
    picker = lobby.players.get(lobby.active_picker_id or 0, "لاعب غير معروف" if lobby.language == "ar" else "Unknown player")
    picker_mention = f"<@{lobby.active_picker_id}>" if lobby.active_picker_id else f"**{picker}**"
    if lobby.language == "ar":
        return f"{picker_mention}، اختر لاعبًا لطرده."
    return f"{picker_mention}, choose a player to eliminate."


def roulette_turn_content(lobby: RouletteLobby, remaining: int) -> str:
    """Render the active picker prompt with a live countdown."""
    seconds = max(0, int(remaining))
    clock = f"{seconds // 60:02d}:{seconds % 60:02d}"
    prompt = roulette_elimination_prompt(lobby)
    if lobby.language == "ar":
        return f"{prompt}\n\n\u0627\u0644\u0648\u0642\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a: **{clock}**"
    return f"{prompt}\n\nTime remaining: **{clock}**"


def roulette_elimination_notice(lobby: RouletteLobby, removed_name: str, *, timed_out: bool) -> str:
    """Show why a player was removed before the next wheel spin."""
    if lobby.language == "ar":
        reason = (
            f"\u0627\u0646\u062a\u0647\u0649 \u0627\u0644\u0648\u0642\u062a\u060c \u0641\u062a\u0645 \u0625\u0642\u0635\u0627\u0621 **{removed_name}**. \u0633\u062a\u062f\u0648\u0631 \u0627\u0644\u0639\u062c\u0644\u0629 \u0645\u0631\u0629 \u0623\u062e\u0631\u0649\u2026"
            if timed_out
            else f"\u062a\u0645 \u0625\u0642\u0635\u0627\u0621 **{removed_name}**. \u0633\u062a\u062f\u0648\u0631 \u0627\u0644\u0639\u062c\u0644\u0629 \u0645\u0631\u0629 \u0623\u062e\u0631\u0649\u2026"
        )
    else:
        reason = (
            f"{removed_name} was removed because the turn timer expired. Spinning again\u2026"
            if timed_out
            else f"**{removed_name}** was eliminated. Spinning again\u2026"
        )
    return f"{roulette_spin_content(lobby, None)}\n\n{reason}"


async def cancel_roulette_turn_timer(lobby: RouletteLobby) -> None:
    task = lobby.turn_task
    lobby.turn_task = None
    lobby.turn_deadline = None
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def advance_roulette_round(
    lobby: RouletteLobby,
    removed_name: str,
    *,
    interaction: discord.Interaction | None = None,
    timed_out: bool = False,
) -> bool:
    """Finish one elimination and schedule the next wheel spin."""
    await cancel_roulette_turn_timer(lobby)
    if len(lobby.players) <= 1:
        winner_id = next(iter(lobby.players), None)
        await finish_roulette_game(lobby, winner_id)
        if interaction:
            try:
                await interaction.edit_original_response(content=roulette_winner_content(lobby), view=None)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        return True

    current_view = lobby.elimination_view
    if current_view is not None and hasattr(current_view, "stop"):
        current_view.stop()
    lobby.elimination_view = None
    content = roulette_elimination_notice(lobby, removed_name, timed_out=timed_out)
    try:
        if interaction:
            message = await interaction.edit_original_response(content=content, view=None)
        elif lobby.elimination_message:
            message = await lobby.elimination_message.edit(content=content, view=None)
        else:
            message = None
        if message is not None:
            lobby.elimination_message = message
    except (discord.HTTPException, discord.Forbidden, discord.NotFound) as error:
        print(f"Roulette next elimination update failed: {error}")
        await fail_roulette_game(
            lobby,
            "\u062a\u0639\u0630\u0631 \u062a\u062d\u062f\u064a\u062b \u062f\u0648\u0631 \u0627\u0644\u0625\u0642\u0635\u0627\u0621." if lobby.language == "ar" else "The next elimination turn could not be updated.",
        )
        return False

    lobby.active_picker_id = None
    if not lobby.finished:
        lobby.spin_task = asyncio.create_task(run_roulette_spin(lobby))
    return True


async def run_roulette_turn_timer(lobby: RouletteLobby, picker_id: int) -> None:
    """Update the turn countdown and remove an inactive picker at zero."""
    try:
        while (
            not lobby.finished
            and lobby.started
            and lobby.turn_deadline is not None
            and lobby.active_picker_id == picker_id
        ):
            remaining = max(0, math.ceil(lobby.turn_deadline - asyncio.get_running_loop().time()))
            if lobby.elimination_message and lobby.elimination_view is not None:
                try:
                    await lobby.elimination_message.edit(
                        content=roulette_turn_content(lobby, remaining),
                        view=lobby.elimination_view,
                    )
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    # A deleted channel/message should not crash the timer;
                    # the next state transition will report a useful failure.
                    pass
            if remaining <= 0:
                break
            await asyncio.sleep(1)

        if lobby.finished or not lobby.started or lobby.active_picker_id != picker_id:
            return
        async with lobby.elimination_lock:
            if lobby.finished or lobby.active_picker_id != picker_id:
                return
            removed_name = lobby.players.pop(picker_id, None)
            if removed_name is None:
                return
            await advance_roulette_round(lobby, removed_name, timed_out=True)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"Roulette turn timer failed ({type(error).__name__}): {error}")
        await fail_roulette_game(
            lobby,
            "\u062d\u062f\u062b \u062e\u0637\u0623 \u0623\u062b\u0646\u0627\u0621 \u0627\u0646\u062a\u0647\u0627\u0621 \u0645\u0647\u0644\u0629 \u0627\u0644\u062f\u0648\u0631." if lobby.language == "ar" else "The turn timer encountered an error. Please start Roulette again.",
        )
    finally:
        if lobby.turn_task is asyncio.current_task():
            lobby.turn_task = None
            lobby.turn_deadline = None


def start_roulette_turn_timer(lobby: RouletteLobby) -> None:
    if lobby.finished or not lobby.started or lobby.active_picker_id is None:
        return
    existing = lobby.turn_task
    if existing and not existing.done():
        existing.cancel()
    lobby.turn_deadline = asyncio.get_running_loop().time() + max(1, int(lobby.turn_timer_seconds))
    lobby.turn_task = asyncio.create_task(run_roulette_turn_timer(lobby, lobby.active_picker_id))


def roulette_winner_content(lobby: RouletteLobby) -> str:
    winner = lobby.winner_name or ("لاعب غير معروف" if lobby.language == "ar" else "Unknown player")
    # Keep the Discord mention, but don't wrap it in visible parentheses.
    mention = f"<@{lobby.winner_id}>" if lobby.winner_id else winner
    if lobby.language == "ar":
        return f"**روليت · الفائز**\nلم يتبقَّ سوى لاعب واحد: {mention}.\nمبروك! 🥳🎉"
    return f"**Roulette · Winner**\nOnly one player remains: {mention}.\nCongratulations! 🥳🎉"


ACTIVE_ROULETTE_LOBBIES: dict[int, RouletteLobby] = {}
# ``discord.py`` keeps component views in memory.  A message can therefore
# outlive its view (for example after a hot reload or a failed message edit).
# Keep a very small in-flight marker so the fallback interaction router below
# never runs a callback twice while the normal ViewStore callback is already
# handling it.
_ROULETTE_INTERACTIONS_IN_FLIGHT: set[int] = set()


def _mark_roulette_interaction(interaction: discord.Interaction) -> None:
    interaction_id = int(getattr(interaction, "id", id(interaction)))
    _ROULETTE_INTERACTIONS_IN_FLIGHT.add(interaction_id)
    # Interaction IDs are unique and this prevents the set growing forever in
    # a busy server.  The delay is longer than the fallback router's grace
    # period, so a normal callback remains protected from duplicate routing.
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(10, _ROULETTE_INTERACTIONS_IN_FLIGHT.discard, interaction_id)
    except RuntimeError:
        _ROULETTE_INTERACTIONS_IN_FLIGHT.discard(interaction_id)


def register_roulette_lobby(lobby: RouletteLobby) -> None:
    ACTIVE_ROULETTE_LOBBIES[id(lobby)] = lobby


def unregister_roulette_lobby(lobby: RouletteLobby) -> None:
    ACTIVE_ROULETTE_LOBBIES.pop(id(lobby), None)


async def refresh_roulette_lobby_message(lobby: RouletteLobby, *, remove_view: bool = False) -> None:
    """Refresh a lobby from a gateway event (for example host disconnect)."""
    if lobby.message is None:
        return
    previous_view = getattr(lobby, "view", None)
    view = None if remove_view else RouletteView(lobby)
    if remove_view:
        lobby.view = None
    try:
        message = await lobby.message.edit(content=roulette_message_content(lobby), view=view)
        lobby.message = message or lobby.message
    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
        if not remove_view and getattr(lobby, "view", None) is view:
            lobby.view = previous_view
            view.stop()  # type: ignore[union-attr]


class RouletteView(discord.ui.View):
    def __init__(self, lobby: RouletteLobby) -> None:
        # The explicit lobby timer below owns expiry; keeping the component
        # view alive avoids a second, unsynchronised Discord timeout.
        super().__init__(timeout=None)
        self.lobby = lobby
        self._deferred_ephemeral: dict[int, bool] = {}
        lobby.view = self
        arabic = lobby.language == "ar"
        labels = ("دخول", "خروج", "اللاعبون", "بدء اللعبة") if arabic else ("Join", "Leave", "Show Players", "Start")
        self.join_button = discord.ui.Button(
            label=labels[0], style=discord.ButtonStyle.secondary,
            emoji=configured_roulette_emoji("ROULETTE_JOIN_EMOJI", "SPY_JOIN_EMOJI"),
            custom_id=f"birdbot:roulette:join:{lobby.host_id}:{lobby.channel.id}"[:100],
            disabled=lobby.started or lobby.finished,
        )
        self.leave_button = discord.ui.Button(
            label=labels[1], style=discord.ButtonStyle.secondary,
            emoji=configured_roulette_emoji("ROULETTE_LEAVE_EMOJI", "SPY_LEAVE_EMOJI"),
            custom_id=f"birdbot:roulette:leave:{lobby.host_id}:{lobby.channel.id}"[:100],
            disabled=lobby.started or lobby.finished,
        )
        self.players_button = discord.ui.Button(
            label=labels[2], style=discord.ButtonStyle.secondary,
            emoji=configured_roulette_emoji("ROULETTE_PLAYERS_EMOJI", "SPY_PLAYERS_EMOJI"),
            custom_id=f"birdbot:roulette:players:{lobby.host_id}:{lobby.channel.id}"[:100],
            disabled=lobby.finished,
        )
        self.start_button = discord.ui.Button(
            label=labels[3], style=discord.ButtonStyle.secondary,
            emoji=configured_roulette_emoji("ROULETTE_START_EMOJI", "SPY_JOIN_EMOJI"),
            custom_id=f"birdbot:roulette:start:{lobby.host_id}:{lobby.channel.id}"[:100],
            disabled=lobby.started or lobby.finished or len(lobby.players) < lobby.minimum_players,
        )
        self.join_button.callback = self.join
        self.leave_button.callback = self.leave
        self.players_button.callback = self.show_players
        self.start_button.callback = self.start
        self.add_item(self.join_button)
        self.add_item(self.leave_button)
        self.add_item(self.players_button)
        self.add_item(self.start_button)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: object) -> None:
        print(f"Roulette interaction failed ({type(error).__name__}): {error}")
        try:
            message = (
                "تعذر إكمال هذا الإجراء في الروليت. حاول مرة أخرى."
                if self.lobby.language == "ar"
                else "Roulette could not complete that action. Please try again."
            )
            if self._deferred_ephemeral.pop(id(interaction), False):
                await interaction.edit_original_response(content=message)
            elif interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass

    async def _defer(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=ephemeral)
        self._deferred_ephemeral[id(interaction)] = ephemeral

    async def _private_response(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if self._deferred_ephemeral.pop(id(interaction), False):
                await interaction.edit_original_response(content=message)
            elif interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass

    async def _check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.lobby.guild.id:
            await self._private_response(interaction, "This Roulette lobby is no longer available here.")
            return False
        if self.lobby.cancelled:
            await self._private_response(interaction, "This Roulette lobby has expired.")
            return False
        return True

    async def _refresh_lobby(self, interaction: discord.Interaction) -> None:
        view = RouletteView(self.lobby)
        try:
            message = await interaction.edit_original_response(content=roulette_message_content(self.lobby), view=view)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            if getattr(self.lobby, "view", None) is view:
                self.lobby.view = self
                view.stop()
            raise
        view.lobby.message = message or interaction.message
        self.stop()

    def start_lobby_timeout(self, *, reset: bool = False, duration: int = LOBBY_FILL_TIMEOUT_SECONDS) -> None:
        lobby = self.lobby
        if lobby.started or lobby.finished:
            return
        existing = lobby.timeout_task
        if existing and not existing.done():
            if not reset:
                return
            existing.cancel()
        lobby.lobby_deadline = asyncio.get_running_loop().time() + max(1, int(duration))
        lobby.timeout_task = asyncio.create_task(self._run_lobby_timeout())

    async def cancel_lobby_timeout(self) -> None:
        task = self.lobby.timeout_task
        self.lobby.timeout_task = None
        self.lobby.lobby_deadline = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _run_lobby_timeout(self) -> None:
        lobby = self.lobby
        try:
            while not lobby.started and not lobby.finished and lobby.lobby_deadline is not None:
                remaining = max(0, int(lobby.lobby_deadline - asyncio.get_running_loop().time() + 0.999))
                if lobby.message:
                    try:
                        await lobby.message.edit(content=roulette_message_content(lobby), view=lobby.view)
                    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                        pass
                if remaining <= 0:
                    break
                await asyncio.sleep(1)
            if lobby.started or lobby.finished:
                return
            lobby.cancelled = True
            lobby.finished = True
            lobby.lobby_deadline = None
            unregister_roulette_lobby(lobby)
            if lobby.message:
                try:
                    await lobby.message.edit(content=roulette_message_content(lobby), view=None)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    pass
            if lobby.view:
                lobby.view.stop()
            lobby.view = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"Roulette lobby timeout failed: {error}")
        finally:
            if lobby.timeout_task is asyncio.current_task():
                lobby.timeout_task = None

    async def join(self, interaction: discord.Interaction) -> None:
        _mark_roulette_interaction(interaction)
        await self._defer(interaction)
        if not await self._check(interaction):
            return
        if self.lobby.started or self.lobby.finished:
            await self._private_response(interaction, "Error: Game has already started. You cannot join or leave mid-game.")
            return
        if interaction.user.id in self.lobby.players:
            await self._private_response(interaction, "Error: You are already in the lobby!")
            return
        if len(self.lobby.players) >= self.lobby.maximum_players:
            await self._private_response(interaction, "Error: This game lobby is currently full!")
            return
        member = await resolve_guild_member(self.lobby.guild, interaction.user.id)
        if member is None:
            await self._private_response(interaction, "Your server membership is still syncing. Try again in a moment.")
            return
        if member.id in self.lobby.players:
            await self._private_response(interaction, "Error: You are already in the lobby!")
            return
        if len(self.lobby.players) >= self.lobby.maximum_players:
            await self._private_response(interaction, "Error: This game lobby is currently full!")
            return
        was_ready = len(self.lobby.players) >= self.lobby.minimum_players
        self.lobby.players[member.id] = member.display_name
        if len(self.lobby.players) >= self.lobby.minimum_players and not was_ready:
            # Once the minimum is reached, give the host a complete one-minute
            # window to press Start.  The earlier fill countdown is replaced.
            self.start_lobby_timeout(reset=True, duration=LOBBY_READY_TIMEOUT_SECONDS)
        await self._refresh_lobby(interaction)

    async def leave(self, interaction: discord.Interaction) -> None:
        _mark_roulette_interaction(interaction)
        await self._defer(interaction)
        if not await self._check(interaction):
            return
        if self.lobby.started or self.lobby.finished:
            await self._private_response(interaction, "Error: Game has already started. You cannot join or leave mid-game.")
            return
        if interaction.user.id not in self.lobby.players:
            await self._private_response(interaction, "Error: You are not currently in this game lobby.")
            return
        self.lobby.players.pop(interaction.user.id, None)
        if interaction.user.id == self.lobby.host_id:
            if self.lobby.players:
                self.lobby.host_id = next(iter(self.lobby.players))
            else:
                self.lobby.cancelled = True
                self.lobby.finished = True
                await self.cancel_lobby_timeout()
                unregister_roulette_lobby(self.lobby)
                await interaction.edit_original_response(content=roulette_message_content(self.lobby), view=None)
                self.lobby.view = None
                self.stop()
                return
        if len(self.lobby.players) < self.lobby.minimum_players:
            self.start_lobby_timeout(reset=True, duration=LOBBY_FILL_TIMEOUT_SECONDS)
        await self._refresh_lobby(interaction)

    async def show_players(self, interaction: discord.Interaction) -> None:
        _mark_roulette_interaction(interaction)
        await self._defer(interaction, ephemeral=True)
        if not await self._check(interaction):
            return
        if interaction.user.id not in self.lobby.players:
            await self._private_response(interaction, "Error: You are not currently in this game lobby.")
            return
        players = [f"{index}. {name}" for index, name in enumerate(self.lobby.players.values(), start=1)]
        await interaction.edit_original_response(content="\n".join(players) or "No players have joined yet.")

    async def start(self, interaction: discord.Interaction) -> None:
        _mark_roulette_interaction(interaction)
        try:
            # Acknowledge before any state checks or network requests.  This
            # keeps the component inside Discord's three-second response
            # window even when the message edit has to retry.
            await self._defer(interaction)
            if not await self._check(interaction):
                return
            if not bool(store.roulette_game_config(str(self.lobby.guild.id)).get("enabled", True)):
                await self._private_response(interaction, "Roulette is disabled for this server. Enable it in the Games settings first.")
                return
            if interaction.user.id != self.lobby.host_id:
                await self._private_response(interaction, "Only the lobby host can start Roulette.")
                return
            if self.lobby.started or self.lobby.finished:
                await self._private_response(interaction, "This Roulette game has already finished.")
                return
            if len(self.lobby.players) < self.lobby.minimum_players:
                await self._private_response(interaction, f"At least {self.lobby.minimum_players} players are required to start.")
                return
            await self.cancel_lobby_timeout()
            self.lobby.started = True
            try:
                await self._refresh_lobby(interaction)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound) as error:
                # Restore a usable lobby if Discord rejects the state update.
                self.lobby.started = False
                self.start_lobby_timeout(
                    reset=True,
                    duration=LOBBY_READY_TIMEOUT_SECONDS if len(self.lobby.players) >= self.lobby.minimum_players else LOBBY_FILL_TIMEOUT_SECONDS,
                )
                print(f"Roulette start update failed: {error}")
                await self._private_response(interaction, "Roulette could not start right now. Please try again.")
                return
            self.lobby.spin_task = asyncio.create_task(run_roulette_spin(self.lobby))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # View callbacks are normally funnelled through ``on_error``.  A
            # final guard here is useful for stale messages and custom Discord
            # client implementations where that hook is bypassed.
            print(f"Roulette start failed ({type(error).__name__}): {error}")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "Roulette could not start right now. Please try again.", ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "Roulette could not start right now. Please try again.", ephemeral=True
                    )
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass

    async def on_timeout(self) -> None:
        # Kept as a defensive hook for Discord versions that apply a default
        # timeout despite timeout=None; the explicit lobby task is authoritative.
        if getattr(self.lobby, "view", None) is self and not self.lobby.started and not self.lobby.finished:
            await self._run_lobby_timeout()


async def finish_roulette_game(lobby: RouletteLobby, winner_id: int | None = None) -> None:
    """Close a game safely and remove every interactive surface."""
    if lobby.finished:
        return
    lobby.finished = True
    await cancel_roulette_turn_timer(lobby)
    if winner_id is not None:
        lobby.winner_id = winner_id
        lobby.winner_name = lobby.players.get(winner_id)
    unregister_roulette_lobby(lobby)
    spin_task = lobby.spin_task
    lobby.spin_task = None
    if spin_task and not spin_task.done() and spin_task is not asyncio.current_task():
        spin_task.cancel()
    if lobby.view:
        lobby.view.stop()
    lobby.view = None
    if lobby.elimination_view:
        lobby.elimination_view.stop()
    lobby.elimination_view = None
    if lobby.elimination_message:
        try:
            await lobby.elimination_message.edit(content=roulette_winner_content(lobby), view=None)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass
    if lobby.message:
        try:
            await lobby.message.edit(content=roulette_winner_content(lobby), view=None)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass


async def fail_roulette_game(lobby: RouletteLobby, reason: str) -> None:
    """Fail closed when Discord cannot publish a spin or elimination turn."""
    if lobby.finished:
        return
    lobby.cancelled = True
    lobby.finished = True
    await cancel_roulette_turn_timer(lobby)
    unregister_roulette_lobby(lobby)
    if lobby.view:
        lobby.view.stop()
    lobby.view = None
    if lobby.elimination_view:
        lobby.elimination_view.stop()
    lobby.elimination_view = None
    if lobby.message:
        try:
            await lobby.message.edit(content=f"**{('روليت' if lobby.language == 'ar' else 'Roulette')}**\n{reason}", view=None)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass


async def run_roulette_spin(lobby: RouletteLobby) -> None:
    """Animate a slowing wheel, then publish the next elimination turn."""
    try:
        player_ids = list(lobby.players)
        if len(player_ids) < 2:
            await finish_roulette_game(lobby, player_ids[0] if player_ids else None)
            return
        selected_id = random.choice(player_ids)
        # Discord can play an animated GIF natively, so one upload gives every
        # participant the same real wheel animation without racing the message
        # edit rate limit.  The generated frames use the joined usernames as
        # equal slices and ease into the selected player.
        try:
            # Image generation is CPU work; keep Discord's gateway/event loop
            # free so other button clicks can still be acknowledged promptly.
            wheel_players = list(lobby.players.items())
            animated_wheel = await asyncio.to_thread(
                build_roulette_spin_gif,
                lobby,
                selected_id,
                wheel_players,
            )
        except Exception as error:
            # Rendering is an enhancement; a font/encoder problem must not
            # cancel a valid game.
            print(f"Roulette wheel rendering failed; using text fallback: {error}")
            animated_wheel = None
        if animated_wheel is not None:
            wheel_bytes, duration_ms = animated_wheel
            try:
                lobby.spin_message = await lobby.channel.send(
                    content=(
                        "**\u0631\u0648\u0644\u064a\u062a · \u062f\u0648\u0631\u0627\u0646 \u0627\u0644\u0639\u062c\u0644\u0629**\n\u062a\u062f\u0648\u0631 \u0627\u0644\u0639\u062c\u0644\u0629…"
                        if lobby.language == "ar"
                        else "**Roulette · Wheel spinning**\nThe wheel is spinning…"
                    ),
                    file=discord.File(io.BytesIO(wheel_bytes), filename="roulette-wheel.gif"),
                )
                await asyncio.sleep(duration_ms / 1000.0 + 0.25)
            except Exception as error:
                # A file upload can fail because of a transient Discord limit;
                # fall back to the lightweight text animation below.
                print(f"Roulette wheel upload failed; using text fallback: {error}")
                animated_wheel = None

        if animated_wheel is None:
            sequence = [random.choice(player_ids) for _ in range(max(1, SPIN_STEPS - 1))]
            sequence.append(selected_id)
            for index, current_id in enumerate(sequence):
                if lobby.finished:
                    return
                content = roulette_spin_content(lobby, current_id)
                try:
                    if lobby.spin_message is None:
                        lobby.spin_message = await lobby.channel.send(content=content)
                    else:
                        await lobby.spin_message.edit(content=content)
                except (discord.HTTPException, discord.Forbidden, discord.NotFound) as error:
                    print(f"Roulette spin update failed: {error}")
                progress = index / max(1, len(sequence) - 1)
                await asyncio.sleep(0.10 + (progress * progress * 0.70))
        if selected_id not in lobby.players:
            remaining = list(lobby.players)
            selected_id = random.choice(remaining) if remaining else None
        if selected_id is None:
            await finish_roulette_game(lobby)
            return
        lobby.active_picker_id = selected_id
        try:
            view = EliminationView(lobby)
            lobby.elimination_message = await lobby.channel.send(
                content=roulette_turn_content(lobby, lobby.turn_timer_seconds),
                view=view,
            )
            start_roulette_turn_timer(lobby)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound) as error:
            print(f"Roulette elimination prompt failed: {error}")
            await fail_roulette_game(
                lobby,
                "تعذر إرسال دور الإقصاء. حاول بدء اللعبة مرة أخرى." if lobby.language == "ar" else "The elimination turn could not be sent. Please start the game again.",
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"Roulette spin failed ({type(error).__name__}): {error}")
        await fail_roulette_game(
            lobby,
            "حدث خطأ أثناء دوران العجلة. حاول مرة أخرى." if lobby.language == "ar" else "The wheel encountered an error. Please try again.",
        )
    finally:
        if lobby.spin_task is asyncio.current_task():
            lobby.spin_task = None


class EliminationView(discord.ui.View):
    """Buttons shown in the channel to the player selected by the wheel."""

    def __init__(self, lobby: RouletteLobby) -> None:
        super().__init__(timeout=900)
        self.lobby = lobby
        lobby.elimination_view = self
        self._deferred_ephemeral: dict[int, bool] = {}
        targets = [(player_id, name) for player_id, name in lobby.players.items() if player_id != lobby.active_picker_id]
        # Discord permits at most 25 buttons. Normal lobbies fit well within
        # this limit; cap a malformed larger configuration instead of causing
        # Discord to reject the entire message.
        for index, (target_id, name) in enumerate(targets[:25]):
            button = discord.ui.Button(
                label=str(name)[:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"birdbot:roulette:eliminate:{lobby.host_id}:{target_id}"[:100],
                row=index // 5,
            )

            async def callback(interaction: discord.Interaction, selected_id: int = target_id) -> None:
                await self.eliminate(interaction, selected_id)

            button.callback = callback
            self.add_item(button)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: object) -> None:
        print(f"Roulette elimination interaction failed ({type(error).__name__}): {error}")
        try:
            message = (
                "تعذر إكمال هذا الإجراء في الروليت. حاول مرة أخرى."
                if self.lobby.language == "ar"
                else "Roulette could not complete that action. Please try again."
            )
            if self._deferred_ephemeral.pop(id(interaction), False):
                await interaction.edit_original_response(content=message)
            elif interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass

    async def _defer(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=ephemeral)
        self._deferred_ephemeral[id(interaction)] = ephemeral

    async def _private_response(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if self._deferred_ephemeral.pop(id(interaction), False):
                await interaction.edit_original_response(content=message)
            elif interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass

    async def eliminate(self, interaction: discord.Interaction, target_id: int) -> None:
        _mark_roulette_interaction(interaction)
        await self._defer(interaction)
        lobby = self.lobby
        if not interaction.guild or interaction.guild.id != lobby.guild.id:
            await self._private_response(interaction, "This Roulette game is no longer available here.")
            return
        if lobby.cancelled or lobby.finished or not lobby.started:
            await self._private_response(interaction, "This Roulette game is no longer active.")
            return
        if interaction.user.id != lobby.active_picker_id:
            await self._private_response(
                interaction,
                "هذه الجولة مخصصة للاعب الذي اختارته الروليت." if lobby.language == "ar" else "Only the player selected by the roulette wheel can choose an elimination.",
            )
            return
        async with lobby.elimination_lock:
            if target_id not in lobby.players or target_id == lobby.active_picker_id:
                await self._private_response(
                    interaction,
                    "هذا اللاعب غير متاح للإقصاء." if lobby.language == "ar" else "That player is no longer available for elimination.",
                )
                return
            # Cancel the countdown before mutating the player set.  The lock
            # makes a button click and timer expiry mutually exclusive.
            await cancel_roulette_turn_timer(lobby)
            removed_name = lobby.players.pop(target_id)
            await advance_roulette_round(lobby, removed_name, interaction=interaction)
            self.stop()
            return


def roulette_language_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Choose Roulette language / اختر لغة الروليت",
        description="Choose the language for this Roulette game.\nاختر لغة لعبة الروليت.",
        colour=discord.Colour.from_rgb(0, 0, 0),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


class RouletteLanguageView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self._deferred: dict[int, bool] = {}
        for label, language, flag in (("EN", "en", "🇺🇸"), ("AR", "ar", "🇦🇪")):
            button = discord.ui.Button(
                label=label,
                emoji=flag,
                style=discord.ButtonStyle.secondary,
                custom_id=f"birdbot:roulette:language:{language}:{owner_id}"[:100],
            )

            async def callback(interaction: discord.Interaction, selected: str = language) -> None:
                await self.select_language(interaction, selected)

            button.callback = callback
            self.add_item(button)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: object) -> None:
        print(f"Roulette language interaction failed ({type(error).__name__}): {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Roulette could not complete that action. Please try again.", ephemeral=True)
            else:
                await interaction.response.send_message("Roulette could not complete that action. Please try again.", ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass

    async def _defer(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        self._deferred[id(interaction)] = False

    async def _error(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass

    async def select_language(self, interaction: discord.Interaction, language: str) -> None:
        _mark_roulette_interaction(interaction)
        await self._defer(interaction)
        if interaction.user.id != self.owner_id:
            await self._error(interaction, "Only the person who opened the lobby can choose its language.")
            return
        if not interaction.guild:
            await self._error(interaction, "Roulette can only be started inside a server.")
            return
        roulette = interaction.client.get_cog("Roulette")
        if roulette is None or not hasattr(roulette, "start_interaction_lobby"):
            await self._error(interaction, "Roulette is still loading. Please try again in a moment.")
            return
        try:
            # Kept only for old messages. New lobbies always use the language
            # saved in the web dashboard, so a stale Discord language button
            # cannot override the server setting.
            created = await roulette.start_interaction_lobby(interaction, deferred=True)
            if created is not False:
                self.stop()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound) as error:
            print(f"Roulette language transition failed: {error}")
            await self._error(interaction, "Roulette could not create the lobby. Please try again.")


class Roulette(commands.Cog):
    """Expose website-created Roulette lobbies and the /start game chooser."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def start_language_selection(self, interaction: discord.Interaction) -> None:
        """Compatibility entry point; language is now configured on the site."""
        await self.start_interaction_lobby(interaction, deferred=True)

    @staticmethod
    def _lobby_for_component(interaction: discord.Interaction, channel_id: int | None) -> RouletteLobby | None:
        """Find the live lobby represented by a component custom ID.

        Component views are process-local.  During an edit/reload Discord can
        still deliver a click for a message whose ViewStore entry is missing;
        resolving by guild/channel lets the fallback router recover it.
        """
        guild = interaction.guild
        if guild is None:
            return None
        candidates = [
            lobby
            for lobby in ACTIVE_ROULETTE_LOBBIES.values()
            if lobby.guild.id == guild.id
            and not lobby.finished
            and not lobby.cancelled
            and (channel_id is None or lobby.channel.id == channel_id)
        ]
        return candidates[-1] if candidates else None

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Recover Roulette clicks when Discord's in-memory ViewStore is stale.

        Normally ``discord.py`` dispatches directly to the View callback.  A
        message created before a reload, however, has no registered view and
        would otherwise show Discord's generic *did not respond* toast.  We
        wait a short grace period for the normal callback, then acknowledge and
        route only still-unhandled Roulette components ourselves.
        """
        if interaction.type is not discord.InteractionType.component:
            return
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        if not isinstance(custom_id, str) or not custom_id.startswith("birdbot:roulette:"):
            return

        # ViewStore callbacks are scheduled before this listener.  Give their
        # immediate defer() call a chance to acknowledge the interaction.
        await asyncio.sleep(0.35)
        interaction_id = int(getattr(interaction, "id", id(interaction)))
        if interaction_id in _ROULETTE_INTERACTIONS_IN_FLIGHT or interaction.response.is_done():
            return

        parts = custom_id.split(":")
        action = parts[2] if len(parts) > 2 else ""
        channel_id: int | None = None
        try:
            # Lobby buttons end with ``host_id:channel_id``.  Language buttons
            # intentionally have no channel and are handled by their own view.
            if len(parts) >= 5:
                channel_id = int(parts[-1])
        except (TypeError, ValueError):
            channel_id = None

        try:
            lobby = self._lobby_for_component(interaction, channel_id)
            if action in {"join", "leave", "players", "start"} and lobby is not None:
                view = lobby.view if isinstance(lobby.view, RouletteView) else RouletteView(lobby)
                callback = {
                    "join": view.join,
                    "leave": view.leave,
                    "players": view.show_players,
                    "start": view.start,
                }[action]
                await callback(interaction)
                return

            # Even if the process was restarted (and the old lobby state is no
            # longer recoverable), acknowledge the click with a useful message
            # instead of leaving Discord to display a timeout toast.
            await interaction.response.defer(ephemeral=True, thinking=True)
            message = (
                "This Roulette lobby is no longer active. Please run /start to create a new one."
                if lobby is None
                else "This Roulette action is no longer available."
            )
            await interaction.edit_original_response(content=message)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            # The interaction token may have expired while recovering a very
            # old message.  There is no safe follow-up to send in that case.
            return
        except Exception as error:
            print(f"Roulette fallback interaction failed ({type(error).__name__}): {error}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Roulette could not process that action. Please run /start again.",
                        ephemeral=True,
                    )
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Keep pre-game lobbies valid when a participant leaves the guild."""
        for lobby in list(ACTIVE_ROULETTE_LOBBIES.values()):
            if lobby.guild.id != member.guild.id or lobby.finished or member.id not in lobby.players:
                continue
            was_host = member.id == lobby.host_id
            lobby.players.pop(member.id, None)
            if lobby.started:
                # A participant leaving during a match is removed from future
                # elimination choices. If they were the active picker, choose
                # a replacement immediately; if one player remains, finish.
                if len(lobby.players) <= 1:
                    await finish_roulette_game(lobby, next(iter(lobby.players), None))
                    continue
                if member.id == lobby.active_picker_id:
                    await cancel_roulette_turn_timer(lobby)
                    lobby.active_picker_id = random.choice(list(lobby.players))
                    if lobby.elimination_message:
                        try:
                            view = EliminationView(lobby)
                            await lobby.elimination_message.edit(
                                content=roulette_turn_content(lobby, lobby.turn_timer_seconds),
                                view=view,
                            )
                            start_roulette_turn_timer(lobby)
                        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                            pass
                continue
            if not lobby.players:
                lobby.cancelled = True
                lobby.finished = True
                if isinstance(lobby.view, RouletteView):
                    await lobby.view.cancel_lobby_timeout()
                unregister_roulette_lobby(lobby)
                await refresh_roulette_lobby_message(lobby, remove_view=True)
                continue
            if was_host:
                lobby.host_id = next(iter(lobby.players))
            if len(lobby.players) < lobby.minimum_players:
                RouletteView(lobby).start_lobby_timeout(reset=True, duration=LOBBY_FILL_TIMEOUT_SECONDS)
            await refresh_roulette_lobby_message(lobby)

    async def start_interaction_lobby(
        self,
        interaction: discord.Interaction,
        *,
        language: str | None = None,
        deferred: bool = False,
    ) -> bool:
        async def private_error(message: str) -> None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass

        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await private_error("Roulette can only be started in a text channel.")
            return False
        config = store.roulette_game_config(str(interaction.guild.id))
        if not bool(config.get("enabled", True)):
            await private_error("Roulette is disabled for this server. An administrator can enable it in the Games settings.")
            return False
        saved_language = str(config.get("language") or "en")
        # The parameter remains for compatibility with old component messages,
        # but the website is the sole source of truth for new and recovered
        # lobbies.
        selected_language = saved_language
        local_banner = banner_path()
        lobby = RouletteLobby(
            guild=interaction.guild,
            host_id=interaction.user.id,
            channel=interaction.channel,
            minimum_players=config["min_players"],
            maximum_players=config["max_players"],
            language=selected_language if selected_language in {"en", "ar"} else "en",
            wheel_mode=str(config.get("wheel_mode") or "multi"),
            wheel_color=str(config.get("wheel_color") or "#6B7280"),
            wheel_colors=list(config.get("wheel_colors") or DEFAULT_WHEEL_COLORS),
            turn_timer_seconds=int(config.get("turn_timer_seconds") or DEFAULT_TURN_TIMER_SECONDS),
            banner_filename=local_banner.name if local_banner else None,
        )
        host = await resolve_guild_member(interaction.guild, interaction.user.id)
        if host is None:
            await private_error("Your server membership is still syncing. Try again in a moment.")
            return False
        lobby.players[host.id] = host.display_name
        lobby.lobby_deadline = asyncio.get_running_loop().time() + LOBBY_FILL_TIMEOUT_SECONDS
        register_roulette_lobby(lobby)
        view = RouletteView(lobby)
        file_path = local_banner
        # Explicitly clear the chooser embed. Webhook edits preserve omitted
        # fields, so without ``embed=None`` Discord renders the old
        # "Choose Your Game" card above the new Roulette lobby.
        kwargs: dict[str, object] = {"content": roulette_message_content(lobby), "embed": None, "view": view}
        if file_path:
            kwargs["attachments"] = [discord.File(str(file_path), filename=file_path.name)]
        try:
            if deferred:
                lobby.message = await interaction.edit_original_response(**kwargs)  # type: ignore[arg-type]
            else:
                await interaction.response.edit_message(**kwargs)  # type: ignore[arg-type]
                lobby.message = interaction.message
            view.start_lobby_timeout()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            unregister_roulette_lobby(lobby)
            raise
        return True

    async def run_dashboard_lobby(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        requested_by: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        me = guild.me
        permissions = channel.permissions_for(me) if me is not None else None
        if permissions is None or not permissions.view_channel or not permissions.send_messages:
            raise ValueError("BirdBot cannot send messages in that channel.")
        host = await resolve_guild_member(guild, requested_by)
        if host is None:
            raise ValueError("Your server membership is still syncing. Wait a moment, then create the lobby again.")
        config = store.roulette_game_config(str(guild.id))
        if not bool(config.get("enabled", True)):
            raise ValueError("Roulette is disabled for this server. Enable it in the Games settings first.")
        language = str(config.get("language") or "en") if config.get("language") in {"en", "ar"} else "en"
        local_banner = banner_path()
        lobby = RouletteLobby(
            guild=guild,
            host_id=host.id,
            channel=channel,
            minimum_players=config["min_players"],
            maximum_players=config["max_players"],
            language=language,
            wheel_mode=str(config.get("wheel_mode") or "multi"),
            wheel_color=str(config.get("wheel_color") or "#6B7280"),
            wheel_colors=list(config.get("wheel_colors") or DEFAULT_WHEEL_COLORS),
            turn_timer_seconds=int(config.get("turn_timer_seconds") or DEFAULT_TURN_TIMER_SECONDS),
            banner_filename=local_banner.name if local_banner else None,
        )
        lobby.players[host.id] = host.display_name
        lobby.lobby_deadline = asyncio.get_running_loop().time() + LOBBY_FILL_TIMEOUT_SECONDS
        register_roulette_lobby(lobby)
        view = RouletteView(lobby)
        kwargs: dict[str, object] = {"content": roulette_message_content(lobby), "view": view}
        if local_banner:
            kwargs["file"] = discord.File(str(local_banner), filename=local_banner.name)
        lobby.message = await channel.send(**kwargs)  # type: ignore[arg-type]
        view.lobby.message = lobby.message
        view.start_lobby_timeout()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roulette(bot))
