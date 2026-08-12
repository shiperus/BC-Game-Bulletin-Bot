from __future__ import annotations

import re

from rapidfuzz import fuzz, utils

from bc_bot.models import Article, TrendingItem
from bc_bot.sources import rss

CONSOLIDATION_THRESHOLD = 80

# Matches CONSOLIDATION_THRESHOLD so a reworded restatement of an already-posted
# story is caught here just as reliably as duplicate titles are merged within a
# single cycle.
DUPLICATE_THRESHOLD = 80

# Phrases that show up in titles announcing, revealing, or confirming a new game --
# as opposed to discounts, patch notes, esports results, memes, etc.
_ANNOUNCEMENT_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bannounc(e|ed|ement|es|ing)\b",
        r"\brevealed?\b",
        r"\bunveil(ed|s|ing)?\b",
        r"\bteaser\b",
        r"\btrailer\b",
        r"\bcoming to\b",
        r"\brelease date\b",
        r"\bconfirmed for\b",
        r"\bnew (game|expansion|dlc)\b",
    ]
]

# Phrases indicating hands-on editorial coverage of a game -- reviews, previews,
# impressions -- which should also be prioritized over routine chatter, esports
# results, sales, and discussion threads, same as announcements.
_COVERAGE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\breview(s|ed)?\b",
        r"\bimpressions?\b",
        r"\bhands[- ]on\b",
        r"\bpreview(s|ed)?\b",
        r"\bfirst look\b",
        r"\bearly access\b",
    ]
]


def consolidate(items: list[TrendingItem]) -> list[TrendingItem]:
    """Merge items across sources that describe the same story.

    Review threads and trailer threads are never merged with anything else -- even
    when the title fuzzy-matches a plain article/link post about the same game (e.g.
    a "<Game> - Review Thread" self-post vs. an IGN "<Game> Review" article post).
    Those are deliberately different destinations (review channel vs. news channel)
    and must both survive as independent items rather than have one swallow the
    other's identity based on which had higher engagement.
    """
    clusters: list[TrendingItem] = []

    for item in sorted(items, key=lambda i: i.engagement, reverse=True):
        cluster = next(
            (
                c
                for c in clusters
                if c.is_review_thread == item.is_review_thread
                and c.is_trailer_thread == item.is_trailer_thread
                and fuzz.token_sort_ratio(
                    item.title, c.title, processor=utils.default_process
                )
                >= CONSOLIDATION_THRESHOLD
            ),
            None,
        )
        if cluster is None:
            clusters.append(item)
            continue

        cluster.engagement += item.engagement
        cluster.sources.add(item.source)
        cluster.confidence = len(cluster.sources)

    return clusters


def enrich_with_articles(items: list[TrendingItem], articles: list[Article]) -> None:
    """Attach a matching RSS article to each item, falling back to its original link."""
    for item in items:
        if item.skip_enrichment:
            continue
        match = rss.find_best_match(item.title, articles)
        if match:
            item.confidence += 1


def boost_announcements(items: list[TrendingItem]) -> None:
    """Bump confidence for items that look like a new-game announcement, or hands-on
    coverage (review/preview/impressions) -- the content this bot should lead with,
    ahead of routine discussion, esports results, or sales posts. A title can match
    both categories (e.g. a review of a just-announced game) and stack the boost."""
    for item in items:
        if any(pattern.search(item.title) for pattern in _ANNOUNCEMENT_PATTERNS):
            item.confidence += 1
        if any(pattern.search(item.title) for pattern in _COVERAGE_PATTERNS):
            item.confidence += 1


def rank(items: list[TrendingItem]) -> list[TrendingItem]:
    return sorted(items, key=lambda i: (i.confidence, i.engagement), reverse=True)


def select_fresh(
    items: list[TrendingItem], recent_posts: list[tuple[str, str]]
) -> list[TrendingItem]:
    """Filter out items that duplicate an already-posted story, by exact link or
    fuzzy title match. Checks both prior-cycle history (recent_posts, from the DB)
    and items already kept earlier in this same call, so near-duplicates that slip
    past consolidate() -- e.g. differently-phrased posts of the same story from
    different subreddits -- can't both get posted in one cycle.

    Fuzzy title matching alone would conflate a "<Game> - Review Thread" self-post
    with a "<Game> Review" article post about the same game -- same problem
    consolidate() guards against, but here across cycles via posting history. Since
    recent_posts doesn't carry the review/trailer flags, matching is restricted to
    candidates that don't have flags at all, so a Review Thread or trailer candidate
    is never dropped as a "duplicate" of a differently-flagged historical post (or
    vice versa); only exact link matches still apply for those.
    """
    seen_titles = [title for title, _ in recent_posts]
    seen_urls = {url for _, url in recent_posts}

    fresh: list[TrendingItem] = []
    kept_titles_by_flags: dict[tuple[bool, bool], list[str]] = {}
    for item in items:
        if item.link in seen_urls:
            continue

        flag_key = (item.is_review_thread, item.is_trailer_thread)
        # Cross-cycle history has no flags recorded, so only compare unflagged
        # candidates against it -- a flagged item can only be deduped by exact link.
        title_pool = list(seen_titles) if flag_key == (False, False) else []
        title_pool += kept_titles_by_flags.get(flag_key, [])

        if any(
            fuzz.token_sort_ratio(item.title, title, processor=utils.default_process)
            >= DUPLICATE_THRESHOLD
            for title in title_pool
        ):
            continue

        fresh.append(item)
        kept_titles_by_flags.setdefault(flag_key, []).append(item.title)
        seen_urls.add(item.link)

    return fresh
