from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import tasks

from bc_bot import pipeline
from bc_bot.config import Config
from bc_bot.db import Store
from bc_bot.models import TrendingItem

logger = logging.getLogger(__name__)


class BcGamingBot(discord.Client):
    def __init__(self, config: Config, store: Store) -> None:
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.store = store
        self.cycle_task = tasks.loop(hours=config.check_interval_hours)(self._run_cycle_safe)

    async def setup_hook(self) -> None:
        self.cycle_task.start()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s", self.user)

    async def _run_cycle_safe(self) -> None:
        try:
            await self._run_cycle()
        except Exception:
            logger.exception("Cycle failed; will retry at the next scheduled interval")

    async def _get_channel(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        return channel

    async def _run_cycle(self) -> None:
        logger.info("Starting trending-news cycle")
        result = await asyncio.to_thread(pipeline.prepare_cycle, self.config, self.store)

        try:
            targets = [
                ("news", self.config.discord_news_channel_id, result.news),
                ("review", self.config.discord_review_channel_id, result.review),
                ("trailer", self.config.discord_trailer_channel_id, result.trailer),
            ]
            for channel_name, channel_id, items in targets:
                if not items:
                    continue
                channel = await self._get_channel(channel_id)
                for item in items:
                    await self._post_item(channel, item)
                    pipeline.record_posted_item(self.store, result, item, channel_name)
        finally:
            # Archive input + decisions even if posting failed part-way, so the
            # weekly review still sees this cycle.
            removed, removed_raw = pipeline.finish_cycle(self.store, self.config, result)

        logger.info(
            "Cycle complete (%s): posted %d news, %d review, %d trailer item(s); "
            "cleaned up %d old posted record(s), %d old raw-feed record(s)",
            result.reddit_source,
            len(result.news),
            len(result.review),
            len(result.trailer),
            removed,
            removed_raw,
        )

    async def _post_item(self, channel: discord.abc.Messageable, item: TrendingItem) -> None:
        await channel.send(f"{item.display_title}\n{item.link}")
