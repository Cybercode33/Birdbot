"""Reliable Discord guild-member resolution shared by dashboard cogs.

The Discord cache is deliberately fast, but it can be incomplete for a short
time after reconnecting or when a member was not recently active.  Dashboard
actions must therefore fall back to Discord's member endpoint instead of
treating a cache miss as proof that the user left the server.
"""

from __future__ import annotations

import asyncio

import discord


async def resolve_guild_member(
    guild: discord.Guild,
    member_id: int | str,
    *,
    attempts: int = 2,
    timeout_seconds: float = 6.0,
) -> discord.Member | None:
    """Return a current member from cache or a bounded REST fallback.

    Only temporary transport/API failures are retried.  A confirmed missing
    member or an access denial returns ``None`` immediately, so callers can
    present a truthful, user-friendly error without hanging a queued action.
    """
    try:
        user_id = int(str(member_id))
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None

    cached = guild.get_member(user_id)
    if cached is not None:
        return cached

    retries = max(0, min(int(attempts), 3))
    for attempt in range(retries + 1):
        try:
            return await asyncio.wait_for(
                guild.fetch_member(user_id),
                timeout=max(1.0, float(timeout_seconds)),
            )
        except discord.NotFound:
            return None
        except discord.Forbidden:
            return None
        except (asyncio.TimeoutError, discord.HTTPException):
            if attempt >= retries:
                return None
            # Small bounded backoff handles gateway/API handovers without
            # turning a normal dashboard action into a long wait.
            await asyncio.sleep(0.25 * (attempt + 1))
        except (TypeError, ValueError):
            return None
    return None
