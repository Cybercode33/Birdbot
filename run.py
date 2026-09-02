"""Production entry point: one global Discord client and the web dashboard."""

from __future__ import annotations

import asyncio
import os

import aiohttp
import discord
import uvicorn

from birdbot import create_bot
from settings import DISCORD_TOKEN
from website.server import app

RETRY_SECONDS = 30


def _report_bot_task_failure(task: asyncio.Task[None]) -> None:
    """Make background bot startup failures visible without stopping FastAPI."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        print(
            "BirdBot background task stopped: "
            f"{type(error).__name__}: {error}"
        )


async def run_bot_forever() -> None:
    """Retry transient Discord network failures without stopping the website."""
    while True:
        bot = create_bot()
        # Let authenticated dashboard requests read safe VC presence state from
        # the currently running worker without exposing any host token.
        app.state.vc_presence = bot.vc_presence
        try:
            await bot.start(DISCORD_TOKEN, reconnect=True)
        except asyncio.CancelledError:
            if not bot.is_closed():
                await bot.close()
            raise
        except discord.LoginFailure:
            # An invalid bot token cannot recover by retrying.
            raise
        except (aiohttp.ClientError, OSError, AttributeError) as error:
            print(
                f"BirdBot could not reach Discord: {error}. "
                f"The dashboard remains online; retrying in {RETRY_SECONDS} seconds."
            )
        finally:
            if not bot.is_closed():
                await bot.close()
            if getattr(app.state, "vc_presence", None) is bot.vc_presence:
                app.state.vc_presence = None
        await asyncio.sleep(RETRY_SECONDS)


async def main() -> None:
    if not DISCORD_TOKEN or DISCORD_TOKEN == "put_your_new_bot_token_here":
        raise RuntimeError("DISCORD_TOKEN is missing. Add it to .env before running BirdBot.")

    port = int(os.getenv("PORT", "8000"))
    web_server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    )
    bot_task = asyncio.create_task(run_bot_forever(), name="discord-bot")
    bot_task.add_done_callback(_report_bot_task_failure)
    try:
        await web_server.serve()
    finally:
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
