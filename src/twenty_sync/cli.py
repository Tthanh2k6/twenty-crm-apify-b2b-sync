"""
Command-line entrypoint for `twenty-crm-apify-b2b-sync`.
Supports `--dry-run`, `--demo-mock`, local JSON files, and live Apify Dataset IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .client import InMemoryTwentyMockTransport, TwentyRestClient, TwentySyncEngine
from .ledger import SyncAuditLedger
from .webhook import fetch_apify_dataset_items


def _default_sample_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "sample_apify_b2b_leads.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="twenty-sync",
        description="Sync enriched B2B leads from Apify Store into Twenty CRM (REST API).",
    )
    parser.add_argument(
        "--input",
        type=str,
        default="",
        help="Path to a JSON file containing Apify B2B lead enrichment items.",
    )
    parser.add_argument(
        "--apify-dataset-id",
        type=str,
        default="",
        help="Live Apify Dataset ID to pull clean items from.",
    )
    parser.add_argument(
        "--twenty-base-url",
        type=str,
        default=os.getenv("TWENTY_BASE_URL", "https://api.twenty.com"),
        help="Twenty CRM base URL (default: https://api.twenty.com or TWENTY_BASE_URL).",
    )
    parser.add_argument(
        "--twenty-api-key",
        type=str,
        default=os.getenv("TWENTY_API_KEY", ""),
        help="Twenty CRM Bearer API key (or set TWENTY_API_KEY).",
    )
    parser.add_argument(
        "--apify-token",
        type=str,
        default=os.getenv("APIFY_API_TOKEN", ""),
        help="Optional Apify API token for private datasets.",
    )
    parser.add_argument(
        "--ledger",
        type=str,
        default=os.getenv("TWENTY_SYNC_LEDGER_PATH", ""),
        help="Path to append-only JSONL idempotency audit ledger.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and transform records into Twenty payloads without network writes.",
    )
    parser.add_argument(
        "--demo-mock",
        action="store_true",
        help="Execute full HTTP pipeline against stateful in-memory Twenty CRM mock server.",
    )
    parser.add_argument(
        "--create-opportunities",
        action="store_true",
        help="Also create a linked NEW-stage Opportunity per enriched Company.",
    )
    parser.add_argument(
        "--no-notes",
        action="store_true",
        help="Disable attaching tech-stack Markdown Notes to Company records.",
    )
    parser.add_argument(
        "--output-preview",
        type=str,
        default="",
        help="Optional file path to write transformed payloads or audit records as JSON.",
    )
    return parser


async def _run_async(args: argparse.Namespace) -> int:
    if args.apify_dataset_id:
        raw_items = await fetch_apify_dataset_items(
            args.apify_dataset_id, apify_token=args.apify_token or None
        )
    else:
        input_path = Path(args.input) if args.input else _default_sample_path()
        if not input_path.exists():
            print(f"[ERROR] Input dataset file not found: {input_path}", file=sys.stderr)
            return 2
        loaded: Any = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            print("[ERROR] Input JSON must be an array of lead objects.", file=sys.stderr)
            return 2
        raw_items = loaded

    ledger_path = args.ledger if args.ledger else None
    ledger = SyncAuditLedger(ledger_path)

    if args.dry_run:
        engine = TwentySyncEngine(
            client=None,
            ledger=ledger,
            dry_run=True,
            create_notes=not args.no_notes,
            create_opportunities=args.create_opportunities,
        )
        summary, preview = await engine.sync_items(raw_items, mode_label="dry-run")
    elif args.demo_mock:
        mock_transport = InMemoryTwentyMockTransport()
        async with TwentyRestClient(
            base_url="https://mock.twenty.local",
            api_key="demo_mock_api_key",
            transport=mock_transport,
        ) as client:
            engine = TwentySyncEngine(
                client=client,
                ledger=ledger,
                dry_run=False,
                create_notes=not args.no_notes,
                create_opportunities=args.create_opportunities,
            )
            summary, preview = await engine.sync_items(raw_items, mode_label="demo-mock")
    else:
        if not args.twenty_api_key.strip():
            print(
                "[ERROR] Missing Twenty API key. Pass --twenty-api-key, set TWENTY_API_KEY, "
                "or run with --dry-run / --demo-mock.",
                file=sys.stderr,
            )
            return 2
        async with TwentyRestClient(
            base_url=args.twenty_base_url,
            api_key=args.twenty_api_key.strip(),
        ) as client:
            engine = TwentySyncEngine(
                client=client,
                ledger=ledger,
                dry_run=False,
                create_notes=not args.no_notes,
                create_opportunities=args.create_opportunities,
            )
            summary, preview = await engine.sync_items(raw_items, mode_label="live")

    if args.output_preview:
        out_path = Path(args.output_preview)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(preview, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary.model_dump(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
