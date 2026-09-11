from __future__ import annotations

import logging
import re
import time
import requests
from html import unescape

import feedparser

from bc_bot.config import Config
from bc_bot.models import TrendingItem

logger = logging.getLogger(__name__)

DELAY_BETWEEN_REQUESTS_SECONDS = 60
MAX_RETRIES_ON_RATE_LIMIT = 3
RETRY_BACKOFF_BASE_SECONDS = 30

# For link posts, Reddit's RSS <link> always points to the comments page; the actual
# submitted URL (e.g. the news article) is embedded in the entry summary as a "[link]"
# anchor instead. Self-posts (discussion threads) have no such anchor.
_SUBMITTED_LINK_PATTERN = re.compile(r'<a href="([^"]+)">\[link\]</a>')

# Recurring community megathreads (not news) that rank high in "hot" every day/week
# and should never be treated as trending stories. Kept as an explicit list for the
# common cases feedparser sees most often; _is_meta_thread() also has a more general
# self-post + date heuristic below for subreddit-specific wordings not covered here.
_META_THREAD_PATTERNS = [
    re.compile(r"\bdaily\b.*\bdiscussion\b", re.IGNORECASE),
    re.compile(r"\bweekly\b.*\b(thread|discussion|megathread)\b", re.IGNORECASE),
    re.compile(r"\bmonthly\b.*\b(thread|discussion|megathread)\b", re.IGNORECASE),
    re.compile(r"free talk friday", re.IGNORECASE),
    re.compile(r"tech support and basic questions thread", re.IGNORECASE),
]

# Matches a date embedded in a title, e.g. "06/29/26", "6-29-2026", or "June 29".
_DATE_TOKEN_PATTERN = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_THREAD_HINT_PATTERN = re.compile(r"\b(thread|discussion|megathread)\b", re.IGNORECASE)

# Many subreddits alliterate their recurring weekly threads with the day they run on
# instead of saying "weekly" (e.g. "Making Friends Monday! Share your game tags here!",
# "Self Promotion Saturday! ..."). The day name immediately followed by "!" is the
# banner-style opener these threads use; real news headlines don't punctuate that way,
# so it's a safe signal distinct from titles that merely mention a day in passing
# (e.g. "Nintendo Direct announced for Wednesday").
_DAY_OF_WEEK_PATTERN = re.compile(
    r"\b(sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b\s*!",
    re.IGNORECASE,
)

# Event/recurring megathread hubs ("[MEGATHREAD] Gamescom 2026") are Reddit
# community-discussion aggregation, not reportable news. The general
# _is_meta_thread() date heuristic below can't catch them (their stamps -- "2026",
# "09.03.26" -- carry no day/month token), so catch the explicit megathread marker
# directly. A "Review (Mega)Thread" is excluded: that is a routed aggregator
# self-post (review channel), not a discussion hub.
_MEGATHREAD_PATTERN = re.compile(r"\b(?:mega\s*thread|megathread)\b", re.IGNORECASE)


# Direct image/video hosts and file extensions Reddit posts commonly link to (memes,
# screenshots, clips) rather than a news article. These carry no headline of their own
# to corroborate or enrich, so they're not useful as "trending news" candidates.
_IMAGE_HOST_PATTERN = re.compile(
    r"^https?://(i\.redd\.it|i\.imgur\.com|preview\.redd\.it|v\.redd\.it)/", re.IGNORECASE
)
_IMAGE_EXTENSION_PATTERN = re.compile(
    r"\.(jpe?g|png|gif|gifv|webp|bmp|mp4)(\?.*)?$", re.IGNORECASE
)

# "Review Thread" megathreads (a self-post aggregating many outlets' reviews) embed a
# "Review Aggregator" section linking to the game's OpenCritic page in the post body.
# That's a better link than any single outlet's review -- fuzzy-matching one via RSS
# would arbitrarily pick one outlet's take and present it as "the" review -- so these
# get special-cased: pull the OpenCritic link out of the post body instead, and
# aggregator.enrich_with_articles() skips RSS matching for them entirely.
_REVIEW_THREAD_PATTERN = re.compile(r"\breview(s)?\s+(mega)?thread\b", re.IGNORECASE)
_OPENCRITIC_LINK_PATTERN = re.compile(
    r'href="(https://opencritic\.com/game/[^"]+)">([^<]+)</a>'
)

# Filter for Trailer thread and youtube links inside the thread
_YOUTUBE_HOST_PATTERN = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE)

# Titles that designate a game-trailer-class video (announce/reveal/teaser/gameplay/
# showcase/preview/trailer). Combined with a YouTube link these belong in the "game
# trailers" channel, not news. Note the plain "<Game> Release Date" phrasing is NOT a
# trailer signal on its own -- only the title keywords below trigger trailer routing,
# and only when the submitted URL is actually a YouTube video.
_TRAILER_TITLE_PATTERN = re.compile(
    r"\b(?:announc\w*|reveal\w*|teaser\w*|gameplay|showcase|preview\w*|trailer\w*)\b",
    re.IGNORECASE,
)

# Each subreddit's review-thread template words the OpenCritic link text a bit
# differently -- e.g. r/Games: "OpenCritic - 88 average - 95% recommended - 58
# reviews"; r/gaming: "OpenCritic: 88 Average - 96% Recommend" (no review count).
# Percent-recommended and review count are optional so both (and other minor
# variants) still extract whatever numbers are actually present.
_OPENCRITIC_STATS_PATTERN = re.compile(
    r"(?P<avg>\d+)\s*average"
    r"(?:\s*-\s*(?P<pct>\d+)%\s*recommend\w*)?"
    r"(?:\s*-\s*(?P<count>\d+)\s*reviews)?",
    re.IGNORECASE,
)


def _extract_submitted_url(summary: str) -> str | None:
    match = _SUBMITTED_LINK_PATTERN.search(summary)
    return unescape(match.group(1)) if match else None


def _is_image_link(url: str) -> bool:
    return bool(_IMAGE_HOST_PATTERN.search(url) or _IMAGE_EXTENSION_PATTERN.search(url))


def _extract_opencritic(summary: str) -> tuple[str, str | None] | None:
    """Return (url, formatted stats) for a review thread's OpenCritic aggregator link,
    or None if the post body has no such section. Stats are None if the link text has
    no "N average" score at all (e.g. too few reviews for OpenCritic to score yet)."""
    match = _OPENCRITIC_LINK_PATTERN.search(summary)
    if match is None:
        return None

    url = unescape(match.group(1))
    stats_match = _OPENCRITIC_STATS_PATTERN.search(unescape(match.group(2)))
    if stats_match is None:
        return url, None

    parts = [f"OpenCritic {stats_match['avg']}"]
    if stats_match["pct"]:
        parts.append(f"{stats_match['pct']}% Recommended")
    if stats_match["count"]:
        parts.append(f"{stats_match['count']} Reviews")
    return url, " · ".join(parts)


def _is_meta_thread(title: str, is_self_post: bool) -> bool:
    if any(pattern.search(title) for pattern in _META_THREAD_PATTERNS):
        return True

    # Explicit megathread marker (e.g. "[MEGATHREAD] Gamescom 2026") that the date
    # heuristic below can't see; never mis-fire on a "Review (Mega)Thread".
    if _MEGATHREAD_PATTERN.search(title) and not _REVIEW_THREAD_PATTERN.search(title):
        return True

    if not is_self_post:
        return False

    # Recurring megathreads are almost always self-posts with a date baked into the
    # title (e.g. "Discussion Thread - Week Beginning 06/29/26"); this catches new
    # subreddits' own wordings without needing a hand-tuned pattern for each one.
    if _THREAD_HINT_PATTERN.search(title) and _DATE_TOKEN_PATTERN.search(title):
        return True

    return bool(_DAY_OF_WEEK_PATTERN.search(title))


# Self-post community-discussion threads (opinion questions, first-person anecdotes/
# gripes, crowd-sourced list posts) rank high in r/gaming "hot" every day but are
# NOT reportable news -- they PROMPT the community instead of REPORTING a fact. The
# news channel should carry game news (article link posts, video posts, and even
# leak/rumour self-posts that report a specific development), not community chatter.
# These patterns catch the unmistakable survey/anecdote phrasings; combined with the
# self-post + not-review/trailer guards in fetch_trending(), they never touch news
# articles, video posts, review threads, or trailer posts.
_LEAD_QUESTION_PATTERN = re.compile(
    r"^(?:what'?s?|which|why|any|has\b|how'?s?|did|does|where|when|who|are|is|do)\b",
    re.IGNORECASE,
)
_LEAD_OPINION_PATTERN = re.compile(r"^(?:i\s|i(?:'|\u2019)\w*\s|im\s)", re.IGNORECASE)
_LEAD_LIST_PATTERN = re.compile(r"^a list of\b", re.IGNORECASE)
_ASK_PHRASE_PATTERN = re.compile(
    r"(your impressions|do you think|what do you|what did you|have you\b"
    r"|what\b.*\b(?:you|your)\b|why\b.*\b(?:you|your)\b)",
    re.IGNORECASE,
)
_REALIZATION_PATTERN = re.compile(
    r"\bi (?:just )?(?:realized|noticed|remember(?:ed)?|never knew)\b", re.IGNORECASE
)
# First-person experiential/preference phrasing that appears mid-title rather than
# as the leading "I ..." caught by _LEAD_OPINION_PATTERN -- e.g. "Superhot is the
# most innovative shooter I've played in years." / "A game doesn't need 100 hours
# ... I'd take 10 unforgettable hours over 100 forgettable ones." Both are first-
# person anecdotes/gripes, not reportable news. Matched against the apostrophe-
# stripped title (see _COMMUNITY_STRIP_PATTERN), so "I've"/"I'd" appear as "ive"/"id".
_FIRST_PERSON_MID_PATTERN = re.compile(
    r"\bive (?:ever )?(?:played|seen|tried|finished|beaten|experienced|encountered)\b"
    r"|\bid (?:take|rather|have|prefer|go)\b",
    re.IGNORECASE,
)
# Normative/contrarian opinion and hot-take phrasing on a self-post: a counterfactual
# "would be worse/better", "are game changers", or "worth the money" -- opinions, not
# reports ("Papers, Please would be worse with a less annoying UI").
_OPINION_PHRASE_PATTERN = re.compile(
    r"\bwould be (?:worse|better|nice|great|cool|bad|good)\b"
    r"|\bare game changers?\b"
    r"|\bworth the money\b",
    re.IGNORECASE,
)
# A self-post soliciting the community rather than reporting: "Gaming recommendations
# for quiet a desk job?", "can we normalize menu wrapping?", "should we ...".
_REQUEST_PATTERN = re.compile(
    r"\brecommend\w*\b"
    r"|\bcan we (?:please )?(?:normalize|stop|bring|get|make|fix|talk|just)\b"
    r"|\bshould we\b",
    re.IGNORECASE,
)
# Community-poll phrasings addressed straight at the reader ("Games you did a complete
# 180 on."). Real news self-posts (leaks/rumours) never address "you did"/"you ever".
_DIRECTED_AT_READER_PATTERN = re.compile(
    r"\byou (?:did|ever|100\w*)\b",
    re.IGNORECASE,
)
# Strip surrounding quotes/punctuation so a lead question/opinion is recognised even
# when the OP wraps it in quotes (e.g. "\"Headshot!\" (UT) ... What sound snippets...").
_COMMUNITY_STRIP_PATTERN = re.compile(r"[“”\"\'.!:?,\u2018\u2019]")


def _is_community_discussion(title: str) -> bool:
    """True for titles phrased as a community survey/anecdote rather than a report:
    leading question words, leading first-person opinions/gripes, a crowd-sourced
    list, an ask directed at "you/your", an "I just realized"-style recollection,
    or first-person/contrarian opinion, recommendation, and reader-addressed
    phrasings that sit mid-title (see patterns above)."""
    t = _COMMUNITY_STRIP_PATTERN.sub("", title)
    return (
        _LEAD_QUESTION_PATTERN.search(t) is not None
        or _LEAD_OPINION_PATTERN.search(t) is not None
        or _LEAD_LIST_PATTERN.search(t) is not None
        or _ASK_PHRASE_PATTERN.search(t) is not None
        or _REALIZATION_PATTERN.search(t) is not None
        or _FIRST_PERSON_MID_PATTERN.search(t) is not None
        or _OPINION_PHRASE_PATTERN.search(t) is not None
        or _REQUEST_PATTERN.search(t) is not None
        or _DIRECTED_AT_READER_PATTERN.search(t) is not None
    )


def fetch_trending(config: Config) -> tuple[list[TrendingItem], dict[str, str]]:
    """Fetch hot posts via Reddit's public Atom feed (reddit.com/r/<sub>/hot/.rss).

    Reddit's official API now gates app creation behind an approval process, and the
    plain .json endpoints are blocked by anti-bot filtering, but the .rss/.atom feeds
    remain reachable. Atom entries don't carry score/comment counts, so engagement is
    approximated by feed position (already hot-ranked by Reddit).
    """
    items: list[TrendingItem] = []
    raw_feeds: dict[str, str] = {}
    
    for index, subreddit_name in enumerate(config.subreddits):
        if index > 0:
            time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)

        url = f"https://www.reddit.com/r/{subreddit_name}/hot/.rss"
        try:
            result = _parse_with_retry(url, config.reddit_user_agent, subreddit_name)
            if result is None:
                continue
            feed, raw_text = result
            raw_feeds[subreddit_name] = raw_text
            if feed is None:
                continue

            skipped_meta_threads = 0
            skipped_image_links = 0
            skipped_community_discussion = 0
            weight = config.subreddit_weights.get(subreddit_name, 1.0)
            total_feed_entries = len(feed.entries)
            for rank, entry in enumerate(feed.entries):
                title = entry.get("title", "")
                comments_url = entry.get("link", "")
                submitted_url = _extract_submitted_url(entry.get("summary", ""))
                # Self-posts still get a "[link]" anchor in the RSS summary, but it just
                # points back at the post's own comments page rather than an external
                # article, so that case must still count as a self-post.
                is_self_post = submitted_url is None or submitted_url.rstrip("/") == comments_url.rstrip("/")
                if _is_meta_thread(title, is_self_post=is_self_post):
                    skipped_meta_threads += 1
                    continue

                if submitted_url and not is_self_post and _is_image_link(submitted_url):
                    skipped_image_links += 1
                    continue

                is_review_thread = bool(_REVIEW_THREAD_PATTERN.search(title))
                is_trailer_thread = bool(submitted_url and _TRAILER_TITLE_PATTERN.search(title) and _YOUTUBE_HOST_PATTERN.search(submitted_url))
                # A self-post community discussion (question/anecdote/list) is not
                # reportable news. Only applies to unflagged self-posts, so leak/rumour
                # news, review threads, and trailer posts are never suppressed.
                if (
                    is_self_post
                    and not is_review_thread
                    and not is_trailer_thread
                    and _is_community_discussion(title)
                ):
                    skipped_community_discussion += 1
                    continue
                opencritic_stats = None
                if is_review_thread:
                    opencritic = _extract_opencritic(entry.get("summary", ""))
                    item_url = opencritic[0] if opencritic else comments_url
                    opencritic_stats = opencritic[1] if opencritic else None
                else:
                    item_url = submitted_url or comments_url

                items.append(
                    TrendingItem(
                        title=title,
                        url=item_url,
                        source="reddit",
                        engagement=(total_feed_entries - rank) * weight,
                        origin=f"r/{subreddit_name}",
                        skip_enrichment=is_review_thread or is_trailer_thread,
                        opencritic_stats=opencritic_stats,
                        raw_data_source=str(entry.get("summary", "")),
                        is_trailer_thread=is_trailer_thread,
                        is_review_thread=is_review_thread,
                    )
                )

            if skipped_meta_threads:
                logger.info(
                    "r/%s: skipped %d recurring meta-thread(s)",
                    subreddit_name,
                    skipped_meta_threads,
                )
            if skipped_image_links:
                logger.info(
                    "r/%s: skipped %d image/video link post(s)",
                    subreddit_name,
                    skipped_image_links,
                )
            if skipped_community_discussion:
                logger.info(
                    "r/%s: skipped %d self-post community-discussion thread(s)",
                    subreddit_name,
                    skipped_community_discussion,
                )
        except Exception:
            logger.exception("Failed to fetch trending posts from r/%s", subreddit_name)

    return [item for item in items if item.title and item.url], raw_feeds

def _parse_with_retry(url: str, user_agent: str, subreddit_name: str) -> tuple[feedparser.FeedParserDict, str] | None:
    """Parse a subreddit feed, retrying with backoff on HTTP 429."""
    for attempt in range(MAX_RETRIES_ON_RATE_LIMIT + 1):
        response = requests.get(url, headers={"User-Agent": user_agent})
        status = response.status_code

        if status == 429 and attempt < MAX_RETRIES_ON_RATE_LIMIT:
            wait_seconds = RETRY_BACKOFF_BASE_SECONDS * (attempt + 1)
            logger.warning(
                "r/%s returned HTTP 429, retrying in %ds (attempt %d/%d)",
                subreddit_name,
                wait_seconds,
                attempt + 1,
                MAX_RETRIES_ON_RATE_LIMIT,
            )
            time.sleep(wait_seconds)
            continue

        if status and status != 200:
            logger.warning("r/%s returned HTTP %s, skipping", subreddit_name, status)
            return None
        feed = feedparser.parse(response.text)
        if feed.bozo and not feed.entries:
            raise feed.bozo_exception

        if attempt == 0:
            logger.info("r/%s succeeded (%d entries)", subreddit_name, len(feed.entries))
        else:
            logger.info(
                "r/%s succeeded after %d retry(ies) (%d entries)",
                subreddit_name,
                attempt,
                len(feed.entries),
            )
        return feed, response.text

    return None
