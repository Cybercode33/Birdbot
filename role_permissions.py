"""Shared role-permission fields used by the dashboard and Discord worker.

The keys intentionally mirror :class:`discord.Permissions` attributes. Keeping
the list in one small module means the website cannot queue arbitrary attribute
names and the bot applies exactly the same set of permissions it displays.
"""

from __future__ import annotations


ROLE_PERMISSION_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("view_channel", "View Channels", "See text, voice, and stage channels."),
    ("send_messages", "Send Messages", "Send messages in text channels."),
    ("embed_links", "Embed Links", "Show rich previews for links."),
    ("attach_files", "Attach Files", "Upload images and other files."),
    ("read_message_history", "Read Message History", "Read earlier messages."),
    ("add_reactions", "Add Reactions", "Add reactions to messages."),
    ("use_external_emojis", "Use External Emojis", "Use emojis from other servers."),
    ("send_messages_in_threads", "Send Messages in Threads", "Reply in threads."),
    ("create_public_threads", "Create Public Threads", "Start public threads."),
    ("create_private_threads", "Create Private Threads", "Start private threads."),
    ("manage_threads", "Manage Threads", "Rename, archive, or delete threads."),
    ("mention_everyone", "Mention Everyone", "Use @everyone and @here mentions."),
    ("manage_messages", "Manage Messages", "Delete or pin other members' messages."),
    ("manage_channels", "Manage Channels", "Create and edit channels."),
    ("manage_roles", "Manage Roles", "Create and edit roles below the bot."),
    ("manage_webhooks", "Manage Webhooks", "Create and manage webhooks."),
    ("view_audit_log", "View Audit Log", "See the server audit log."),
    ("kick_members", "Kick Members", "Remove members from the server."),
    ("ban_members", "Ban Members", "Ban members from the server."),
    ("moderate_members", "Moderate Members", "Timeout or otherwise moderate members."),
    ("change_nickname", "Change Nickname", "Change your own nickname."),
    ("manage_nicknames", "Manage Nicknames", "Change other members' nicknames."),
    ("connect", "Connect", "Join voice channels."),
    ("speak", "Speak", "Transmit audio in voice channels."),
    ("stream", "Video", "Share video or stream in voice channels."),
    ("use_voice_activation", "Use Voice Activity", "Use voice activation instead of push-to-talk."),
    ("mute_members", "Mute Members", "Mute members in voice channels."),
    ("deafen_members", "Deafen Members", "Deafen members in voice channels."),
    ("move_members", "Move Members", "Move members between voice channels."),
    ("use_application_commands", "Use App Commands", "Use slash commands and other app commands."),
    ("manage_events", "Manage Events", "Create and manage server events."),
    ("administrator", "Administrator", "Grant every permission. Use with care."),
)

ROLE_PERMISSION_KEYS: tuple[str, ...] = tuple(field[0] for field in ROLE_PERMISSION_FIELDS)
ROLE_PERMISSION_LABELS: dict[str, str] = {field[0]: field[1] for field in ROLE_PERMISSION_FIELDS}


def normalize_role_permissions(value: object) -> dict[str, bool]:
    """Return a complete, JSON-safe permission mapping.

    Unknown keys are rejected by the HTTP layer. This helper is deliberately
    tolerant of missing keys so older stored role snapshots remain usable.
    """

    if not isinstance(value, dict):
        return {key: False for key in ROLE_PERMISSION_KEYS}
    return {key: bool(value.get(key, False)) for key in ROLE_PERMISSION_KEYS}

