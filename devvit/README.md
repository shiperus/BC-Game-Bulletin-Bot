# bc-bulletin (Devvit app)

The Reddit-side half of BC Game Bulletin. It runs on Reddit's Devvit platform and,
every 4 hours, reads the hot posts of the source subreddits through Reddit's own
API (real scores, comment counts, post bodies, stickied flags), then posts them as
an `ingest.json` attachment to a private Discord channel via webhook. The Discord
bot in the repo root picks the payload up from there (`REDDIT_SOURCE=devvit`).

Built from the Devvit React starter (Vite, React, Hono, Tailwind, TypeScript).

## Setup

Requires Node 24 (see `.nvmrc`).

```
cd devvit
npm install
npm run login                                   # devvit login
npx devvit settings set discordWebhookUrl       # paste the #bc-ingest webhook URL (stored as a secret)
npx devvit playtest <your-test-subreddit>       # playtest on your own small test subreddit
npm run deploy                                  # type-check + lint + upload a new version
```

The test subreddit is intentionally not stored in `devvit.json`. If `devvit
playtest` adds a `"dev": { "subreddit": ... }` block to it, don't commit that hunk.

After `npm run deploy`, install the uploaded version on your subreddit so the
scheduler keeps running without a playtest session.

Never commit the webhook URL -- it only lives in the Devvit secret setting.

## What runs

| Piece | Where | What |
| --- | --- | --- |
| Scheduler task `ingest` | `devvit.json` (`0 */4 * * *`) -> `src/server/routes/scheduler.ts` | Fetch + send every 4h |
| Mod menu "Send test bulletin to Discord" | `src/server/routes/menu.ts` | Same thing, on demand; result shown as a toast |
| Fetch + payload | `src/server/core/ingest.ts` | Top 25 hot posts of r/gaming, r/Games, r/GamingLeaksAndRumours |
| HTTP allowlist | `devvit.json` `permissions.http.domains` | `discord.com` |

## Payload contract (version 1)

Shared with the bot's parser (`bc_bot/sources/devvit_ingest.py`); change both
sides in the same PR and bump `version` for breaking changes. A real example
lives at `tests/fixtures/ingest_sample.json`.

```jsonc
{
  "version": 1,
  "fetchedAt": "2026-11-23T08:00:35.116Z",
  "errors": [],                 // per-subreddit fetch failures, if any
  "posts": [{
    "id": "t3_1y21hd3",
    "subreddit": "GamingLeaksAndRumours",
    "rank": 7,                  // 1-based position in that subreddit's hot list
    "title": "...",
    "url": "...",               // submitted URL (== permalink for self-posts)
    "permalink": "https://www.reddit.com/r/.../comments/...",
    "isSelf": true,
    "body": "...",              // selftext markdown, max 4000 chars; null if none
    "score": 200,
    "numComments": 71,
    "flair": "Leak",            // null if none
    "createdUtc": 1795279618,
    "stickied": false,
    "nsfw": false
  }]
}
```

## Commands

- `npm run dev`: playtest live on Reddit
- `npm run build`: build client and server
- `npm run deploy`: type-check, lint, upload a new version
- `npm run lint` / `npm run test:types`
