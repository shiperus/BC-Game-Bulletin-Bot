from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import tasks

from bc_bot import aggregator
from bc_bot.config import Config
from bc_bot.db import Store
from bc_bot.models import TrendingItem
from bc_bot.sources import reddit, rss
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class BcGamingBot(discord.Client):
    def __init__(self, config: Config, store: Store) -> None:
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.store = store
        self.cycle_task = tasks.loop(hours=config.check_interval_hours)(self._run_cycle_safe)
    
    def store_posted_data(self, item, cycle_started_at, channel):
        self.store.record_posted(
                item.title,
                item.link,
                "+".join(sorted(item.sources)),
                item.confidence,
                origin=item.origin,
                engagement=item.engagement,
                reddit_url=item.url,
                article_url=item.article_url,
                article_title=item.article_title,
                opencritic_stats=item.opencritic_stats,
                raw_data_source=item.raw_data_source,
                cycle_started_at=cycle_started_at,
                channel=channel,
            )

    async def setup_hook(self) -> None:
        self.cycle_task.start()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s", self.user)

    async def _run_cycle_safe(self) -> None:
        try:
            await self._run_cycle()
        except Exception:
            logger.exception("Cycle failed; will retry at the next scheduled interval")

    async def _run_cycle(self) -> None:
        logger.info("Starting trending-news cycle")

        cycle_started_at = datetime.now(timezone.utc).isoformat()
        (reddit_items, raw_feeds), articles = await asyncio.gather(
            asyncio.to_thread(reddit.fetch_trending, self.config),
            asyncio.to_thread(rss.fetch_articles, self.config),
        )

        consolidated = aggregator.consolidate(reddit_items)
        aggregator.boost_announcements(consolidated)
        aggregator.enrich_with_articles(consolidated, articles)
        ranked = aggregator.rank(consolidated)

        recent_posts = self.store.recent_posts(self.config.retention_days)
        fresh = aggregator.select_fresh(ranked, recent_posts)
        review_items_to_post = [i for i in fresh if i.is_review_thread]
        trailer_items_to_post = [i for i in fresh if i.is_trailer_thread]
        remaining_items = [i for i in fresh if not i.is_review_thread and not i.is_trailer_thread]
        news_items_to_post = remaining_items[: self.config.posts_per_cycle]
        channel_news = self.get_channel(self.config.discord_news_channel_id)
        channel_review = self.get_channel(self.config.discord_review_channel_id)
        channel_trailer = self.get_channel(self.config.discord_trailer_channel_id)
        if channel_news is None:
            channel_news = await self.fetch_channel(self.config.discord_news_channel_id)
        if channel_review is None:
            channel_review = await self.fetch_channel(self.config.discord_review_channel_id)
        if channel_trailer is None:
            channel_trailer = await self.fetch_channel(self.config.discord_trailer_channel_id)

        self.store.record_cycle_raw_feeds(cycle_started_at, raw_feeds)
        for item in news_items_to_post:
            await self._post_item(channel_news, item)
            self.store_posted_data(item, cycle_started_at, "news")
        
        for item in review_items_to_post:
            await self._post_item(channel_review, item)
            self.store_posted_data(item, cycle_started_at, "review")
        
        for item in trailer_items_to_post:
            await self._post_item(channel_trailer, item)
            self.store_posted_data(item, cycle_started_at, "trailer")

        removed = self.store.cleanup_old(self.config.retention_days)
        logger.info(
            "Cycle complete: posted %d news items, posted %d review items, posted %d trailer items, cleaned up %d old records", len(news_items_to_post), len(review_items_to_post), len(trailer_items_to_post), removed
        )

        removed_cycle_raw_feeds = self.store.cleanup_old_cycle_data(self.config.retention_days)
        logger.info(
            "Cycle raw feeds: cleaned up %d old records", removed_cycle_raw_feeds
        )

    async def _post_item(self, channel: discord.abc.Messageable, item: TrendingItem) -> None:
        await channel.send(f"{item.display_title}\n{item.link}")
