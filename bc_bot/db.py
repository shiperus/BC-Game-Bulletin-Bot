from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS posted_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence INTEGER NOT NULL DEFAULT 1,
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posted_at ON posted_items (posted_at);
"""

# Debugging columns added after the initial release, capturing the full picture of
# how an item was assembled (which subreddit, its raw engagement score, the
# Reddit-submitted URL vs. whichever RSS article got fuzzy-matched to it) -- e.g. the
# title/link mismatches that MATCH_THRESHOLD tuning in sources/rss.py needs to
# diagnose. Applied via ALTER TABLE so an already-deployed DB (e.g. on the Pi) picks
# them up without a manual migration; all columns are nullable so old rows stay valid.
_MIGRATION_COLUMNS = {
    "origin": "TEXT",
    "engagement": "REAL",
    "reddit_url": "TEXT",
    "article_url": "TEXT",
    "article_title": "TEXT",
    "opencritic_stats": "TEXT",
    "raw_data_source": "TEXT"
}


class Store:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(posted_items)")}
        for column, column_type in _MIGRATION_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE posted_items ADD COLUMN {column} {column_type}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def recent_posts(self, retention_days: int) -> list[tuple[str, str]]:
        """Return (title, url) pairs posted within the retention window, for the
        caller to dedup fresh candidates against -- fetched once per cycle rather
        than re-querying per candidate."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT title, url FROM posted_items WHERE posted_at >= ?", (cutoff,)
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def record_posted(
        self,
        title: str,
        url: str,
        source: str,
        confidence: int,
        *,
        origin: str | None = None,
        engagement: float | None = None,
        reddit_url: str | None = None,
        article_url: str | None = None,
        article_title: str | None = None,
        opencritic_stats: str | None = None,
        raw_data_source: str | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO posted_items "
                "(title, url, source, confidence, posted_at, origin, engagement, "
                "reddit_url, article_url, article_title, opencritic_stats, raw_data_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    title,
                    url,
                    source,
                    confidence,
                    datetime.now(timezone.utc).isoformat(),
                    origin,
                    engagement,
                    reddit_url,
                    article_url,
                    article_title,
                    opencritic_stats,
                    raw_data_source
                ),
            )

    def cleanup_old(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM posted_items WHERE posted_at < ?", (cutoff,))
            return cursor.rowcount
