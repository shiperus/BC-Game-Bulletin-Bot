"""Weekly review of the bot's decisions.

    python -m bc_bot.report                # last 7 days
    python -m bc_bot.report --days 14 --high-score 1000

Reads cycle_decisions (written every cycle by pipeline.finish_cycle) and prints:
what was posted, how many posts each filter rule dropped, and the decisions most
worth a second look -- high-score posts a rule dropped (possible false negatives)
and low-score posts that made it out (possible false positives). Scores only exist
for Devvit-ingested cycles; RSS cycles show "-".
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

from bc_bot.config import Config
from bc_bot.db import Store

# Filter reasons that are structural (pinned/megathread/media) rather than judgment
# calls; excluded from the "possible false negatives" list to keep it short.
_STRUCTURAL_REASONS = {"stickied", "meta_thread", "image_link"}


def _score(value: int | None) -> str:
    return "-" if value is None else str(value)


def main() -> None:
    # Titles carry curly quotes/emoji; Windows consoles default to cp1252.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Summarize the bot's recent decisions.")
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument(
        "--high-score", type=int, default=500, help="Flag dropped posts at or above this score"
    )
    parser.add_argument(
        "--low-score", type=int, default=50, help="Flag posted items below this score"
    )
    args = parser.parse_args()

    config = Config()
    rows = Store(config.db_path).decisions_since(args.days)
    if not rows:
        print(f"No decisions recorded in the last {args.days:g} day(s) in {config.db_path}.")
        return

    cycles = {r["cycle_started_at"]: r["reddit_source"] for r in rows}
    print(f"=== Bot decisions, last {args.days:g} day(s) ({config.db_path}) ===")
    print(f"Cycles: {len(cycles)}  by source: {dict(Counter(cycles.values()))}\n")

    # A post is usually seen in several consecutive cycles; report each post once,
    # using its most recent decision for that stage.
    latest: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["post_id"] or r["title"], r["stage"])
        latest[key] = dict(r)

    posted = [r for r in rows if r["decision"] == "posted"]
    print(f"--- Posted ({len(posted)}) ---")
    for r in posted:
        print(f"[{r['reason']:<7}] {r['title']}")
        print(f"          {r['url']}  (r/{r['subreddit']}, score {_score(r['score'])})")
    print()

    filter_rows = [r for r in latest.values() if r["stage"] == "filter"]
    by_reason: dict[str, Counter] = defaultdict(Counter)
    for r in filter_rows:
        by_reason[r["reason"]][r["subreddit"]] += 1
    print("--- Filter stage (unique posts) ---")
    for reason, subs in sorted(by_reason.items(), key=lambda kv: -sum(kv[1].values())):
        detail = ", ".join(f"r/{s}={n}" for s, n in subs.most_common())
        print(f"{reason:<22} {sum(subs.values()):>4}   {detail}")
    print()

    other = Counter(
        (r["stage"], r["reason"] if r["stage"] != "consolidate" else "merged")
        for r in latest.values()
        if r["stage"] in ("consolidate", "dedup", "cap")
    )
    if other:
        print("--- Later stages (unique posts) ---")
        for (stage, reason), n in other.most_common():
            print(f"{stage}:{reason:<18} {n:>4}")
        print()

    suspects = sorted(
        (
            r
            for r in filter_rows
            if r["decision"] == "dropped"
            and r["reason"] not in _STRUCTURAL_REASONS
            and r["score"] is not None
            and r["score"] >= args.high_score
        ),
        key=lambda r: -r["score"],
    )
    print(f"--- Dropped by a rule with score >= {args.high_score} (possible false negatives) ---")
    for r in suspects[:25] or []:
        print(f"{r['score']:>6}  {r['reason']:<22} r/{r['subreddit']}: {r['title']}")
    if not suspects:
        print("(none)")
    print()

    over_cap = sorted(
        (r for r in latest.values() if r["reason"] == "over_cap" and r["score"] is not None),
        key=lambda r: -r["score"],
    )
    if over_cap:
        print("--- Highest-score items cut by POSTS_PER_CYCLE ---")
        for r in over_cap[:10]:
            print(f"{r['score']:>6}  r/{r['subreddit']}: {r['title']}")
        print()

    weak = [r for r in posted if r["score"] is not None and r["score"] < args.low_score]
    print(f"--- Posted with score < {args.low_score} (possible false positives) ---")
    for r in weak:
        print(f"{r['score']:>6}  [{r['reason']}] r/{r['subreddit']}: {r['title']}")
    if not weak:
        print("(none)")


if __name__ == "__main__":
    main()
