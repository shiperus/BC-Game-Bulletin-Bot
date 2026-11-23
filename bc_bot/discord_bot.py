from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

from bc_bot import pipeline
from bc_bot.config import Config
from bc_bot.db import Store
from bc_bot.models import TrendingItem

logger = logging.getLogger(__name__)

# UTC hours at which the bc-bulletin Devvit app posts an ingest payload
# (devvit.json scheduler: cron "0 */4 * * *").
INGEST_SLOT_HOURS = (0, 4, 8, 12, 16, 20)


def seconds_until_next_ingest_cycle(now: datetime, minute_offset: int = 5) -> float:
    """Seconds until the next ingest-aligned cycle: ``minute_offset`` minutes past
    the next 4-hour slot (UTC 00/04/08/12/16/20), matching the Devvit app's
    ``0 */4 * * *`` cron so the bot posts shortly after each payload lands."""
    for hour in INGEST_SLOT_HOURS:
        candidate = now.replace(hour=hour, minute=minute_offset, second=0, microsecond=0)
        if candidate > now:
            return (candidate - now).total_seconds()
    candidate = (now + timedelta(days=1)).replace(
        hour=0, minute=minute_offset, second=0, microsecond=0
    )
    return (candidate - now).total_seconds()


class BcGamingBot(discord.Client):
    def __init__(self, config: Config, store: Store) -> None:
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.store = store
        self.cycle_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        self.cycle_task = asyncio.create_task(self._cycle_loop())

    async def _cycle_loop(self) -> None:
        """Run cycles on the configured cadence.

        With the Devvit ingest source, align each cycle to the bc-bulletin app's
        ingest cron (every 4h at UTC 00/04/08/12/16/20 plus
        INGEST_CYCLE_MINUTE_OFFSET), so posts go out a few minutes after each
        payload lands. With the RSS source, preserve the historical behaviour:
        first cycle immediately, then every CHECK_INTERVAL_HOURS.
        """
        if self.config.reddit_source == "devvit":
            while True:
                delay = seconds_until_next_ingest_cycle(
                    datetime.now(timezone.utc), self.config.ingest_cycle_minute_offset
                )
                logger.info("Next ingest-aligned cycle in %.0fs (%.1f min)", delay, delay / 60)
                await asyncio.sleep(delay)
                await self._run_cycle_safe()
        else:
            while True:
                await self._run_cycle_safe()
                await asyncio.sleep(self.config.check_interval_hours * 3600)

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
