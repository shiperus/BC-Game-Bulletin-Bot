from __future__ import annotations

import re
from collections import defaultdict

from rapidfuzz import fuzz, utils

from bc_bot.models import Article, TrendingItem
from bc_bot.sources import rss

CONSOLIDATION_THRESHOLD = 80

# Matches CONSOLIDATION_THRESHOLD so a reworded restatement of an already-posted
# story is caught here just as reliably as duplicate titles are merged within a
# single cycle.
DUPLICATE_THRESHOLD = 80

# Collapses dots inside acronym-style tokens so differently-spelled titles for the
# same entry still match, e.g. "S.T.A.L.K.E.R. 2 ... Launch Trailer" vs
# "STALKER 2 ... Launch Trailer" score ~89 (≳ threshold) instead of ~74 (below it).
# Applied only to the fuzzy-match inputs, never to the stored/posted title itself.
_TITLE_NORM_PATTERN = re.compile(r"(?<=\b[A-Za-z])\.(?=[A-Za-z])")


def _match_title_tokens(title_a: str, title_b: str) -> float:
    a = _TITLE_NORM_PATTERN.sub("", title_a)
    b = _TITLE_NORM_PATTERN.sub("", title_b)
    return fuzz.token_sort_ratio(a, b, processor=utils.default_process)

# YouTube hosts/formats that can all point at the same video. Trailer posts are
# deduped by exact link only (flagged items skip fuzzy-title matching), so a single
# video surfacing as youtube.com/watch?v= in one cycle and youtu.be/... with different
# tracking params in the next was escaping dedup and getting double-posted. Reducing
# every YouTube URL to its canonical video ID closes that hole.
_YT_ID_PATTERNS = [
    re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?[^#]*?v=|shorts/|embed/|live/|v/))([A-Za-z0-9_-]{11})")
]


def canonical_url(url: str) -> str:
    """Return a URL to dedup on. YouTube links are reduced to `yt:<video-id>` so
    youtu.be vs youtube.com/watch?v= spelling and tracking params can't defeat
    exact-link dedup; every other URL is returned unchanged."""
    for pattern in _YT_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return f"yt:{match.group(1)}"
    return url

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

    Review threads and trailer threads never merge with anything else -- even on a
    fuzzy title match to a plain article about the same game. They go to different
    channels (review/news) and must both survive as independent items.
    """
    clusters: list[TrendingItem] = []

    for item in sorted(items, key=lambda i: i.engagement, reverse=True):
        cluster = next(
            (
                c
                for c in clusters
                if c.is_review_thread == item.is_review_thread
                and c.is_trailer_thread == item.is_trailer_thread
                and _match_title_tokens(item.title, c.title)
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
    items: list[TrendingItem], recent_posts: list[tuple[str, str, str | None]]
) -> list[TrendingItem]:
    """Filter out items that duplicate an already-posted story, by exact link or
    fuzzy title match, against both prior-cycle history and items already kept in
    this call so near-duplicates can't both get posted in one cycle.

    Fuzzy history matching is scoped per channel via the stored channel tag:
    plain news items match against news history, trailer items against trailer
    history, review threads against review history. This lets a re-uploaded trailer
    with a *different* YouTube video ID still be caught (same title in the same
    channel) while never conflating a "<Game> Review Thread" (review channel) with
    a "<Game> Review" news article. Pre-migration rows with no channel (NULL) are
    treated as news history, preserving prior behavior for undifferentiated rows.
    """
    seen_urls = {canonical_url(url) for _, url, _ in recent_posts}

    # History title pools per channel. Pre-migration rows (channel is NULL) predate
    # channel routing and are folded into the news pool.
    history_titles: dict[str, list[str]] = defaultdict(list)
    for title, _, channel in recent_posts:
        key = channel if channel in ("news", "review", "trailer") else "news"
        history_titles[key].append(title)

    fresh: list[TrendingItem] = []
    kept_titles_by_flags: dict[tuple[bool, bool], list[str]] = {}
    for item in items:
        item_link = canonical_url(item.link)
        if item_link in seen_urls:
            continue

        flag_key = (item.is_review_thread, item.is_trailer_thread)
        # Match history only within the item's own channel so flagged items
        # (trailers, review threads) dedup against their own history.
        if flag_key == (False, False):
            pool_key = "news"
        else:
            pool_key = "trailer" if item.is_trailer_thread else "review"

        title_pool = list(history_titles[pool_key])
        title_pool += kept_titles_by_flags.get(flag_key, [])

        if any(
            _match_title_tokens(item.title, title) >= DUPLICATE_THRESHOLD
            for title in title_pool
        ):
            continue

        fresh.append(item)
        kept_titles_by_flags.setdefault(flag_key, []).append(item.title)
        seen_urls.add(item_link)

    return fresh
