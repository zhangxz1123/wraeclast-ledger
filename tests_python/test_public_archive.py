from __future__ import annotations

import gzip
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from poe_advisor.archive import (
    create_compressed_database_snapshot,
    create_public_market_snapshot,
)
from poe_advisor.demo import seed_demo
from poe_advisor.models import PricePoint, parse_datetime
from poe_advisor.storage import Storage


class PublicArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "source.sqlite3"
        self.storage = Storage(self.database)
        seed_demo(self.storage, make_current=True)
        self.league = self.storage.get_current_league()
        assert self.league is not None

        self.private_payload = (
            b"private-local-provider-payload:"
            + os.urandom(512 * 1024)
        )
        self.private_snapshot_id, created = self.storage.add_snapshot(
            source="private-fixture",
            endpoint="https://private.invalid/secret",
            league_id=self.league.id,
            category="Private",
            fetched_at="2026-07-31T12:00:00Z",
            status_code=200,
            raw=self.private_payload,
            etag='"private-etag"',
            metadata={"warning": "private diagnostic"},
        )
        self.assertTrue(created)
        self._add_public_and_private_state()
        self.coverage_item_key = "skillgem:awakened-enlighten-support"
        league_start = parse_datetime(self.league.start_at)
        assert league_start is not None
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=self.league.id,
                    item_key=self.coverage_item_key,
                    name="Awakened Enlighten Support",
                    category="SkillGem",
                    source="poe.ninja",
                    observed_at=(
                        league_start + timedelta(days=day - 1)
                    ).isoformat().replace("+00:00", "Z"),
                    chaos_value=3_000.0 + day,
                    divine_value=30.0 + day,
                    confidence=0.9,
                )
                for day in (2, 3)
            ]
        )
        self.storage.upsert_current_item_history_coverage(
            league_id=self.league.id,
            item_key=self.coverage_item_key,
            provider="poe.ninja",
            source_item_id="95714",
            category="SkillGem",
            history_kind="stash-item",
            endpoint="https://poe.ninja/fixture/history/95714",
            fetched_at="2026-07-31T12:00:00Z",
            metadata={
                "provider_observed_days": [2, 3],
                "provider_missing_days": [1],
                "normalized_days": [2, 3],
                "missing_divine_anchor_days": [],
                "interpolation": "none",
            },
        )

    def _add_public_and_private_state(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO historical_assets(
                    source, source_item_id, item_key, name, category,
                    source_category, source_group, variant_json,
                    current_daily, current_chaos, current_divine,
                    low_confidence, eligible, seen_at
                ) VALUES(
                    'fixture', '42', 'gem:fixture', 'Fixture Gem', 'Gem',
                    'SkillGem', 'Gems', '{}', 10, 100, 1, 0, 1,
                    '2026-07-31T12:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO historical_fetch_state(
                    source, league_id, source_item_id, status,
                    points_written, last_error, updated_at
                ) VALUES(
                    'fixture', ?, '42', 'error', 1,
                    'private local filesystem error',
                    '2026-07-31T12:00:00Z'
                )
                """,
                (self.league.id,),
            )
            connection.execute(
                """
                INSERT INTO seasonal_prices(
                    league_id, item_key, source, source_item_id, league_day,
                    observed_at, chaos_value, divine_value, volume,
                    confidence, snapshot_id, details_json, updated_at
                ) VALUES(
                    ?, 'gem:fixture', 'fixture', '42', 1,
                    '2026-07-31T12:00:00Z', 100, 1, 2, 0.9, ?, '{}',
                    '2026-07-31T12:00:00Z'
                )
                """,
                (self.league.id, self.private_snapshot_id),
            )
            connection.execute(
                """
                INSERT INTO meta_class_snapshots(
                    league_id, observed_at, source, league_day, sample_size,
                    page_count, counts_json, shares_json, snapshot_ids_json,
                    created_at
                ) VALUES(
                    ?, '2026-07-31T12:00:00Z', 'fixture', 1, 100, 1,
                    '{"Witch":100}', '{"Witch":1}', ?,
                    '2026-07-31T12:00:00Z'
                )
                """,
                (self.league.id, f"[{self.private_snapshot_id}]"),
            )
            connection.execute(
                """
                INSERT INTO sync_runs(
                    started_at, finished_at, status, league_id,
                    rows_written, snapshots_written, message, warnings_json
                ) VALUES(
                    '2026-07-31T12:00:00Z', '2026-07-31T12:01:00Z',
                    'partial', ?, 1, 1, 'private machine error',
                    '["private warning"]'
                )
                """,
                (self.league.id,),
            )
            connection.execute(
                """
                INSERT INTO recommendation_runs(
                    league_id, generated_at, budget, horizon_days, payload_json
                ) VALUES(
                    ?, '2026-07-31T12:00:00Z', 100, 7,
                    '{"rankings":[{"item_key":"gem:fixture"}]}'
                )
                """,
                (self.league.id,),
            )
            settings = {
                "exchange_categories": '["Currency"]',
                "item_categories": '["Gem"]',
                "ggg_currency_cursor:Currency": "1785000000",
                "poe_ninja_dump:Mirage": json.dumps(
                    {
                        "import_version": 1,
                        "league_name": "Mirage",
                        "min_date": "2025-10-31T00:00:00Z",
                        "max_date": "2026-03-03T00:00:00Z",
                        "zip_name": "Mirage.zip",
                        "status": "success",
                        "sha256": "a" * 64,
                        "download_bytes": 54748036,
                        "seasonal_rows_written": 15475,
                        "stored_seasonal_rows": 15475,
                        "raw_source_rows_seen": 20000,
                        "normalized_source_rows": 18000,
                        "eligible_source_rows": 16000,
                        "storage_mode": "compact",
                        "imported_at": "2026-07-31T12:00:00Z",
                    }
                ),
                "gggXcurrencyXcursor:lookalike": '"must-delete"',
                "ggg_oauth_token": '"must-delete"',
                "local_path": '"C:/private/path"',
            }
            connection.executemany(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES(?, ?, '2026-07-31T12:00:00Z')
                """,
                settings.items(),
            )
            connection.commit()

    def _restore(self, compressed: Path, name: str) -> Path:
        restored = self.root / name
        with gzip.open(compressed, "rb") as source:
            restored.write_bytes(source.read())
        return restored

    def _seed_additive_compact_conversion(self) -> None:
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.ninja-history",
                    "source_item_id": "compact-fixture",
                    "item_key": "currency:compact-fixture",
                    "name": "Compact Fixture",
                    "category": "Currency",
                    "eligible": True,
                }
            ]
        )
        self.storage.upsert_seasonal_prices(
            [
                {
                    "league_id": self.league.id,
                    "item_key": "currency:compact-fixture",
                    "source": "poe.ninja-history",
                    "source_item_id": "compact-fixture",
                    "league_day": day,
                    "observed_at": f"2026-07-{day:02d}T00:00:00Z",
                    "chaos_value": float(day),
                    "divine_value": float(day) / 100.0,
                    "confidence": 0.9,
                }
                for day in (1, 2)
            ]
        )
        converted = self.storage.compact_official_history_from_full(
            [self.league.id]
        )
        self.assertEqual(converted["stored_rows"], 2)

    def _dump_marker(self, *, compact: bool) -> dict[str, object]:
        marker: dict[str, object] = {
            "import_version": 3,
            "league_name": self.league.id,
            "min_date": "2026-07-01T00:00:00Z",
            "max_date": "2026-07-31T00:00:00Z",
            "zip_name": f"{self.league.id}.zip",
            "status": "success",
            "sha256": "b" * 64,
            "download_bytes": 1234,
            "seasonal_rows_written": 2,
            "imported_at": "2026-07-31T12:00:00Z",
        }
        if compact:
            marker.update(
                {
                    "stored_seasonal_rows": 2,
                    "raw_source_rows_seen": 2,
                    "normalized_source_rows": 2,
                    "eligible_source_rows": 2,
                    "storage_mode": "compact",
                }
            )
        return marker

    def test_public_snapshot_strips_private_state_and_keeps_market_data(
        self,
    ) -> None:
        with closing(sqlite3.connect(self.database)) as source:
            preserved_counts = {
                table: source.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "leagues",
                    "price_points",
                    "historical_assets",
                    "historical_fetch_state",
                    "seasonal_prices",
                    "current_item_history_coverage",
                    "meta_class_snapshots",
                    "recommendation_runs",
                )
            }
            source_raw_count = source.execute(
                "SELECT COUNT(*) FROM raw_snapshots"
            ).fetchone()[0]

        compressed = self.root / "archive" / "public.sqlite3.gz"
        summary = create_public_market_snapshot(
            self.database,
            compressed,
            compression_level=1,
        )

        self.assertEqual(summary["archive_kind"], "public-market")
        self.assertTrue(summary["sanitized"])
        self.assertEqual(summary["integrity"], "ok")
        self.assertLess(
            summary["database_bytes_after_sanitization"],
            summary["database_bytes_before_sanitization"],
        )
        self.assertEqual(
            summary["sanitization"]["raw_snapshots_removed"],
            source_raw_count,
        )
        self.assertGreater(
            summary["sanitization"]["snapshot_references_cleared"],
            0,
        )
        self.assertGreater(summary["sanitization"]["settings_removed"], 0)

        restored = self._restore(compressed, "public-restored.sqlite3")
        with closing(sqlite3.connect(restored)) as archive:
            archive.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(
                archive.execute("PRAGMA quick_check").fetchone()[0],
                "ok",
            )
            self.assertEqual(
                archive.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            for table, expected in preserved_counts.items():
                self.assertEqual(
                    archive.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    expected,
                    table,
                )
            self.assertEqual(
                archive.execute(
                    "SELECT COUNT(*) FROM raw_snapshots"
                ).fetchone()[0],
                0,
            )

            self.assertEqual(
                archive.execute(
                    """
                    SELECT COUNT(*) FROM price_points
                    WHERE snapshot_id IS NOT NULL
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                archive.execute(
                    """
                    SELECT COUNT(*) FROM seasonal_prices
                    WHERE snapshot_id IS NOT NULL
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                archive.execute(
                    "SELECT snapshot_ids_json FROM meta_class_snapshots"
                ).fetchone()[0],
                "[]",
            )
            self.assertIsNone(
                archive.execute(
                    "SELECT last_error FROM historical_fetch_state"
                ).fetchone()[0]
            )
            self.assertEqual(
                archive.execute(
                    "SELECT COUNT(*) FROM source_state"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                archive.execute(
                    "SELECT COUNT(*) FROM sync_runs"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                [
                    row[0]
                    for row in archive.execute(
                        "SELECT key FROM settings ORDER BY key"
                    )
                ],
                ["ggg_currency_cursor:Currency", "poe_ninja_dump:Mirage"],
            )

        restored_storage = Storage(restored)
        restored_coverage = restored_storage.current_item_history_archive(
            self.league.id,
            self.coverage_item_key,
            provider="poe.ninja",
        )
        assert restored_coverage is not None
        self.assertTrue(restored_coverage["normalized_price_days_complete"])
        self.assertEqual(restored_coverage["normalized_days"], [2, 3])
        restored_curve = restored_storage.daily_item_history(
            self.league.id,
            self.coverage_item_key,
            self.league.start_at,
            minimum_confidence=0.0,
            sources=("poe.ninja",),
        )
        self.assertEqual(
            [int(point["league_day"]) for point in restored_curve],
            [2, 3],
        )

        # VACUUM must physically remove the exact compressed private blob,
        # not merely make it unreachable in SQLite's b-tree.
        private_blob = gzip.compress(
            self.private_payload,
            compresslevel=6,
            mtime=0,
        )
        self.assertNotIn(private_blob, restored.read_bytes())

        # The resulting schema remains writable by the next daily update.
        updater_storage = Storage(restored)
        coverage = updater_storage.current_item_history_archive(
            self.league.id,
            self.coverage_item_key,
            provider="poe.ninja",
        )
        assert coverage is not None
        self.assertTrue(coverage["durable"])
        self.assertEqual(coverage["provider_observed_days"], [2, 3])
        self.assertEqual(coverage["provider_missing_days"], [1])
        _, created = updater_storage.add_snapshot(
            source="fixture-next-update",
            endpoint="https://example.invalid/current",
            league_id=self.league.id,
            category="Currency",
            fetched_at="2026-08-01T12:00:00Z",
            status_code=200,
            raw=b'{"next":"update"}',
        )
        self.assertTrue(created)
        self.assertTrue(updater_storage.healthcheck())

        # Sanitization is applied only to the copy.
        self.assertEqual(
            self.storage.read_snapshot(self.private_snapshot_id),
            self.private_payload,
        )

    def test_full_snapshot_remains_a_full_backup(self) -> None:
        compressed = self.root / "archive" / "full.sqlite3.gz"
        summary = create_compressed_database_snapshot(
            self.database,
            compressed,
            compression_level=1,
        )
        self.assertEqual(summary["archive_kind"], "full")
        self.assertFalse(summary["sanitized"])

        restored = self._restore(compressed, "full-restored.sqlite3")
        full_storage = Storage(restored)
        self.assertEqual(
            full_storage.read_snapshot(self.private_snapshot_id),
            self.private_payload,
        )

    def test_public_snapshot_drops_only_redundant_verbose_golden_rows(
        self,
    ) -> None:
        self._seed_additive_compact_conversion()
        self.storage.set_setting(
            f"poe_ninja_dump:{self.league.id}",
            self._dump_marker(compact=True),
        )

        compressed = self.root / "archive" / "compact-public.sqlite3.gz"
        summary = create_public_market_snapshot(
            self.database,
            compressed,
            compression_level=1,
        )
        self.assertEqual(
            summary["sanitization"]["verbose_seasonal_rows_removed"],
            2,
        )
        self.assertEqual(
            summary["sanitization"]["compact_leagues_validated"],
            1,
        )
        with closing(sqlite3.connect(self.database)) as source:
            self.assertEqual(
                source.execute(
                    """
                    SELECT COUNT(*) FROM seasonal_prices
                    WHERE source = 'poe.ninja-history'
                    """
                ).fetchone()[0],
                2,
            )

        restored = self._restore(compressed, "compact-public-restored.sqlite3")
        with closing(sqlite3.connect(restored)) as archive:
            self.assertEqual(
                archive.execute(
                    """
                    SELECT COUNT(*) FROM seasonal_prices
                    WHERE source = 'poe.ninja-history'
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                archive.execute(
                    "SELECT COUNT(*) FROM compact_seasonal_prices"
                ).fetchone()[0],
                2,
            )

    def test_public_snapshot_keeps_verbose_rows_for_full_marker(self) -> None:
        self._seed_additive_compact_conversion()
        marker_key = f"poe_ninja_dump:{self.league.id}"
        self.storage.set_setting(
            marker_key,
            self._dump_marker(compact=False),
        )

        compressed = self.root / "archive" / "full-marker-public.sqlite3.gz"
        summary = create_public_market_snapshot(
            self.database,
            compressed,
            compression_level=1,
        )
        self.assertEqual(
            summary["sanitization"]["compact_leagues_validated"],
            0,
        )
        self.assertEqual(
            summary["sanitization"]["verbose_seasonal_rows_removed"],
            0,
        )

        restored = self._restore(
            compressed,
            "full-marker-public-restored.sqlite3",
        )
        with closing(sqlite3.connect(restored)) as archive:
            self.assertEqual(
                archive.execute(
                    """
                    SELECT COUNT(*) FROM seasonal_prices
                    WHERE source = 'poe.ninja-history'
                    """
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                archive.execute(
                    "SELECT COUNT(*) FROM compact_seasonal_prices"
                ).fetchone()[0],
                2,
            )
            saved = json.loads(
                archive.execute(
                    "SELECT value_json FROM settings WHERE key = ?",
                    (marker_key,),
                ).fetchone()[0]
            )
            self.assertNotIn("storage_mode", saved)
            self.assertEqual(saved["seasonal_rows_written"], 2)


if __name__ == "__main__":
    unittest.main()
