"""One fetch -> filter -> consolidate -> enrich -> rank -> dedup cycle, shared by the
live bot (discord_bot.py) and the console dry run (dry_run.py). The two entry points
differ only in what they do with the selected items (post to Discord vs print).

Every post's fate is recorded as a PostDecision (filter rule, merge, duplicate,
over cap, posted) so the weekly review can see why each post did or didn't go out.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bc_bot import aggregator
from bc_bot.config import Config
from bc_bot.db import Store
from bc_bot.models import PostDecision, TrendingItem
from bc_bot.sources import devvit_ingest, reddit, rss

logger = logging.getLogger(__name__)


@dataclass
class CycleResult:
    cycle_started_at: str
    reddit_source: str  # "rss" | "devvit" | "devvit-file"
    news: list[TrendingItem]
    review: list[TrendingItem]
    trailer: list[TrendingItem]
    candidate_count: int
    raw_feeds: dict[str, str]
    decisions: list[PostDecision] = field(default_factory=list)
    ingest_message_id: str | None = None


def collect_posts(
    config: Config, store: Store, ingest_file: Path | None = None
) -> tuple[list[TrendingItem], dict[str, str], list[PostDecision], str, str | None]:
    """Fetch and filter hot posts from the configured source.

    Returns (items, raw_feeds, filter_decisions, source_label, ingest_message_id).
    With REDDIT_SOURCE=devvit and no new/fresh payload, falls back to RSS when
    INGEST_FALLBACK_TO_RSS is on (default), otherwise returns nothing this cycle.
    """
    if ingest_file is not None:
        ingest = devvit_ingest.load_payload_file(ingest_file)
        posts, raw_feeds = devvit_ingest.payload_to_posts(ingest.payload)
        items, decisions = reddit.filter_posts(posts, config)
        return items, raw_feeds, decisions, "devvit-file", None

    if config.reddit_source == "devvit":
        try:
            last_id = store.get_value(devvit_ingest.LAST_MESSAGE_KEY)
            ingest = devvit_ingest.fetch_latest_payload(config, last_id)
        except Exception:
            logger.exception("Failed to read the Devvit ingest channel")
            ingest = None
        if ingest is not None:
            posts, raw_feeds = devvit_ingest.payload_to_posts(ingest.payload)
            items, decisions = reddit.filter_posts(posts, config)
            return items, raw_feeds, decisions, "devvit", ingest.message_id
        if not config.ingest_fallback_to_rss:
            logger.warning("No usable Devvit payload and RSS fallback is off; skipping cycle")
            return [], {}, [], "devvit", None
        logger.warning("No usable Devvit payload; falling back to RSS for this cycle")

    items, raw_feeds, decisions = reddit.fetch_trending(config)
    return items, raw_feeds, decisions, "rss", None


def _subreddit(item: TrendingItem) -> str:
    return item.origin.removeprefix("r/")


def _decision(item: TrendingItem, stage: str, decision: str, reason: str) -> PostDecision:
    return PostDecision(
        post_id=item.post_id,
        subreddit=_subreddit(item),
        title=item.title,
        stage=stage,
        decision=decision,
        reason=reason,
        url=item.link,
        score=item.score,
        num_comments=item.num_comments,
    )


def prepare_cycle(config: Config, store: Store, ingest_file: Path | None = None) -> CycleResult:
    """Run everything up to (but not including) posting."""
    cycle_started_at = datetime.now(timezone.utc).isoformat()

    with ThreadPoolExecutor(max_workers=2) as pool:
        posts_future = pool.submit(collect_posts, config, store, ingest_file)
        articles_future = pool.submit(rss.fetch_articles, config)
        reddit_items, raw_feeds, decisions, source, message_id = posts_future.result()
        articles = articles_future.result()
    logger.info(
        "Collected %d candidate post(s) from %s, %d RSS article(s)",
        len(reddit_items),
        source,
        len(articles),
    )

    consolidated = aggregator.consolidate(reddit_items)
    kept_ids = {id(c) for c in consolidated}
    for item in reddit_items:
        if id(item) in kept_ids:
            continue
        cluster = next((c for c in consolidated if item.post_id in c.merged_post_ids), None)
        target = cluster.post_id if cluster and cluster.post_id else "another item"
        decisions.append(_decision(item, "consolidate", "merged", f"merged into {target}"))

    aggregator.boost_announcements(consolidated)
    aggregator.boost_popular(consolidated, config.popular_score)
    aggregator.enrich_with_articles(consolidated, articles)
    # Enrichment can attach a YouTube URL to a link post whose title already carries a
    # trailer keyword; reclassify those as trailer items so they reach the trailer
    # channel instead of news (see flag_post_enrichment_trailers).
    reddit.flag_post_enrichment_trailers(consolidated)
    ranked = aggregator.rank(consolidated)

    recent_posts = store.recent_posts(config.retention_days)
    fresh = aggregator.select_fresh(ranked, recent_posts)
    fresh_ids = {id(i) for i in fresh}
    for item in ranked:
        if id(item) not in fresh_ids:
            decisions.append(_decision(item, "dedup", "dropped", "duplicate"))

    review = [i for i in fresh if i.is_review_thread]
    trailer = [i for i in fresh if i.is_trailer_thread]
    remaining = [i for i in fresh if not i.is_review_thread and not i.is_trailer_thread]
    news = remaining[: config.posts_per_cycle]
    for item in remaining[config.posts_per_cycle :]:
        decisions.append(_decision(item, "cap", "dropped", "over_cap"))

    return CycleResult(
        cycle_started_at=cycle_started_at,
        reddit_source=source,
        news=news,
        review=review,
        trailer=trailer,
        candidate_count=len(ranked),
        raw_feeds=raw_feeds,
        decisions=decisions,
        ingest_message_id=message_id,
    )


def record_posted_item(store: Store, result: CycleResult, item: TrendingItem, channel: str) -> None:
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
        raw_data_source=item.raw_data_source,
        cycle_started_at=result.cycle_started_at,
        channel=channel,
    )
    result.decisions.append(_decision(item, "post", "posted", channel))


def finish_cycle(store: Store, config: Config, result: CycleResult) -> tuple[int, int]:
    """Archive the raw input + decisions, mark the ingest payload processed, and
    purge old rows. Returns (removed posted rows, removed raw-feed rows)."""
    store.record_cycle_raw_feeds(result.cycle_started_at, result.raw_feeds)
    store.record_decisions(result.cycle_started_at, result.reddit_source, result.decisions)
    if result.ingest_message_id:
        store.set_value(devvit_ingest.LAST_MESSAGE_KEY, result.ingest_message_id)
    removed = store.cleanup_old(config.retention_days)
    removed_raw = store.cleanup_old_cycle_data(config.retention_days)
    return removed, removed_raw
