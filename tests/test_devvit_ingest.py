"""Run with:  python -m unittest discover -s tests"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bc_bot import pipeline
from bc_bot.config import Config
from bc_bot.db import Store
from bc_bot.sources import devvit_ingest, reddit

FIXTURE = Path(__file__).parent / "fixtures" / "ingest_sample.json"


def _decisions_by_id(config: Config) -> dict[str, tuple[str, str | None]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    posts, _ = devvit_ingest.payload_to_posts(payload)
    _, decisions = reddit.filter_posts(posts, config)
    return {d.post_id: (d.reason, d.url) for d in decisions}


class FilterRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.decisions = _decisions_by_id(Config())

    def reason(self, post_id: str) -> str:
        return self.decisions[post_id][0]

    def url(self, post_id: str) -> str | None:
        return self.decisions[post_id][1]

    def test_stickied_and_megathreads_dropped(self) -> None:
        self.assertEqual(self.reason("t3_1y2j0li"), "meta_thread")  # "Simple Questions Sunday!"
        self.assertEqual(self.reason("t3_1y092pt"), "meta_thread")  # [MEGATHREAD]
        self.assertEqual(self.reason("t3_1xy0j3v"), "meta_thread")  # weekly thread w/ date

    def test_image_post_dropped(self) -> None:
        self.assertEqual(self.reason("t3_1y37kum"), "image_link")

    def test_community_discussion_dropped(self) -> None:
        self.assertEqual(self.reason("t3_1y2yr0t"), "community_discussion")

    def test_link_posts_keep_submitted_url(self) -> None:
        self.assertEqual(self.reason("t3_1y1cgxo"), "kept")
        self.assertEqual(
            self.url("t3_1y1cgxo"),
            "https://variety.com/2026/gaming/news/gta-6-sales-3-79-billion-1236954198/",
        )

    def test_self_post_source_extraction(self) -> None:
        # bare URL on the first line
        self.assertEqual(
            self.url("t3_1y21hd3"), "https://insider-gaming.com/nioh-3-complete-edition-rated/"
        )
        # identical to the r/Games link post -> exact-URL dedup can merge them
        self.assertEqual(self.url("t3_1y1z2uf"), self.url("t3_1y1wss6"))
        # [Source](...) label wins
        self.assertEqual(
            self.url("t3_1y1w171"),
            "https://openai.com/index/advancing-chip-design-with-our-models/",
        )
        # escaped underscore restored
        self.assertEqual(
            self.url("t3_1xzn1ut"), "https://twitterviewer.net/TTGames/status/2122392889917403439"
        )

    def test_self_post_without_usable_source_keeps_comments_page(self) -> None:
        # only a reddit share link -> falls back to the post itself
        self.assertEqual(
            self.url("t3_1y0o8w0"),
            "https://www.reddit.com/r/GamingLeaksAndRumours/comments/1y0o8w0/kotor_remake_likely_to_be_rerevealed_at_the_game/",
        )
        # no links at all
        self.assertEqual(
            self.url("t3_1y238j2"),
            "https://www.reddit.com/r/gaming/comments/1y238j2/another_crabs_treasure/",
        )

    def test_youtube_is_last_resort_source(self) -> None:
        self.assertEqual(self.url("t3_1y3398f"), "https://youtu.be/8VE-SyldTgU")
        # Indie Sunday: Steam is skipped, trailer remains
        self.assertEqual(self.url("t3_1y2nfwj"), "https://youtu.be/9gvBWs78XBM")


class StickiedFlagTest(unittest.TestCase):
    def test_stickied_without_meta_title_is_dropped(self) -> None:
        post = reddit.RawPost(
            post_id="t3_x",
            subreddit="Games",
            title="Rockstar announces new studio",
            comments_url="https://www.reddit.com/r/Games/comments/x/",
            submitted_url="https://example.com/news",
            is_self_post=False,
            body="",
            body_format="markdown",
            rank=0,
            total=1,
            stickied=True,
        )
        item, reason = reddit.build_item(post, 1.0)
        self.assertIsNone(item)
        self.assertEqual(reason, "stickied")


class MarkdownHelpersTest(unittest.TestCase):
    def test_opencritic_markdown(self) -> None:
        body = (
            "Reviews below.\n\n**Review Aggregator:**\n\n"
            "[**OpenCritic - 88 average - 95% recommended - 58 reviews**]"
            "(https://opencritic.com/game/1234/some-game)"
        )
        self.assertEqual(
            reddit._extract_opencritic_markdown(body),
            (
                "https://opencritic.com/game/1234/some-game",
                "OpenCritic 88 · 95% Recommended · 58 Reviews",
            ),
        )

    def test_unescape_markdown(self) -> None:
        self.assertEqual(reddit.unescape_markdown(r"LEGO\_Group \[SPOILERS\]"), "LEGO_Group [SPOILERS]")


class PayloadTest(unittest.TestCase):
    def _variety(self, config: Config):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        posts, raw_feeds = devvit_ingest.payload_to_posts(payload)
        self.assertEqual(set(raw_feeds), {"gaming", "Games", "GamingLeaksAndRumours"})
        items, _ = reddit.filter_posts(posts, config)
        return next(i for i in items if i.post_id == "t3_1y1cgxo")

    def test_engagement_is_score_by_default(self) -> None:
        variety = self._variety(Config())
        self.assertEqual(variety.engagement, 51005.0)
        self.assertEqual(variety.score, 51005)

    def test_engagement_position_mode(self) -> None:
        config = Config()
        config.rank_by_score = False
        # r/gaming has 6 posts in the fixture; variety is rank 6 (index 5) -> (6-5) * 3
        self.assertEqual(self._variety(config).engagement, 3.0)


class ScoreRulesTest(unittest.TestCase):
    def _post(self, title: str, score: int | None) -> reddit.RawPost:
        return reddit.RawPost(
            post_id="t3_s",
            subreddit="gaming",
            title=title,
            comments_url="https://www.reddit.com/r/gaming/comments/s/",
            submitted_url=None,
            is_self_post=True,
            body="",
            body_format="markdown",
            rank=0,
            total=1,
            score=score,
        )

    def test_low_score_dropped_only_when_score_known(self) -> None:
        title = "Valid criticism in ratings is a great thing, but blatant lies in review bombing are childish"
        item, reason = reddit.build_item(self._post(title, 0), 1.0, min_score=10)
        self.assertIsNone(item)
        self.assertEqual(reason, "low_score")
        # RSS posts have no score -> unaffected
        item, reason = reddit.build_item(self._post(title, None), 1.0, min_score=10)
        self.assertEqual(reason, "kept")

    def test_review_bombing_is_not_coverage(self) -> None:
        from bc_bot import aggregator
        from bc_bot.models import TrendingItem

        bomb = TrendingItem("Blatant lies in review bombing are childish", "u", "reddit", 1, "r/gaming")
        review = TrendingItem("Phantom Blade Zero review roundup", "u2", "reddit", 1, "r/Games")
        aggregator.boost_announcements([bomb, review])
        self.assertEqual(bomb.confidence, 1)
        self.assertEqual(review.confidence, 2)

    def test_popular_boost(self) -> None:
        from bc_bot import aggregator
        from bc_bot.models import TrendingItem

        big = TrendingItem("GTA VI makes $3.79B", "a", "reddit", 1, "r/gaming", score=51046)
        small = TrendingItem("Small story", "b", "reddit", 1, "r/gaming", score=200)
        rss_item = TrendingItem("RSS story", "c", "reddit", 1, "r/Games")
        aggregator.boost_popular([big, small, rss_item], 1000)
        self.assertEqual([big.confidence, small.confidence, rss_item.confidence], [2, 1, 1])


_ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>t3_aaa</id><title>Weekly /r/Games Discussion - November 22, 2026</title>
<link href="https://www.reddit.com/r/Games/comments/aaa/weekly/"/>
<content type="html">&lt;a href="https://www.reddit.com/r/Games/comments/aaa/weekly/"&gt;[link]&lt;/a&gt;</content></entry>
<entry><id>t3_bbb</id><title>Rockstar explains why you can't play darts in GTA 6</title>
<link href="https://www.reddit.com/r/Games/comments/bbb/darts/"/>
<content type="html">&lt;a href="https://www.videogameschronicle.com/news/darts/"&gt;[link]&lt;/a&gt;</content></entry>
<entry><id>t3_ccc</id><title>What game are you playing this weekend?</title>
<link href="https://www.reddit.com/r/Games/comments/ccc/what/"/>
<content type="html">just curious</content></entry>
</feed>"""


class RssPathTest(unittest.TestCase):
    def test_rss_entries_use_same_rules(self) -> None:
        import feedparser

        config = Config()
        config.subreddits = ["Games"]
        parsed = (feedparser.parse(_ATOM_FEED), _ATOM_FEED)
        with mock.patch.object(reddit, "_parse_with_retry", return_value=parsed):
            items, raw_feeds, decisions = reddit.fetch_trending(config)
        reasons = {d.post_id: d.reason for d in decisions}
        self.assertEqual(
            reasons, {"t3_aaa": "meta_thread", "t3_bbb": "kept", "t3_ccc": "community_discussion"}
        )
        self.assertEqual([i.url for i in items], ["https://www.videogameschronicle.com/news/darts/"])
        self.assertEqual(items[0].engagement, 2 * 1.5)  # (3 - 1) * Games weight
        self.assertIn("Games", raw_feeds)


class PipelineTest(unittest.TestCase):
    def test_offline_cycle_records_decisions_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config()
            config.db_path = Path(tmp) / "test.db"
            store = Store(config.db_path)
            with mock.patch("bc_bot.sources.rss.fetch_articles", return_value=[]):
                result = pipeline.prepare_cycle(config, store, ingest_file=FIXTURE)
            for item in result.news + result.review + result.trailer:
                pipeline.record_posted_item(store, result, item, "news")
            pipeline.finish_cycle(store, config, result)

            self.assertEqual(result.reddit_source, "devvit-file")
            self.assertTrue(result.news)
            self.assertIn("t3_1y2y6gr", [i.post_id for i in result.trailer])  # HoMM3 trailer
            rows = store.decisions_since(1)
            self.assertEqual(
                len([r for r in rows if r["stage"] == "filter"]),
                len(json.loads(FIXTURE.read_text(encoding="utf-8"))["posts"]),
            )
            self.assertTrue(any(r["decision"] == "posted" for r in rows))
            # The two memory-price posts share a URL: one is kept, the other
            # merged (same title) or deduped (same URL).
            memory = [r for r in rows if r["post_id"] in ("t3_1y1wss6", "t3_1y1z2uf")]
            self.assertTrue(any(r["stage"] in ("consolidate", "dedup") for r in memory))
            self.assertEqual(len(store.raw_feeds_since(1)), 3)


class CommunityDiscussionPatternTest(unittest.TestCase):
    """Direct pattern-level checks for community-gripe self-posts that must be
    dropped from the news channel (independent of the ingest fixture)."""

    def test_slop_gripe_is_community_discussion(self) -> None:
        # r/gaming rant caught in the audit data (id 2185): venting that a platform's
        # update is "slop", not reportable news.
        title = (
            'Previously Beloved Game Platform "Humble Bundle" Releases '
            "Slop Website Update that Breaks Security and Basic Functionality"
        )
        self.assertTrue(reddit._is_community_discussion(title))


if __name__ == "__main__":
    unittest.main()
