# BC Game Bulletin Bot

A self-hosted Discord bot that keeps your Discord channels up to date with trending
gaming news — without you having to scroll Reddit yourself.

Every few hours it pulls hot posts from a set of gaming subreddits (via
Reddit's public RSS feeds, so no API key is needed), merges duplicate
coverage of the same story, and cross-checks each story against recent
articles from gaming news outlets (IGN, Polygon, PC Gamer, etc.) as a
corroboration signal. Items are routed to the Discord channel matching
their category — currently general news, OpenCritic review threads and Trailer threads
each go to their own channel. A local SQLite database remembers what was
already posted, so the same story never shows up twice — even if it's
reworded. Lightweight enough to run 24/7 on a Raspberry Pi.

## Repository layout

```
bc_bot/        Discord bot (Python) -- runs on the Raspberry Pi
tests/         Bot unit tests + a real Devvit payload fixture
deploy/        systemd unit for the Pi
devvit/        bc-bulletin Devvit app (TypeScript) -- runs on Reddit; see devvit/README.md
```

The two halves only talk through the `ingest.json` payload the Devvit app posts to
a private Discord channel (see "Devvit source" below and `devvit/README.md`). The
bot works without the Devvit app (`REDDIT_SOURCE=rss`, the default).

## Setup

1. Create and activate a virtualenv, then install dependencies:

   ```
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   source .venv/bin/activate     # Linux / Raspberry Pi
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN` / `DISCORD_NEWS_CHANNEL_ID` / `DISCORD_REVIEW_CHANNEL_ID` / `DISCORD_TRAILER_CHANNEL_ID` — create a bot at
     https://discord.com/developers/applications, invite it to your server
     with the `Send Messages` and `Embed Links` permissions, and copy the
     target channel's ID (enable Developer Mode in Discord to copy IDs).
   - `REDDIT_USER_AGENT` — no API key needed; Reddit's official self-serve
     app creation is now gated behind an approval process, so this bot uses
     the public, unauthenticated `reddit.com/r/<sub>/hot/.rss` feeds
     instead. Just set a distinctive User-Agent string (the default is
     fine).

3. Run it:

   ```
   python -m bc_bot.main
   ```

   The bot runs an immediate check on startup, then repeats every
   `CHECK_INTERVAL_HOURS` (default 4). State (which stories were already
   posted) is kept in a local SQLite file (`bc_bot.db`); records older than
   `RETENTION_DAYS` (default 30) are purged automatically each cycle to keep
   the footprint small on a Raspberry Pi 4.

## Testing without posting to Discord

`bc_bot.dry_run` repeats the full fetch → consolidate → enrich cycle every
10 minutes and prints what *would* be posted to the console. It records
posted items to the database (so duplicate suppression behaves exactly like
production across cycles) but never touches Discord. `DISCORD_TOKEN` /
`DISCORD_NEWS_CHANNEL_ID` / `DISCORD_REVIEW_CHANNEL_ID` / `DISCORD_TRAILER_CHANNEL_ID` aren't required for this. Stop it with Ctrl+C.

```
python -m bc_bot.dry_run
python -m bc_bot.dry_run --once                                      # one cycle, then exit
python -m bc_bot.dry_run --ingest-file tests/fixtures/ingest_sample.json   # offline, no network for Reddit
```

Set `DB_PATH=bc_bot_test.db` in `.env` while testing so dry-run "posts" don't
pollute the production dedup history.

## Devvit source (optional)

With `REDDIT_SOURCE=devvit`, hot posts come from the **bc-bulletin** Devvit app
instead of the RSS feeds. The app runs on Reddit, reads hot posts through
Reddit's own API every 4h (real scores, comment counts, post bodies, stickied
flags) and posts them as an `ingest.json` attachment to a private Discord channel
via webhook. Each cycle the bot reads the newest unprocessed payload from that
channel over Discord's REST API, then runs the exact same filter → consolidate →
enrich → rank → dedup pipeline. If no fresh payload exists (older than
`INGEST_MAX_AGE_HOURS`), it falls back to RSS for that cycle.

Setup:
1. In the Discord developer portal, enable the bot's **Message Content intent**
   (Bot → Privileged Gateway Intents), otherwise webhook messages come back empty.
2. Give the bot *View Channel* + *Read Message History* on the ingest channel.
3. Set `REDDIT_SOURCE=devvit` and `DISCORD_INGEST_CHANNEL_ID=<channel id>`.

For text-only posts (e.g. r/GamingLeaksAndRumours) the source link is extracted
from the post body (`[Source](...)` first, then the first non-Reddit/media/store
link, YouTube as a last resort), so the posted link is the article rather than
the comments page.

## Weekly review

Everything needed to review the bot's logic is in the SQLite DB (`DB_PATH`),
kept for `RETENTION_DAYS`:

| Table | One row per | Key columns |
| --- | --- | --- |
| `cycle_raw_feeds` | subreddit per cycle | `raw_feed`: the exact input -- RSS XML, or the Devvit payload JSON (starts with `{`) |
| `cycle_decisions` | post per stage per cycle | `stage`, `decision`, `reason`, `score`, `num_comments`, `flair`, `url`, `reddit_source` |
| `posted_items` | item posted | `title`, `url`, `channel`, `confidence`, `engagement`, `article_url` |

`cycle_decisions` values:
- `stage=filter`: `reason` is `kept` or the rule that dropped it -- `meta_thread`,
  `stickied`, `image_link`, `community_discussion`, `low_score`
- `stage=consolidate`: `merged into <post_id>` (same story, other subreddit)
- `stage=dedup`: `duplicate` (already posted, or same story this cycle)
- `stage=cap`: `over_cap` (cut by `POSTS_PER_CYCLE`)
- `stage=post`: `reason` is the channel (`news` / `review` / `trailer`)

Useful questions to ask of it: high-score posts dropped by a filter rule (rule
too broad), low-score posts that were posted (rule missing), high-score items
cut by `over_cap`. All rows of one cycle share `cycle_started_at`.

After editing a filter rule, `python -m bc_bot.replay` re-runs the archived
Devvit payloads through the current rules and lists which decisions would change.

Tests: `python -m unittest discover -s tests`

## How it works

1. **Collect** — pull hot posts from configured subreddits (Reddit's public
   RSS feeds), scored by feed position (already hot-ranked by Reddit) times
   that subreddit's `SUBREDDIT_WEIGHTS` multiplier, so a top slot in a much
   bigger subreddit outranks a top slot in a smaller one. Recurring
   community megathreads (Daily Discussion, Free Talk Friday, Tech Support
   threads, and self-posts with a date baked into the title, e.g. "Thread -
   Week Beginning 06/29/26") are filtered out since they aren't news.
2. **Consolidate** — fuzzy-match titles across subreddits so the same story
   posted in multiple subs becomes a single higher-engagement item.
3. **Boost announcements** — items whose title looks like a new-game reveal,
   trailer, teaser, or release-date confirmation get a confidence boost, so
   announcement news ranks above reviews, discounts, patch notes, etc.
4. **Enrich** — fuzzy-match each trending topic against recent articles from
   the configured gaming RSS feeds; a match adds a confidence boost as a
   corroboration signal, but the posted link always stays the original
   Reddit-submitted link.
5. **Post** — items that haven't been posted in the last `RETENTION_DAYS`
   are routed by category: OpenCritic review-thread items go to the review
   channel (`DISCORD_REVIEW_CHANNEL_ID`) uncapped, Trailer thread items go to 
   the trailer channel (`DISCORD_TRAILER_CHANNEL_ID`) uncapped, while everything else is
   ranked by confidence then engagement and the top `POSTS_PER_CYCLE` go to
   the news channel (`DISCORD_NEWS_CHANNEL_ID`).

Any failure during a cycle (API outage, rate limit, network blip) is logged
and swallowed; the bot simply waits for the next scheduled cycle instead of
crashing.

## Deploying on a Raspberry Pi

1. Get the code onto the Pi (clone the repo, or `rsync`/`scp` it over — exclude
   `.venv`, `.env`, `bc_bot.db`, and `bc_bot.log*`, which are per-machine and
   already in `.gitignore`), then set up the venv on the Pi itself (the venv
   can't be copied from another machine/architecture):

   ```
   git clone <this-repo-url> ~/BC_BOT
   cd ~/BC_BOT
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

2. Fill in `.env` with real `DISCORD_TOKEN` / `DISCORD_NEWS_CHANNEL_ID` / `DISCORD_REVIEW_CHANNEL_ID` / `DISCORD_TRAILER_CHANNEL_ID` (see
   Setup above), then verify it works before wiring up the service:

   ```
   python -m bc_bot.dry_run
   ```

3. Install as a `systemd` service so it survives reboots. `deploy/bc-bot.service`
   already assumes `User=pi` and `WorkingDirectory=/home/pi/BC_BOT` — adjust
   those (and `ExecStart`) if your username or clone path differ, then:

   ```
   sudo cp deploy/bc-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now bc-bot
   ```

   Check status and logs with:

   ```
   sudo systemctl status bc-bot
   journalctl -u bc-bot -f      # live systemd/stdout logs
   tail -f ~/BC_BOT/bc_bot.log  # the bot's own rotating log file
   ```

## Configuration reference (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `SUBREDDITS` | `Games,gaming,GamingLeaksAndRumours` | Comma-separated subreddits to poll |
| `SUBREDDIT_WEIGHTS` | `gaming:3,Games:1.5,GamingLeaksAndRumours:1` | Comma-separated `subreddit:weight` pairs; unlisted subreddits default to `1` |
| `RSS_FEEDS` | IGN, Polygon, PC Gamer, Eurogamer, GameSpot, VG247, Rock Paper Shotgun, PCGamesN | Comma-separated gaming news RSS feed URLs |
| `CHECK_INTERVAL_HOURS` | `4` | Hours between cycles |
| `RETENTION_DAYS` | `30` | How long posted-item history (and dedup window) is kept |
| `POSTS_PER_CYCLE` | `5` | Max news items posted per cycle to `DISCORD_NEWS_CHANNEL_ID` (review, trailer items aren't capped) |
| `DB_PATH` | `bc_bot.db` | SQLite file location |
| `REDDIT_SOURCE` | `rss` | `rss` or `devvit` (see "Devvit source") |
| `DISCORD_INGEST_CHANNEL_ID` | — | Channel the Devvit app's webhook posts payloads to (required for `devvit`) |
| `INGEST_MAX_AGE_HOURS` | `6` | Ignore payloads older than this |
| `INGEST_FALLBACK_TO_RSS` | `true` | Use RSS when no fresh payload exists |
| `MIN_SCORE` | `10` | Drop posts below this score (devvit only; `0` disables) |
| `RANK_BY_SCORE` | `true` | Within equal confidence, rank by real score instead of feed position × weight (devvit only) |
| `POPULAR_SCORE` | `1000` | +1 confidence at or above this score (devvit only; `0` disables) |
