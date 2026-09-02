"""Secure secondary Discord clients used for voice-channel presence.

The primary BirdBot and the dashboard never receive a token from a user.  A
host operator may configure up to five additional bot tokens as private
environment secrets (``VC_BOT_1_TOKEN`` ... ``VC_BOT_5_TOKEN``).  This manager
keeps those clients process-local and exposes only safe status information.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import discord


RECONNECT_SECONDS = 20


@dataclass
class _PresenceSlot:
    number: int
    token: str | None
    client: discord.Client | None = None
    task: asyncio.Task[None] | None = None
    error: str | None = None


class VCPresenceManager:
    """Own and reconcile optional voice-presence clients on one event loop."""

    def __init__(self, tokens: tuple[str | None, ...], store: Any) -> None:
        self.store = store
        self.slots = [_PresenceSlot(index, token) for index, token in enumerate(tokens, 1)]
        self._slot_locks = {slot.number: asyncio.Lock() for slot in self.slots}
        self._started = False
        self._closed = False

    def slot_configured(self, slot: int) -> bool:
        return 1 <= int(slot) <= len(self.slots) and bool(self.slots[int(slot) - 1].token)

    async def start(self) -> None:
        """Start all configured clients without blocking the primary bot."""
        if self._started:
            return
        self._started = True
        self._closed = False
        for slot in self.slots:
            if not slot.token:
                continue
            slot.task = asyncio.create_task(
                self._run_slot(slot),
                name=f"vc-presence-{slot.number}",
            )

    def _new_client(self, slot: _PresenceSlot) -> discord.Client:
        intents = discord.Intents.default()
        # Voice state events are needed for Discord's voice handshake;
        # message/member intents remain disabled for these presence bots.
        intents.voice_states = True
        client = discord.Client(intents=intents)
        slot.client = client

        @client.event
        async def on_ready(client: discord.Client = client, number: int = slot.number) -> None:
            await self._handle_ready(number, client)

        return client

    async def _run_slot(self, slot: _PresenceSlot) -> None:
        while not self._closed and slot.token:
            client = self._new_client(slot)
            try:
                await client.start(slot.token, reconnect=True)
                if not self._closed:
                    slot.error = "The voice bot connection ended; reconnecting."
            except asyncio.CancelledError:
                raise
            except discord.LoginFailure:
                # Never retry a bad secret in a tight loop.  The dashboard
                # reports this as a safe status and the host can rotate it.
                slot.error = "The host token was rejected by Discord."
                return
            except Exception as error:  # gateway/library errors vary by version
                slot.error = f"Connection failed: {type(error).__name__}."
            finally:
                if not client.is_closed():
                    await client.close()
            if not self._closed:
                await asyncio.sleep(RECONNECT_SECONDS)

    async def _handle_ready(self, number: int, client: discord.Client) -> None:
        slot = self.slots[number - 1]
        slot.error = None
        await self._reconcile_slot(slot)

    @staticmethod
    def _voice_client(client: discord.Client, guild_id: int) -> discord.VoiceClient | None:
        return next(
            (voice for voice in client.voice_clients if getattr(voice.guild, "id", None) == guild_id),
            None,
        )

    async def _voice_channel(self, guild: discord.Guild, channel_id: int) -> discord.VoiceChannel | discord.StageChannel:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException) as error:
                raise RuntimeError("The selected voice channel could not be fetched.") from error
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise RuntimeError("The selected channel is not a voice channel.")
        return channel

    async def _join_slot(
        self,
        slot: _PresenceSlot,
        guild_id: int,
        channel_id: int,
    ) -> None:
        client = slot.client
        if client is None or not client.is_ready():
            return
        async with self._slot_locks[slot.number]:
            guild = client.get_guild(guild_id)
            if guild is None:
                raise RuntimeError("This voice bot is not invited to the selected server.")
            channel = await self._voice_channel(guild, channel_id)
            existing = self._voice_client(client, guild_id)
            if existing and getattr(existing.channel, "id", None) == channel.id:
                return
            if existing:
                await existing.disconnect(force=True)
            await channel.connect(self_deaf=True)

    async def _leave_slot(self, slot: _PresenceSlot, guild_id: int) -> None:
        client = slot.client
        if client is None:
            return
        async with self._slot_locks[slot.number]:
            voice = self._voice_client(client, guild_id)
            if voice:
                await voice.disconnect(force=True)

    async def _reconcile_slot(self, slot: _PresenceSlot, guild_id: str | None = None) -> None:
        if not slot.token or slot.client is None or not slot.client.is_ready():
            return
        configs = (
            [self.store.vc_presence_config(guild_id, slot.number)]
            if guild_id is not None
            else self.store.vc_presence_configs_for_slot(slot.number)
        )
        for config in configs:
            if not config or not config.get("enabled") or not config.get("channel_id"):
                continue
            if not self.store.is_guild_activated(str(config.get("guild_id") or "")):
                continue
            try:
                await self._join_slot(slot, int(config["guild_id"]), int(config["channel_id"]))
            except (ValueError, TypeError, discord.ClientException, discord.Forbidden, discord.HTTPException, RuntimeError) as error:
                slot.error = str(error)[:240]

    async def reconcile_all(self) -> None:
        """Re-apply saved placements after the primary bot reconnects."""
        for slot in self.slots:
            if slot.client and slot.client.is_ready():
                await self._reconcile_slot(slot)

    async def apply_action(self, guild_id: int, payload: dict[str, object]) -> None:
        """Apply one persisted dashboard placement to its secondary client."""
        try:
            number = int(payload.get("slot", 0))
        except (TypeError, ValueError) as error:
            raise ValueError("The VC bot slot is invalid.") from error
        if not self.slot_configured(number):
            raise ValueError("This VC bot slot is not configured on the host.")
        slot = self.slots[number - 1]
        enabled = bool(payload.get("enabled"))
        if not enabled:
            await self._leave_slot(slot, guild_id)
            slot.error = None
            return
        try:
            channel_id = int(str(payload.get("channel_id") or "0"))
        except (TypeError, ValueError) as error:
            raise ValueError("Choose a voice channel for this VC bot.") from error
        if channel_id <= 0:
            raise ValueError("Choose a voice channel for this VC bot.")
        # A configured client may still be connecting.  The saved row is
        # reconciled from on_ready, so this request can complete safely now.
        if slot.client and slot.client.is_ready():
            try:
                await self._join_slot(slot, guild_id, channel_id)
                slot.error = None
            except (discord.ClientException, discord.Forbidden, discord.HTTPException, RuntimeError) as error:
                slot.error = str(error)[:240]
                raise

    def status_for_guild(self, guild_id: str) -> list[dict[str, object]]:
        """Return dashboard-safe state; tokens are deliberately absent."""
        configs = {
            int(config["slot"]): config
            for config in self.store.vc_presence_configs(guild_id)
            if str(config.get("slot", "")).isdigit()
        }
        result: list[dict[str, object]] = []
        for slot in self.slots:
            config = configs.get(slot.number, {})
            client = slot.client
            guild_id_int = int(guild_id) if str(guild_id).isdigit() else 0
            voice = self._voice_client(client, guild_id_int) if client and client.is_ready() else None
            channel = getattr(voice, "channel", None)
            result.append(
                {
                    "slot": slot.number,
                    "configured": bool(slot.token),
                    "online": bool(client and client.is_ready()),
                    "bot_name": str(client.user) if client and client.user else None,
                    "bot_id": str(client.user.id) if client and client.user else None,
                    "guild_count": len(client.guilds) if client and client.is_ready() else 0,
                    "enabled": bool(config.get("enabled")),
                    "channel_id": str(getattr(channel, "id", None) or config.get("channel_id") or "") or None,
                    "channel_name": getattr(channel, "name", None),
                    "connected": bool(voice),
                    "error": slot.error,
                }
            )
        return result

    async def close(self) -> None:
        if self._closed and not self._started:
            return
        self._closed = True
        for slot in self.slots:
            client = slot.client
            if client:
                for voice in list(client.voice_clients):
                    try:
                        await voice.disconnect(force=True)
                    except Exception:
                        pass
                if not client.is_closed():
                    await client.close()
        tasks = [slot.task for slot in self.slots if slot.task and not slot.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._started = False
