from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_SUBREDDITS = "Games,gaming,GamingLeaksAndRumours"
# Rough relative-size weights so a #1 "hot" slot in a much bigger subreddit outranks a
# #1 slot in a smaller one; feed position alone isn't comparable across subreddits
# since Reddit's public RSS feeds don't expose upvote counts. Unlisted subreddits
# default to 1.0.
DEFAULT_SUBREDDIT_WEIGHTS = "gaming:3,Games:1.5,GamingLeaksAndRumours:1"
DEFAULT_RSS_FEEDS = ",".join(
    [
        "https://www.ign.com/rss/articles/feed?tags=games",
        "https://www.polygon.com/rss/gaming/index.xml",
        "https://www.pcgamer.com/rss/",
        "https://www.eurogamer.net/feed",
        "https://www.gamespot.com/feeds/game-news",
        "https://www.vg247.com/feed",
        "https://www.rockpapershotgun.com/feed",
        "https://www.pcgamesn.com/feed",
    ]
)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_weights(value: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for pair in _split_csv(value):
        name, _, weight = pair.partition(":")
        if name and weight:
            weights[name] = float(weight)
    return weights


def _env_or_default(name: str, default: str) -> str:
    # os.environ.get(name, default) only falls back when the key is absent, not when
    # it's present-but-blank (e.g. "DISCORD_NEWS_CHANNEL_ID=" left empty in .env), so treat
    # blank the same as unset.
    return os.environ.get(name) or default


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass
class Config:
    # Only required to actually run the live bot (see require_discord_credentials());
    # left optional so the dry-run pipeline can be tested without a Discord app set up.
    discord_token: str = field(default_factory=lambda: _env_or_default("DISCORD_TOKEN", ""))
    discord_news_channel_id: int = field(
        default_factory=lambda: int(_env_or_default("DISCORD_NEWS_CHANNEL_ID", "0"))
    )
    discord_review_channel_id: int = field(
        default_factory=lambda: int(_env_or_default("DISCORD_REVIEW_CHANNEL_ID", "0"))
    )
    discord_trailer_channel_id: int = field(
        default_factory=lambda: int(_env_or_default("DISCORD_TRAILER_CHANNEL_ID", "0"))
    )

    # Reddit's public RSS feeds require a distinctive User-Agent but no API key/app.
    reddit_user_agent: str = field(
        default_factory=lambda: _env_or_default("REDDIT_USER_AGENT", "bc-gaming-bot/1.0")
    )

    subreddits: list[str] = field(
        default_factory=lambda: _split_csv(_env_or_default("SUBREDDITS", DEFAULT_SUBREDDITS))
    )
    subreddit_weights: dict[str, float] = field(
        default_factory=lambda: _parse_weights(
            _env_or_default("SUBREDDIT_WEIGHTS", DEFAULT_SUBREDDIT_WEIGHTS)
        )
    )
    rss_feeds: list[str] = field(
        default_factory=lambda: _split_csv(_env_or_default("RSS_FEEDS", DEFAULT_RSS_FEEDS))
    )

    check_interval_hours: float = field(
        default_factory=lambda: float(_env_or_default("CHECK_INTERVAL_HOURS", "4"))
    )
    retention_days: int = field(
        default_factory=lambda: int(_env_or_default("RETENTION_DAYS", "30"))
    )
    posts_per_cycle: int = field(
        default_factory=lambda: int(_env_or_default("POSTS_PER_CYCLE", "5"))
    )

    db_path: Path = field(
        default_factory=lambda: Path(_env_or_default("DB_PATH", "bc_bot.db"))
    )

    # Where hot posts come from: "rss" (Reddit's public feeds, the original source)
    # or "devvit" (JSON payloads the bc-bulletin Devvit app posts to a private
    # Discord channel via webhook -- real scores, post bodies, stickied flags).
    reddit_source: str = field(
        default_factory=lambda: _env_or_default("REDDIT_SOURCE", "rss").strip().lower()
    )
    discord_ingest_channel_id: int = field(
        default_factory=lambda: int(_env_or_default("DISCORD_INGEST_CHANNEL_ID", "0"))
    )
    # A payload older than this is treated as missing (the Devvit app stopped
    # posting), so the cycle falls back to RSS instead of re-reading stale data.
    ingest_max_age_hours: float = field(
        default_factory=lambda: float(_env_or_default("INGEST_MAX_AGE_HOURS", "6"))
    )
    ingest_fallback_to_rss: bool = field(
        default_factory=lambda: _env_or_default("INGEST_FALLBACK_TO_RSS", "true").strip().lower()
        in ("1", "true", "yes")
    )

    # Score-based rules; only apply to posts with a known score (Devvit source).
    # MIN_SCORE: drop posts below it (0 disables). RANK_BY_SCORE: within the same
    # confidence, order by real upvotes instead of feed position x weight.
    # POPULAR_SCORE: +1 confidence at or above it, so a story with massive
    # community traction isn't outranked by low-score posts that merely picked up
    # a keyword boost (0 disables).
    min_score: int = field(default_factory=lambda: int(_env_or_default("MIN_SCORE", "10")))
    rank_by_score: bool = field(
        default_factory=lambda: _env_or_default("RANK_BY_SCORE", "true").strip().lower()
        in ("1", "true", "yes")
    )
    popular_score: int = field(
        default_factory=lambda: int(_env_or_default("POPULAR_SCORE", "1000"))
    )

    def require_ingest_settings(self) -> None:
        if self.reddit_source not in ("rss", "devvit"):
            raise RuntimeError(f"REDDIT_SOURCE must be 'rss' or 'devvit', got {self.reddit_source!r}")
        if self.reddit_source == "devvit" and (
            not self.discord_token or not self.discord_ingest_channel_id
        ):
            raise RuntimeError(
                "REDDIT_SOURCE=devvit needs DISCORD_TOKEN and DISCORD_INGEST_CHANNEL_ID "
                "(or run bc_bot.dry_run --ingest-file <payload.json> to test offline)"
            )

    def require_discord_credentials(self) -> None:
        if not self.discord_token or not self.discord_news_channel_id or not self.discord_review_channel_id or not self.discord_trailer_channel_id:
            raise RuntimeError(
                "DISCORD_TOKEN, DISCORD_NEWS_CHANNEL_ID, DISCORD_REVIEW_CHANNEL_ID and DISCORD_TRAILER_CHANNEL_ID must be set to run the live bot "
                "(use bc_bot.dry_run to test without them)"
            )
