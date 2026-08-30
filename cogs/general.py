"""Activated-server commands for Discord and the web dashboard."""

from __future__ import annotations

import asyncio
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

from discord_members import resolve_guild_member
from settings import DASHBOARD_PUBLIC_URL
from storage import UNCLAIMED_TICKET_TIMEOUT_SECONDS, store


SUPPORT_ROLE_ERROR = "Error: You do not have the required Support Role to claim or close tickets."
DM_DELIVERED = "delivered"
DM_FAILED = "failed"
AUTO_TIMEOUT_EVENT = "Ticket Auto-Deleted (Unclaimed Timeout - 5 min)"


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


def server_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(title=f"{guild.name} server info", colour=discord.Colour.from_rgb(255, 255, 255))
    embed.add_field(name="Server ID", value=str(guild.id), inline=True)
    embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else "Unavailable", inline=True)
    embed.add_field(name="Members", value=str(guild.member_count or 0), inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "D"), inline=True)
    embed.add_field(name="Boost level", value=f"Level {int(guild.premium_tier)}", inline=True)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if guild.banner:
        embed.set_image(url=guild.banner.url)
    return embed


def profile_embed(member: discord.Member) -> discord.Embed:
    roles = [role.mention for role in member.roles[1:]]
    embed = discord.Embed(title=f"{member.display_name} profile", colour=discord.Colour.from_rgb(255, 255, 255))
    embed.add_field(name="User ID", value=str(member.id), inline=True)
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "D"), inline=True)
    embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "D") if member.joined_at else "Unavailable", inline=True)
    embed.add_field(name="Roles", value=" ".join(roles) if roles else "No roles", inline=False)
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
        return bool(ctx.guild and store.is_guild_activated(ctx.guild.id))

    async def active_interaction(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not store.is_guild_activated(interaction.guild.id):
            await interaction.response.send_message("This server has not enabled BirdBot yet.", ephemeral=True)
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

    async def send_dashboard_ping(self, channel: discord.TextChannel, requested_by: str) -> None:
        started = time.perf_counter()
        message = await channel.send("Checking connection...")
        embed = discord.Embed(title="Connection check", description="BirdBot is online and responding.", colour=discord.Colour.from_rgb(255, 255, 255))
        embed.add_field(name="Response", value=f"{round((time.perf_counter() - started) * 1000)}ms", inline=True)
        embed.add_field(name="Gateway", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.set_footer(text=f"Requested from dashboard by {requested_by}")
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
        if action not in {"create", "edit", "delete"}:
            raise ValueError("That role action is not available.")

        role: discord.Role | None = None
        if action in {"edit", "delete"}:
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
                await role.edit(name=name, colour=colour, reason="Updated from the BirdBot Control Panel")
        else:
            assert role is not None
            await role.delete(reason="Deleted from the BirdBot Control Panel")

        # Keep the management page in sync immediately; the normal heartbeat
        # will still reconcile every guild later as a safety net.
        try:
            store.sync_bot_roles(guild)
        except Exception as error:
            print(f"Role snapshot refresh failed for {guild.id}: {error}")

    async def run_dashboard_command(self, guild: discord.Guild, channel: discord.TextChannel, name: str, requested_by: str, payload: dict[str, Any]) -> None:
        bot_member = guild.me
        if not bot_member:
            raise ValueError("BirdBot is not ready in this server.")
        channel_permissions = channel.permissions_for(bot_member)
        if not channel_permissions.view_channel or not channel_permissions.send_messages:
            raise ValueError("BirdBot cannot access that channel. Grant it View Channel and Send Messages permissions.")
        if name in {"server", "profile", "ticket_post"} and not channel_permissions.embed_links:
            raise ValueError("BirdBot needs Embed Links permission in that channel to send this panel.")
        if name == "ping":
            await self.send_dashboard_ping(channel, requested_by)
            return
        if name == "server":
            await channel.send(embed=server_embed(guild))
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
            await channel.send(embed=profile_embed(member))
            return
        if name == "unban":
            if not guild.me or not guild.me.guild_permissions.ban_members: raise ValueError("BirdBot needs the Ban Members permission.")
            user = await self.bot.fetch_user(int(str(payload["member_id"])))
            await guild.unban(user, reason=f"Dashboard action by {requested_by}")
            await channel.send(f"{user.mention} was unbanned.")
            await self.refresh_bans(guild)
            return
        member = await resolve_guild_member(guild, str(payload.get("member_id") or ""))
        if not member:
            raise ValueError("That member could not be loaded from Discord. Search again and retry in a moment.")
        problem = self.moderation_problem(guild, member)
        if problem: raise ValueError(problem)
        reason = str(payload.get("reason") or f"Dashboard action by {requested_by}")[:512]
        if name == "kick":
            if not guild.me or not guild.me.guild_permissions.kick_members: raise ValueError("BirdBot needs the Kick Members permission.")
            await member.kick(reason=reason)
            await channel.send(f"{member.mention} has been Kicked from the server")
            return
        if name == "ban":
            if not guild.me or not guild.me.guild_permissions.ban_members: raise ValueError("BirdBot needs the Ban Members permission.")
            await member.ban(reason=reason, delete_message_seconds=int(payload.get("delete_message_seconds", 0)))
            await channel.send(f"{member.mention} has been Banned from the server")
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
        await ctx.send("Use `!ticket add @member`, `!ticket remove @member`, or the ticket buttons inside a ticket channel.")

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

    @commands.command(name="ping", aliases=("p",))
    async def ping(self, ctx: commands.Context[commands.Bot]) -> None:
        await self.send_dashboard_ping(ctx.channel, ctx.author.display_name)  # type: ignore[arg-type]

    @commands.command(name="server")
    async def server(self, ctx: commands.Context[commands.Bot]) -> None:
        await ctx.send(embed=server_embed(ctx.guild))  # type: ignore[arg-type]

    @commands.command(name="profile")
    async def profile(self, ctx: commands.Context[commands.Bot], member: discord.Member | None = None) -> None:
        await ctx.send(embed=profile_embed(member or ctx.author))  # type: ignore[arg-type]

    @commands.command(name="kick")
    @commands.has_guild_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context[commands.Bot], member: discord.Member, *, reason: str | None = None) -> None:
        problem = self.moderation_problem(ctx.guild, member)  # type: ignore[arg-type]
        if problem: await ctx.send(problem); return
        await member.kick(reason=reason or f"Requested by {ctx.author}")
        await ctx.send(f"({member.mention}) has been Kicked from the server")

    @commands.command(name="ban")
    @commands.has_guild_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context[commands.Bot], member: discord.Member, delete_message_days: commands.Range[int, 0, 7] = 0, *, reason: str | None = None) -> None:
        problem = self.moderation_problem(ctx.guild, member)  # type: ignore[arg-type]
        if problem: await ctx.send(problem); return
        await member.ban(reason=reason or f"Requested by {ctx.author}", delete_message_seconds=delete_message_days * 86_400)
        await ctx.send(f"{member.mention} has been Banned from the server")

    @app_commands.command(name="ping", description="Check BirdBot's connection and uptime.")
    async def slash_ping(self, interaction: discord.Interaction) -> None:
        if await self.active_interaction(interaction):
            await interaction.response.send_message(embed=discord.Embed(title="Connection check", description=f"Gateway: {round(self.bot.latency * 1000)}ms", colour=discord.Colour.from_rgb(255, 255, 255)))

    @app_commands.command(name="server", description="Show information about this server.")
    async def slash_server(self, interaction: discord.Interaction) -> None:
        if not await self.active_interaction(interaction): return
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(embed=server_embed(interaction.guild))  # type: ignore[arg-type]

    @app_commands.command(name="profile", description="Show a member's profile.")
    async def slash_profile(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if not await self.active_interaction(interaction): return
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(embed=profile_embed(user or interaction.user))  # type: ignore[arg-type]

    @app_commands.command(name="kick", description="Kick a member from this server.")
    @app_commands.default_permissions(kick_members=True)
    async def slash_kick(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None) -> None:
        if not await self.active_interaction(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.kick_members:
            await interaction.followup.send("You need Kick Members permission.", ephemeral=True); return
        problem = self.moderation_problem(interaction.guild, user)  # type: ignore[arg-type]
        if problem: await interaction.followup.send(problem, ephemeral=True); return
        await user.kick(reason=reason or f"Requested by {interaction.user}")
        await interaction.followup.send(f"({user.mention}) has been Kicked from the server", ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member from this server.")
    @app_commands.default_permissions(ban_members=True)
    async def slash_ban(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None, delete_message_days: app_commands.Range[int, 0, 7] = 0) -> None:
        if not await self.active_interaction(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.guild_permissions.ban_members:
            await interaction.followup.send("You need Ban Members permission.", ephemeral=True); return
        problem = self.moderation_problem(interaction.guild, user)  # type: ignore[arg-type]
        if problem: await interaction.followup.send(problem, ephemeral=True); return
        await user.ban(reason=reason or f"Requested by {interaction.user}", delete_message_seconds=delete_message_days * 86_400)
        await interaction.followup.send(f"{user.mention} has been Banned from the server", ephemeral=True)

    @kick.error
    @ban.error
    async def moderation_error(self, ctx: commands.Context[commands.Bot], error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You do not have permission to use that command.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Choose a member first.")
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
