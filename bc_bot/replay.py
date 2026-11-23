"""Re-run archived Devvit payloads through the *current* filter rules and show which
per-post decisions would change -- the "did my rule edit do what I wanted, and
nothing else?" check before deploying a filter change.

    python -m bc_bot.replay              # last 7 days
    python -m bc_bot.replay --days 14
    python -m bc_bot.replay --show-all   # also list every unchanged decision

Only the per-post filter stage is replayed (meta-thread, stickied, image link,
community discussion, source-link extraction). Consolidation/dedup depend on what
was posted at the time, so they're not re-simulated. RSS-sourced cycles are skipped
because their archived XML has no stable post scores to compare.
"""

from __future__ import annotations

import argparse
import sys
import json
from collections import defaultdict

from bc_bot.config import Config
from bc_bot.db import Store
from bc_bot.sources import devvit_ingest, reddit


def main() -> None:
    # Titles carry curly quotes/emoji; Windows consoles default to cp1252.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Replay archived payloads through current rules.")
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--show-all", action="store_true")
    args = parser.parse_args()

    config = Config()
    store = Store(config.db_path)

    payloads: dict[str, dict] = {}
    for cycle_started_at, _subreddit, raw_feed in store.raw_feeds_since(args.days):
        if not devvit_ingest.is_ingest_raw_feed(raw_feed):
            continue
        part = json.loads(raw_feed)
        payload = payloads.setdefault(
            cycle_started_at,
            {"version": part["version"], "fetchedAt": part["fetchedAt"], "posts": []},
        )
        payload["posts"].extend(part["posts"])

    if not payloads:
        print(f"No archived Devvit payloads in the last {args.days:g} day(s) in {config.db_path}.")
        return

    recorded: dict[tuple[str, str], tuple[str, str | None]] = {}
    for r in store.decisions_since(args.days):
        if r["stage"] == "filter" and r["post_id"]:
            recorded[(r["cycle_started_at"], r["post_id"])] = (r["reason"], r["url"])

    changes: dict[str, list[str]] = defaultdict(list)
    seen_changed: set[tuple[str, str, str]] = set()
    total = 0
    for cycle_started_at, payload in sorted(payloads.items()):
        posts, _ = devvit_ingest.payload_to_posts(payload)
        _, decisions = reddit.filter_posts(posts, config)
        for d in decisions:
            total += 1
            before = recorded.get((cycle_started_at, d.post_id or ""))
            if before is None:
                continue
            old_reason, old_url = before
            if old_reason != d.reason:
                key = (d.post_id or "", old_reason, d.reason)
                if key in seen_changed:
                    continue  # same post, same change, seen in an earlier cycle
                seen_changed.add(key)
                changes[f"{old_reason} -> {d.reason}"].append(
                    f"  {d.score if d.score is not None else '-':>6}  r/{d.subreddit}: {d.title}"
                )
            elif d.reason == "kept" and old_url != d.url:
                key = (d.post_id or "", "url", d.url or "")
                if key in seen_changed:
                    continue
                seen_changed.add(key)
                changes["link changed"].append(
                    f"  r/{d.subreddit}: {d.title}\n      {old_url}\n   -> {d.url}"
                )
            elif args.show_all:
                changes[f"unchanged: {d.reason}"].append(f"  r/{d.subreddit}: {d.title}")

    print(
        f"Replayed {len(payloads)} cycle(s), {total} post decision(s) from the last "
        f"{args.days:g} day(s) through the current rules.\n"
    )
    if not any(not k.startswith("unchanged") for k in changes):
        print("No filter decisions would change.")
    for transition, lines in sorted(changes.items()):
        print(f"--- {transition} ({len(lines)}) ---")
        print("\n".join(lines))
        print()


if __name__ == "__main__":
    main()
