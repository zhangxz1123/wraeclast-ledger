from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Sequence

from .demo import seed_demo
from .historical import HistoricalBackfillService
from .recommendation import RecommendationEngine
from .server import create_server
from .storage import Storage
from .sync import SyncService


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "poe_advisor.sqlite3"
DEFAULT_WEB_DIR = PROJECT_ROOT / "web"


def _database_path(raw: str | None) -> Path:
    configured = raw or os.environ.get("POE_ADVISOR_DB")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_DATABASE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m poe_advisor",
        description=(
            "Local-first Path of Exile softcore trade-league market research."
        ),
    )
    parser.add_argument(
        "--db",
        help="SQLite archive path (default: data/poe_advisor.sqlite3).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the interactive local dashboard.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument(
        "--open",
        action="store_true",
        help="Open the dashboard in the default browser after startup.",
    )
    serve.add_argument(
        "--no-demo-fallback",
        action="store_true",
        help="Do not seed labelled demo data when every live source is unavailable.",
    )

    sync = subparsers.add_parser("sync", help="Run one market sync without the UI.")
    sync.add_argument(
        "--history-hours",
        type=int,
        default=168,
        help="Official exchange hours to retain on first sync (0-336).",
    )
    sync.add_argument(
        "--no-demo-fallback",
        action="store_true",
        help="Return an empty archive rather than labelled demo data on total failure.",
    )

    daily_update = subparsers.add_parser(
        "daily-update",
        help="Run the complete unattended market, curve, and model refresh.",
    )
    daily_update.add_argument(
        "--history-hours",
        type=int,
        default=168,
        help="Official exchange recovery window (0-336; default: 168).",
    )
    daily_update.add_argument(
        "--seasonal-items",
        type=int,
        default=20,
        help="Completed-league assets to advance (0-2000; default: 20).",
    )
    daily_update.add_argument(
        "--current-history-items",
        type=int,
        default=100,
        help="Top current-league curves to refresh (0-2000; default: 100).",
    )

    recommend = subparsers.add_parser(
        "recommend",
        help="Print the current recommendation payload as JSON.",
    )
    recommend.add_argument(
        "--budget",
        type=float,
        default=100.0,
        help="Legacy compatibility value; does not affect priority ranking.",
    )
    recommend.add_argument("--horizon", type=int, choices=(3, 7, 14), default=7)

    seasonal_sync = subparsers.add_parser(
        "seasonal-sync",
        help="Build or continue the completed-league item archive.",
    )
    seasonal_sync.add_argument(
        "--items",
        type=int,
        default=80,
        help="Common assets to process in this resumable pass (1-2000).",
    )

    export_pages = subparsers.add_parser(
        "export-pages",
        help="Export a read-only GitHub Pages artifact from the local archive.",
    )
    export_pages.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "pages-dist"),
        help="Dedicated output directory (default: pages-dist).",
    )
    export_pages.add_argument(
        "--repository",
        help="GitHub repository in owner/name form for update links.",
    )
    export_pages.add_argument(
        "--skip-histories",
        action="store_true",
        help="Skip lazy price-curve shards (useful only for quick diagnostics).",
    )

    archive_snapshot = subparsers.add_parser(
        "archive-snapshot",
        help="Create a consistent gzip-compressed SQLite backup.",
    )
    archive_snapshot.add_argument(
        "--output",
        required=True,
        help="Destination .sqlite3.gz path.",
    )
    archive_snapshot.add_argument(
        "--compression-level",
        type=int,
        choices=range(1, 10),
        default=6,
    )

    subparsers.add_parser(
        "seed-demo",
        help="Populate a clearly labelled offline fixture for UI exploration.",
    )
    subparsers.add_parser("status", help="Print local archive status as JSON.")
    return parser


def _validate_port(value: int) -> int:
    if not 0 <= value <= 65535:
        raise ValueError("Port must be between 0 and 65535.")
    return value


def _validate_host(value: str) -> str:
    if value.lower() not in {"127.0.0.1", "localhost"}:
        raise ValueError(
            "This local-only service may bind only to 127.0.0.1 or localhost."
        )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    database = _database_path(args.db)

    if args.command == "serve":
        try:
            port = _validate_port(args.port)
            host = _validate_host(args.host)
        except ValueError as error:
            parser.error(str(error))
        if not DEFAULT_WEB_DIR.is_dir():
            parser.error(f"Dashboard files are missing: {DEFAULT_WEB_DIR}")
        server = create_server(
            host=host,
            port=port,
            database_path=database,
            web_dir=DEFAULT_WEB_DIR,
            allow_demo_seed=not args.no_demo_fallback,
        )
        actual_host, actual_port = server.server_address[:2]
        browser_host = (
            "127.0.0.1" if actual_host in {"0.0.0.0", "::", ""} else actual_host
        )
        url = f"http://{browser_host}:{actual_port}/"
        print(f"Wraeclast Ledger is running at {url}")
        print(f"Local archive: {database}")
        print("Press Ctrl+C to stop.")
        if args.open:
            webbrowser.open(url, new=2)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping Wraeclast Ledger.")
        finally:
            server.server_close()
        return 0

    storage = Storage(database)
    if args.command == "sync":
        if not 0 <= args.history_hours <= 336:
            parser.error("--history-hours must be between 0 and 336.")
        result = SyncService(
            storage,
            allow_demo_seed=not args.no_demo_fallback,
        ).sync(backfill_hours=args.history_hours)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "daily-update":
        if not 0 <= args.history_hours <= 336:
            parser.error("--history-hours must be between 0 and 336.")
        if not 0 <= args.seasonal_items <= 2000:
            parser.error("--seasonal-items must be between 0 and 2000.")
        if not 0 <= args.current_history_items <= 2000:
            parser.error("--current-history-items must be between 0 and 2000.")
        from .automation import run_daily_update

        result = run_daily_update(
            database_path=database,
            web_dir=DEFAULT_WEB_DIR,
            history_hours=args.history_hours,
            seasonal_items=args.seasonal_items,
            current_history_items=args.current_history_items,
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "recommend":
        league = storage.get_current_league()
        if league is None:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "message": "No local data. Run the sync command first.",
                    },
                    indent=2,
                )
            )
            return 1
        payload = RecommendationEngine(storage).generate(
            league,
            budget=args.budget,
            horizon=args.horizon,
        )
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "seasonal-sync":
        if not 1 <= args.items <= 2000:
            parser.error("--items must be between 1 and 2000.")
        league = storage.get_current_league()
        if league is None or league.is_demo:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "message": (
                            "Run a successful live market sync before the "
                            "completed-league backfill."
                        ),
                    },
                    indent=2,
                )
            )
            return 1
        result = HistoricalBackfillService(storage).backfill(
            league,
            max_items=args.items,
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") in {"success", "partial"} else 1

    if args.command == "export-pages":
        from .static_export import export_github_pages

        result = export_github_pages(
            database_path=database,
            web_dir=DEFAULT_WEB_DIR,
            output_path=args.output,
            repository=args.repository,
            include_histories=not args.skip_histories,
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "archive-snapshot":
        from .archive import create_compressed_database_snapshot

        result = create_compressed_database_snapshot(
            database,
            args.output,
            compression_level=args.compression_level,
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "seed-demo":
        print(json.dumps(seed_demo(storage, make_current=True), indent=2))
        return 0

    if args.command == "status":
        league = storage.get_current_league()
        print(
            json.dumps(
                {
                    "database": str(storage.path),
                    "healthy": storage.healthcheck(),
                    "league": (
                        {
                            "id": league.id,
                            "name": league.name,
                            "start_at": league.start_at,
                            "day": league.day,
                            "demo": league.is_demo,
                        }
                        if league
                        else None
                    ),
                    "counts": storage.status_counts(league.id if league else None),
                    "last_sync_at": storage.last_sync_at(
                        league.id if league else None
                    ),
                    "sources": storage.list_source_summaries(),
                },
                indent=2,
            )
        )
        return 0

    parser.error("Unknown command.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
