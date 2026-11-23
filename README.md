# BC Game Bulletin Bot

A self-hosted Discord bot that keeps your Discord channels up to date with trending
gaming news — without you having to scroll Reddit yourself.

Every few hours it takes the hot posts from a set of gaming subreddits, merges
duplicate coverage of the same story, and cross-checks each story against recent
articles from gaming news outlets (IGN, Polygon, PC Gamer, etc.) as a
corroboration signal. Items are routed to the Discord channel matching their
category — general news, OpenCritic review threads and trailer threads each go to
their own channel. A local SQLite database remembers what was already posted, so
the same story never shows up twice — even if it's reworded. Lightweight enough to
run 24/7 on a Raspberry Pi.

> [!NOTE]
> **The Reddit RSS source is deprecated.** The bot originally read Reddit's
> public `reddit.com/r/<sub>/hot/.rss` feeds. Those carry no scores, comment
> counts or stickied flags (so ranking had to guess from feed position), need a
> 60-second pause between subreddits, and regularly answer HTTP 429. Hot posts now
> come from the companion **bc-bulletin Devvit app** (`devvit/`), which reads them
> through Reddit's own API. Set `REDDIT_SOURCE=devvit`.
>
> `REDDIT_SOURCE=rss` still works and is still the code default so an existing
> install keeps running unchanged, and it's the automatic fallback when no fresh
> Devvit payload arrives — but new setups should use `devvit`, and the score-based
> rules (`MIN_SCORE`, `RANK_BY_SCORE`, `POPULAR_SCORE`) only work with it.
>
> This is only about *Reddit* RSS. The news-outlet feeds in `RSS_FEEDS` (IGN,
> Polygon, …) are still used for corroboration.

## Architecture

```
 Reddit                         Discord                          Raspberry Pi
┌──────────────────────┐      ┌───────────────────────┐        ┌────────────────────────────────┐
│ bc-bulletin Devvit   │      │ #bc-ingest (private)  │        │ bc_bot (Python, systemd)       │
│ app (devvit/)        │      │                       │        │                                │
│                      │ HTTP │  message +            │  REST  │ 1. read newest ingest.json     │
│ every 4h (cron):     ├─────►│  ingest.json          ├───────►│ 2. filter (meta threads,       │
│  getHotPosts for     │ web- │  attachment           │        │    stickied, images, community │
│  r/gaming, r/Games,  │ hook │                       │        │    posts, low score)           │
│  r/GamingLeaks...    │      └───────────────────────┘        │ 3. consolidate same story      │
│  (score, comments,   │                                       │ 4. boost + enrich with news    │◄── news RSS
│   body, flair,       │      ┌───────────────────────┐        │    RSS (IGN, Polygon, …)       │    (IGN, …)
│   stickied)          │      │ #news #reviews        │  post  │ 5. rank, dedup vs SQLite,      │
└──────────────────────┘      │ #trailers             │◄───────┤    cap news at POSTS_PER_CYCLE │
                              └───────────────────────┘        │ 6. store raw input + every     │
                                                               │    decision in SQLite          │
                                                               └────────────────────────────────┘
```

Why the Discord hop: the Pi accepts no inbound connections and Devvit apps have no
public URL, but both can reach Discord. The two halves share nothing except the
`ingest.json` payload format (documented in `devvit/README.md`).

If no payload newer than `INGEST_MAX_AGE_HOURS` (default 6) exists, the bot uses
the deprecated Reddit RSS feeds for that cycle (`INGEST_FALLBACK_TO_RSS=true`).

### Repository layout

```
bc_bot/        Discord bot (Python) -- runs on the Raspberry Pi
tests/         Bot unit tests + a real Devvit payload fixture
deploy/        systemd unit for the Pi
devvit/        bc-bulletin Devvit app (TypeScript) -- runs on Reddit; see devvit/README.md
```

## Setup

Three parts, in this order: Discord (channels + bot + webhook), the Devvit app,
then the bot.

### 1. Discord

1. Create a bot at https://discord.com/developers/applications and invite it to
   your server with *Send Messages*, *Embed Links*, *View Channels* and
   *Read Message History*.
2. In the bot's settings (**Bot → Privileged Gateway Intents**) enable
   **Message Content Intent** — without it, Discord returns the webhook messages
   with no attachments and the bot sees no payloads.
3. Create the output channels (news, reviews, trailers — they may be the same
   channel while testing) and a **private** `#bc-ingest` channel the bot can read.
4. In `#bc-ingest`: **Edit Channel → Integrations → Webhooks → New Webhook**, and
   copy its URL. Treat it like a password: anyone with it can post there.
5. Enable Developer Mode (**User Settings → Advanced**) and copy the channel IDs
   (right-click a channel → Copy Channel ID).

### 2. Devvit app

Requires Node 24 (`devvit/.nvmrc`) and a Reddit account with access to
https://developers.reddit.com.

1. Create a small subreddit you moderate to host the app (e.g. a private test
   subreddit). Apps that are uploaded but not published can only be installed on
   subreddits with **fewer than 200 members**.
2. Install and log in:

   ```
   cd devvit
   npm install
   npm run login
   ```

3. App names are unique across Reddit. If you're setting this up from a fork,
   change `"name"` in `devvit/devvit.json` to your own.
4. Store the webhook URL as a secret app setting (it never goes in a file):

   ```
   npx devvit settings set discordWebhookUrl
   ```

5. Try it with a playtest (installs a dev build and streams logs):

   ```
   npx devvit playtest <your-subreddit>
   ```

   Open the subreddit, then in the mod tools menu (**⋯**) choose
   **Send test bulletin to Discord**. A toast should say "Sent 75 posts to
   Discord" and `#bc-ingest` should get a `bc-ingest v1 …` message with
   `ingest.json` attached.

   If playtest adds a `"dev": { "subreddit": ... }` block to `devvit.json`,
   don't commit it.

6. Make it permanent — upload a version and install it, so the 4-hourly
   scheduler (`0 */4 * * *`, UTC) runs without a playtest session:

   ```
   npm run deploy                      # type-check + lint + devvit upload
   npx devvit install <your-subreddit>
   ```

   Re-run both after changing the app.

### 3. Bot

1. Create and activate a virtualenv, then install dependencies:

   ```
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   source .venv/bin/activate     # Linux / Raspberry Pi
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`,
   `DISCORD_NEWS_CHANNEL_ID`, `DISCORD_REVIEW_CHANNEL_ID`,
   `DISCORD_TRAILER_CHANNEL_ID`, `REDDIT_SOURCE=devvit` and
   `DISCORD_INGEST_CHANNEL_ID` (the `#bc-ingest` ID).

3. Check it end to end without posting anything:

   ```
   python -m bc_bot.dry_run --once
   ```

   Look for `Devvit ingest payload: message …, 75 posts` and `DRY RUN (devvit)`.

4. Run it:

   ```
   python -m bc_bot.main
   ```

   The bot runs a cycle on startup, then every `CHECK_INTERVAL_HOURS` (default 4).
   Each cycle reads the newest payload it hasn't processed yet (the last processed
   message ID is stored in the DB). Records older than `RETENTION_DAYS`
   (default 30) are purged automatically.

## Testing without posting to Discord

`bc_bot.dry_run` runs the full cycle and prints what *would* be posted. It records
to the database (so duplicate suppression behaves like production across cycles)
but never posts to Discord.

```
python -m bc_bot.dry_run                                                   # every 10 minutes until Ctrl+C
python -m bc_bot.dry_run --once                                            # one cycle, then exit
python -m bc_bot.dry_run --ingest-file tests/fixtures/ingest_sample.json   # offline, from a saved payload
```

Set `DB_PATH=bc_bot_test.db` in `.env` while testing so dry-run "posts" don't end
up in the production dedup history.

Unit tests (offline, no secrets needed): `python -m unittest discover -s tests`

## Weekly review

Everything needed to review the bot's logic is in the SQLite DB (`DB_PATH`),
kept for `RETENTION_DAYS`:

| Table | One row per | Key columns |
| --- | --- | --- |
| `cycle_raw_feeds` | subreddit per cycle | `raw_feed`: the exact input -- the Devvit payload JSON (starts with `{`), or RSS XML for fallback cycles |
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

## How it works

1. **Collect** — read the newest `ingest.json` from `#bc-ingest`: the top 25 hot
   posts of each subreddit with score, comment count, flair, body and stickied flag.
2. **Filter** — drop recurring megathreads (Daily Discussion, Free Talk Friday,
   self-posts with a date in the title, …), stickied posts, image/video posts,
   community-discussion self-posts (questions, anecdotes, recommendation requests)
   and posts under `MIN_SCORE`. For text-only posts (e.g. r/GamingLeaksAndRumours)
   the source link is taken from the body (`[Source](...)` first, then the first
   non-Reddit/media/store link, YouTube as a last resort), so the posted link is
   the article rather than the comments page.
3. **Consolidate** — fuzzy-match titles across subreddits so the same story
   posted in multiple subs becomes a single item.
4. **Boost** — +1 confidence for titles that look like announcements/reveals/
   trailers, for hands-on coverage (reviews, previews, impressions), and for posts
   at or above `POPULAR_SCORE`.
5. **Enrich** — fuzzy-match each story against recent articles from the news
   feeds in `RSS_FEEDS`; a match adds +1 confidence as corroboration. The posted
   link stays the Reddit-submitted link.
6. **Rank and post** — items not posted within `RETENTION_DAYS` (exact link or
   fuzzy title match) are routed by category: OpenCritic review threads to the
   review channel and YouTube trailer posts to the trailer channel (both
   uncapped); everything else is ranked by confidence, then score, and the top
   `POSTS_PER_CYCLE` go to the news channel.
7. **Record** — the raw payload and one decision per post are stored for the
   weekly review.

Any failure during a cycle (Discord or network outage, bad payload) is logged and
swallowed; the bot waits for the next scheduled cycle instead of crashing.

## Deploying on a Raspberry Pi

1. Get the code onto the Pi (clone the repo, or `rsync`/`scp` it over — exclude
   `.venv`, `.env`, `*.db`, `bc_bot.log*` and `devvit/node_modules`), then set up
   the venv on the Pi itself (a venv can't be copied between machines):

   ```
   git clone <this-repo-url> ~/BC_BOT
   cd ~/BC_BOT
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

   The Pi only runs the bot; `devvit/` is deployed from your PC (Setup step 2).

2. Fill in `.env` (see Setup step 3), then verify before wiring up the service:

   ```
   python -m bc_bot.dry_run --once
   ```

3. Install as a `systemd` service so it survives reboots. `deploy/bc-bot.service`
   assumes `User=pi` and `WorkingDirectory=/home/pi/BC_BOT` — adjust those (and
   `ExecStart`) if your username or clone path differ, then:

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

   Updating an existing install: `git pull`, add any new `.env` keys, then
   `sudo systemctl restart bc-bot`. New DB tables are created automatically.

## Configuration reference (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Bot token (required) |
| `DISCORD_NEWS_CHANNEL_ID` / `DISCORD_REVIEW_CHANNEL_ID` / `DISCORD_TRAILER_CHANNEL_ID` | — | Output channels (required for the live bot) |
| `REDDIT_SOURCE` | `rss` | `devvit` (recommended) or `rss` (deprecated, see note at top) |
| `DISCORD_INGEST_CHANNEL_ID` | — | `#bc-ingest`, where the Devvit app's webhook posts payloads (required for `devvit`) |
| `INGEST_MAX_AGE_HOURS` | `6` | Ignore payloads older than this |
| `INGEST_FALLBACK_TO_RSS` | `true` | Use the Reddit RSS feeds when no fresh payload exists |
| `MIN_SCORE` | `10` | Drop posts below this score (`devvit` only; `0` disables) |
| `RANK_BY_SCORE` | `true` | Within equal confidence, rank by real score instead of feed position × weight (`devvit` only) |
| `POPULAR_SCORE` | `1000` | +1 confidence at or above this score (`devvit` only; `0` disables) |
| `RSS_FEEDS` | IGN, Polygon, PC Gamer, Eurogamer, GameSpot, VG247, Rock Paper Shotgun, PCGamesN | News-outlet feeds used for corroboration (not deprecated) |
| `CHECK_INTERVAL_HOURS` | `4` | Hours between cycles |
| `RETENTION_DAYS` | `30` | How long posted-item history (and dedup window) and review data are kept |
| `POSTS_PER_CYCLE` | `5` | Max news items per cycle (review and trailer items aren't capped) |
| `DB_PATH` | `bc_bot.db` | SQLite file location |
| `SUBREDDITS` | `Games,gaming,GamingLeaksAndRumours` | Subreddits for the RSS source (the Devvit app's list is in `devvit/src/server/core/ingest.ts`) |
| `SUBREDDIT_WEIGHTS` | `gaming:3,Games:1.5,GamingLeaksAndRumours:1` | Position weights for the RSS source / `RANK_BY_SCORE=false` |
| `REDDIT_USER_AGENT` | `bc-gaming-bot/1.0` | User-Agent for RSS and Discord REST requests |
