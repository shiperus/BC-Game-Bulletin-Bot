# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Setup (Windows; use `source .venv/bin/activate` on Linux):

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` / `DISCORD_NEWS_CHANNEL_ID` / `DISCORD_REVIEW_CHANNEL_ID` before running the live bot; `REDDIT_USER_AGENT` needs no API key, just a distinctive string.

Run the live bot (posts to Discord on a `CHECK_INTERVAL_HOURS` loop, default 4h):

```
python -m bc_bot.main
```

Test the pipeline without touching Discord (loops every 10 minutes, prints candidates, still writes to the DB):

```
python -m bc_bot.dry_run
```

There is no test suite, linter, or type-checker configured in this repo (`requirements.txt` only lists runtime dependencies).

Production deployment target is a Raspberry Pi 4 via `systemd` (`deploy/bc-bot.service`); see the README's "Deploying on a Raspberry Pi" section for the full setup sequence.

A full fetch cycle takes several minutes in practice — `reddit.py` sleeps 60s between subreddits and retries with backoff on HTTP 429, so don't expect either entry point to return quickly.

## Architecture

Both entry points (`main.py` for the live Discord bot, `dry_run.py` for console-only testing) run the same pipeline; they share `Config`, `Store`, and the aggregator, and differ only in what happens to the final list (post embeds vs. print). Reading `discord_bot.py`'s `_run_cycle` and `dry_run.py`'s `run_cycle` side by side is the fastest way to see the whole shape of the app.

Pipeline stages, in order:

1. **`sources/reddit.py`** — fetches hot posts via Reddit's public `reddit.com/r/<sub>/hot/.rss` Atom feeds (the official JSON API is gated behind app approval, so this avoids needing a key). Since Atom entries carry no upvote/comment counts, `engagement` is approximated as `(feed position) * SUBREDDIT_WEIGHTS[subreddit]` — the weight exists because feed position alone isn't comparable across subreddits of very different size/activity. Meta-threads (recurring megathreads) are filtered by two layers: an explicit regex list for known phrasings (`_META_THREAD_PATTERNS`), plus a general heuristic — self-post (no `[link]` anchor in the summary) + a thread/discussion word + a date-like token in the title — so new subreddits' own megathread wording doesn't need a hand-written pattern.
2. **`sources/rss.py`** — fetches recent articles from configured gaming news feeds. `find_best_match` uses `rapidfuzz.fuzz.token_set_ratio` (not `token_sort_ratio` — empirically, cross-outlet headline pairs describing the same story score too close to unrelated pairs under `token_sort_ratio` due to length asymmetry between terse Reddit titles and verbose news headlines; `token_set_ratio` separates them more cleanly) against a `MATCH_THRESHOLD` of 62.
3. **`aggregator.py`** — `consolidate()` fuzzy-merges same-story items across subreddits (bumping `confidence` to the merged source count); `boost_announcements()` adds +1 confidence when a title matches announcement/reveal/trailer phrasing; `enrich_with_articles()` attaches a matching RSS article (+1 confidence) or leaves the original Reddit-submitted URL; `rank()` sorts by `(confidence, engagement)` descending — confidence (corroboration signal) always outranks raw engagement.
4. **`db.py`** (`Store`) — SQLite-backed dedup: `is_duplicate()` fuzzy-matches a candidate title against titles posted within `RETENTION_DAYS`, so a reworded restatement of an already-posted story is still caught. `record_posted()` and `cleanup_old()` are called identically by both `main.py` and `dry_run.py` — **`dry_run.py` writes to the same `DB_PATH` as production by default**, so items it "posts" to the console are recorded and will be treated as duplicates if the live bot later sees the same story. Set a different `DB_PATH` in `.env` when testing if that matters.
5. **`models.py`** — `TrendingItem.link` returns `article_url` if RSS enrichment matched, else the original Reddit-submitted URL (or the comments page for self-posts).

Config (`config.py`) is a single dataclass populated from environment variables via `python-dotenv`, with defaults for everything except Discord credentials. `Config.require_discord_credentials()` is only called from `main.py`, which is why `dry_run.py` works without a Discord app configured at all.
