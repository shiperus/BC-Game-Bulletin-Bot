from __future__ import annotations

import logging
import time

from bc_bot import aggregator
from bc_bot.config import Config
from bc_bot.db import Store
from bc_bot.sources import reddit, rss

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 10 * 60


def run_cycle(config: Config, store: Store) -> None:
    reddit_items = reddit.fetch_trending(config)
    articles = rss.fetch_articles(config)
    print(f"\nFetched {len(reddit_items)} Reddit posts, {len(articles)} RSS articles.\n")

    consolidated = aggregator.consolidate(reddit_items)
    aggregator.boost_announcements(consolidated)
    aggregator.enrich_with_articles(consolidated, articles)
    ranked = aggregator.rank(consolidated)

    recent_posts = store.recent_posts(config.retention_days)
    fresh = aggregator.select_fresh(ranked, recent_posts)
    review_items_to_post = [i for i in fresh if i.opencritic_stats is not None]
    remaining_items = [i for i in fresh if i.opencritic_stats is None]
    news_items_to_post = remaining_items[: config.posts_per_cycle]

    print(f"=== DRY RUN: {len(news_items_to_post)} news item(s) would be posted (of {len(ranked)} candidates) and {len(review_items_to_post)} of review items ===\n")
    for i, item in enumerate(news_items_to_post, 1):
        print(f"{i}. {item.display_title}")
        print(f"   Link: {item.link}")
        print(
            f"   Origin: {item.origin}  Sources: {', '.join(sorted(item.sources))}  "
            f"Confidence: x{item.confidence}"
        )
        print()
        store.record_posted(
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
            raw_data_source=item.raw_data_source
        )
    
    for i, item in enumerate(review_items_to_post, 1):
        print(f"{i}. {item.display_title}")
        print(f"   Link: {item.link}")
        print()
        store.record_posted(
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
            raw_data_source=item.raw_data_source
        )

    removed = store.cleanup_old(config.retention_days)
    print(
        f"Recorded {len(news_items_to_post)} news item(s) to the database (nothing was posted to Discord). "
        f"Recorded {len(review_items_to_post)} review item(s) to the database (nothing was posted to Discord). "
        f"Cleaned up {removed} old record(s).\n"
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = Config()
    store = Store(config.db_path)

    cycle_num = 0
    try:
        while True:
            cycle_num += 1
            logger.info("Starting dry-run cycle %d", cycle_num)
            try:
                run_cycle(config, store)
            except Exception:
                logger.exception("Dry-run cycle failed; will retry at the next scheduled interval")
            logger.info("Sleeping %d seconds until next cycle", CHECK_INTERVAL_SECONDS)
            time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Dry-run stopped after %d cycle(s)", cycle_num)


if __name__ == "__main__":
    main()
