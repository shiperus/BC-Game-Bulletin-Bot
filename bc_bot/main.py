from __future__ import annotations

import logging
import logging.handlers

from bc_bot.config import Config
from bc_bot.db import Store
from bc_bot.discord_bot import BcGamingBot


def configure_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(
        "bc_bot.log", maxBytes=1_000_000, backupCount=2
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )


def main() -> None:
    configure_logging()
    config = Config()
    config.require_discord_credentials()
    config.require_ingest_settings()
    store = Store(config.db_path)
    bot = BcGamingBot(config, store)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
