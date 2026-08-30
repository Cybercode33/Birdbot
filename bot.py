"""Start BirdBot from the terminal without the website dashboard."""

from birdbot import create_bot
from settings import DISCORD_TOKEN


def main() -> None:
    if not DISCORD_TOKEN or DISCORD_TOKEN == "put_your_new_bot_token_here":
        raise RuntimeError("Your Discord token is missing. Add it to .env, then run: python bot.py")
    create_bot().run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
