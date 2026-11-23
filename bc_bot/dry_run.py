from __future__ import annotations

import argparse
import sys
import logging
import time
from pathlib import Path

from bc_bot import pipeline
from bc_bot.config import Config
from bc_bot.db import Store

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 10 * 60


def run_cycle(config: Config, store: Store, ingest_file: Path | None = None) -> None:
    result = pipeline.prepare_cycle(config, store, ingest_file)
    print(
        f"\n=== DRY RUN ({result.reddit_source}): {len(result.news)} news item(s) would be posted "
        f"(of {result.candidate_count} candidates), {len(result.review)} review item(s), "
        f"{len(result.trailer)} trailer item(s) ===\n"
    )

    for channel_name, items in (
        ("news", result.news),
        ("review", result.review),
        ("trailer", result.trailer),
    ):
        for i, item in enumerate(items, 1):
            print(f"[{channel_name}] {i}. {item.display_title}")
            print(f"   Link: {item.link}")
            print(
                f"   Origin: {item.origin}  Confidence: x{item.confidence}  "
                f"Score: {item.score if item.score is not None else '-'}  "
                f"Comments: {item.num_comments if item.num_comments is not None else '-'}"
            )
            print()
            pipeline.record_posted_item(store, result, item, channel_name)

    dropped: dict[str, int] = {}
    for d in result.decisions:
        if d.decision in ("dropped", "merged"):
            key = f"{d.stage}:{d.reason}" if d.stage != "consolidate" else "consolidate:merged"
            dropped[key] = dropped.get(key, 0) + 1
    if dropped:
        print("Dropped this cycle: " + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))

    removed, removed_raw = pipeline.finish_cycle(store, config, result)
    print(
        f"Recorded {len(result.news) + len(result.review) + len(result.trailer)} item(s) and "
        f"{len(result.decisions)} decision(s) to {config.db_path} (nothing was posted to Discord). "
        f"Cleaned up {removed} old posted record(s), {removed_raw} old raw-feed record(s).\n"
    )


def main() -> None:
    # Titles carry curly quotes/emoji; Windows consoles default to cp1252.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run the pipeline without posting to Discord.")
    parser.add_argument(
        "--ingest-file",
        type=Path,
        help="Use a saved Devvit ingest.json instead of fetching (offline test)",
    )
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = Config()
    if args.ingest_file is None:
        config.require_ingest_settings()
    store = Store(config.db_path)
    logger.info("Using database %s", config.db_path)

    cycle_num = 0
    try:
        while True:
            cycle_num += 1
            logger.info("Starting dry-run cycle %d", cycle_num)
            try:
                run_cycle(config, store, args.ingest_file)
            except Exception:
                logger.exception("Dry-run cycle failed; will retry at the next scheduled interval")
            if args.once or args.ingest_file:
                break
            logger.info("Sleeping %d seconds until next cycle", CHECK_INTERVAL_SECONDS)
            time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Dry-run stopped after %d cycle(s)", cycle_num)


if __name__ == "__main__":
    main()
