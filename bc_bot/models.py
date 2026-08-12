from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TrendingItem:
    title: str
    url: str
    source: str  # "reddit"
    engagement: float
    origin: str  # e.g. "r/Games" or channel title
    sources: set[str] = field(default_factory=set)
    confidence: int = 1
    article_url: str | None = None
    article_title: str | None = None
    skip_enrichment: bool = False
    opencritic_stats: str | None = None
    raw_data_source: str | None = None
    is_trailer_thread: bool = False
    is_review_thread: bool = False

    def __post_init__(self) -> None:
        if not self.sources:
            self.sources = {self.source}

    @property
    def link(self) -> str:
        return self.article_url or self.url

    @property
    def display_title(self) -> str:
        """Title as it should be posted -- title stays unadorned everywhere else
        (consolidate(), select_fresh()) so fuzzy matching isn't thrown off by a
        suffix that may or may not be present on a given duplicate."""
        if self.opencritic_stats:
            return f"{self.title} — {self.opencritic_stats}"
        return self.title


@dataclass
class Article:
    title: str
    link: str
    source: str
    published: str | None = None
