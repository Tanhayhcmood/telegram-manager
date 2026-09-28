"""Smoke test the two reported Telegram link forms.

Parser-only (safe, no Telegram connection):
    python scripts/test_join_links.py

Queue and wait for the real account to process both links:
    python scripts/test_join_links.py --live --wait

The live mode uses the production queue rules, including the configured
40–90 minute delay and daily limit. It never prints secrets.
"""
import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


TEST_LINKS = (
    "https://t.me/+cXjPrswyqdM4OGU0",
    "https://t.me/chat_serveravps",
)


def _load_validator():
    """Load the parser without importing the bot's settings/dependencies."""
    path = str(Path(__file__).resolve().parents[1] / "app" / "utils" / "validators.py")
    spec = importlib.util.spec_from_file_location("telegram_link_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load validator from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.LinkValidator


def print_parsed() -> list[tuple[str, object]]:
    LinkValidator = _load_validator()
    parsed_items: list[tuple[str, object]] = []
    for raw_link in TEST_LINKS:
        parsed = LinkValidator.parse(raw_link)
        if parsed is None:
            print(json.dumps({
                "input": raw_link,
                "type": None,
                "key": None,
                "normalized": None,
                "result": "rejected_by_parser",
            }, ensure_ascii=False))
            continue
        parsed_items.append((raw_link, parsed))
        print(json.dumps({
            "input": raw_link,
            "type": parsed.type,
            "key": parsed.key,
            "normalized": parsed.normalized,
            "result": "parsed",
        }, ensure_ascii=False))
    return parsed_items


async def run_live(wait_for_result: bool) -> None:
    from app.database.connection import AsyncSessionLocal
    from app.models.discovered_link import LinkStatus
    from app.repositories.discovered_link_repository import DiscoveredLinkRepository
    from app.services.discovery_service import DiscoveryService
    from app.services.join_queue_service import JoinQueueService
    from app.services.telegram_service import TelegramUserService
    from app.utils.logger import setup_logging

    setup_logging()
    tg = TelegramUserService.get_instance()
    await tg.start()
    queue = JoinQueueService.get_instance()
    queue.set_tg_service(tg)
    await queue.start()
    discovery = DiscoveryService(tg)

    try:
        parsed_items = print_parsed()
        for raw_link, parsed in parsed_items:
            await discovery._register_link(
                parsed.normalized,
                source="script:test_join_links",
            )
            print(json.dumps({
                "input": raw_link,
                "type": parsed.type,
                "key": parsed.key,
                "normalized": parsed.normalized,
                "result": "queued",
            }, ensure_ascii=False))

        if not wait_for_result:
            return

        terminal = {
            LinkStatus.JOINED,
            LinkStatus.FAILED,
            LinkStatus.EXPIRED,
            LinkStatus.REQUEST_SENT,
            LinkStatus.SKIPPED_NOT_GROUP,
        }
        remaining = {parsed.key for _, parsed in parsed_items}
        while remaining:
            await asyncio.sleep(30)
            async with AsyncSessionLocal() as session:
                repo = DiscoveredLinkRepository(session)
                for raw_link, parsed in parsed_items:
                    record = await repo.get_by_canonical_key(parsed.key)
                    if record and record.status in terminal:
                        print(json.dumps({
                            "input": raw_link,
                            "type": parsed.type,
                            "key": parsed.key,
                            "normalized": parsed.normalized,
                            "status": record.status.value,
                            "notes": record.notes,
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        }, ensure_ascii=False))
                        remaining.discard(parsed.key)
    finally:
        await queue.stop()
        await tg.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="connect to Telegram and place both links in the real join queue",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="keep running until both queued links reach a terminal status",
    )
    args = parser.parse_args()

    if args.wait and not args.live:
        print("--wait requires --live", file=sys.stderr)
        raise SystemExit(2)
    if not args.live:
        print_parsed()
        print("result=parser_only; use --live to enqueue and join")
        return
    asyncio.run(run_live(args.wait))


if __name__ == "__main__":
    main()