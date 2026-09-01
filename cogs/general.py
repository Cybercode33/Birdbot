"""Activated-server commands for Discord and the web dashboard."""

from __future__ import annotations

import asyncio
import io
import time
import re
import html
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from command_messages import command_message
from discord_members import resolve_guild_member
from role_permissions import ROLE_PERMISSION_KEYS
from settings import DASHBOARD_PUBLIC_URL
from storage import LOG_EVENT_CATEGORIES, UNCLAIMED_TICKET_TIMEOUT_SECONDS, store


SUPPORT_ROLE_ERROR = "Error: You do not have the required Support Role to claim or close tickets."
DM_DELIVERED = "delivered"
DM_FAILED = "failed"
AUTO_TIMEOUT_EVENT = "Ticket Auto-Deleted (Unclaimed Timeout - 5 min)"

LOG_EVENT_LABELS = {
    "voice_join": "Voice channel joined",
    "voice_leave": "Voice channel left",
    "voice_move": "Voice channel moved",
    "voice_disconnect": "Disconnected from voice",
    "voice_server_mute": "Server mute changed",
    "voice_server_deaf": "Server deaf changed",
    "message_sent": "Message sent",
    "message_edited": "Message edited",
    "message_deleted": "Message deleted",
    "server_update": "Server settings changed",
    "channel_create": "Channel created",
    "channel_update": "Channel updated",
    "channel_delete": "Channel deleted",
    "role_create": "Role created",
    "role_update": "Role updated",
    "role_delete": "Role deleted",
    "member_join": "Member joined",
    "member_leave": "Member left",
    "member_update": "Member updated",
    "moderation_kick": "Member kicked",
    "moderation_ban": "Member banned",
    "moderation_unban": "Member unbanned",
    "moderation_warning": "Warning issued",
    "moderation_unwarning": "Warning removed",
    "moderation_timeout": "Member timed out",
}


def _log_person(value: object | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, dict):
        identifier = value.get("id") or value.get("member_id") or value.get("target_id") or value.get("user_id")
        name = value.get("display_name") or value.get("member_name") or value.get("target_name") or value.get("user_name") or value.get("name")
    else:
        identifier = getattr(value, "id", None)
        name = getattr(value, "display_name", None) or getattr(value, "name", None)
    return (str(identifier) if identifier is not None else None, str(name) if name else None)


def _log_avatar_url(value: object | None) -> str | None:
    if value is None:
        return None
    avatar = getattr(value, "display_avatar", None) or getattr(value, "avatar", None)
    url = getattr(avatar, "url", None)
    return str(url) if url else None


def log_event_embed(
    guild: discord.Guild,
    event_type: str,
    *,
    actor_id: str | None,
    actor_name: str | None,
    actor_avatar_url: str | None,
    target_id: str | None,
    target_name: str | None,
    channel_id: str | None,
    channel_name: str | None,
    details: str,
    created_at: str,
) -> discord.Embed:
    category = LOG_EVENT_CATEGORIES.get(event_type, "server")
    colour = {
        "voice": discord.Colour.blurple(),
        "messages": discord.Colour.green(),
        "members": discord.Colour.teal(),
        "server": discord.Colour.orange(),
        "moderation": discord.Colour.red(),
    }.get(category, discord.Colour.blurple())
    embed = discord.Embed(
        title=LOG_EVENT_LABELS.get(event_type, event_type.replace("_", " ").title()),
        description=(details or "No additional details.")[:4_000],
        colour=colour,
    )
    if actor_id and actor_id.isdigit():
        embed.set_author(name=f"<@{actor_id}>", icon_url=actor_avatar_url)
    elif actor_name:
        embed.set_author(name=actor_name[:1_024], icon_url=actor_avatar_url)
    if target_id and target_id.isdigit():
        embed.add_field(name="Member / target", value=f"<@{target_id}>", inline=True)
    elif target_name:
        embed.add_field(name="Member / target", value=target_name[:1_024], inline=True)
    if channel_id and channel_id.isdigit():
        embed.add_field(name="Channel", value=f"<#{channel_id}>", inline=True)
    elif channel_name:
        embed.add_field(name="Channel", value=f"#{channel_name}"[:1_024], inline=True)
    embed.set_footer(text=f"{guild.name} · BirdBot logs")
    try:
        embed.timestamp = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        embed.timestamp = discord.utils.utcnow()
    return embed


def dm_status_label(status: str | None) -> str:
    """Return the user-facing label shared by Discord and dashboard logs."""
    return "Delivered" if status == DM_DELIVERED else "Failed (DMs Closed)"


def ticket_log_colour(title: str) -> discord.Colour:
    """Keep lifecycle log colours consistent between Discord and the dashboard."""
    normalized = title.casefold()
    if "claim" in normalized:
        return discord.Colour.from_rgb(88, 101, 242)  # Discord blurple
    if "clos" in normalized or "delet" in normalized or "timeout" in normalized or "transcript" in normalized:
        return discord.Colour.from_rgb(237, 66, 69)  # Discord red
    if "creat" in normalized or "open" in normalized:
        return discord.Colour.from_rgb(87, 242, 135)  # Discord green
    return discord.Colour.from_rgb(255, 255, 255)


def format_uptime(duration: timedelta) -> str:
    seconds = int(duration.total_seconds())
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    return " ".join(value for value in (f"{days}d" if days else "", f"{hours}h" if hours or days else "", f"{minutes}m", f"{seconds}s") if value)


def server_embed(guild: discord.Guild, language: str = "en") -> discord.Embed:
    embed = discord.Embed(title=command_message("server", language, "title", name=guild.name), colour=discord.Colour.from_rgb(255, 255, 255))
    unavailable = command_message("server", language, "unavailable")
    embed.add_field(name=command_message("server", language, "server_id"), value=str(guild.id), inline=True)
    embed.add_field(name=command_message("server", language, "owner"), value=guild.owner.mention if guild.owner else unavailable, inline=True)
    embed.add_field(name=command_message("server", language, "members"), value=str(guild.member_count or 0), inline=True)
    embed.add_field(name=command_message("server", language, "created"), value=discord.utils.format_dt(guild.created_at, "D"), inline=True)
    embed.add_field(name=command_message("server", language, "boost_level"), value=command_message("server", language, "boost_value", level=int(guild.premium_tier)), inline=True)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if guild.banner:
        embed.set_image(url=guild.banner.url)
    return embed


def profile_embed(member: discord.Member, language: str = "en") -> discord.Embed:
    roles = [role.mention for role in member.roles[1:]]
    unavailable = command_message("profile", language, "unavailable")
    embed = discord.Embed(title=command_message("profile", language, "title", name=member.display_name), colour=discord.Colour.from_rgb(255, 255, 255))
    embed.add_field(name=command_message("profile", language, "user_id"), value=str(member.id), inline=True)
    embed.add_field(name=command_message("profile", language, "account_created"), value=discord.utils.format_dt(member.created_at, "D"), inline=True)
    embed.add_field(name=command_message("profile", language, "joined_server"), value=discord.utils.format_dt(member.joined_at, "D") if member.joined_at else unavailable, inline=True)
    embed.add_field(name=command_message("profile", language, "roles"), value=" ".join(roles) if roles else command_message("profile", language, "no_roles"), inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    return embed


def ticket_creator_dm_embed(
    guild: discord.Guild,
    ticket: dict[str, object],
    closed_by: discord.Member,
    transcript_url: str | None,
    auto_deleted: bool = False,
) -> discord.Embed:
    """Build the closure notice sent privately to the ticket creator."""
    closed_at = discord.utils.utcnow()
    if auto_deleted:
        title = "Your BirdBot ticket expired"
        description = (
            f"Your ticket channel in **{guild.name}** was automatically closed and deleted "
            f"after {UNCLAIMED_TICKET_TIMEOUT_SECONDS // 60} minutes without being claimed by support."
        )
        status = f"Closed automatically · unclaimed for {UNCLAIMED_TICKET_TIMEOUT_SECONDS // 60} minutes"
    else:
        title = "Your BirdBot ticket was closed"
        description = (
            f"Your ticket channel in **{guild.name}** has been closed and deleted. "
            "Thank you for contacting the support team."
        )
        status = "Closed and deleted"
    embed = discord.Embed(
        title=title,
        description=description,
        colour=discord.Colour.from_rgb(237, 66, 69),
        timestamp=closed_at,
    )
    embed.add_field(name="Status", value=status, inline=False)
    embed.add_field(name="Server", value=guild.name, inline=True)
    embed.add_field(name="Ticket", value=f"{ticket.get('channel_name') or 'Ticket'}\n`{ticket.get('ticket_id') or 'Unknown'}`", inline=True)
    embed.add_field(name="Closed by", value=closed_by.display_name, inline=True)
    embed.add_field(name="Closed at", value=discord.utils.format_dt(closed_at, "F"), inline=False)
    if transcript_url:
        embed.add_field(name="Transcript", value=f"[View transcript]({transcript_url})", inline=False)
    else:
        embed.add_field(name="Transcript", value="A transcript is not available.", inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="BirdBot ticket notification")
    return embed


async def send_ticket_log(
    guild: discord.Guild,
    log_channel_id: str | None,
    title: str,
    description: str,
    transcript_url: str | None = None,
    dm_status: str | None = None,
) -> None:
    """Send a best-effort lifecycle event to the configured ticket log channel."""
    if not log_channel_id or not str(log_channel_id).isdigit():
        return
    channel = guild.get_channel(int(str(log_channel_id)))
    if not isinstance(channel, discord.TextChannel) or not guild.me:
        return
    permissions = channel.permissions_for(guild.me)
    if not permissions.view_channel or not permissions.send_messages:
        return
    if transcript_url:
        description = f"{description}\n\n[View transcript]({transcript_url})"
    if dm_status:
        description = f"{description}\n\nCreator DM: {dm_status_label(dm_status)}"
    embed = discord.Embed(
        title=title,
        description=description[:4_000],
        colour=ticket_log_colour(title),
        timestamp=discord.utils.utcnow(),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="BirdBot ticket logs")
    if transcript_url:
        embed.add_field(name="Transcript", value=f"[Open transcript]({transcript_url})", inline=False)
    if dm_status:
        embed.add_field(name="Creator DM", value=dm_status_label(dm_status), inline=True)
    try:
        if permissions.embed_links:
            await channel.send(embed=embed)
        else:
            await channel.send(f"**{title}**\n{description[:1_900]}")
    except (discord.Forbidden, discord.HTTPException):
        return


def ticket_staff_member(member: discord.Member, config: dict[str, object] | None = None) -> bool:
    """Return whether a member may claim/close/manage tickets.

    Ticket claim/close permissions are intentionally role-based. The
    configured support role is required even when a member has other roles;
    this keeps Discord and dashboard actions consistent.
    """
    config = config or store.ticket_config(str(member.guild.id))
    raw_roles = config.get("support_role_ids")
    configured = {str(role_id) for role_id in raw_roles} if isinstance(raw_roles, list) else set()
    return bool(configured and any(str(role.id) in configured for role in member.roles))


def ticket_status_embed(message: discord.Message, ticket: dict[str, object]) -> discord.Embed:
    """Keep the original ticket description while refreshing its status field."""
    if message.embeds:
        embed = message.embeds[0].copy()
    else:
        embed = discord.Embed(title="Support ticket", colour=discord.Colour.from_rgb(255, 255, 255))
    embed.clear_fields()
    status = str(ticket.get("status") or "open").title()
    if status == "Open":
        status = "Active"
    status_text = status
    if ticket.get("claimed_by_name"):
        status_text += f" · {ticket['claimed_by_name']}"
    embed.add_field(name="Status", value=status_text, inline=False)
    return embed


TRANSCRIPT_DIR = store.path.parent / "transcripts"
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)


async def write_ticket_transcript(channel: discord.TextChannel, ticket: dict[str, object]) -> str:
    """Fetch channel history and write an escaped, self-contained HTML transcript."""
    messages: list[discord.Message] = []
    try:
        async for message in channel.history(limit=None, oldest_first=True):
            messages.append(message)
    except (discord.Forbidden, discord.HTTPException):
        messages = []
    rows: list[str] = []
    for message in messages:
        author = html.escape(getattr(message.author, "display_name", str(message.author)))
        timestamp = html.escape(message.created_at.astimezone(timezone.utc).isoformat())
        content = html.escape(message.content or "")
        attachments = "".join(
            f'<li><a href="{html.escape(attachment.url, quote=True)}">{html.escape(attachment.filename)}</a></li>'
            for attachment in message.attachments
        )
        attachment_html = f"<ul class=\"attachments\">{attachments}</ul>" if attachments else ""
        rows.append(
            f'<article class="message"><header><strong>{author}</strong><time>{timestamp}</time></header>'
            f'<div class="content">{content or "<em>No text</em>"}</div>{attachment_html}</article>'
        )
    if not rows:
        rows.append('<p class="empty">No messages were available, or BirdBot lacked permission to read history.</p>')
    filename = f"{channel.guild.id}-{ticket['ticket_id']}-{secrets.token_hex(8)}.html"
    path = TRANSCRIPT_DIR / filename
    title = html.escape(str(ticket.get("channel_name") or "Ticket transcript"))
    creator = html.escape(str(ticket.get("creator_name") or "Unknown user"))
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} transcript</title><style>
body{{margin:0;background:#111;color:#eee;font:15px/1.5 system-ui,sans-serif}}main{{max-width:900px;margin:32px auto;padding:0 18px}}
h1{{font-size:24px}}.meta{{color:#aaa;margin-bottom:24px}}.message{{padding:14px 16px;margin:10px 0;background:#1e1e1e;border:1px solid #333;border-radius:10px}}
header{{display:flex;justify-content:space-between;gap:16px;color:#fff}}time{{color:#999;font-size:12px}}.content{{white-space:pre-wrap;margin-top:8px}}a{{color:#bdb4ff}}.attachments{{margin:8px 0 0 18px}}.empty{{color:#aaa}}
</style></head><body><main><h1>{title}</h1><div class="meta">Creator: {creator} · Priority: {html.escape(str(ticket.get("priority") or "medium").title())}</div>{''.join(rows)}</main></body></html>"""
    path.write_text(document, encoding="utf-8")
    return f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/uploads/transcripts/{filename}"


def ticket_panel_embed(guild: discord.Guild, config: dict[str, Any]) -> discord.Embed:
    """Build the white ticket panel embed with the guild's live branding."""
    options = config.get("options")
    option_descriptions = [
        f"**{str(option.get('label') or 'Support')}** — {str(option.get('description')).strip()}"
        for option in options
        if isinstance(option, dict) and str(option.get("description") or "").strip()
    ] if isinstance(options, list) else []
    prompt = str(config.get("description_prompt") or "").strip()
    description_parts: list[str] = []
    if prompt and (config.get("require_description") or prompt != "Please describe your request."):
        description_parts.append(prompt)
    description_parts.extend(option_descriptions)
    description = "\n\n".join(description_parts) or "Choose an option below to open a support ticket."
    if len(description) > 4_000:
        description = description[:3_997].rstrip() + "..."
    embed = discord.Embed(
        title=f"{guild.name} support",
        description=description,
        colour=discord.Colour.from_rgb(255, 255, 255),
    )
    custom_icon_url = str(config.get("custom_icon_url") or "").strip()
    if custom_icon_url.startswith(("http://", "https://", "attachment://")):
        embed.set_thumbnail(url=custom_icon_url)
    elif guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if guild.banner:
        embed.set_image(url=guild.banner.url)
    priority = str(config.get("priority") or "medium").title()
    embed.add_field(name="Priority", value=priority, inline=True)
    role_ids = config.get("support_role_ids") or []
    roles = [guild.get_role(int(role_id)) for role_id in role_ids if str(role_id).isdigit()]
    role_mentions = [role.mention for role in roles if role and not role.is_default()]
    if role_mentions:
        embed.add_field(name="Support team", value=" ".join(role_mentions), inline=False)
    embed.set_footer(text="BirdBot ticket system")
    return embed


class TicketDescriptionModal(discord.ui.Modal):
    def __init__(self, panel: "TicketPanelView", option: dict[str, object]) -> None:
        super().__init__(title="Describe your request")
        self.panel = panel
        self.option = option
        self.description = discord.ui.TextInput(
            label="What do you need help with?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1_000,
            placeholder=panel.description_prompt[:100],
        )
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.panel.create_ticket(interaction, self.option, str(self.description.value))


class TicketCloseConfirmView(discord.ui.View):
    """One-shot confirmation shown before a Discord ticket is closed."""

    def __init__(self, ticket_id: str) -> None:
        super().__init__(timeout=45)
        self.ticket_id = str(ticket_id)

    async def _finish(self, interaction: discord.Interaction, message: str) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await interaction.response.edit_message(content=message, view=self)
        except discord.HTTPException:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Confirm close", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        general = interaction.client.get_cog("General")
        if not isinstance(general, General) or not interaction.guild:
            await self._finish(interaction, "Ticket management is unavailable right now.")
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await general.close_ticket_channel(interaction.guild, self.ticket_id, interaction.user, source="discord")
        except (ValueError, discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await interaction.followup.send("Ticket closed, transcript generated, and event logged.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, "Ticket close cancelled.")
        self.stop()


class TicketControlView(discord.ui.View):
    """Persistent Claim/Close controls attached to every ticket channel."""

    def __init__(self, ticket_id: str, claimed: bool = False) -> None:
        super().__init__(timeout=None)
        self.ticket_id = str(ticket_id)
        claim = discord.ui.Button(
            label="Claim Ticket",
            style=discord.ButtonStyle.primary,
            custom_id=f"birdbot:ticket:claim:{self.ticket_id}"[:100],
        )
        claim.callback = self.claim
        claim.disabled = bool(claimed)
        close = discord.ui.Button(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            custom_id=f"birdbot:ticket:close:{self.ticket_id}"[:100],
        )
        close.callback = self.close
        self.add_item(claim)
        self.add_item(close)

    async def claim(self, interaction: discord.Interaction) -> None:
        general = interaction.client.get_cog("General")
        if not isinstance(general, General) or not interaction.guild:
            await interaction.response.send_message("Ticket management is unavailable right now.", ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            updated, changed = await general.claim_ticket_channel(interaction.guild, self.ticket_id, interaction.user, interaction.message)
        except (ValueError, discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        if not changed:
            await interaction.followup.send(f"This ticket is already claimed by {updated.get('claimed_by_name') or 'another staff member'}.", ephemeral=True)
            return
        await interaction.followup.send("Ticket claimed. You now have management access.", ephemeral=True)

    async def close(self, interaction: discord.Interaction) -> None:
        general = interaction.client.get_cog("General")
        if not isinstance(general, General) or not interaction.guild:
            await interaction.response.send_message("Ticket management is unavailable right now.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not ticket_staff_member(interaction.user):
            await interaction.response.send_message(SUPPORT_ROLE_ERROR, ephemeral=True)
            return
        await interaction.response.send_message(
            "Close this ticket? BirdBot will generate a transcript, write a log entry, and delete the channel.",
            ephemeral=True,
            view=TicketCloseConfirmView(self.ticket_id),
        )


class TicketPanelView(discord.ui.View):
    """Interactive ticket panel posted by the dashboard's Post action.

    Both layouts deliberately call ``_handle_option`` so button clicks and
    select-menu choices share the exact same ticket creation and validation
    pipeline.
    """

    def __init__(
        self,
        guild_id: str,
        category_id: str | None,
        support_role_ids: list[str],
        priority: str,
        require_description: bool,
        description_prompt: str,
        log_channel_id: str | None,
        options: list[dict[str, object]],
        max_open_tickets: int = 1,
        panel_layout: str = "select_menu",
    ) -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.category_id = category_id
        self.support_role_ids = support_role_ids
        self.priority = priority
        try:
            configured_limit = int(max_open_tickets)
        except (TypeError, ValueError):
            configured_limit = 1
        self.max_open_tickets = max(1, min(configured_limit, 25))
        self.require_description = require_description
        self.description_prompt = description_prompt or "Please describe your request."
        self.log_channel_id = str(log_channel_id) if log_channel_id else None
        self.panel_layout = panel_layout if panel_layout in {"buttons", "select_menu"} else "select_menu"
        self.options = options[:25]
        if self.panel_layout == "buttons":
            self._add_button_items()
        else:
            self._add_select_item()

    def _add_select_item(self) -> None:
        select_options: list[discord.SelectOption] = []
        for option in self.options:
            kwargs: dict[str, object] = {
                "label": str(option.get("label") or "Support")[:100],
                "value": str(option.get("value") or "support")[:100],
                "description": str(option.get("description") or "")[:100] or None,
            }
            emoji = str(option.get("emoji") or "").strip()
            if emoji:
                kwargs["emoji"] = emoji
            try:
                select_options.append(discord.SelectOption(**kwargs))
            except (TypeError, ValueError):
                kwargs.pop("emoji", None)
                select_options.append(discord.SelectOption(**kwargs))
        select = discord.ui.Select(
            placeholder="Choose a support topic",
            min_values=1,
            max_values=1,
            options=select_options,
            custom_id=f"birdbot:tickets:{self.guild_id}"[:100],
        )
        select.callback = self.on_select
        self.add_item(select)

    def _add_button_items(self) -> None:
        styles = {
            "primary": discord.ButtonStyle.primary,
            "success": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger,
            "secondary": discord.ButtonStyle.secondary,
        }
        default_styles = ("primary", "success", "danger", "secondary")
        for index, option in enumerate(self.options):
            configured_style = str(option.get("button_style") or "").casefold()
            style_name = configured_style if configured_style in styles else default_styles[index % len(default_styles)]
            kwargs: dict[str, object] = {
                "label": str(option.get("label") or "Support")[:80],
                "style": styles[style_name],
                "custom_id": f"birdbot:tickets:button:{self.guild_id}:{index}"[:100],
                "row": index // 5,
            }
            emoji = str(option.get("emoji") or "").strip()
            if emoji:
                kwargs["emoji"] = emoji
            try:
                button = discord.ui.Button(**kwargs)
            except (TypeError, ValueError):
                kwargs.pop("emoji", None)
                button = discord.ui.Button(**kwargs)

            async def callback(interaction: discord.Interaction, option_index: int = index) -> None:
                await self.on_button(interaction, option_index)

            button.callback = callback
            self.add_item(button)

    def _option_for_value(self, value: str) -> dict[str, object] | None:
        return next((option for option in self.options if str(option.get("value")) == value), None)

    async def _reset_select_menu(self, interaction: discord.Interaction) -> None:
        """Redraw the panel after a selection so it is ready for another ticket."""
        if self.panel_layout != "select_menu" or not interaction.message:
            return
        try:
            # Editing the original panel clears the member's client-side
            # selection while retaining the same persistent view/custom ID.
            await interaction.message.edit(view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # A deleted or inaccessible panel should never block ticket work.
            return

    async def _handle_option(self, interaction: discord.Interaction, value: str) -> None:
        selected = self._option_for_value(value)
        if not selected:
            await interaction.response.send_message("That ticket option is no longer available.", ephemeral=True)
            return
        if self.require_description:
            await interaction.response.send_modal(TicketDescriptionModal(self, selected))
            await self._reset_select_menu(interaction)
            return
        await self.create_ticket(interaction, selected, "")

    async def on_select(self, interaction: discord.Interaction) -> None:
        data = interaction.data or {}
        values = data.get("values", []) if hasattr(data, "get") else []
        selected_value = str(values[0]) if values else ""
        await self._handle_option(interaction, selected_value)

    async def on_button(self, interaction: discord.Interaction, option_index: int) -> None:
        if option_index < 0 or option_index >= len(self.options):
            await interaction.response.send_message("That ticket option is no longer available.", ephemeral=True)
            return
        await self._handle_option(interaction, str(self.options[option_index].get("value") or ""))

    async def create_ticket(self, interaction: discord.Interaction, option: dict[str, object], description: str) -> None:
        guild = interaction.guild
        if not guild or str(guild.id) != self.guild_id:
            if not interaction.response.is_done():
                await interaction.response.send_message("This ticket panel is no longer available.", ephemeral=True)
            return
        if not store.is_guild_activated(guild.id):
            if not interaction.response.is_done():
                await interaction.response.send_message("Ticket features are currently disabled for this server.", ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        await self._reset_select_menu(interaction)
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        bot_member = guild.me
        if not member or not bot_member:
            await interaction.followup.send("BirdBot could not resolve your server membership.", ephemeral=True)
            return
        # Read the current setting on every interaction so saving the panel
        # configuration takes effect even before an administrator re-posts it.
        configured_limit = store.ticket_config(str(guild.id)).get("max_open_tickets", self.max_open_tickets)
        try:
            ticket_limit = max(1, min(int(configured_limit), 25))
        except (TypeError, ValueError):
            ticket_limit = self.max_open_tickets
        active_count = store.active_ticket_count(str(guild.id), str(member.id))
        if active_count >= ticket_limit:
            ticket_word = "ticket" if ticket_limit == 1 else "tickets"
            await interaction.followup.send(
                f"You already have {active_count} active {ticket_word}. The limit is {ticket_limit} open ticket{'s' if ticket_limit != 1 else ''} per user.",
                ephemeral=True,
            )
            return
        category = None
        if self.category_id and self.category_id.isdigit():
            candidate = guild.get_channel(int(self.category_id))
            if isinstance(candidate, discord.CategoryChannel):
                category = candidate
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        support_roles: list[discord.Role] = []
        for role_id in self.support_role_ids:
            if not str(role_id).isdigit():
                continue
            role = guild.get_role(int(role_id))
            if role and not role.is_default() and not role.managed:
                support_roles.append(role)
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                )
        base_name = re.sub(r"[^a-z0-9-]+", "-", member.display_name.casefold()).strip("-") or "user"
        channel_name = f"ticket-{base_name[:80]}"
        try:
            ticket_channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket opened by {member} via BirdBot",
            )
            mentions = " ".join([member.mention, *(role.mention for role in support_roles)])
            ticket_embed = discord.Embed(
                title=f"{str(option.get('label') or 'Support')} ticket",
                description=description or "A member of the support team will be with you shortly.",
                colour=discord.Colour.from_rgb(255, 255, 255),
            )
            if guild.icon:
                ticket_embed.set_thumbnail(url=guild.icon.url)
            ticket_embed.add_field(name="Status", value="Active", inline=False)
            ticket_message = await ticket_channel.send(
                content=mentions or None,
                embed=ticket_embed,
                view=TicketControlView(str(ticket_channel.id)),
                allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
            )
            record = store.create_ticket(
                str(guild.id),
                str(ticket_channel.id),
                ticket_channel.name,
                str(member.id),
                member.display_name,
                str(option.get("label") or "Support"),
                self.category_id,
                category.name if category else None,
                self.priority,
            )
            store.create_ticket_log(
                str(guild.id),
                "opened",
                ticket_id=str(ticket_channel.id),
                actor_id=str(member.id),
                actor_name=member.display_name,
                creator_id=str(member.id),
                creator_name=member.display_name,
                channel_id=str(ticket_channel.id),
                channel_name=ticket_channel.name,
                priority=self.priority,
                details=f"Ticket opened for {str(option.get('label') or 'Support')}",
            )
            configured_log_channel = store.ticket_config(str(guild.id)).get("log_channel_id")
            await send_ticket_log(
                guild,
                str(configured_log_channel) if configured_log_channel else None,
                "Ticket created",
                f"{ticket_channel.mention} was opened by {member.mention}.\nTopic: {str(option.get('label') or 'Support')} · Priority: {self.priority.title()}",
            )
        except discord.Forbidden:
            await interaction.followup.send("BirdBot needs Manage Channels and permission to mention the support roles.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("BirdBot could not create the ticket channel right now. Please try again.", ephemeral=True)
            return
        await interaction.followup.send(f"Your ticket was created: {ticket_channel.mention}", ephemeral=True)


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._ticket_views_registered: set[str] = set()
        self._ticket_control_views_registered: set[str] = set()

    @staticmethod
    def _is_log_delivery_message(message: discord.Message) -> bool:
        """Avoid recursively logging BirdBot's own messages in the log channel."""
        if not message.author.bot or not message.guild:
            return False
        config = store.log_config(str(message.guild.id))
        category_channels = config.get("category_channels")
        channel_id = str(getattr(message.channel, "id", ""))
        if isinstance(category_channels, dict):
            return channel_id in {str(value) for value in category_channels.values() if value}
        return channel_id == str(config.get("log_channel_id") or "")

    async def write_log(
        self,
        guild: discord.Guild,
        event_type: str,
        *,
        actor: object | None = None,
        target: object | None = None,
        channel: object | None = None,
        details: str = "",
    ) -> None:
        """Persist and, when configured, publish one server activity event.

        Event handlers are intentionally best-effort: a missing log channel or
        a temporary Discord error must never interrupt the actual moderation,
        message, or voice event that triggered this method.
        """
        category = LOG_EVENT_CATEGORIES.get(event_type)
        if not category or not guild:
            return
        config = store.log_config(str(guild.id))
        categories = config.get("categories")
        if not config.get("enabled") or not isinstance(categories, dict) or not categories.get(category, True):
            return
        actor_id, actor_name = _log_person(actor)
        target_id, target_name = _log_person(target)
        channel_id, channel_name = _log_person(channel)
        actor_avatar_url = _log_avatar_url(actor)
        # A channel name is more useful than display_name for Discord channel
        # objects, while _log_person still handles deleted/unknown objects.
        if channel is not None:
            channel_name = str(getattr(channel, "name", None) or channel_name or "") or None
        record = store.create_log(
            str(guild.id), event_type, actor_id=actor_id, actor_name=actor_name,
            actor_avatar_url=actor_avatar_url,
            target_id=target_id, target_name=target_name, channel_id=channel_id,
            channel_name=channel_name, details=details,
        )
        category_channels = config.get("category_channels")
        raw_channel_id = category_channels.get(category) if isinstance(category_channels, dict) else config.get("log_channel_id")
        if not raw_channel_id or not str(raw_channel_id).isdigit():
            return
        log_channel = guild.get_channel(int(str(raw_channel_id)))
        if not isinstance(log_channel, discord.TextChannel):
            return
        bot_member = guild.me
        if not bot_member:
            return
        permissions = log_channel.permissions_for(bot_member)
        if not permissions.view_channel or not permissions.send_messages:
            return
        embed = log_event_embed(
            guild, event_type, actor_id=actor_id, actor_name=actor_name,
            actor_avatar_url=actor_avatar_url, target_id=target_id,
            target_name=target_name, channel_id=channel_id, channel_name=channel_name,
            details=str(record.get("details") or ""),
            created_at=str(record.get("created_at") or ""),
        )
        try:
            if permissions.embed_links:
                await log_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            else:
                await log_channel.send(
                    f"**{embed.title}**\n{str(record.get('details') or 'No additional details.')[:1_900]}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.Forbidden, discord.HTTPException):
            return

    async def apply_auto_reacts(self, message: discord.Message) -> None:
        """Apply every enabled reaction rule configured for this channel."""
        if not message.guild or message.author.bot:
            return
        channel_id = str(getattr(message.channel, "id", ""))
        for rule in store.auto_reacts(str(message.guild.id)):
            if not rule.get("enabled", True) or str(rule.get("channel_id") or "") != channel_id:
                continue
            emoji = str(rule.get("emoji") or "").strip()
            if not emoji:
                continue
            try:
                await message.add_reaction(emoji)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                # A deleted message, an unavailable custom emoji, or a
                # missing Add Reactions permission should not interrupt normal
                # command processing or activity logging.
                continue

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or self._is_log_delivery_message(message):
            return
        await self.apply_auto_reacts(message)
        content = str(message.content or "").strip() or "[No text]"
        if message.attachments:
            content += "\nAttachments: " + ", ".join(attachment.filename for attachment in message.attachments[:8])
        await self.write_log(message.guild, "message_sent", actor=message.author, channel=message.channel, details=content[:4_000])

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or self._is_log_delivery_message(after):
            return
        old = str(before.content or "").strip() or "[No text]"
        new = str(after.content or "").strip() or "[No text]"
        if old == new and not after.attachments:
            return
        await self.write_log(
            after.guild, "message_edited", actor=after.author, channel=after.channel,
            details=f"Before: {old}\nAfter: {new}"[:4_000],
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or self._is_log_delivery_message(message):
            return
        content = str(message.content or "").strip() or "[Content unavailable]"
        await self.write_log(message.guild, "message_deleted", actor=message.author, channel=message.channel, details=content[:4_000])

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        before_channel = before.channel
        after_channel = after.channel
        if before_channel is None and after_channel is not None:
            await self.write_log(member.guild, "voice_join", actor=member, target=member, channel=after_channel, details=f"Joined {after_channel.name}.")
        elif before_channel is not None and after_channel is None:
            await self.write_log(member.guild, "voice_disconnect", actor=member, target=member, channel=before_channel, details=f"Disconnected from {before_channel.name}.")
        elif before_channel is not None and after_channel is not None and before_channel.id != after_channel.id:
            await self.write_log(member.guild, "voice_move", actor=member, target=member, channel=after_channel, details=f"Moved from {before_channel.name} to {after_channel.name}.")
        if before.mute != after.mute:
            action = "server-muted" if after.mute else "server-unmuted"
            await self.write_log(member.guild, "voice_server_mute", actor=member, target=member, channel=after_channel or before_channel, details=f"Member was {action}.")
        if before.deaf != after.deaf:
            action = "server-deafened" if after.deaf else "server-undeafened"
            await self.write_log(member.guild, "voice_server_deaf", actor=member, target=member, channel=after_channel or before_channel, details=f"Member was {action}.")

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        changed = []
        for field in ("name", "verification_level", "default_notifications", "explicit_content_filter", "afk_timeout"):
            if getattr(before, field, None) != getattr(after, field, None):
                changed.append(field.replace("_", " "))
        if changed:
            await self.write_log(after, "server_update", details="Changed: " + ", ".join(changed))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self.write_log(channel.guild, "channel_create", target=channel, channel=channel, details=f"Created {channel.name}.")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        changed = []
        for field in ("name", "category", "position", "topic", "slowmode_delay", "nsfw"):
            if getattr(before, field, None) != getattr(after, field, None):
                changed.append(field.replace("_", " "))
        if changed:
            await self.write_log(after.guild, "channel_update", target=after, channel=after, details="Changed: " + ", ".join(changed))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.write_log(channel.guild, "channel_delete", target=channel, channel=channel, details=f"Deleted {channel.name}.")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self.write_log(role.guild, "role_create", target=role, details=f"Created role {role.name}.")

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        changed = []
        for field in ("name", "colour", "position", "permissions", "mentionable", "hoist"):
            if getattr(before, field, None) != getattr(after, field, None):
                changed.append(field)
        if changed:
            await self.write_log(after.guild, "role_update", target=after, details="Changed: " + ", ".join(changed))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.write_log(role.guild, "role_delete", target=role, details=f"Deleted role {role.name}.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self.write_log(member.guild, "member_join", actor=member, target=member, details=f"{member} joined the server.")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.write_log(member.guild, "member_leave", actor=member, target=member, details=f"{member} left the server.")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        changed = []
        if before.nick != after.nick:
            changed.append("nickname")
        if before.roles != after.roles:
            changed.append("roles")
        if before.pending != after.pending:
            changed.append("verification status")
        if changed:
            await self.write_log(after.guild, "member_update", actor=after, target=after, details="Changed: " + ", ".join(changed))

    @staticmethod
    def sync_existing_ticket_channels(guild: discord.Guild, config: dict[str, object]) -> None:
        """Backfill records for ticket channels created before ticket history was added."""
        priority = str(config.get("priority") or "medium")
        category_id = str(config.get("category_id")) if config.get("category_id") else None
        for channel in guild.text_channels:
            if not channel.name.casefold().startswith("ticket-") or store.ticket(str(guild.id), str(channel.id)):
                continue
            creator = next(
                (
                    target
                    for target, overwrite in channel.overwrites.items()
                    if isinstance(target, discord.Member)
                    and (not guild.me or target.id != guild.me.id)
                    and overwrite.view_channel is True
                ),
                None,
            )
            store.create_ticket(
                str(guild.id),
                str(channel.id),
                channel.name,
                str(creator.id) if creator else "0",
                creator.display_name if creator else "Unknown member",
                "Support",
                category_id,
                channel.category.name if channel.category else None,
                priority,
            )

    async def register_ticket_views(self) -> None:
        """Restore posted ticket panels after a gateway reconnect or process restart."""
        for guild in self.bot.guilds:
            guild_id = str(guild.id)
            self.sync_existing_ticket_channels(guild, store.ticket_config(guild_id))
            # Ticket controls use dynamic custom IDs, so register one
            # persistent view for every still-open ticket after a restart.
            for ticket in store.tickets(guild_id):
                ticket_id = str(ticket["ticket_id"])
                if ticket.get("status") != "closed" and ticket_id not in self._ticket_control_views_registered:
                    self.bot.add_view(TicketControlView(ticket_id, claimed=ticket.get("status") == "claimed"))
                    self._ticket_control_views_registered.add(ticket_id)
            if guild_id in self._ticket_views_registered:
                continue
            config = store.ticket_config(guild_id)
            raw_options = config.get("options")
            options = [option for option in raw_options if isinstance(option, dict)] if isinstance(raw_options, list) else []
            if not config.get("setup_channel_id") or not options:
                continue
            role_ids = config.get("support_role_ids")
            self.bot.add_view(
                TicketPanelView(
                    guild_id,
                    str(config.get("category_id")) if config.get("category_id") else None,
                    [str(role_id) for role_id in role_ids] if isinstance(role_ids, list) else [],
                    str(config.get("priority") or "medium"),
                    bool(config.get("require_description")),
                    str(config.get("description_prompt") or "Please describe your request."),
                    str(config.get("log_channel_id")) if config.get("log_channel_id") else None,
                    options,
                    max_open_tickets=config.get("max_open_tickets") or 1,
                    panel_layout=str(config.get("panel_layout") or "select_menu"),
                )
            )
            self._ticket_views_registered.add(guild_id)

    async def resolve_ticket_member(self, guild: discord.Guild, member_or_user: discord.abc.User) -> discord.Member | None:
        if isinstance(member_or_user, discord.Member):
            return member_or_user
        return await resolve_guild_member(guild, member_or_user.id)

    async def notify_ticket_creator(
        self,
        guild: discord.Guild,
        ticket: dict[str, object],
        closed_by: discord.Member,
        transcript_url: str | None,
        auto_deleted: bool = False,
    ) -> str:
        """Send a best-effort closure DM and return its persisted delivery status.

        Discord may reject the send when the creator has disabled DMs, blocked
        BirdBot, or left the platform. Those failures are intentionally
        swallowed so channel deletion and audit logging always finish.
        """
        creator_id = str(ticket.get("creator_id") or "")
        if not creator_id.isdigit():
            return DM_FAILED
        try:
            async def dispatch() -> None:
                creator = self.bot.get_user(int(creator_id)) or await self.bot.fetch_user(int(creator_id))
                await creator.send(embed=ticket_creator_dm_embed(guild, ticket, closed_by, transcript_url, auto_deleted))

            await asyncio.wait_for(
                dispatch(),
                timeout=8.0,
            )
        except (asyncio.TimeoutError, discord.Forbidden, discord.NotFound, discord.HTTPException, ValueError, TypeError):
            return DM_FAILED
        except Exception:
            # A DM must never prevent closing a ticket if an unexpected client
            # or transport error occurs.
            return DM_FAILED
        return DM_DELIVERED

    async def auto_delete_unclaimed_ticket(self, guild: discord.Guild, ticket: dict[str, object]) -> bool:
        """Close one timeout-reserved ticket and notify its creator.

        The timeout worker reserves tickets in SQLite before entering this
        method.  ``close_unclaimed_ticket`` then performs a second atomic check
        so a staff claim or manual close can win a race without duplicate
        deletion, DMs, or log entries.
        """
        ticket_id = str(ticket.get("ticket_id") or "")
        current = store.ticket(str(guild.id), ticket_id)
        if not current or current.get("status") != "open" or current.get("claimed_by"):
            return False
        bot_member = guild.me
        if not bot_member:
            raise ValueError("BirdBot is not ready in this server.")

        channel_id = str(current.get("channel_id") or ticket_id)
        channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        channel_name = str(current.get("channel_name") or f"ticket-{channel_id}")
        channel_mention = channel.mention if isinstance(channel, discord.TextChannel) else f"<#{channel_id}>"
        transcript_url: str | None = None
        if isinstance(channel, discord.TextChannel):
            if not bot_member.guild_permissions.manage_channels:
                raise ValueError("BirdBot needs Manage Channels permission to auto-delete timed out tickets.")
            transcript_url = await write_ticket_transcript(channel, current)
            try:
                await channel.send(
                    "This ticket was automatically closed because no support member claimed it within 5 minutes."
                )
            except (discord.Forbidden, discord.HTTPException):
                # Deletion and audit logging should still proceed if the bot
                # lost Send Messages permission in the ticket channel.
                pass
            try:
                await channel.delete(reason="Ticket auto-deleted after 5 minutes without a claim")
            except discord.NotFound:
                # A moderator may have deleted the channel manually; archive
                # the persistent record anyway.
                pass

        actor_name = bot_member.display_name
        updated = store.close_unclaimed_ticket(
            str(guild.id), ticket_id, str(bot_member.id), actor_name, transcript_url
        )
        if not updated:
            return False
        try:
            created_at = datetime.fromisoformat(str(current.get("created_at")))
            duration = max(0, int((datetime.now(timezone.utc) - created_at).total_seconds()))
        except (TypeError, ValueError):
            duration = None
        config = store.ticket_config(str(guild.id))
        details = (
            f"{AUTO_TIMEOUT_EVENT}. {channel_mention} was closed because it was not claimed within "
            f"{UNCLAIMED_TICKET_TIMEOUT_SECONDS // 60} minutes."
        )
        if duration is not None:
            details += f" Open for {duration // 3600}h {(duration % 3600) // 60}m."
        dm_status = await self.notify_ticket_creator(
            guild, current, bot_member, transcript_url, auto_deleted=True
        )
        store.create_ticket_log(
            str(guild.id), "auto_deleted", ticket_id=ticket_id, actor_id=str(bot_member.id),
            actor_name=actor_name, creator_id=str(current.get("creator_id") or ""),
            creator_name=str(current.get("creator_name") or ""), channel_id=channel_id,
            channel_name=channel_name, priority=str(current.get("priority") or "medium"),
            duration_seconds=duration, transcript_url=transcript_url, dm_status=dm_status,
            details=details,
        )
        await send_ticket_log(
            guild, str(config.get("log_channel_id") or "") or None, AUTO_TIMEOUT_EVENT,
            f"{details}\nCreator: {current.get('creator_name') or 'Unknown user'}",
            transcript_url=transcript_url,
            dm_status=dm_status,
        )
        return True

    async def expire_unclaimed_tickets(self) -> None:
        """Process due five-minute timers from the single global bot client."""
        try:
            due_tickets = store.unclaimed_tickets_due(limit=100)
        except Exception as error:
            print(f"Could not read unclaimed ticket timers: {error}")
            return
        for ticket in due_tickets:
            guild_id = str(ticket.get("guild_id") or "")
            ticket_id = str(ticket.get("ticket_id") or "")
            if not guild_id.isdigit() or not ticket_id:
                continue
            guild = self.bot.get_guild(int(guild_id))
            if not guild or not store.reserve_unclaimed_timeout(guild_id, ticket_id):
                continue
            try:
                completed = await self.auto_delete_unclaimed_ticket(guild, ticket)
                if not completed:
                    store.clear_unclaimed_timeout(guild_id, ticket_id)
            except (discord.Forbidden, discord.HTTPException, ValueError) as error:
                store.clear_unclaimed_timeout(guild_id, ticket_id)
                print(f"Could not auto-delete unclaimed ticket {ticket_id}: {error}")
            except Exception as error:
                store.clear_unclaimed_timeout(guild_id, ticket_id)
                print(f"Unexpected timeout cleanup failure for ticket {ticket_id}: {error}")

    async def claim_ticket_channel(
        self,
        guild: discord.Guild,
        ticket_id: str,
        actor: discord.abc.User,
        message: discord.Message | None = None,
    ) -> tuple[dict[str, object], bool]:
        ticket = store.ticket(str(guild.id), str(ticket_id))
        if not ticket:
            raise ValueError("That ticket could not be found.")
        if ticket.get("status") == "closed":
            raise ValueError("Closed tickets cannot be claimed.")
        member = await self.resolve_ticket_member(guild, actor)
        if not member or not ticket_staff_member(member):
            raise ValueError(SUPPORT_ROLE_ERROR)
        bot_member = guild.me
        channel = guild.get_channel(int(str(ticket["channel_id"])))
        if not isinstance(channel, discord.TextChannel) or not bot_member:
            raise ValueError("The ticket channel is no longer available.")
        if not bot_member.guild_permissions.manage_channels:
            raise ValueError("BirdBot needs Manage Channels permission to claim tickets.")
        updated = store.claim_ticket(str(guild.id), str(ticket_id), str(member.id), member.display_name)
        if not updated:
            raise ValueError("That ticket could not be claimed.")
        changed = str(updated.get("claimed_by")) == str(member.id) and str(ticket.get("claimed_by") or "") != str(member.id)
        if not changed:
            return updated, False
        overwrite = channel.overwrites_for(member)
        overwrite.view_channel = True
        overwrite.send_messages = True
        overwrite.read_message_history = True
        overwrite.manage_messages = True
        overwrite.manage_channels = True
        await channel.set_permissions(member, overwrite=overwrite, reason=f"Ticket claimed by {member}")
        config = store.ticket_config(str(guild.id))
        # Support roles keep read/send access but lose management access once
        # an individual staff member owns the ticket. Admin permissions remain.
        for role_id in config.get("support_role_ids") or []:
            if not str(role_id).isdigit():
                continue
            role = guild.get_role(int(str(role_id)))
            if not role or role.is_default() or role.managed:
                continue
            role_overwrite = channel.overwrites_for(role)
            role_overwrite.manage_messages = False
            role_overwrite.manage_channels = False
            await channel.set_permissions(role, overwrite=role_overwrite, reason="Ticket claimed by staff member")
        await channel.send(f"This ticket has been claimed by {member.mention}.")
        if message is None:
            try:
                async for candidate in channel.history(limit=1, oldest_first=True):
                    message = candidate
                    break
            except (discord.Forbidden, discord.HTTPException):
                message = None
        if message:
            try:
                await message.edit(embed=ticket_status_embed(message, updated), view=self._view_with_claim_disabled(str(ticket_id)))
            except (discord.Forbidden, discord.HTTPException):
                pass
        store.create_ticket_log(
            str(guild.id), "claimed", ticket_id=str(ticket_id), actor_id=str(member.id),
            actor_name=member.display_name, creator_id=str(updated.get("creator_id") or ""),
            creator_name=str(updated.get("creator_name") or ""), channel_id=str(channel.id),
            channel_name=channel.name, priority=str(updated.get("priority") or "medium"),
            details=f"Ticket claimed by {member.display_name}",
        )
        await send_ticket_log(
            guild, str(config.get("log_channel_id") or "") or None, "Ticket claimed",
            f"{channel.mention} was claimed by {member.mention}.\nPriority: {str(updated.get('priority') or 'medium').title()}",
        )
        return updated, True

    def _view_with_claim_disabled(self, ticket_id: str) -> TicketControlView:
        view = TicketControlView(ticket_id)
        if view.children and isinstance(view.children[0], discord.ui.Button):
            view.children[0].disabled = True
        return view

    async def close_ticket_channel(
        self,
        guild: discord.Guild,
        ticket_id: str,
        actor: discord.abc.User,
        source: str = "dashboard",
    ) -> dict[str, object]:
        ticket = store.ticket(str(guild.id), str(ticket_id))
        if not ticket:
            raise ValueError("That ticket could not be found.")
        if ticket.get("status") == "closed":
            raise ValueError("That ticket is already closed.")
        member = await self.resolve_ticket_member(guild, actor)
        if not member or not ticket_staff_member(member):
            raise ValueError(SUPPORT_ROLE_ERROR)
        bot_member = guild.me
        channel = guild.get_channel(int(str(ticket["channel_id"])))
        if not isinstance(channel, discord.TextChannel) or not bot_member:
            raise ValueError("The ticket channel is no longer available.")
        if not bot_member.guild_permissions.manage_channels:
            raise ValueError("BirdBot needs Manage Channels permission to close tickets.")
        transcript_url = await write_ticket_transcript(channel, ticket)
        channel_name = channel.name
        channel_mention = channel.mention
        await channel.delete(reason=f"Ticket closed by {member} via {source}")
        updated = store.close_ticket(str(guild.id), str(ticket_id), str(member.id), member.display_name, transcript_url)
        if not updated:
            raise ValueError("That ticket was already closed by another staff member.")
        try:
            created_at = datetime.fromisoformat(str(ticket.get("created_at")))
            duration = max(0, int((datetime.now(timezone.utc) - created_at).total_seconds()))
        except (TypeError, ValueError):
            duration = None
        config = store.ticket_config(str(guild.id))
        details = f"{channel_mention} was closed by {member.mention}."
        if duration is not None:
            details += f" Open for {duration // 3600}h {(duration % 3600) // 60}m."
        dm_status = await self.notify_ticket_creator(guild, ticket, member, transcript_url)
        store.create_ticket_log(
            str(guild.id), "closed", ticket_id=str(ticket_id), actor_id=str(member.id),
            actor_name=member.display_name, creator_id=str(ticket.get("creator_id") or ""),
            creator_name=str(ticket.get("creator_name") or ""), channel_id=str(ticket.get("channel_id") or ""),
            channel_name=channel_name, priority=str(ticket.get("priority") or "medium"),
            duration_seconds=duration, transcript_url=transcript_url, dm_status=dm_status,
            details=details,
        )
        await send_ticket_log(
            guild, str(config.get("log_channel_id") or "") or None, "Ticket closed",
            f"{details}\nCreator: {ticket.get('creator_name') or 'Unknown user'}",
            transcript_url=transcript_url,
            dm_status=dm_status,
        )
        return updated

    async def ticket_member_action(
        self,
        guild: discord.Guild,
        ticket_id: str,
        actor: discord.abc.User,
        target: discord.Member,
        add: bool,
    ) -> None:
        ticket = store.ticket(str(guild.id), str(ticket_id))
        if not ticket or ticket.get("status") == "closed":
            raise ValueError("That ticket is not available.")
        staff = await self.resolve_ticket_member(guild, actor)
        if not staff or not ticket_staff_member(staff):
            raise ValueError("Only configured support staff or administrators can manage ticket members.")
        if not guild.me or not guild.me.guild_permissions.manage_channels:
            raise ValueError("BirdBot needs Manage Channels permission to update ticket members.")
        channel = guild.get_channel(int(str(ticket["channel_id"])))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("The ticket channel is no longer available.")
        if not add and str(target.id) == str(ticket.get("creator_id")):
            raise ValueError("The ticket creator cannot be removed from their ticket.")
        if add:
            overwrite = channel.overwrites_for(target)
            overwrite.view_channel = True
            overwrite.send_messages = True
            overwrite.read_message_history = True
            await channel.set_permissions(target, overwrite=overwrite, reason=f"Member added by {staff}")
            action = "added"
            await channel.send(f"{target.mention} was added to this ticket by {staff.mention}.")
        else:
            # An explicit deny is required when the member also has a support
            # role that grants the channel through its role overwrite.
            await channel.set_permissions(
                target,
                overwrite=discord.PermissionOverwrite(
                    view_channel=False,
                    send_messages=False,
                    read_message_history=False,
                ),
                reason=f"Member removed by {staff}",
            )
            action = "removed"
            await channel.send(f"{target.mention} was removed from this ticket by {staff.mention}.")
        store.create_ticket_log(
            str(guild.id), f"member_{action}", ticket_id=str(ticket_id), actor_id=str(staff.id),
            actor_name=staff.display_name, creator_id=str(ticket.get("creator_id") or ""),
            creator_name=str(ticket.get("creator_name") or ""), channel_id=str(channel.id),
            channel_name=channel.name, priority=str(ticket.get("priority") or "medium"),
            details=f"{target} was {action} by {staff}",
        )
        await send_ticket_log(
            guild,
            str(store.ticket_config(str(guild.id)).get("log_channel_id") or "") or None,
            f"Ticket member {action}",
            f"{target.mention} was {action} in {channel.mention} by {staff.mention}.",
        )

    async def cog_check(self, ctx: commands.Context[commands.Bot]) -> bool:
        if not ctx.guild or not store.is_guild_activated(ctx.guild.id):
            return False
        command_name = str(ctx.command.qualified_name if ctx.command else "").replace(" ", "_")
        if command_name == "show_warnings":
            command_name = "show_warning"
        if command_name in {"ping", "server", "profile", "kick", "ban", "warning", "unwarning", "show_warning", "timeout", "lock", "unlock", "delete"}:
            return bool(store.command_config(str(ctx.guild.id), command_name).get("enabled", True))
        return True

    async def active_interaction(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not store.is_guild_activated(interaction.guild.id):
            await interaction.response.send_message("This server has not enabled BirdBot yet.", ephemeral=True)
            return False
        command = interaction.command
        command_name = str(getattr(command, "qualified_name", "") or getattr(command, "name", "")).replace(" ", "_")
        if command_name == "show_warnings":
            command_name = "show_warning"
        if command_name in {"ping", "server", "profile", "kick", "ban", "warning", "unwarning", "show_warning", "timeout", "lock", "unlock", "delete"} and not store.command_config(str(interaction.guild.id), command_name).get("enabled", True):
            language = str(store.command_config(str(interaction.guild.id), command_name).get("language") or "en")
            await interaction.response.send_message(command_message("common", language, "disabled"), ephemeral=True)
            return False
        return True

    @staticmethod
    def moderation_problem(guild: discord.Guild, target: discord.Member) -> str | None:
        bot_member = guild.me
        if not bot_member:
            return "BirdBot is not ready in this server."
        if target.bot:
            return "Bots cannot be moderated with this command."
        if target.id == guild.owner_id or target.guild_permissions.administrator:
            return "Server owners and Administrators cannot be moderated with this command."
        if target.top_role >= bot_member.top_role:
            return "BirdBot's role must be above that member's highest role."
        return None

    @staticmethod
    def _warning_language(guild: discord.Guild, command_name: str = "warning") -> str:
        return str(store.command_config(str(guild.id), command_name).get("language") or "en")

    @staticmethod
    def _warning_reason(reason: str | None, language: str = "en") -> str:
        normalized = str(reason or "").strip()
        return normalized[:512] or command_message("warning", language, "no_reason")

    async def _issue_warning(
        self,
        guild: discord.Guild,
        member: discord.Member,
        moderator: discord.abc.User,
        reason: str | None,
        language: str,
    ) -> tuple[dict[str, object], bool, str]:
        """Persist a warning and notify the member without making DM failure fatal."""
        normalized_reason = self._warning_reason(reason, language)
        warning = store.add_warning(
            str(guild.id),
            str(member.id),
            member.display_name,
            str(moderator.id),
            getattr(moderator, "display_name", str(moderator)),
            normalized_reason,
        )
        await self.write_log(
            guild,
            "moderation_warning",
            actor=moderator,
            target=member,
            details=f"Warning #{warning['warning_id']} issued.\nReason: {normalized_reason}",
        )
        dm_delivered = True
        try:
            await member.send(
                command_message(
                    "warning",
                    language,
                    "dm",
                    number=warning["warning_id"],
                    server=guild.name,
                    reason=normalized_reason,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            dm_delivered = False
        return warning, dm_delivered, normalized_reason

    @staticmethod
    def _warning_date(value: object) -> str:
        text = str(value or "")
        if "T" in text:
            text = text.replace("T", " ", 1)
        return text[:19] or "Unknown date"

    def _warning_embed(self, member: discord.Member, warnings: list[dict[str, object]], language: str) -> discord.Embed:
        if warnings:
            description = command_message("show_warning", language, "count", count=len(warnings))
        else:
            description = command_message("show_warning", language, "none", member=member.mention)
        embed = discord.Embed(
            title=command_message("show_warning", language, "title", member=member.display_name),
            description=description,
            colour=discord.Colour.from_rgb(255, 255, 255),
        )
        for warning in warnings[:25]:
            reason = str(warning.get("reason") or command_message("warning", language, "no_reason"))
            moderator = str(warning.get("moderator_name") or warning.get("moderator_id") or "Unknown")
            value = command_message(
                "show_warning",
                language,
                "entry",
                number=warning.get("warning_id"),
                date=self._warning_date(warning.get("created_at")),
                moderator=moderator,
                reason=reason,
            )
            embed.add_field(name=f"#{warning.get('warning_id')}", value=value[:1024], inline=False)
        return embed

    async def _warning_member_problem(self, guild: discord.Guild, member: discord.Member) -> str | None:
        bot_member = guild.me
        if not bot_member:
            return "BirdBot is not ready in this server."
        if not bot_member.guild_permissions.moderate_members:
            return "BirdBot needs the Moderate Members permission to manage warnings."
        return None

    async def send_dashboard_ping(self, channel: discord.TextChannel, requested_by: str, language: str = "en") -> None:
        started = time.perf_counter()
        message = await channel.send(command_message("ping", language, "checking"))
        embed = discord.Embed(
            title=command_message("ping", language, "title"),
            description=command_message("ping", language, "description"),
            colour=discord.Colour.from_rgb(255, 255, 255),
        )
        embed.add_field(name=command_message("ping", language, "response"), value=f"{round((time.perf_counter() - started) * 1000)}ms", inline=True)
        embed.add_field(name=command_message("ping", language, "gateway"), value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.set_footer(text=command_message("ping", language, "footer", requested_by=requested_by))
        await message.edit(content=None, embed=embed)

    async def refresh_bans(self, guild: discord.Guild) -> None:
        if guild.me and guild.me.guild_permissions.ban_members:
            store.sync_bans(str(guild.id), [ban async for ban in guild.bans(limit=500)])

    async def run_dashboard_server_message(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        payload: dict[str, Any],
    ) -> None:
        """Send a dashboard-authored normal message or embed quickly.

        This is deliberately separate from the slash-command renderer: the
        dashboard message is user-provided content, while commands such as
        ``/server`` have their own structured embeds.  Permissions are checked
        against the live channel so a stale dashboard cannot silently succeed.
        """
        bot_member = guild.me
        if not bot_member:
            raise ValueError("BirdBot is not ready in this server.")
        channel_permissions = channel.permissions_for(bot_member)
        if not channel_permissions.view_channel or not channel_permissions.send_messages:
            raise ValueError("BirdBot cannot access that channel. Grant it View Channel and Send Messages permissions.")
        message_type = str(payload.get("message_type") or "normal").strip().lower()
        raw_mentions = payload.get("mention_user_ids", [])
        if raw_mentions is None:
            raw_mentions = []
        if not isinstance(raw_mentions, list) or len(raw_mentions) > 25:
            raise ValueError("Choose up to 25 members to mention.")
        mention_members: list[discord.Member] = []
        for member_id in raw_mentions:
            if not isinstance(member_id, str) or not member_id.isdigit() or not 17 <= len(member_id) <= 20:
                raise ValueError("One of the selected members is invalid. Choose them again.")
            member = await resolve_guild_member(guild, member_id, attempts=1, timeout_seconds=4)
            if member is None:
                raise ValueError("One of the selected members is no longer in this server.")
            mention_members.append(member)
        allowed_mentions = discord.AllowedMentions(
            users=mention_members,
            roles=False,
            everyone=False,
            replied_user=False,
        )

        reply_to = str(payload.get("reply_to") or "").strip()
        reply_target: discord.Message | None = None
        if reply_to:
            link = re.fullmatch(
                r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)",
                reply_to,
                flags=re.IGNORECASE,
            )
            if link:
                linked_guild_id, linked_channel_id, linked_message_id = link.groups()
                if linked_guild_id != str(guild.id) or linked_channel_id != str(channel.id):
                    raise ValueError("The reply message must belong to the selected server channel.")
                reply_to = linked_message_id
            if not reply_to.isdigit() or not 17 <= len(reply_to) <= 20:
                raise ValueError("Paste a valid Discord message link or message ID to reply.")
            if not channel_permissions.read_message_history:
                raise ValueError("BirdBot needs Read Message History permission to reply to that message.")
            try:
                reply_target = await asyncio.wait_for(channel.fetch_message(int(reply_to)), timeout=8)
            except asyncio.TimeoutError as error:
                raise ValueError("Discord took too long to load the reply target. Please try again.") from error
            except discord.NotFound as error:
                raise ValueError("That message could not be found in the selected channel.") from error
            except discord.Forbidden as error:
                raise ValueError("BirdBot cannot read that message in the selected channel.") from error
            except discord.HTTPException as error:
                raise ValueError("Discord rejected the reply target. Please try again.") from error

        if message_type == "normal":
            content = str(payload.get("content") or "").strip()
            if not content:
                raise ValueError("Write a message before sending it.")
            if len(content) > 2_000:
                raise ValueError("Normal messages must be 2,000 characters or fewer.")
            if reply_target:
                await reply_target.reply(content=content, allowed_mentions=allowed_mentions)
            else:
                await channel.send(content=content, allowed_mentions=allowed_mentions)
            return
        if message_type != "embed":
            raise ValueError("Choose either a normal message or an embed message.")
        if not channel_permissions.embed_links:
            raise ValueError("BirdBot needs Embed Links permission in that channel to send an embed.")
        title = str(payload.get("title") or "").strip()
        description = str(payload.get("description") or "").strip()
        if not description:
            raise ValueError("Write an embed description before sending it.")
        if len(title) > 256 or len(description) > 4_096:
            raise ValueError("The embed is longer than Discord allows.")
        embed = discord.Embed(
            title=title or None,
            description=description,
            colour=discord.Colour.from_rgb(255, 255, 255),
        )
        if reply_target:
            await reply_target.reply(embed=embed, allowed_mentions=allowed_mentions)
        else:
            await channel.send(embed=embed, allowed_mentions=allowed_mentions)

    async def run_dashboard_bot_profile(self, guild: discord.Guild, payload: dict[str, Any]) -> None:
        """Apply a nickname/avatar to BirdBot's member in one guild only."""
        bot_member = guild.me
        if bot_member is None:
            raise ValueError("BirdBot is not ready in this server.")
        if not bot_member.guild_permissions.change_nickname:
            raise ValueError("BirdBot needs Change Nickname permission to update its server profile.")
        nickname = str(payload.get("nickname") or "").strip()
        if len(nickname) > 32:
            raise ValueError("The bot nickname must be 32 characters or fewer.")
        avatar_action = str(payload.get("avatar_action") or "keep").casefold()
        if avatar_action not in {"keep", "set", "remove"}:
            raise ValueError("That avatar action is not available.")
        kwargs: dict[str, object] = {"nick": nickname or None}
        avatar_root = (store.path.parent / "bot-profile-avatars").resolve()
        avatar_path = str(payload.get("avatar_path") or "")
        avatar_file: Path | None = None
        if avatar_action == "set":
            try:
                candidate = Path(avatar_path).resolve()
                if candidate.parent != avatar_root or not candidate.is_file():
                    raise ValueError
                avatar_bytes = candidate.read_bytes()
                avatar_file = candidate
            except (OSError, ValueError) as error:
                raise ValueError("The uploaded avatar is no longer available. Upload it again.") from error
            if not avatar_bytes or len(avatar_bytes) > 8 * 1024 * 1024:
                raise ValueError("The uploaded avatar is invalid or too large.")
            kwargs["avatar"] = avatar_bytes
        elif avatar_action == "remove":
            kwargs["avatar"] = None
        try:
            updated = await bot_member.edit(**kwargs, reason="Updated from the BirdBot Control Panel")
        except TypeError as error:
            if avatar_file is not None:
                try:
                    avatar_file.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ValueError("Update discord.py on the bot host to enable server avatars.") from error
        except (discord.HTTPException, discord.Forbidden):
            if avatar_file is not None:
                try:
                    avatar_file.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        actual_member = updated or bot_member
        guild_avatar = getattr(actual_member, "guild_avatar", None)
        avatar_url = str(getattr(guild_avatar, "url", "")) or str(payload.get("previous_avatar_url") or "") or None
        saved_avatar_path = avatar_path if avatar_action == "set" else None
        store.save_bot_profile(
            str(guild.id),
            nickname,
            saved_avatar_path,
            avatar_url,
            str(payload.get("updated_by") or "") or None,
        )
        previous_path = str(payload.get("previous_avatar_path") or "")
        if previous_path and previous_path != saved_avatar_path:
            try:
                previous = Path(previous_path).resolve()
                if previous.parent == avatar_root and previous.is_file():
                    previous.unlink()
            except OSError:
                pass

    async def run_dashboard_dm_message(self, guild: discord.Guild, payload: dict[str, Any]) -> dict[str, object]:
        """Send a private message to one member or all human server members."""
        recipient_mode = str(payload.get("recipient_mode") or "member").strip().casefold()
        if recipient_mode not in {"member", "everyone"}:
            raise ValueError("Choose one member or everyone in the server.")
        target: discord.Member | None = None
        if recipient_mode == "member":
            member_id = str(payload.get("member_id") or "")
            if not member_id.isdigit() or not 17 <= len(member_id) <= 20:
                raise ValueError("Choose a valid server member.")
            target = await resolve_guild_member(guild, member_id, attempts=1, timeout_seconds=4)
            if target is None:
                raise ValueError("That member is no longer in this server.")
            if target.bot:
                raise ValueError("Private messages can only be sent to human server members.")
        raw_mentions = payload.get("mention_user_ids", [])
        if not isinstance(raw_mentions, list) or len(raw_mentions) > 25:
            raise ValueError("Choose up to 25 members to mention.")
        mention_members: list[discord.Member] = []
        for mention_id in raw_mentions:
            mention_member = await resolve_guild_member(guild, str(mention_id), attempts=1, timeout_seconds=4)
            if mention_member is None:
                raise ValueError("One of the selected mention members is no longer in this server.")
            mention_members.append(mention_member)
        allowed_mentions = discord.AllowedMentions(
            users=mention_members,
            roles=False,
            everyone=False,
            replied_user=False,
        )
        media_path = str(payload.get("media_path") or "")
        media_bytes: bytes | None = None
        media_filename = str(payload.get("media_filename") or "attachment")
        media_content_type = str(payload.get("media_content_type") or "").casefold()
        media_root = (store.path.parent / "dm-media").resolve()
        message_type = str(payload.get("message_type") or "normal").casefold()
        if message_type not in {"normal", "embed"}:
            raise ValueError("Choose either a normal message or an embed message.")
        content = str(payload.get("content") or "").strip()
        title = str(payload.get("title") or "").strip()
        description = str(payload.get("description") or "").strip()
        if message_type == "normal" and not content:
            raise ValueError("Write a private message before sending it.")
        if message_type == "embed" and not description:
            raise ValueError("Write an embed description before sending it.")

        try:
            if media_path:
                try:
                    candidate = Path(media_path).resolve()
                    if candidate.parent != media_root or not candidate.is_file():
                        raise ValueError
                    if candidate.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError
                    media_bytes = candidate.read_bytes()
                    media_filename = str(payload.get("media_filename") or candidate.name)
                except (OSError, ValueError) as error:
                    raise ValueError("The uploaded attachment is no longer available. Upload it again.") from error

            async def send_to_member(member: discord.Member) -> None:
                file: discord.File | None = None
                try:
                    if media_bytes is not None:
                        file = discord.File(io.BytesIO(media_bytes), filename=media_filename)
                    if message_type == "normal":
                        kwargs: dict[str, object] = {"content": content, "allowed_mentions": allowed_mentions}
                        if file is not None:
                            kwargs["file"] = file
                        await member.send(**kwargs)
                    else:
                        embed = discord.Embed(
                            title=title or None,
                            description=description,
                            colour=discord.Colour.from_rgb(255, 255, 255),
                        )
                        if file is not None and media_content_type.startswith("image/"):
                            embed.set_image(url=f"attachment://{media_filename}")
                        kwargs = {"embed": embed, "allowed_mentions": allowed_mentions}
                        if file is not None:
                            kwargs["file"] = file
                        await member.send(**kwargs)
                finally:
                    if file is not None:
                        file.close()

            if recipient_mode == "member":
                assert target is not None
                await send_to_member(target)
                return {"recipient_mode": "member", "total": 1, "delivered": 1, "failed": 0, "skipped_bots": 0}

            cached_members = {member.id: member for member in guild.members}
            expected_members = int(guild.member_count or 0)
            if expected_members and len(cached_members) < expected_members:
                try:
                    async for fetched_member in guild.fetch_members(limit=None):
                        cached_members[fetched_member.id] = fetched_member
                except (discord.Forbidden, discord.HTTPException) as error:
                    if len(cached_members) < expected_members:
                        raise ValueError("BirdBot could not load the complete server member list. Enable the Server Members Intent and try again.") from error
            recipients = [member for member in cached_members.values() if not member.bot]
            skipped_bots = len(cached_members) - len(recipients)
            semaphore = asyncio.Semaphore(8)

            async def deliver(member: discord.Member) -> bool:
                async with semaphore:
                    try:
                        await asyncio.wait_for(send_to_member(member), timeout=15)
                        return True
                    except (asyncio.TimeoutError, discord.DiscordException, OSError):
                        return False

            outcomes = await asyncio.gather(*(deliver(member) for member in recipients))
            delivered = sum(1 for outcome in outcomes if outcome)
            return {
                "recipient_mode": "everyone",
                "total": len(recipients),
                "delivered": delivered,
                "failed": len(recipients) - delivered,
                "skipped_bots": skipped_bots,
            }
        finally:
            if media_path:
                try:
                    candidate = Path(media_path).resolve()
                    if candidate.parent == media_root and candidate.is_file():
                        candidate.unlink()
                except OSError:
                    pass

    async def run_dashboard_role_command(
        self,
        guild: discord.Guild,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        """Create, edit, or delete a guild role requested by the dashboard."""
        bot_member = guild.me
        if bot_member is None:
            raise ValueError("BirdBot is not ready in this server.")
        if not bot_member.guild_permissions.manage_roles:
            raise ValueError("BirdBot needs Manage Roles permission to manage server roles.")
        action = str(action or "").strip().lower()
        if action not in {"create", "edit", "delete", "permissions"}:
            raise ValueError("That role action is not available.")

        role: discord.Role | None = None
        if action in {"edit", "delete", "permissions"}:
            role_id = str(payload.get("role_id") or "")
            if not role_id.isdigit() or not 17 <= len(role_id) <= 20:
                raise ValueError("The role identifier is invalid.")
            role = guild.get_role(int(role_id))
            if role is None:
                raise ValueError("That role is no longer available.")
            if role.is_default():
                raise ValueError("The @everyone role cannot be changed or deleted.")
            if role.managed:
                raise ValueError("Managed integration roles cannot be changed or deleted.")
            if role.position >= bot_member.top_role.position:
                raise ValueError("BirdBot's role must be above the selected role.")

        if action in {"create", "edit"}:
            name = str(payload.get("name") or "").strip()
            color_value = str(payload.get("color") or "").strip()
            if not 1 <= len(name) <= 100 or name.casefold() == "@everyone":
                raise ValueError("Role names must be 1–100 characters and cannot be @everyone.")
            if not re.fullmatch(r"#?[0-9a-fA-F]{6}", color_value):
                raise ValueError("Choose a valid six-digit hexadecimal color.")
            colour = discord.Colour(int(color_value.lstrip("#"), 16))
            if action == "create":
                existing = next((candidate for candidate in guild.roles if candidate.name.casefold() == name.casefold()), None)
                if existing is not None:
                    raise ValueError("A role with that name already exists.")
                await guild.create_role(name=name, colour=colour, reason="Created from the BirdBot Control Panel")
            else:
                assert role is not None
                edit_kwargs: dict[str, Any] = {"name": name, "colour": colour}
                if "permissions" in payload:
                    raw_permissions = payload.get("permissions")
                    if not isinstance(raw_permissions, dict):
                        raise ValueError("Role permissions must be an object.")
                    permissions = discord.Permissions.none()
                    for permission_name in ROLE_PERMISSION_KEYS:
                        setattr(permissions, permission_name, bool(raw_permissions.get(permission_name, False)))
                    edit_kwargs["permissions"] = permissions
                await role.edit(**edit_kwargs, reason="Updated from the BirdBot Control Panel")
        elif action == "permissions":
            assert role is not None
            raw_permissions = payload.get("permissions")
            if not isinstance(raw_permissions, dict):
                raise ValueError("Role permissions must be an object.")
            permissions = discord.Permissions.none()
            for permission_name in ROLE_PERMISSION_KEYS:
                setattr(permissions, permission_name, bool(raw_permissions.get(permission_name, False)))
            await role.edit(permissions=permissions, reason="Updated role permissions from the BirdBot Control Panel")
        else:
            assert role is not None
            await role.delete(reason="Deleted from the BirdBot Control Panel")

        # Keep the management page in sync immediately; the normal heartbeat
        # will still reconcile every guild later as a safety net.
        try:
            store.sync_bot_roles(guild)
        except Exception as error:
            print(f"Role snapshot refresh failed for {guild.id}: {error}")

    async def _set_channel_lock(self, channel: discord.TextChannel, locked: bool, language: str = "en") -> None:
        bot_member = channel.guild.me
        if not bot_member:
            raise ValueError("BirdBot is not ready in this server.")
        if not bot_member.guild_permissions.manage_channels:
            raise ValueError(command_message("lock" if locked else "unlock", language, "dashboard_permission"))
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = False if locked else None
        await channel.set_permissions(
            channel.guild.default_role,
            overwrite=overwrite,
            reason="Locked from BirdBot" if locked else "Unlocked from BirdBot",
        )

    async def _delete_channel_messages(self, channel: discord.TextChannel, amount: int, language: str = "en") -> int:
        bot_member = channel.guild.me
        if not bot_member:
            raise ValueError("BirdBot is not ready in this server.")
        permissions = channel.permissions_for(bot_member)
        if not permissions.manage_messages:
            raise ValueError(command_message("delete", language, "dashboard_permission"))
        if not permissions.read_message_history:
            raise ValueError(command_message("delete", language, "history_permission"))
        try:
            deleted = await channel.purge(limit=amount, bulk=True, reason="Messages deleted from BirdBot")
        except discord.Forbidden as error:
            raise ValueError(command_message("delete", language, "dashboard_permission")) from error
        except discord.HTTPException as error:
            raise ValueError(command_message("delete", language, "failed")) from error
        return len(deleted)

    async def run_dashboard_command(self, guild: discord.Guild, channel: discord.TextChannel, name: str, requested_by: str, payload: dict[str, Any]) -> None:
        if name in {"ping", "server", "profile", "kick", "ban", "warning", "unwarning", "show_warning", "timeout", "lock", "unlock", "delete"} and not store.command_config(str(guild.id), name).get("enabled", True):
            raise ValueError("That command is disabled for this server. Enable it from the website Commands tab first.")
        bot_member = guild.me
        if not bot_member:
            raise ValueError("BirdBot is not ready in this server.")
        channel_permissions = channel.permissions_for(bot_member)
        # Locking a channel intentionally removes Send Messages for
        # @everyone, so an administrator must still be able to queue
        # /unlock afterwards. Manage Channels is sufficient for the lock
        # operations themselves; delete similarly only needs moderation
        # permissions to remove messages.
        requires_send_messages = name not in {"lock", "unlock", "delete"}
        if not channel_permissions.view_channel or (requires_send_messages and not channel_permissions.send_messages):
            raise ValueError("BirdBot cannot access that channel. Grant it View Channel and Send Messages permissions.")
        if name in {"server", "profile", "ticket_post"} and not channel_permissions.embed_links:
            raise ValueError("BirdBot needs Embed Links permission in that channel to send this panel.")
        language = str(store.command_config(str(guild.id), name).get("language") or "en")
        if name == "ping":
            await self.send_dashboard_ping(channel, requested_by, language)
            return
        if name == "server":
            await channel.send(embed=server_embed(guild, language))
            return
        if name == "ticket_post":
            raw_options = payload.get("options")
            options = [option for option in raw_options if isinstance(option, dict)] if isinstance(raw_options, list) else []
            if not options:
                raise ValueError("Add at least one ticket option before posting the panel.")
            support_role_ids = payload.get("support_role_ids")
            if not isinstance(support_role_ids, list):
                support_role_ids = []
            icon_file = None
            embed_config = dict(payload)
            icon_path = str(payload.get("custom_icon_path") or "")
            if icon_path:
                candidate = Path(icon_path)
                if candidate.is_file():
                    icon_file = discord.File(str(candidate), filename=candidate.name)
                    embed_config["custom_icon_url"] = f"attachment://{candidate.name}"
            await channel.send(
                embed=ticket_panel_embed(guild, embed_config),
                view=TicketPanelView(
                    str(guild.id),
                    str(payload.get("category_id")) if payload.get("category_id") else None,
                    [str(role_id) for role_id in support_role_ids],
                    str(payload.get("priority") or "medium"),
                    bool(payload.get("require_description")),
                    str(payload.get("description_prompt") or "Please describe your request."),
                    str(payload.get("log_channel_id")) if payload.get("log_channel_id") else None,
                    options,
                    max_open_tickets=payload.get("max_open_tickets") or 1,
                    panel_layout=str(payload.get("panel_layout") or "select_menu"),
                ),
                file=icon_file,
            )
            return
        if name in {"ticket_claim", "ticket_close"}:
            ticket_id = str(payload.get("ticket_id") or "")
            actor = await resolve_guild_member(guild, requested_by)
            if not actor:
                raise ValueError("Your server membership is still syncing. Wait a moment, then try again.")
            if name == "ticket_claim":
                await self.claim_ticket_channel(guild, ticket_id, actor)
            else:
                await self.close_ticket_channel(guild, ticket_id, actor)
            return
        if name in {"ticket_add_member", "ticket_remove_member"}:
            ticket_id = str(payload.get("ticket_id") or "")
            member_id = str(payload.get("member_id") or "")
            if not member_id.isdigit():
                raise ValueError("Choose a valid server member.")
            target = await resolve_guild_member(guild, member_id)
            actor = await resolve_guild_member(guild, requested_by)
            if not target or not actor:
                raise ValueError("That member could not be loaded from Discord. Search again and retry in a moment.")
            await self.ticket_member_action(guild, ticket_id, actor, target, name == "ticket_add_member")
            return

        if name in {"ticket_claim_legacy", "ticket_close_legacy"}:
            ticket_id = str(payload.get("ticket_id") or "")
            ticket = store.ticket(str(guild.id), ticket_id)
            if not ticket:
                raise ValueError("That ticket could not be found.")
            if ticket.get("status") == "closed":
                raise ValueError("That ticket is already closed.")
            ticket_channel = guild.get_channel(int(str(ticket["channel_id"])))
            if not isinstance(ticket_channel, discord.TextChannel):
                raise ValueError("The ticket channel is no longer available.")
            if name == "ticket_claim_legacy":
                staff = await resolve_guild_member(guild, requested_by)
                if not staff:
                    raise ValueError("Your server membership is still syncing. Wait a moment, then try again.")
                if not bot_member.guild_permissions.manage_channels:
                    raise ValueError("BirdBot needs Manage Channels permission to claim tickets.")
                overwrite = ticket_channel.overwrites_for(staff)
                overwrite.view_channel = True
                overwrite.send_messages = True
                overwrite.read_message_history = True
                await ticket_channel.set_permissions(staff, overwrite=overwrite, reason=f"Ticket claimed by {staff}")
                updated = store.claim_ticket(str(guild.id), ticket_id, requested_by, staff.display_name)
                if updated and updated.get("status") == "claimed":
                    await ticket_channel.send(f"This ticket has been claimed by {staff.mention}.")
                    await send_ticket_log(
                        guild,
                        str(store.ticket_config(str(guild.id)).get("log_channel_id") or "") or None,
                        "Ticket claimed",
                        f"{ticket_channel.mention} was claimed by {staff.mention}.",
                    )
                return
            actor = await resolve_guild_member(guild, requested_by)
            if not actor:
                raise ValueError("Your server membership is still syncing. Wait a moment, then try again.")
            await self.close_ticket_channel(guild, ticket_id, actor, source="dashboard")
            return
        if name == "profile":
            member = await resolve_guild_member(guild, str(payload.get("member_id") or ""))
            if not member:
                raise ValueError("That member could not be loaded from Discord. Search again and retry in a moment.")
            await channel.send(embed=profile_embed(member, language))
            return
        if name == "timeout":
            if not bot_member.guild_permissions.moderate_members:
                raise ValueError(command_message("timeout", language, "dashboard_permission"))
            member = await resolve_guild_member(guild, str(payload.get("member_id") or ""))
            if not member:
                raise ValueError("That member could not be loaded from Discord. Search again and retry in a moment.")
            problem = self.moderation_problem(guild, member)
            if problem:
                raise ValueError(problem)
            duration = payload.get("duration_minutes", 10)
            try:
                duration = int(duration)
            except (TypeError, ValueError) as error:
                raise ValueError(command_message("timeout", language, "invalid_duration")) from error
            if not 1 <= duration <= 40_320:
                raise ValueError(command_message("timeout", language, "invalid_duration"))
            supplied_reason = str(payload.get("reason") or "").strip()
            reason = (supplied_reason or f"Dashboard action by {requested_by}")[:512]
            display_reason = supplied_reason[:512] or command_message("timeout", language, "no_reason")
            moderator = await resolve_guild_member(guild, str(requested_by), attempts=1, timeout_seconds=4)
            await member.timeout(timedelta(minutes=duration), reason=reason)
            await self.write_log(
                guild,
                "moderation_timeout",
                actor=moderator,
                target=member,
                details=f"Duration: {duration} minute(s).\nReason: {display_reason}",
            )
            await channel.send(command_message("timeout", language, "success", member=member.mention, duration=duration, reason=display_reason), allowed_mentions=discord.AllowedMentions(users=[member]))
            return
        if name in {"lock", "unlock"}:
            await self._set_channel_lock(channel, name == "lock", language)
            try:
                await channel.send(command_message(name, language, "success"))
            except discord.Forbidden:
                # A strict @everyone deny can also deny the bot's inherited
                # Send Messages permission. The lock operation itself already
                # succeeded, so do not report the queued dashboard action as a
                # failure merely because its confirmation cannot be posted.
                pass
            return
        if name == "delete":
            raw_amount = payload.get("amount", 10)
            try:
                amount = int(raw_amount)
            except (TypeError, ValueError) as error:
                raise ValueError(command_message("delete", language, "invalid_amount")) from error
            if not 1 <= amount <= 100:
                raise ValueError(command_message("delete", language, "invalid_amount"))
            deleted_count = await self._delete_channel_messages(channel, amount, language)
            try:
                await channel.send(command_message("delete", language, "success", count=deleted_count))
            except discord.Forbidden:
                # Deletion can succeed in a channel where the bot has
                # moderation permissions but no Send Messages permission.
                pass
            return
        if name == "warning":
            if not bot_member.guild_permissions.moderate_members:
                raise ValueError(command_message("warning", language, "dashboard_permission"))
            member = await resolve_guild_member(guild, str(payload.get("member_id") or ""))
            moderator = await resolve_guild_member(guild, str(requested_by))
            if not member or not moderator:
                raise ValueError("That member could not be loaded from Discord. Search again and retry in a moment.")
            problem = await self._warning_member_problem(guild, member)
            if problem:
                raise ValueError(problem)
            warning, dm_delivered, reason = await self._issue_warning(guild, member, moderator, payload.get("reason"), language)
            message = command_message(
                "warning",
                language,
                "success",
                member=member.mention,
                number=warning["warning_id"],
                reason=reason,
            )
            if not dm_delivered:
                message += command_message("warning", language, "dm_failed")
            await channel.send(message, allowed_mentions=discord.AllowedMentions(users=[member]))
            return
        if name == "unwarning":
            if not bot_member.guild_permissions.moderate_members:
                raise ValueError(command_message("unwarning", language, "dashboard_permission"))
            raw_warning_id = str(payload.get("warning_id") or "").strip()
            if not raw_warning_id.isdigit() or int(raw_warning_id) < 1:
                raise ValueError(command_message("unwarning", language, "invalid_number"))
            moderator = await resolve_guild_member(guild, str(requested_by))
            if not moderator:
                raise ValueError("Your server membership is still syncing. Wait a moment, then try again.")
            warning = store.remove_warning(str(guild.id), int(raw_warning_id), str(moderator.id), moderator.display_name)
            if not warning:
                raise ValueError(command_message("unwarning", language, "not_found", number=raw_warning_id))
            target = guild.get_member(int(str(warning.get("member_id") or "0")))
            await self.write_log(
                guild,
                "moderation_unwarning",
                actor=moderator,
                target=target or warning,
                details=f"Warning #{warning['warning_id']} removed.\nOriginal reason: {warning.get('reason') or 'No reason provided.'}",
            )
            member_label = target.mention if target else str(warning.get("member_name") or warning.get("member_id"))
            await channel.send(
                command_message("unwarning", language, "success", number=warning["warning_id"], member=member_label),
                allowed_mentions=discord.AllowedMentions(users=[target] if target else []),
            )
            return
        if name == "show_warning":
            if not bot_member.guild_permissions.moderate_members:
                raise ValueError(command_message("show_warning", language, "permission"))
            member = await resolve_guild_member(guild, str(payload.get("member_id") or ""))
            if not member:
                raise ValueError("That member could not be loaded from Discord. Search again and retry in a moment.")
            warnings = store.warnings_for_member(str(guild.id), str(member.id))
            await channel.send(embed=self._warning_embed(member, warnings, language))
            return
        if name == "unban":
            if not guild.me or not guild.me.guild_permissions.ban_members: raise ValueError("BirdBot needs the Ban Members permission.")
            user = await self.bot.fetch_user(int(str(payload["member_id"])))
            moderator = await resolve_guild_member(guild, str(requested_by), attempts=1, timeout_seconds=4)
            await guild.unban(user, reason=f"Dashboard action by {requested_by}")
            await self.write_log(guild, "moderation_unban", actor=moderator, target=user, details="Member unbanned from the server.")
            await channel.send(command_message("unban", language, "success", member=user.mention))
            await self.refresh_bans(guild)
            return
        member = await resolve_guild_member(guild, str(payload.get("member_id") or ""))
        if not member:
            raise ValueError("That member could not be loaded from Discord. Search again and retry in a moment.")
        problem = self.moderation_problem(guild, member)
        if problem: raise ValueError(problem)
        supplied_reason = str(payload.get("reason") or "").strip()
        reason = (supplied_reason or f"Dashboard action by {requested_by}")[:512]
        display_reason = supplied_reason[:512] or command_message(name, language, "no_reason")
        moderator = await resolve_guild_member(guild, str(requested_by), attempts=1, timeout_seconds=4)
        if name == "kick":
            if not guild.me or not guild.me.guild_permissions.kick_members: raise ValueError("BirdBot needs the Kick Members permission.")
            await member.kick(reason=reason)
            await self.write_log(guild, "moderation_kick", actor=moderator, target=member, details=f"Reason: {display_reason}")
            await channel.send(command_message("kick", language, "success", member=member.mention, reason=display_reason))
            return
        if name == "ban":
            if not guild.me or not guild.me.guild_permissions.ban_members: raise ValueError("BirdBot needs the Ban Members permission.")
            await member.ban(reason=reason, delete_message_seconds=int(payload.get("delete_message_seconds", 0)))
            await self.write_log(guild, "moderation_ban", actor=moderator, target=member, details=f"Reason: {display_reason}")
            await channel.send(command_message("ban", language, "success", member=member.mention, reason=display_reason))
            await self.refresh_bans(guild)
            return
        raise ValueError("That dashboard command is not available.")

    async def _ticket_from_context(self, ctx: commands.Context[commands.Bot]) -> dict[str, object] | None:
        if not ctx.guild:
            await ctx.send("Ticket commands can only be used in a server.")
            return None
        ticket = store.ticket(str(ctx.guild.id), str(ctx.channel.id))
        if not ticket:
            await ctx.send("This command must be used inside a BirdBot ticket channel.")
            return None
        return ticket

    @commands.group(name="ticket", invoke_without_command=True)
    async def ticket_prefix(self, ctx: commands.Context[commands.Bot]) -> None:
        prefix = str(store.command_settings(str(ctx.guild.id)).get("prefix") or "!") if ctx.guild else "!"
        await ctx.send(f"Use `{prefix}ticket add @member`, `{prefix}ticket remove @member`, or the ticket buttons inside a ticket channel.")

    @ticket_prefix.command(name="add")
    async def ticket_add_prefix(self, ctx: commands.Context[commands.Bot], member: discord.Member) -> None:
        ticket = await self._ticket_from_context(ctx)
        if not ticket or not ctx.guild:
            return
        try:
            await self.ticket_member_action(ctx.guild, str(ticket["ticket_id"]), ctx.author, member, True)
        except (ValueError, discord.Forbidden, discord.HTTPException) as error:
            await ctx.send(str(error))

    @ticket_prefix.command(name="remove")
    async def ticket_remove_prefix(self, ctx: commands.Context[commands.Bot], member: discord.Member) -> None:
        ticket = await self._ticket_from_context(ctx)
        if not ticket or not ctx.guild:
            return
        try:
            await self.ticket_member_action(ctx.guild, str(ticket["ticket_id"]), ctx.author, member, False)
        except (ValueError, discord.Forbidden, discord.HTTPException) as error:
            await ctx.send(str(error))

    ticket_group = app_commands.Group(name="ticket", description="Manage the current BirdBot ticket.")

    @ticket_group.command(name="add", description="Add a member to this ticket channel.")
    @app_commands.describe(user="Member who should be able to view this ticket")
    async def slash_ticket_add(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self.active_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return
        ticket = store.ticket(str(interaction.guild.id), str(interaction.channel_id))
        if not ticket:
            await interaction.followup.send("Use this command inside a BirdBot ticket channel.", ephemeral=True)
            return
        try:
            await self.ticket_member_action(interaction.guild, str(ticket["ticket_id"]), interaction.user, user, True)
        except (ValueError, discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await interaction.followup.send(f"{user.mention} was added to this ticket.", ephemeral=True)

    @ticket_group.command(name="remove", description="Remove a member from this ticket channel.")
    @app_commands.describe(user="Member who should lose access to this ticket")
    async def slash_ticket_remove(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self.active_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return
        ticket = store.ticket(str(interaction.guild.id), str(interaction.channel_id))
        if not ticket:
            await interaction.followup.send("Use this command inside a BirdBot ticket channel.", ephemeral=True)
            return
        try:
            await self.ticket_member_action(interaction.guild, str(ticket["ticket_id"]), interaction.user, user, False)
        except (ValueError, discord.Forbidden, discord.HTTPException) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        await interaction.followup.send(f"{user.mention} was removed from this ticket.", ephemeral=True)

    show_group = app_commands.Group(name="show", description="Show BirdBot moderation records.")

    @commands.group(name="show", invoke_without_command=True)
    async def show_prefix(self, ctx: commands.Context[commands.Bot]) -> None:
        prefix = str(store.command_settings(str(ctx.guild.id)).get("prefix") or "!") if ctx.guild else "!"
        await ctx.send(f"Use `{prefix}show warning @member` to view a member's active warnings.")

    @show_prefix.command(name="warning", aliases=("warnings",))
    @commands.has_guild_permissions(moderate_members=True)
    async def show_warning(self, ctx: commands.Context[commands.Bot], member: discord.Member) -> None:
        if not ctx.guild:
            return
        language = self._warning_language(ctx.guild, "show_warning")
        warnings = store.warnings_for_member(str(ctx.guild.id), str(member.id))
        await ctx.send(embed=self._warning_embed(member, warnings, language))

    @show_group.command(name="warning", description="Show a member's active warnings.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member whose warnings should be displayed")
    async def slash_show_warning(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not await self.active_interaction(interaction):
            return
        language = self._warning_language(interaction.guild, "show_warning")  # type: ignore[arg-type]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.followup.send(command_message("show_warning", language, "permission"), ephemeral=True)
            return
        warnings = store.warnings_for_member(str(interaction.guild.id), str(user.id))  # type: ignore[union-attr]
        await interaction.followup.send(embed=self._warning_embed(user, warnings, language), ephemeral=True)

    @show_group.command(name="warnings", description="Show a member's active warnings.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member whose warnings should be displayed")
    async def slash_show_warnings(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """Plural alias for the dashboard's /show warnings wording."""
        if not await self.active_interaction(interaction):
            return
        language = self._warning_language(interaction.guild, "show_warning")  # type: ignore[arg-type]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.followup.send(command_message("show_warning", language, "permission"), ephemeral=True)
            return
        warnings = store.warnings_for_member(str(interaction.guild.id), str(user.id))  # type: ignore[union-attr]
        await interaction.followup.send(embed=self._warning_embed(user, warnings, language), ephemeral=True)

    @commands.command(name="warning", aliases=("warn",))
    @commands.has_guild_permissions(moderate_members=True)
    async def warning(self, ctx: commands.Context[commands.Bot], member: discord.Member, *, reason: str | None = None) -> None:
        if not ctx.guild:
            return
        language = self._warning_language(ctx.guild, "warning")
        problem = await self._warning_member_problem(ctx.guild, member)
        if problem:
            await ctx.send(problem)
            return
        warning, dm_delivered, normalized_reason = await self._issue_warning(ctx.guild, member, ctx.author, reason, language)
        message = command_message("warning", language, "success", member=member.mention, number=warning["warning_id"], reason=normalized_reason)
        if not dm_delivered:
            message += command_message("warning", language, "dm_failed")
        await ctx.send(message, allowed_mentions=discord.AllowedMentions(users=[member]))

    @commands.command(name="unwarning", aliases=("unwarn",))
    @commands.has_guild_permissions(moderate_members=True)
    async def unwarning(self, ctx: commands.Context[commands.Bot], warning_number: int) -> None:
        if not ctx.guild:
            return
        language = self._warning_language(ctx.guild, "unwarning")
        if warning_number < 1:
            await ctx.send(command_message("unwarning", language, "invalid_number"))
            return
        warning = store.remove_warning(str(ctx.guild.id), warning_number, str(ctx.author.id), ctx.author.display_name)
        if not warning:
            await ctx.send(command_message("unwarning", language, "not_found", number=warning_number))
            return
        target = ctx.guild.get_member(int(str(warning.get("member_id") or "0")))
        await self.write_log(
            ctx.guild,
            "moderation_unwarning",
            actor=ctx.author,
            target=target or warning,
            details=f"Warning #{warning['warning_id']} removed.\nOriginal reason: {warning.get('reason') or 'No reason provided.'}",
        )
        member_label = target.mention if target else str(warning.get("member_name") or warning.get("member_id"))
        await ctx.send(
            command_message("unwarning", language, "success", number=warning["warning_id"], member=member_label),
            allowed_mentions=discord.AllowedMentions(users=[target] if target else []),
        )

    @app_commands.command(name="warning", description="Give a numbered warning to a server member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member who should receive the warning", reason="Why the warning is being issued")
    async def slash_warning(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None) -> None:
        if not await self.active_interaction(interaction):
            return
        language = self._warning_language(interaction.guild, "warning")  # type: ignore[arg-type]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.followup.send(command_message("warning", language, "permission"), ephemeral=True)
            return
        problem = await self._warning_member_problem(interaction.guild, user)  # type: ignore[arg-type]
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        warning, dm_delivered, normalized_reason = await self._issue_warning(interaction.guild, user, interaction.user, reason, language)  # type: ignore[arg-type]
        message = command_message("warning", language, "success", member=user.mention, number=warning["warning_id"], reason=normalized_reason)
        if not dm_delivered:
            message += command_message("warning", language, "dm_failed")
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions(users=[user]))

    @app_commands.command(name="unwarning", description="Remove an active warning by its number.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(warning_number="The unique warning number to remove")
    async def slash_unwarning(self, interaction: discord.Interaction, warning_number: app_commands.Range[int, 1, 2_147_483_647]) -> None:
        if not await self.active_interaction(interaction):
            return
        language = self._warning_language(interaction.guild, "unwarning")  # type: ignore[arg-type]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.followup.send(command_message("unwarning", language, "permission"), ephemeral=True)
            return
        warning = store.remove_warning(str(interaction.guild.id), int(warning_number), str(interaction.user.id), interaction.user.display_name)  # type: ignore[union-attr]
        if not warning:
            await interaction.followup.send(command_message("unwarning", language, "not_found", number=warning_number), ephemeral=True)
            return
        target = interaction.guild.get_member(int(str(warning.get("member_id") or "0")))  # type: ignore[union-attr]
        await self.write_log(
            interaction.guild,
            "moderation_unwarning",
            actor=interaction.user,
            target=target or warning,
            details=f"Warning #{warning['warning_id']} removed.\nOriginal reason: {warning.get('reason') or 'No reason provided.'}",
        )
        member_label = target.mention if target else str(warning.get("member_name") or warning.get("member_id"))
        await interaction.followup.send(command_message("unwarning", language, "success", number=warning["warning_id"], member=member_label), ephemeral=True, allowed_mentions=discord.AllowedMentions(users=[target] if target else []))

    @commands.command(name="ping", aliases=("p",))
    async def ping(self, ctx: commands.Context[commands.Bot]) -> None:
        language = str(store.command_config(str(ctx.guild.id), "ping").get("language") or "en")  # type: ignore[union-attr]
        await self.send_dashboard_ping(ctx.channel, ctx.author.display_name, language)  # type: ignore[arg-type]

    @commands.command(name="server")
    async def server(self, ctx: commands.Context[commands.Bot]) -> None:
        language = str(store.command_config(str(ctx.guild.id), "server").get("language") or "en")  # type: ignore[union-attr]
        await ctx.send(embed=server_embed(ctx.guild, language))  # type: ignore[arg-type]

    @commands.command(name="profile")
    async def profile(self, ctx: commands.Context[commands.Bot], member: discord.Member | None = None) -> None:
        language = str(store.command_config(str(ctx.guild.id), "profile").get("language") or "en")  # type: ignore[union-attr]
        await ctx.send(embed=profile_embed(member or ctx.author, language))  # type: ignore[arg-type]

    @commands.command(name="kick")
    @commands.has_guild_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context[commands.Bot], member: discord.Member, *, reason: str | None = None) -> None:
        problem = self.moderation_problem(ctx.guild, member)  # type: ignore[arg-type]
        if problem: await ctx.send(problem); return
        await member.kick(reason=reason or f"Requested by {ctx.author}")
        language = str(store.command_config(str(ctx.guild.id), "kick").get("language") or "en")  # type: ignore[union-attr]
        await self.write_log(ctx.guild, "moderation_kick", actor=ctx.author, target=member, details=f"Reason: {reason or command_message('kick', language, 'no_reason')}")
        await ctx.send(command_message("kick", language, "success", member=member.mention, reason=reason or command_message("kick", language, "no_reason")))

    @commands.command(name="ban")
    @commands.has_guild_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context[commands.Bot], member: discord.Member, delete_message_days: commands.Range[int, 0, 7] = 0, *, reason: str | None = None) -> None:
        problem = self.moderation_problem(ctx.guild, member)  # type: ignore[arg-type]
        if problem: await ctx.send(problem); return
        await member.ban(reason=reason or f"Requested by {ctx.author}", delete_message_seconds=delete_message_days * 86_400)
        language = str(store.command_config(str(ctx.guild.id), "ban").get("language") or "en")  # type: ignore[union-attr]
        await self.write_log(ctx.guild, "moderation_ban", actor=ctx.author, target=member, details=f"Reason: {reason or command_message('ban', language, 'no_reason')}")
        await ctx.send(command_message("ban", language, "success", member=member.mention, reason=reason or command_message("ban", language, "no_reason")))

    @commands.command(name="timeout")
    @commands.has_guild_permissions(moderate_members=True)
    async def timeout(self, ctx: commands.Context[commands.Bot], member: discord.Member, duration_minutes: commands.Range[int, 1, 40_320], *, reason: str | None = None) -> None:
        if not ctx.guild:
            return
        language = str(store.command_config(str(ctx.guild.id), "timeout").get("language") or "en")
        problem = self.moderation_problem(ctx.guild, member)
        if problem:
            await ctx.send(problem)
            return
        if not ctx.guild.me or not ctx.guild.me.guild_permissions.moderate_members:
            await ctx.send(command_message("timeout", language, "dashboard_permission"))
            return
        await member.timeout(timedelta(minutes=int(duration_minutes)), reason=reason or f"Requested by {ctx.author}")
        await self.write_log(ctx.guild, "moderation_timeout", actor=ctx.author, target=member, details=f"Duration: {duration_minutes} minute(s).\nReason: {reason or command_message('timeout', language, 'no_reason')}")
        await ctx.send(command_message("timeout", language, "success", member=member.mention, duration=duration_minutes, reason=reason or command_message("timeout", language, "no_reason")), allowed_mentions=discord.AllowedMentions(users=[member]))

    @commands.command(name="lock")
    @commands.has_guild_permissions(manage_channels=True)
    async def lock(self, ctx: commands.Context[commands.Bot]) -> None:
        if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
            return
        language = str(store.command_config(str(ctx.guild.id), "lock").get("language") or "en")
        await self._set_channel_lock(ctx.channel, True, language)
        try:
            await ctx.send(command_message("lock", language, "success"))
        except discord.Forbidden:
            pass

    @commands.command(name="unlock")
    @commands.has_guild_permissions(manage_channels=True)
    async def unlock(self, ctx: commands.Context[commands.Bot]) -> None:
        if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
            return
        language = str(store.command_config(str(ctx.guild.id), "unlock").get("language") or "en")
        await self._set_channel_lock(ctx.channel, False, language)
        await ctx.send(command_message("unlock", language, "success"))

    @commands.command(name="delete")
    @commands.has_guild_permissions(manage_messages=True)
    async def delete(self, ctx: commands.Context[commands.Bot], amount: commands.Range[int, 1, 100]) -> None:
        if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
            return
        language = str(store.command_config(str(ctx.guild.id), "delete").get("language") or "en")
        deleted_count = await self._delete_channel_messages(ctx.channel, int(amount), language)
        try:
            await ctx.send(command_message("delete", language, "success", count=deleted_count))
        except discord.Forbidden:
            pass

    @app_commands.command(name="ping", description="Check BirdBot's connection and uptime.")
    async def slash_ping(self, interaction: discord.Interaction) -> None:
        if await self.active_interaction(interaction):
            language = str(store.command_config(str(interaction.guild.id), "ping").get("language") or "en")  # type: ignore[union-attr]
            await interaction.response.send_message(embed=discord.Embed(title=command_message("ping", language, "title"), description=f"{command_message('ping', language, 'gateway')}: {round(self.bot.latency * 1000)}ms", colour=discord.Colour.from_rgb(255, 255, 255)))

    @app_commands.command(name="server", description="Show information about this server.")
    async def slash_server(self, interaction: discord.Interaction) -> None:
        if not await self.active_interaction(interaction): return
        await interaction.response.defer(thinking=True)
        language = str(store.command_config(str(interaction.guild.id), "server").get("language") or "en")  # type: ignore[union-attr]
        await interaction.followup.send(embed=server_embed(interaction.guild, language))  # type: ignore[arg-type]

    @app_commands.command(name="profile", description="Show a member's profile.")
    async def slash_profile(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if not await self.active_interaction(interaction): return
        await interaction.response.defer(thinking=True)
        language = str(store.command_config(str(interaction.guild.id), "profile").get("language") or "en")  # type: ignore[union-attr]
        await interaction.followup.send(embed=profile_embed(user or interaction.user, language))  # type: ignore[arg-type]

    @app_commands.command(name="kick", description="Kick a member from this server.")
    @app_commands.default_permissions(kick_members=True)
    async def slash_kick(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None) -> None:
        if not await self.active_interaction(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.kick_members:
            language = str(store.command_config(str(interaction.guild.id), "kick").get("language") or "en")  # type: ignore[union-attr]
            await interaction.followup.send(command_message("kick", language, "permission"), ephemeral=True); return
        problem = self.moderation_problem(interaction.guild, user)  # type: ignore[arg-type]
        if problem: await interaction.followup.send(problem, ephemeral=True); return
        await user.kick(reason=reason or f"Requested by {interaction.user}")
        language = str(store.command_config(str(interaction.guild.id), "kick").get("language") or "en")  # type: ignore[union-attr]
        await self.write_log(interaction.guild, "moderation_kick", actor=interaction.user, target=user, details=f"Reason: {reason or command_message('kick', language, 'no_reason')}")
        await interaction.followup.send(command_message("kick", language, "success", member=user.mention, reason=reason or command_message("kick", language, "no_reason")), ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member from this server.")
    @app_commands.default_permissions(ban_members=True)
    async def slash_ban(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None, delete_message_days: app_commands.Range[int, 0, 7] = 0) -> None:
        if not await self.active_interaction(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.ban_members:
            language = str(store.command_config(str(interaction.guild.id), "ban").get("language") or "en")  # type: ignore[union-attr]
            await interaction.followup.send(command_message("ban", language, "permission"), ephemeral=True); return
        problem = self.moderation_problem(interaction.guild, user)  # type: ignore[arg-type]
        if problem: await interaction.followup.send(problem, ephemeral=True); return
        await user.ban(reason=reason or f"Requested by {interaction.user}", delete_message_seconds=delete_message_days * 86_400)
        language = str(store.command_config(str(interaction.guild.id), "ban").get("language") or "en")  # type: ignore[union-attr]
        await self.write_log(interaction.guild, "moderation_ban", actor=interaction.user, target=user, details=f"Reason: {reason or command_message('ban', language, 'no_reason')}")
        await interaction.followup.send(command_message("ban", language, "success", member=user.mention, reason=reason or command_message("ban", language, "no_reason")), ephemeral=True)

    @app_commands.command(name="timeout", description="Temporarily timeout a server member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to timeout", duration_minutes="Timeout length in minutes (1-40320)", reason="Why the timeout is being applied")
    async def slash_timeout(self, interaction: discord.Interaction, user: discord.Member, duration_minutes: app_commands.Range[int, 1, 40_320], reason: str | None = None) -> None:
        if not await self.active_interaction(interaction):
            return
        language = str(store.command_config(str(interaction.guild.id), "timeout").get("language") or "en")  # type: ignore[union-attr]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.followup.send(command_message("timeout", language, "permission"), ephemeral=True)
            return
        problem = self.moderation_problem(interaction.guild, user)  # type: ignore[arg-type]
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        if not interaction.guild.me or not interaction.guild.me.guild_permissions.moderate_members:  # type: ignore[union-attr]
            await interaction.followup.send(command_message("timeout", language, "dashboard_permission"), ephemeral=True)
            return
        await user.timeout(timedelta(minutes=int(duration_minutes)), reason=reason or f"Requested by {interaction.user}")
        await self.write_log(interaction.guild, "moderation_timeout", actor=interaction.user, target=user, details=f"Duration: {duration_minutes} minute(s).\nReason: {reason or command_message('timeout', language, 'no_reason')}")
        await interaction.followup.send(command_message("timeout", language, "success", member=user.mention, duration=duration_minutes, reason=reason or command_message("timeout", language, "no_reason")), ephemeral=True, allowed_mentions=discord.AllowedMentions(users=[user]))

    @app_commands.command(name="lock", description="Lock this channel for @everyone.")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_lock(self, interaction: discord.Interaction) -> None:
        if not await self.active_interaction(interaction):
            return
        language = str(store.command_config(str(interaction.guild.id), "lock").get("language") or "en")  # type: ignore[union-attr]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.followup.send(command_message("lock", language, "permission"), ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(command_message("lock", language, "invalid_channel"), ephemeral=True)
            return
        await self._set_channel_lock(interaction.channel, True, language)
        await interaction.followup.send(command_message("lock", language, "success"), ephemeral=True)

    @app_commands.command(name="unlock", description="Unlock this channel for @everyone.")
    @app_commands.default_permissions(manage_channels=True)
    async def slash_unlock(self, interaction: discord.Interaction) -> None:
        if not await self.active_interaction(interaction):
            return
        language = str(store.command_config(str(interaction.guild.id), "unlock").get("language") or "en")  # type: ignore[union-attr]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.followup.send(command_message("unlock", language, "permission"), ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(command_message("unlock", language, "invalid_channel"), ephemeral=True)
            return
        await self._set_channel_lock(interaction.channel, False, language)
        await interaction.followup.send(command_message("unlock", language, "success"), ephemeral=True)

    @app_commands.command(name="delete", description="Delete recent messages from this channel.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(amount="Number of recent messages to delete (1-100)")
    async def slash_delete(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]) -> None:
        if not await self.active_interaction(interaction):
            return
        language = str(store.command_config(str(interaction.guild.id), "delete").get("language") or "en")  # type: ignore[union-attr]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.followup.send(command_message("delete", language, "permission"), ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(command_message("delete", language, "invalid_channel"), ephemeral=True)
            return
        deleted_count = await self._delete_channel_messages(interaction.channel, int(amount), language)
        await interaction.followup.send(command_message("delete", language, "success", count=deleted_count), ephemeral=True)

    @kick.error
    @ban.error
    async def moderation_error(self, ctx: commands.Context[commands.Bot], error: commands.CommandError) -> None:
        command_name = str(ctx.command.qualified_name if ctx.command else "").split(" ", 1)[0]
        arabic = bool(ctx.guild and store.command_config(str(ctx.guild.id), command_name).get("language") == "ar") if command_name else False
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(command_message("common", "ar" if arabic else "en", "permission"))
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(command_message(command_name, "ar" if arabic else "en", "missing_member"))
        else:
            raise error

    @timeout.error
    @lock.error
    @unlock.error
    @delete.error
    async def channel_moderation_error(self, ctx: commands.Context[commands.Bot], error: commands.CommandError) -> None:
        command_name = str(ctx.command.qualified_name if ctx.command else "")
        language = "ar" if ctx.guild and store.command_config(str(ctx.guild.id), command_name).get("language") == "ar" else "en"
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(command_message(command_name, language, "permission"))
        elif isinstance(error, commands.MissingRequiredArgument):
            key = "missing_member" if command_name == "timeout" and error.param.name == "member" else "missing_duration" if command_name == "timeout" else "missing_amount"
            await ctx.send(command_message(command_name, language, key))
        elif isinstance(error, commands.BadArgument):
            key = "invalid_member" if command_name == "timeout" else "invalid_duration" if command_name == "timeout" else "invalid_amount"
            await ctx.send(command_message(command_name, language, key))
        else:
            raise error

    @warning.error
    @unwarning.error
    @show_warning.error
    async def warning_error(self, ctx: commands.Context[commands.Bot], error: commands.CommandError) -> None:
        command_name = str(ctx.command.qualified_name if ctx.command else "").replace(" ", "_")
        language = "ar" if ctx.guild and store.command_config(str(ctx.guild.id), command_name).get("language") == "ar" else "en"
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(command_message(command_name, language, "permission"))
        elif isinstance(error, commands.MissingRequiredArgument):
            key = "missing_number" if command_name == "unwarning" else "missing_member"
            await ctx.send(command_message(command_name, language, key))
        elif isinstance(error, commands.BadArgument):
            key = "invalid_number" if command_name == "unwarning" else "missing_member"
            await ctx.send(command_message(command_name, language, key))
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
