from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from poe_advisor.archive import (
    create_compressed_database_snapshot,
    create_public_market_snapshot,
)
from poe_advisor.demo import seed_demo
from poe_advisor.provenance import production_price_provenance
from poe_advisor.static_export import (
    _assert_completed_history_ready,
    _assert_curve_provenance,
    _assert_export_provenance,
    _compact_forecast,
    _compact_static_comparison,
    _rank_map_for_horizon,
    export_github_pages,
)
from poe_advisor.storage import Storage


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class StaticExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "source.sqlite3"
        seed_demo(Storage(self.database), make_current=True)

    def test_compact_forecast_retains_expected_price(self) -> None:
        compact = _compact_forecast(
            {
                "days": 7,
                "expected_gain_pct": 50.0,
                "expected_price_divine": 1.5,
                "historical_target_divine": 4.0,
            },
            7,
        )
        self.assertEqual(compact["expected_price_divine"], 1.5)
        self.assertEqual(compact["historical_target_divine"], 4.0)

    def test_export_is_hashed_complete_and_excludes_database(self) -> None:
        output = self.root / "pages"
        result = export_github_pages(
            database_path=self.database,
            web_dir=PROJECT_ROOT / "web",
            output_path=output,
            repository="example/wraeclast-ledger",
            allow_demo_export=True,
        )

        self.assertTrue(result["ok"])
        self.assertGreater(result["ranked_items"], 0)
        self.assertTrue((output / ".nojekyll").is_file())
        self.assertEqual(
            (output / "index.html").read_text(encoding="utf-8").count(
                'href="./styles.css"'
            ),
            1,
        )
        manifest = json.loads(
            (output / "data" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["mode"], "github-pages")
        self.assertEqual(
            manifest["workflow_url"],
            (
                "https://github.com/example/wraeclast-ledger/actions/"
                "workflows/daily-pages.yml"
            ),
        )
        self.assertRegex(
            manifest["catalog"],
            r"^data/recommendations\.[0-9a-f]{16}\.json$",
        )
        self.assertRegex(
            manifest["ranking_index"],
            r"^data/ranking-index\.[0-9a-f]{16}\.json$",
        )
        self.assertRegex(
            manifest["status"],
            r"^data/status\.[0-9a-f]{16}\.json$",
        )
        self.assertEqual(
            manifest["archive"]["asset"],
            "poe_market_compact_history.sqlite3.gz",
        )
        self.assertTrue(manifest["archive"]["public_market_only"])
        self.assertEqual(
            manifest["price_provenance"]["policy"],
            "offline-demo-fixture",
        )

        catalog = json.loads(
            (output / manifest["catalog"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            catalog["price_provenance"],
            manifest["price_provenance"],
        )
        self.assertNotIn("rankings", catalog)
        self.assertNotIn("recommendations", catalog)
        self.assertNotIn("watchlist", catalog)

        index_payload = json.loads(
            (output / manifest["ranking_index"]).read_text(encoding="utf-8")
        )
        positions = {
            field: index for index, field in enumerate(index_payload["fields"])
        }
        keys = [
            row[positions["key"]]
            for row in index_payload["items"]
        ]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), manifest["ranked_items"])
        for horizon in ("3", "7", "14"):
            ranks = sorted(
                row[positions[f"rank_{horizon}d"]]
                for row in index_payload["items"]
            )
            self.assertEqual(ranks, list(range(1, len(keys) + 1)))

        ranking_pages = manifest["ranking_pages"]
        self.assertEqual(ranking_pages["page_size"], 100)
        rows_by_key = {}
        for horizon, page_paths in ranking_pages["horizons"].items():
            published_ranks = []
            published_keys = []
            for page_number, relative_path in enumerate(page_paths, start=1):
                self.assertRegex(
                    relative_path,
                    rf"^data/rankings/{horizon}/[0-9]{{4}}\.[0-9a-f]{{16}}\.json$",
                )
                page_payload = json.loads(
                    (output / relative_path).read_text(encoding="utf-8")
                )
                self.assertEqual(page_payload["page"], page_number)
                self.assertLessEqual(len(page_payload["items"]), 100)
                for row in page_payload["items"]:
                    published_ranks.append(row["rank"])
                    published_keys.append(row["key"])
                    rows_by_key[row["key"]] = row
                    for forecast_key in (
                        "forecast_3d",
                        "forecast_7d",
                        "forecast_14d",
                    ):
                        self.assertNotIn(
                            "historical_observations",
                            row[forecast_key],
                        )
            self.assertEqual(published_ranks, list(range(1, len(keys) + 1)))
            self.assertEqual(set(published_keys), set(keys))

        for shard, relative_path in manifest["history_shards"].items():
            self.assertRegex(shard, r"^[0-9a-f]{2}$")
            shard_payload = json.loads(
                (output / relative_path).read_text(encoding="utf-8")
            )
            for item_key in shard_payload["items"]:
                row = rows_by_key[item_key]
                self.assertEqual(row["history_shard"], shard)

        published_files = [
            path.relative_to(output).as_posix() for path in output.rglob("*")
            if path.is_file()
        ]
        self.assertFalse(
            any(
                name.endswith((".sqlite", ".sqlite3", ".sqlite3.gz"))
                for name in published_files
            )
        )

    def test_demo_export_is_rejected_by_default(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "demo fixture"):
            export_github_pages(
                database_path=self.database,
                web_dir=PROJECT_ROOT / "web",
                output_path=self.root / "rejected-pages",
            )

    def test_horizon_rank_map_uses_embedded_forecasts(self) -> None:
        payload = {
            "rankings": [
                {
                    "key": "item:b",
                    "name": "Beta",
                    "forecast_3d": {"expected_gain": None},
                    "forecast_7d": {"expected_gain": 0.2},
                    "forecast_14d": {"expected_gain": -0.1},
                },
                {
                    "key": "item:a",
                    "name": "Alpha",
                    "forecast_3d": {"expected_gain": 0.3},
                    "forecast_7d": {"expected_gain": 0.1},
                    "forecast_14d": {"expected_gain": -0.1},
                },
            ]
        }

        self.assertEqual(
            _rank_map_for_horizon(payload, 3),
            {"item:a": 1, "item:b": 2},
        )
        self.assertEqual(
            _rank_map_for_horizon(payload, 7),
            {"item:b": 1, "item:a": 2},
        )
        self.assertEqual(
            _rank_map_for_horizon(payload, 14),
            {"item:a": 1, "item:b": 2},
        )

    def test_static_comparison_keeps_only_rendered_weighted_window(self) -> None:
        comparison = {
            "current_league": {"points": [{"league_day": 8}]},
            "weighted_historical": {
                "points": [
                    {"league_day": 1, "divine_value": 1.0},
                    {"league_day": 22, "divine_value": 2.0},
                    {"league_day": 23, "divine_value": 3.0},
                ]
            },
            "past_leagues": [
                {"league_id": "Mirage", "points": [{"league_day": 1}]}
            ],
        }

        compact = _compact_static_comparison(
            comparison,
            maximum_league_day=22,
        )

        self.assertNotIn("past_leagues", compact)
        weighted = compact["weighted_historical"]
        self.assertEqual(
            [point["league_day"] for point in weighted["points"]],
            [1, 22],
        )
        self.assertEqual(weighted["omitted_points"], 1)
        self.assertEqual(weighted["static_maximum_league_day"], 22)

    def test_completed_history_marker_must_match_actual_row_count(self) -> None:
        from poe_advisor.historical import BROADLY_COVERED_LEAGUE_IDS
        from poe_advisor.ninja_history import (
            DUMP_IMPORT_VERSION,
            DUMP_SETTING_PREFIX,
        )

        database = self.root / "history-ready.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE seasonal_prices (league_id TEXT, source TEXT)"
            )
            connection.executemany(
                "INSERT INTO seasonal_prices VALUES (?, 'poe.ninja-history')",
                [(league_id,) for league_id in BROADLY_COVERED_LEAGUE_IDS],
            )
            connection.commit()

        class FakeStorage:
            def __init__(self, path: Path):
                self.path = path
                self.settings = {
                    f"{DUMP_SETTING_PREFIX}{league_id}": {
                        "status": "success",
                        "import_version": DUMP_IMPORT_VERSION,
                        "league_name": league_id,
                        "seasonal_rows_written": 1,
                    }
                    for league_id in BROADLY_COVERED_LEAGUE_IDS
                }

            def get_setting(self, key, default):
                return self.settings.get(key, default)

            def connect(self):
                return sqlite3.connect(self.path)

        storage = FakeStorage(database)
        _assert_completed_history_ready(storage)
        marker_key = f"{DUMP_SETTING_PREFIX}{BROADLY_COVERED_LEAGUE_IDS[0]}"
        storage.settings[marker_key]["seasonal_rows_written"] = 2
        with self.assertRaisesRegex(RuntimeError, "complete poe.ninja"):
            _assert_completed_history_ready(storage)

    def test_completed_history_accepts_only_exact_compact_counts(self) -> None:
        from poe_advisor.historical import BROADLY_COVERED_LEAGUE_IDS
        from poe_advisor.ninja_history import (
            DUMP_IMPORT_VERSION,
            DUMP_SETTING_PREFIX,
        )

        storage = Storage(
            self.root / "compact-history-ready.sqlite3",
            compact_history=True,
        )
        for index, league_id in enumerate(BROADLY_COVERED_LEAGUE_IDS, start=1):
            storage.upsert_compact_seasonal_prices(
                [
                    {
                        "league_id": league_id,
                        "item_key": f"currency:fixture-{index}",
                        "source": "poe.ninja-history",
                        "source_item_id": f"fixture-{index}",
                        "league_day": 1,
                        "observed_at": "2025-01-01T00:00:00Z",
                        "chaos_value": float(index),
                        "divine_value": float(index) / 100.0,
                        "confidence": 0.9,
                    }
                ],
                staging=False,
            )
            storage.set_setting(
                f"{DUMP_SETTING_PREFIX}{league_id}",
                {
                    "status": "success",
                    "import_version": DUMP_IMPORT_VERSION,
                    "league_name": league_id,
                    "seasonal_rows_written": 1,
                    "stored_seasonal_rows": 1,
                    "raw_source_rows_seen": 3,
                    "normalized_source_rows": 2,
                    "eligible_source_rows": 1,
                    "storage_mode": "compact",
                },
            )

        _assert_completed_history_ready(storage)
        marker_key = f"{DUMP_SETTING_PREFIX}{BROADLY_COVERED_LEAGUE_IDS[0]}"
        marker = storage.get_setting(marker_key)
        marker["eligible_source_rows"] = 0
        storage.set_setting(marker_key, marker)
        with self.assertRaisesRegex(RuntimeError, "complete poe.ninja"):
            _assert_completed_history_ready(storage)

        marker["eligible_source_rows"] = 1
        storage.set_setting(marker_key, marker)
        with storage.transaction() as connection:
            connection.execute(
                """
                DELETE FROM compact_seasonal_prices
                WHERE league_key = (
                    SELECT id FROM compact_seasonal_leagues
                    WHERE league_id = ?
                )
                """,
                (BROADLY_COVERED_LEAGUE_IDS[0],),
            )
        with self.assertRaisesRegex(RuntimeError, "complete poe.ninja"):
            _assert_completed_history_ready(storage)

    def test_production_export_requires_exact_golden_source_policy(self) -> None:
        _assert_export_provenance(
            {"price_provenance": production_price_provenance()},
            is_demo=False,
        )
        with self.assertRaisesRegex(RuntimeError, "poe.ninja"):
            _assert_export_provenance(
                {
                    "price_provenance": {
                        "policy": "untrusted",
                    }
                },
                is_demo=False,
            )

    def test_static_curve_rejects_non_golden_sources(self) -> None:
        _assert_curve_provenance(
            {
                "current_league": {
                    "points": [{"source": "poe.ninja"}],
                },
                "past_leagues": [
                    {"points": [{"source": "poe.ninja-history"}]},
                ],
            },
            is_demo=False,
        )
        with self.assertRaisesRegex(RuntimeError, "historical curve"):
            _assert_curve_provenance(
                {
                    "current_league": {
                        "points": [{"source": "poe.ninja"}],
                    },
                    "past_leagues": [
                        {"points": [{"source": "poe.watch"}]},
                    ],
                },
                is_demo=False,
            )

    def test_compressed_snapshot_round_trips_with_integrity(self) -> None:
        compressed = self.root / "archive" / "poe_advisor.sqlite3.gz"
        summary = create_compressed_database_snapshot(
            self.database,
            compressed,
            compression_level=1,
        )
        self.assertEqual(summary["integrity"], "ok")
        self.assertGreater(summary["compressed_bytes"], 0)

        restored = self.root / "restored.sqlite3"
        with gzip.open(compressed, "rb") as source:
            restored.write_bytes(source.read())
        restored_storage = Storage(restored)
        self.assertTrue(restored_storage.healthcheck())
        self.assertEqual(
            restored_storage.status_counts()["price_points"],
            Storage(self.database).status_counts()["price_points"],
        )

    def test_public_market_snapshot_strips_private_and_raw_state(self) -> None:
        original_price_points = Storage(self.database).status_counts()[
            "price_points"
        ]
        with closing(sqlite3.connect(self.database)) as connection:
            league_id = connection.execute(
                "SELECT id FROM leagues WHERE is_current = 1"
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO raw_snapshots(
                    source, endpoint, league_id, category, fetched_at,
                    status_code, sha256, payload_gzip, payload_bytes,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "private-test",
                    "https://example.invalid/private",
                    league_id,
                    "test",
                    "2026-01-01T00:00:00Z",
                    200,
                    "a" * 64,
                    b"private raw response",
                    20,
                    '{"authorization":"Bearer private"}',
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE price_points
                SET snapshot_id = ?
                WHERE id = (SELECT MIN(id) FROM price_points)
                """,
                (snapshot_id,),
            )
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES ('oauth_token', '"private"', '2026-01-01T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES (
                    'poe_ninja_dump:Mirage', ?,
                    '2026-01-01T00:00:00Z'
                )
                """,
                (
                    json.dumps(
                        {
                            "import_version": 1,
                            "league_name": "Mirage",
                            "min_date": "2025-10-31T00:00:00Z",
                            "max_date": "2026-03-03T00:00:00Z",
                            "zip_name": "Mirage.zip",
                            "status": "success",
                            "sha256": "b" * 64,
                            "download_bytes": 54748036,
                            "seasonal_rows_written": 15475,
                            "imported_at": "2026-07-31T12:00:00Z",
                        }
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES (
                    'ggg_currency_cursor:test',
                    '1785000000',
                    '2026-01-01T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO source_state(
                    source, endpoint, league_id, category, status, detail
                ) VALUES (
                    'private-test', 'https://example.invalid/private',
                    ?, 'test', 'failed', 'C:\\Users\\Someone\\secret.txt'
                )
                """,
                (league_id,),
            )
            connection.execute(
                """
                INSERT INTO sync_runs(
                    started_at, status, league_id, message, warnings_json
                ) VALUES (
                    '2026-01-01T00:00:00Z', 'failed', ?,
                    'Authorization: Bearer private',
                    '["client_secret"]'
                )
                """,
                (league_id,),
            )
            connection.commit()

        compressed = self.root / "archive" / "poe_market_history.sqlite3.gz"
        summary = create_public_market_snapshot(
            self.database,
            compressed,
            compression_level=1,
        )
        self.assertEqual(summary["archive_kind"], "public-market")
        self.assertTrue(summary["sanitized"])
        self.assertGreater(summary["sanitization"]["text_values_scanned"], 0)

        restored = self.root / "public.sqlite3"
        with gzip.open(compressed, "rb") as source:
            restored.write_bytes(source.read())
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM price_points"
                ).fetchone()[0],
                original_price_points,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM raw_snapshots"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM price_points "
                    "WHERE snapshot_id IS NOT NULL"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM source_state"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sync_runs"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT key, value_json FROM settings ORDER BY key"
                ).fetchall(),
                [
                    ("ggg_currency_cursor:test", "1785000000"),
                    (
                        "poe_ninja_dump:Mirage",
                        json.dumps(
                            {
                                "import_version": 1,
                                "league_name": "Mirage",
                                "min_date": "2025-10-31T00:00:00Z",
                                "max_date": "2026-03-03T00:00:00Z",
                                "zip_name": "Mirage.zip",
                                "status": "success",
                                "sha256": "b" * 64,
                                "download_bytes": 54748036,
                                "seasonal_rows_written": 15475,
                                "imported_at": "2026-07-31T12:00:00Z",
                            }
                        ),
                    ),
                ],
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_public_market_snapshot_rejects_private_text_in_kept_rows(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                UPDATE price_points
                SET details_json = '{"client_secret":"must-not-publish"}'
                WHERE id = (SELECT MIN(id) FROM price_points)
                """
            )
            connection.commit()
        output = self.root / "archive" / "blocked.sqlite3.gz"
        with self.assertRaisesRegex(RuntimeError, "prohibited private text"):
            create_public_market_snapshot(
                self.database,
                output,
                compression_level=1,
            )
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
