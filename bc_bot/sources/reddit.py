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
    # r/gaming's recurring weekly indie-game showcase hub ("Indie Sunday Hub - <Month> <Day>, <Year>")
    # is a community megathread, not reportable news. It shows up every week in "hot" and carries
    # no external article link, so without this it falls through to the news channel.
    re.compile(r"\bindie sunday hub\b", re.IGNORECASE),
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
#
# "showcase" carries "\w*" like the other keywords so inflected forms ("...Showcased
# by Embark Studios...", "showcasing") still trigger trailer routing -- previously a
# plain "showcase\b" (word-boundary) missed "showcased"/"showcasing" and let a
# YouTube gameplay-showcase post land in news.
_TRAILER_TITLE_PATTERN = re.compile(
    r"\b(?:announc\w*|reveal\w*|teaser\w*|gameplay|showcas\w*|preview\w*|trailer\w*)\b",
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
# "reccomend\w*" is the common misspelling that slips past the "recommend\w*" branch.
_REQUEST_PATTERN = re.compile(
    r"\brecommend\w*\b"
    r"|\breccomend\w*\b"
    r"|\bcan we (?:please )?(?:normalize|stop|bring|get|make|fix|talk|just)\b"
    r"|\bshould we\b",
    re.IGNORECASE,
)
# Community-poll phrasings addressed straight at the reader ("Games you did a complete
# 180 on." / "Games You Miss That Were Shut Down"). Real news self-posts (leaks/
# rumours) never address "you did"/"you ever"/"you miss".
_DIRECTED_AT_READER_PATTERN = re.compile(
    r"\byou (?:did|ever|100\w*|miss|remember|regret)\b",
    re.IGNORECASE,
)
# A self-post that opens with an explicit "(unpopular opinion)"/"rant" disclaimer is a
# first-person opinion post regardless of what follows ("Rant: Don't announce a game
# unless you have gameplay to show for it"). News reports never self-disclaim as a rant.
_RANT_PREFIX_PATTERN = re.compile(r"^\s*(?:rant|unpopular opinion)(?:\s*[:?\u2014\u2013]|\b)", re.IGNORECASE)
# A comparison/share prompt aimed at players who have played something ("For those that
# have played Valheim and Dragonwilds…") solicits the community, it doesn't report news.
_MID_COMMUNITY_SEEK_PATTERN = re.compile(
    r"\bfor (?:those|anyone|everyone|people|someone) (?:that|who) (?:have|has) played\b",
    re.IGNORECASE,
)
# A leading "In which …" survey/poll question ("In which games you've played, were there
# many references to other games…?") rather than a report; survey phrasings never lead
# a real news leak/rumour self-post.
_LEAD_IN_WHICH_PATTERN = re.compile(r"^\s*in which\b", re.IGNORECASE)
# A superlative "…the best/greatest X?" question asking the community for its pick
# ("Games with the best stealth gameplay?"). Guarded to self-posts at the call site, so a
# news-article headline containing "the best" can't be suppressed.
_SUPERLATIVE_QUESTION_PATTERN = re.compile(r"\bthe (?:best|greatest)\b.*\?", re.IGNORECASE)
# Subreddit/meta housekeeping self-posts ("Don't forget to join the official … Discord!")
# are community promotion, not gaming news.
_META_PROMO_PATTERN = re.compile(
    r"^\s*don'?t forget to (?:join|check|follow|subscribe)\b", re.IGNORECASE
)
# A self-post that opens with a leading conditional first-person gripe ("If I never
# encounter another pack of wild wolves in an RPG it'll still be too soon") is an
# anecdote/complaint, not a report. News self-posts (leaks/rumours) never open "If I".
_LEAD_IF_PATTERN = re.compile(r"^\s*if i\b", re.IGNORECASE)
# A bare leading superlative/favourite pick ("Most cinematic game ?", "Favorite Twin
# stick shooter") is a community poll/shout-out, not a news report. Guarded to
# self-posts, so a news headline that happens to start "Most anticipated …" (a link
# post or an article) is untouched.
_LEAD_SUPERLATIVE_PATTERN = re.compile(
    r"^\s*(?:the )?(?:most|best|greatest|favorite|favourite)\b", re.IGNORECASE
)
# A self-post asking for game recommendations ("Looking for games: Liminal or
# similarly minimalist walking sims…", "Looking for a game: leveling up minions…")
# solicits the community rather than reporting news.
_LOOKING_FOR_GAME_PATTERN = re.compile(
    r"\blooking for (?:a |an |some |new |good )?(?:game|games)\b", re.IGNORECASE
)
# First-person experiential wording on a self-post: "<Game> made me appreciate …",
# "<Studio> has shown me that …" -- an anecdote, not a report.
_MADE_ME_PATTERN = re.compile(
    r"\bmade me (?:appreciate|realize|notice|think|feel|enjoy|want|hate|love)\b",
    re.IGNORECASE,
)
_SHOWN_ME_PATTERN = re.compile(r"\bshown me (?:that|how)\b", re.IGNORECASE)
# A bare evaluative verdict on a self-post ("Blood of Dawnwalker is very good",
# "Onimusha is incredible") is an opinion post. End-anchored so a phrase like
# "…is good news" in the middle of a report isn't suppressed; self-post guard means
# article headlines are never at risk.
_BARE_VERDICT_PATTERN = re.compile(
    r"\bis (?:very |so |really |incredibly )?(?:good|great|incredible|amazing|"
    r"fantastic|excellent|wonderful|terrible|awful|boring|overrated|underrated)\b\s*$",
    re.IGNORECASE,
)
# A subjective observation/anecdote restated as a general truth ("Sometimes is nice
# when an NPC acknowledges your weird behavior that you take for granted") -- a
# first-person opinion, not a report. The "take for granted" / "is nice when" combo
# phrasings are community-opinion markers never found in news-article headlines.
_OBSERVATION_PATTERN = re.compile(
    r"\btake(?:n)? for granted\b|(?:\bis|\bit'?s) (?:nice|great|cool|weird|odd|satisfying) when\b",
    re.IGNORECASE,
)
# A self-post recounting a personal completion/playthrough ("Just 100% completed
# both Shadow of Mordor and Shadow of War back-to-back") is an anecdote, not a
# report. The completion verb guard keeps "Just Dance…" / "just announced…" news
# titles (link posts) from matching.
_JUST_PLAYED_PATTERN = re.compile(
    r"^\s*just\b[^.!?\n]{0,45}\b(?:completed|finished|beat|replayed|platinum|100%)\b",
    re.IGNORECASE,
)
# A community poll/posed prompt ("Name one thing in any game that everyone agrees
# is either perfect or absolutely sucks") solicits a single answer, not a report.
_NAME_ONE_PATTERN = re.compile(r"^\s*name (?:one|a|an|your|the)\b", re.IGNORECASE)
# A reader-experience survey ("people with ADHD, how does it affect you and
# gaming?") addressed straight at the respondent rather than reporting news.
_READER_EXPERIENCE_PATTERN = re.compile(
    r"\bhow does (?:it|that)\b[^?!\n]{0,40}\b(?:you|your)\b|\baffects? you\b",
    re.IGNORECASE,
)
# A "What <X> is best/recommended for <Y>?" help-me-choose prompt ("what version of
# Linux is best for gaming and why?") rather than a report. Matched against the RAW
# title (it needs the trailing "?"), so a restated news headline that happens to ask
# a question ("Will the next Mass Effect release under EA?") is untouched.
_WHAT_BEST_QUESTION_PATTERN = re.compile(
    r"\bwhat\b[^?\n]{0,50}\b(?:best|recommend\w*)\b[^?\n]{0,60}\?", re.IGNORECASE
)
# A bare "[plural noun] that feel(s) like <season/mood>?" recommendation prompt
# ("games that feel like October?"). Matched against the raw title for the "?".
_FEEL_LIKE_ASK_PATTERN = re.compile(
    r"^\s*(?:games?|things?)\b[^?\n]{0,40}\bfeel(?:s)? like\b[^?\n]{0,40}\?",
    re.IGNORECASE,
)
# A leading "games that <mix/are/feature/...>?" surrogate-selection prompt ("Games that
# mix horror and comedy?", "Games that tells stories in creative ways other than
# cutscenes") solicits the community for titles rather than reporting news. Guarded to
# self-posts, so a news-article headline starting "Games that ..." (a link post) is
# never suppressed.
_GAMES_THAT_PROMPT_PATTERN = re.compile(r"^\s*games?\s+that\b", re.IGNORECASE)
# A "Best <thing> games?" help-me-choose ask ("New to PC gaming: Best Puzzle / Mystery
# Games?") rather than a report. Broader than _SUPERLATIVE_QUESTION_PATTERN (which
# requires a leading "the") and guarded to self-posts at the call site.
_BEST_GAMES_QUESTION_PATTERN = re.compile(
    r"\bbest\b[^?\n]{0,50}\bgames?\b[^A-Za-z]{0,8}\?", re.IGNORECASE
)
# A community self-post recounting a playthrough/completion "for the Nth time" ("A
# buddy just finished Witcher 3 for the 14th time.") is an anecdote, not a report. The
# completion verb is required so a news headline merely mentioning a count ("...for a
# second time...") can't be suppressed; self-post guard keeps article headlines safe.
_N_TH_TIME_PATTERN = re.compile(
    r"\b(?:just )?(?:finished|beat|beaten|completed|replayed|platinum\w*|100%s?)\b"
    r"[^.\n]{0,40}\bfor (?:the|a) \d+[a-z]* time\b",
    re.IGNORECASE,
)
# A self-post promoting something and asking the reader to act ("...on GOG. Check it out
# and please vote for it!") is community promotion, not news. "vote for"/"check it out"
# phrasings never lead a real news self-post.
_VOTE_PROMO_PATTERN = re.compile(
    r"\b(?:please )?vote\w* for\b|\bcheck it out\b", re.IGNORECASE
)
# A self-post that is a long-form community write-up ("Star Citizen in 2026 - A
# comprehensive review") rather than a news report. Guarded to self-posts so an outlet's
# "comprehensive review" article headline (a link post) is never suppressed.
_COMPREHENSIVE_REVIEW_PATTERN = re.compile(r"\bcomprehensive review\b", re.IGNORECASE)
# A self-post that is an AMA ("Ask us anything") -- a community Q&A on r/Games / r/gaming
# with a developer/profile (dev launches, career retrospectives), e.g. "Right after
# graduation, I jumped into indie dev ... - AMA" or "AMA: Alex & friends from ATOM TEAM ...
# Ask us anything!". Solicits the community rather than reporting news. Guarded to
# self-posts, so a news headline that happens to contain "AMA" as a word (e.g. a medical
# licensing story) is never suppressed as a link post.
_AMA_PATTERN = re.compile(r"\bama\b|ask us anything", re.IGNORECASE)
# A self-post phrased as "Am I the only one ...?" / "the only one ...?" -- a first-person
# gripe/survey addressed at the community rather than a report (e.g. "Am I the only one
# that just wants long simple games?"). News self-posts (leaks/rumours) never open with
# an "am I the only one" identity-confession.
_ONLY_ONE_PATTERN = re.compile(r"\bthe only one\b", re.IGNORECASE)
# Recurring daily/weekly community HUB posts build on the day of the week but don't use
# the "!monday"-style banner _DAY_OF_WEEK_PATTERN requires -- e.g. "Indie Sunday Hub -
# October 11, 2026". These are recurring community roundups, not reportable news. The
# follow-on noun is required so "announced for Wednesday" news headlines are untouched.
_DAY_HUB_PATTERN = re.compile(
    r"\b(sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b\s+"
    r"(?:hub|digest|catchup|recap|megathread)\b",
    re.IGNORECASE,
)
# A crowd-sourced subset list request ("Non-sequel games that have numbers in the title",
# "Co-op games that are fun with two players") -- the OP enumerates a category of games
# and asks the community to fill in titles, rather than reporting news. Broader than
# _GAMES_THAT_PROMPT_PATTERN (which requires "games that" at the very start) and guarded
# to self-posts at the call site so "Open-world games that ..." article headlines (link
# posts) are never suppressed.
_SUBSET_GAMES_LIST_PATTERN = re.compile(
    r"\b(?:non[- ]sequel|co[- ]op|local|multiplayer|single[- ]player|indie|party|"
    r"open[- ]world|roguelike|puzzle|strategy|horror|retro|classic|chill|relaxing)\s+"
    r"games?\s+that\b",
    re.IGNORECASE,
)
# Advice/decision-help question aimed at the reader ("...DLC. Should I start a new save or is it a
# late game expansion?"), a first-person seek poll ("Am I the only one that just wants long simple
# games?"), and a reverie/recollection opener ("Was there a classic game that had a genuinely obtuse
# unlock or secret") -- all community prompts, not reports. Guarded to self-posts at the call site.
_ASK_I_PATTERN = re.compile(r"\bshould i\b|\bwould i\b|\bam i the only one\b", re.IGNORECASE)
_RECOLLECTION_PATTERN = re.compile(r"^(?:was|were) there\b|\bdo you remember\b", re.IGNORECASE)
# Recommendation-request phrasing on a self-post: "Games with good martial arts combat", "Games
# similar to Viva Piñata?" -- the OP wants suggestions (a list of titles from the community), not
# news. The bare "Games with/similar to" opener either ends in a "?" or implies one.
_RECOMMEND_LIST_PATTERN = re.compile(
    r"^\s*games?\s+(?:with|similar to|that play like|like)\b|\bwhat games?\s+",
    re.IGNORECASE,
)
# A community poll/opinion solicitation ("In your opinion, what video game has the worst
# challenges?", "...has, in a way, taught me to get the most enjoyment out of single player games").
# "in your opinion" and "taught me" are unmistakable opinion/discussion signals on a self-post.
_OPINION_SOLICIT_PATTERN = re.compile(r"\bin your opinion\b|\btaught me\b", re.IGNORECASE)
# A first-person "I would say ...", "I do dislike/love ..." verdict on a self-post ("Star Wars:
# Galactic Racer has got to be the best ... I would say", "New Star Wars games have been great
# lately. But I do dislike ..."). First-person verdict phrasing mid-title is a personal take, not a
# report. Reuses the apostrophe-stripped title like _FIRST_PERSON_MID_PATTERN.
_FIRST_PERSON_VERDICT_PATTERN = re.compile(
    r"\bi (?:would say|do (?:dislike|hate|love|wish)|personally)\b", re.IGNORECASE
)
# A leading all-caps community plea/advice ("PLEASE do some quick research before getting a game ...")
# is a reader-directed PSA/hot-take, not reportable news.
_LEAD_PLEASE_PATTERN = re.compile(r"^please\b", re.IGNORECASE)
# A self-post that is a crowd-sourced reminder/retrospective list ("With the Bioware
# Shutdown, Here's a Reminder of EA's History of Acquisitions") -- the OP compiles a
# community recap rather than reporting news. "here's a reminder"/"a reminder of"
# phrasings never lead a news self-post (leaks/rumours).
_REMINDER_LIST_PATTERN = re.compile(
    r"\bhere'?s a reminder\b|\ba reminder of\b", re.IGNORECASE
)
# A reader-directed "you were cheated" gripe ("If you bought Blightstone in early
# access you just got robbed.") -- a consumer complaint/anecdote aimed at the
# community, not a report.
_YOU_GOT_SCREWED_PATTERN = re.compile(
    r"\byou (?:just )?(?:got|were) (?:robbed|scammed|screwed|ripped off)\b", re.IGNORECASE
)
# A "That one <thing> from <game> ..." recollection poll ("That one quote/phrase from
# the game that sticks in your head for +10 years?") -- the OP invites the community to
# share its own pick, not a report.
_THAT_ONE_PATTERN = re.compile(r"^\s*that one\b", re.IGNORECASE)
# A community-opinion question aimed at a company's course of action ("Should Steam
# add an AI filter?") rather than a report. Matched against the RAW title so the
# trailing "?" is still present; guarded to self-posts at the call site.
_SHOULD_COMPANY_QUESTION_PATTERN = re.compile(
    r"\bshould (?:steam|valve|nintendo|sony|microsoft|xbox|ea|ubisoft|capcom|rockstar|"
    r"epic|bethesda|activision|blizzard|cd projekt|fromsoftware|insomniac|bungie|konami|"
    r"square enix|playstation)\b[^?\n]{0,60}\?",
    re.IGNORECASE,
)
# A self-post that opens with an explicit "Discussion:" banner ("Discussion: EA is
# squatting on a gold mine of dormant IPs") is a community-discussion prompt, not a
# report. Only matched at the very start on self-posts, so an article headline that
# happens to contain "discussion" later on is untouched.
_DISCUSSION_PREFIX_PATTERN = re.compile(r"^\s*discussion\b\s*[:：]?", re.IGNORECASE)
# A normative community gripe about the industry ("...is ruining online games. Developers
# are punishing players instead of addressing the problem.") -- an opinion post, not a
# report. Guarded to self-posts so an article headline with "ruining" (a link post) is safe.
_RUINING_GRIPE_PATTERN = re.compile(r"\b(?:is|are) ruining\b", re.IGNORECASE)
# A survey soliciting examples ("Examples of video game franchises where they took on
# feedback...?") -- the OP asks the community to enumerate titles, not a report.
_EXAMPLES_OF_PATTERN = re.compile(r"^\s*examples? of\b", re.IGNORECASE)
# A retrospective recap ("A look back at The Game Awards 2025 Trailers and Announcements
# and how they reviewed!") -- a community roundup, not reportable news. Guarded to
# self-posts so a news article titled "A look back at ..." (a link post) is never suppressed.
_LOOK_BACK_PATTERN = re.compile(r"^\s*a look back at\b", re.IGNORECASE)
# A long-form community review write-up ("A Review of RAGE (2011) | What's to Rage About
# RAGE?") -- a user's opinion piece, not a report. Guarded to self-posts so a link to an
# outlet's review article is never suppressed.
_A_REVIEW_OF_PATTERN = re.compile(r"^\s*a review of\b", re.IGNORECASE)
# Strip surrounding quotes/punctuation so a lead question/opinion is recognised even
# when the OP wraps it in quotes (e.g. "\"Headshot!\" (UT) ... What sound snippets...").
_COMMUNITY_STRIP_PATTERN = re.compile(r"[“”\"\'.!:?,\u2018\u2019]")


def _is_community_discussion(title: str) -> bool:
    """True for titles phrased as a community survey/anecdote rather than a report:
    leading question words, leading first-person opinions/gripes, a crowd-sourced
    list, an ask directed at "you/your", an "I just realized"-style recollection,
    first-person/contrarian opinion, recommendation, and reader-addressed
    phrasings that sit mid-title, an explicit rant/"unpopular opinion" opener, a
    "for those who have played X" comparison prompt, an "in which …" survey
    question, a "…the best X?" pick question, a subreddit-promo housekeeping
    post, a leading conditional "If I …" gripe, a bare superlative/"favorite …"
    pick, a "Looking for games: …" recommendation request, a "<Game> made me …" /
    "…has shown me that …" anecdote, or a bare "X is very good/incredible"
    verdict (see patterns above)."""
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
        or _RANT_PREFIX_PATTERN.search(t) is not None
        or _MID_COMMUNITY_SEEK_PATTERN.search(t) is not None
        or _LEAD_IN_WHICH_PATTERN.search(t) is not None
        or _SUPERLATIVE_QUESTION_PATTERN.search(t) is not None
        or _SUPERLATIVE_QUESTION_PATTERN.search(title) is not None
        or _META_PROMO_PATTERN.search(t) is not None
        or _LEAD_IF_PATTERN.search(t) is not None
        or _LEAD_SUPERLATIVE_PATTERN.search(t) is not None
        or _LOOKING_FOR_GAME_PATTERN.search(t) is not None
        or _MADE_ME_PATTERN.search(t) is not None
        or _SHOWN_ME_PATTERN.search(t) is not None
        or _BARE_VERDICT_PATTERN.search(t) is not None
        or _OBSERVATION_PATTERN.search(t) is not None
        or _JUST_PLAYED_PATTERN.search(t) is not None
        or _NAME_ONE_PATTERN.search(t) is not None
        or _READER_EXPERIENCE_PATTERN.search(t) is not None
        # These two need a literal trailing "?" so they match the raw (un-stripped)
        # title; _COMMUNITY_STRIP_PATTERN would remove the "?" first.
        or _WHAT_BEST_QUESTION_PATTERN.search(title) is not None
        or _FEEL_LIKE_ASK_PATTERN.search(title) is not None
        or _GAMES_THAT_PROMPT_PATTERN.search(t) is not None
        or _BEST_GAMES_QUESTION_PATTERN.search(title) is not None
        or _N_TH_TIME_PATTERN.search(t) is not None
        or _VOTE_PROMO_PATTERN.search(t) is not None
        or _COMPREHENSIVE_REVIEW_PATTERN.search(t) is not None
        or _AMA_PATTERN.search(t) is not None
        or _ONLY_ONE_PATTERN.search(t) is not None
        or _DAY_HUB_PATTERN.search(t) is not None
        or _SUBSET_GAMES_LIST_PATTERN.search(t) is not None
        or _ASK_I_PATTERN.search(t) is not None
        or _RECOLLECTION_PATTERN.search(t) is not None
        or _RECOMMEND_LIST_PATTERN.search(t) is not None
        or _OPINION_SOLICIT_PATTERN.search(t) is not None
        or _FIRST_PERSON_VERDICT_PATTERN.search(t) is not None
        or _LEAD_PLEASE_PATTERN.search(t) is not None
        or _REMINDER_LIST_PATTERN.search(t) is not None
        or _YOU_GOT_SCREWED_PATTERN.search(t) is not None
        or _THAT_ONE_PATTERN.search(t) is not None
        or _DISCUSSION_PREFIX_PATTERN.search(t) is not None
        or _RUINING_GRIPE_PATTERN.search(t) is not None
        or _EXAMPLES_OF_PATTERN.search(t) is not None
        or _LOOK_BACK_PATTERN.search(t) is not None
        or _A_REVIEW_OF_PATTERN.search(t) is not None
        or _SHOULD_COMPANY_QUESTION_PATTERN.search(title) is not None
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

def flag_post_enrichment_trailers(items: list[TrendingItem]) -> None:
    """Re-classify items whose final (post-enrichment) link is a YouTube video with a
    trailer-class title as trailer-channel items.

    At fetch time an item is only flagged ``is_trailer_thread`` when the
    *Reddit-submitted* URL is itself a YouTube link. But RSS enrichment can attach a
    matching YouTube article/video URL afterwards -- e.g. a Reddit link post whose
    final link (``item.link``) becomes ``https://youtube.com/watch?v=...`` -- while
    the title already contains a trailer keyword (announce/reveal/teaser/gameplay/
    showcase/preview/trailer). Such items are game-trailer-channel content and were
    falling through to the news channel. Review threads are never touched, and items
    whose final link is not a YouTube video are left alone.
    """
    for item in items:
        if item.is_review_thread or item.is_trailer_thread:
            continue
        if (
            item.link
            and _TRAILER_TITLE_PATTERN.search(item.title)
            and _YOUTUBE_HOST_PATTERN.search(item.link)
        ):
            item.is_trailer_thread = True


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
