"""Hot posts from the bc-bulletin Devvit app.

The Devvit app (runs on Reddit) fetches hot posts through Reddit's own API every 4h
and posts them as an ``ingest.json`` attachment to a private Discord channel via
webhook. This module reads the newest unprocessed payload from that channel over
Discord's REST API and turns it into the same RawPost records the RSS source
produces, so both run through identical filter rules (reddit.filter_posts).

Payload shape (version 1)::

    {"version": 1, "fetchedAt": "<iso>", "errors": [...],
     "posts": [{"id": "t3_..", "subreddit": "Games", "rank": 1, "title": "..",
                "url": "..", "permalink": "..", "isSelf": false, "body": ".."|null,
                "score": 123, "numComments": 45, "flair": ".."|null,
                "createdUtc": 1795330825, "stickied": false, "nsfw": false}, ...]}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from bc_bot.config import Config
from bc_bot.sources.reddit import RawPost

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
ATTACHMENT_NAME = "ingest.json"
CONTENT_PREFIX = "bc-ingest"
SUPPORTED_VERSION = 1
LAST_MESSAGE_KEY = "devvit_ingest_last_message_id"
HISTORY_PAGE_SIZE = 25
REQUEST_TIMEOUT_SECONDS = 30


@dataclass
class IngestPayload:
    message_id: str | None  # None when loaded from a local file
    fetched_at: datetime
    payload: dict


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate(payload: dict) -> dict:
    if payload.get("version") != SUPPORTED_VERSION:
        raise ValueError(f"Unsupported ingest payload version: {payload.get('version')!r}")
    if not isinstance(payload.get("posts"), list):
        raise ValueError("Ingest payload has no 'posts' list")
    for error in payload.get("errors") or []:
        logger.warning("Devvit app reported a fetch error: %s", error)
    return payload


def load_payload_file(path: Path) -> IngestPayload:
    payload = _validate(json.loads(path.read_text(encoding="utf-8")))
    return IngestPayload(None, _parse_iso(payload["fetchedAt"]), payload)


def fetch_latest_payload(config: Config, after_message_id: str | None) -> IngestPayload | None:
    """Newest ingest payload posted after ``after_message_id``, or None if there is
    none (or it's older than INGEST_MAX_AGE_HOURS). Older unprocessed payloads are
    skipped on purpose: each one is a full hot-list snapshot, so only the newest
    matters."""
    headers = {
        "Authorization": f"Bot {config.discord_token}",
        "User-Agent": config.reddit_user_agent,
    }
    response = requests.get(
        f"{DISCORD_API}/channels/{config.discord_ingest_channel_id}/messages",
        params={"limit": HISTORY_PAGE_SIZE},
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    after = int(after_message_id) if after_message_id else 0
    candidates = []
    for message in response.json():
        if int(message["id"]) <= after:
            continue
        attachment = next(
            (a for a in message.get("attachments", []) if a.get("filename") == ATTACHMENT_NAME),
            None,
        )
        if attachment is None:
            # Webhook messages need the bot's Message Content intent enabled, or
            # Discord returns them with empty content/attachments.
            if not message.get("content") and not message.get("attachments"):
                logger.warning(
                    "Ingest channel message %s has no content/attachments -- is the "
                    "Message Content intent enabled for the bot?",
                    message["id"],
                )
            continue
        candidates.append((int(message["id"]), attachment))

    if not candidates:
        logger.info("No new Devvit ingest payload since message %s", after_message_id)
        return None

    message_id, attachment = max(candidates, key=lambda c: c[0])
    file_response = requests.get(attachment["url"], timeout=REQUEST_TIMEOUT_SECONDS)
    file_response.raise_for_status()
    payload = _validate(file_response.json())
    fetched_at = _parse_iso(payload["fetchedAt"])

    age = datetime.now(timezone.utc) - fetched_at
    if age > timedelta(hours=config.ingest_max_age_hours):
        logger.warning(
            "Newest Devvit ingest payload (message %s) is %.1fh old (> %.1fh); ignoring it",
            message_id,
            age.total_seconds() / 3600,
            config.ingest_max_age_hours,
        )
        return None

    logger.info(
        "Devvit ingest payload: message %s, fetched %s, %d posts",
        message_id,
        payload["fetchedAt"],
        len(payload["posts"]),
    )
    return IngestPayload(str(message_id), fetched_at, payload)


def payload_to_posts(payload: dict) -> tuple[list[RawPost], dict[str, str]]:
    """Convert a payload into RawPosts plus per-subreddit raw JSON for
    cycle_raw_feeds (the archive the weekly review/replay reads)."""
    posts_by_sub: dict[str, list[dict]] = {}
    for post in payload["posts"]:
        posts_by_sub.setdefault(post["subreddit"], []).append(post)

    raw_posts: list[RawPost] = []
    raw_feeds: dict[str, str] = {}
    for subreddit, sub_posts in posts_by_sub.items():
        raw_feeds[subreddit] = json.dumps(
            {"version": payload["version"], "fetchedAt": payload["fetchedAt"], "posts": sub_posts},
            ensure_ascii=False,
        )
        ordered = sorted(sub_posts, key=lambda p: p["rank"])
        for index, post in enumerate(ordered):
            is_self = bool(post["isSelf"])
            raw_posts.append(
                RawPost(
                    post_id=post["id"],
                    subreddit=subreddit,
                    title=post["title"],
                    comments_url=post["permalink"],
                    submitted_url=None if is_self else post["url"],
                    is_self_post=is_self,
                    body=post.get("body") or "",
                    body_format="markdown",
                    rank=index,
                    total=len(ordered),
                    stickied=bool(post.get("stickied")),
                    score=post.get("score"),
                    num_comments=post.get("numComments"),
                    flair=post.get("flair"),
                )
            )
    return raw_posts, raw_feeds


def is_ingest_raw_feed(raw_feed: str) -> bool:
    """cycle_raw_feeds holds RSS XML for RSS cycles and JSON for Devvit cycles."""
    return raw_feed.lstrip().startswith("{")
