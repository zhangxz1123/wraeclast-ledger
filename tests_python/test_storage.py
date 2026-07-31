from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from poe_advisor.models import League, PricePoint
from poe_advisor.storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "market-history.sqlite3"
        )
        self.storage = Storage(self.database_path)
        self.league = League(
            id="Test Softcore",
            name="Test Softcore",
            start_at="2026-07-24T20:00:00Z",
        )
        self.storage.upsert_league(self.league)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_raw_snapshot_round_trip_and_content_deduplication(self) -> None:
        payload = json.dumps(
            {"lines": [{"name": "Veiled Orb", "divineValue": 1.5}]},
            separators=(",", ":"),
        ).encode("utf-8")
        snapshot_id, created = self.storage.add_snapshot(
            source="poe.ninja",
            endpoint="https://example.invalid/overview",
            league_id=self.league.id,
            category="Currency",
            fetched_at="2026-07-29T12:00:00Z",
            status_code=200,
            raw=payload,
            etag='"fixture-v1"',
            metadata={"fixture": True},
        )
        duplicate_id, duplicate_created = self.storage.add_snapshot(
            source="poe.ninja",
            endpoint="https://example.invalid/overview",
            league_id=self.league.id,
            category="Currency",
            fetched_at="2026-07-29T13:00:00Z",
            status_code=200,
            raw=payload,
            etag='"fixture-v1"',
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(snapshot_id, duplicate_id)
        self.assertEqual(self.storage.read_snapshot(snapshot_id), payload)
        self.assertIsNone(self.storage.read_snapshot(999_999))
        latest = self.storage.latest_snapshot(
            source="poe.ninja",
            endpoint="https://example.invalid/overview",
            league_id=self.league.id,
            category="Currency",
        )
        assert latest is not None
        self.assertEqual(latest["id"], snapshot_id)
        self.assertEqual(latest["raw"], payload)
        self.assertEqual(latest["metadata"], {"fixture": True})
        self.assertEqual(
            self.storage.status_counts(self.league.id)["snapshots"],
            1,
        )

    def test_price_point_upsert_history_and_status_counts(self) -> None:
        snapshot_id, _ = self.storage.add_snapshot(
            source="fixture",
            endpoint="fixture://prices",
            league_id=self.league.id,
            category="Currency",
            fetched_at="2026-07-29T12:00:00Z",
            status_code=200,
            raw=b"{}",
        )
        first = PricePoint(
            league_id=self.league.id,
            item_key="currency:veiled-orb",
            name="Veiled Orb",
            category="Currency",
            source="ggg-currency-exchange",
            observed_at="2026-07-29T12:00:00Z",
            chaos_value=210.0,
            divine_value=1.5,
            listing_count=None,
            volume=900.0,
            confidence=3.0,
            details={"fixture": 1},
            snapshot_id=snapshot_id,
        )
        second_hour = PricePoint(
            league_id=self.league.id,
            item_key="currency:veiled-orb",
            name="Veiled Orb",
            category="Currency",
            source="ggg-currency-exchange",
            observed_at="2026-07-29T13:00:00Z",
            chaos_value=224.0,
            divine_value=1.6,
            volume=1000.0,
            confidence=0.8,
            snapshot_id=snapshot_id,
        )
        self.assertEqual(self.storage.insert_price_points([first, second_hour]), 2)

        replacement = PricePoint(
            league_id=self.league.id,
            item_key=first.item_key,
            name=first.name,
            category=first.category,
            source=first.source,
            observed_at=first.observed_at,
            chaos_value=217.0,
            divine_value=1.55,
            volume=950.0,
            confidence=-2.0,
            details={"fixture": 2},
            snapshot_id=snapshot_id,
        )
        self.storage.insert_price_points([replacement])

        counts = self.storage.status_counts(self.league.id)
        self.assertEqual(counts["price_points"], 2)
        self.assertEqual(counts["exchange_hours"], 2)
        self.assertGreater(counts["size_bytes"], 0)

        history = self.storage.all_time_item_history(
            self.league.id,
            first.item_key,
        )
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["divine_value"], 1.55)
        self.assertEqual(history[0]["confidence"], 0.0)
        grouped = self.storage.item_histories(
            self.league.id,
            item_key=first.item_key,
        )
        self.assertEqual(list(grouped), [first.item_key])
        self.assertEqual(grouped[first.item_key][0]["details"], {"fixture": 2})

    def test_current_league_source_state_settings_and_sync_status(self) -> None:
        replacement = League(
            id="New Softcore",
            name="New Softcore",
            start_at="2026-08-01T20:00:00Z",
        )
        self.storage.upsert_league(replacement, current=True)
        self.assertEqual(self.storage.get_current_league(), replacement)
        self.assertEqual(self.storage.get_league(self.league.id), self.league)

        self.storage.update_source_state(
            source="poe.ninja",
            endpoint="fixture://economy",
            league_id=replacement.id,
            category="Currency",
            status="ok",
            detail="fixture response",
            etag='"v2"',
            success=True,
        )
        state = self.storage.get_source_state(
            "poe.ninja",
            "fixture://economy",
            replacement.id,
            "Currency",
        )
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "ok")
        self.assertIsNotNone(state["last_success_at"])
        self.assertEqual(
            self.storage.list_source_summaries()[0]["source"],
            "poe.ninja",
        )

        self.storage.set_setting("item_categories", ["Currency", "Scarab"])
        self.storage.set_setting("api_key", "must-not-leak")
        self.assertEqual(
            self.storage.get_setting("item_categories"),
            ["Currency", "Scarab"],
        )
        self.assertNotIn("api_key", self.storage.public_settings())

        run_id = self.storage.start_sync_run(replacement.id)
        self.storage.finish_sync_run(
            run_id,
            status="success",
            rows_written=12,
            snapshots_written=2,
            message="fixture sync",
            warnings=[],
        )
        self.assertIsNotNone(self.storage.last_sync_at(replacement.id))
        self.assertTrue(self.storage.healthcheck())

    def test_history_limit_keeps_the_newest_observations(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        points = [
            PricePoint(
                league_id=self.league.id,
                item_key="currency:fixture-orb",
                name="Fixture Orb",
                category="Currency",
                source="fixture",
                observed_at=(start + timedelta(hours=index))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                chaos_value=float(index),
                divine_value=float(index + 1),
            )
            for index in range(1005)
        ]
        self.assertEqual(self.storage.insert_price_points(points), 1005)

        history = self.storage.all_time_item_history(
            self.league.id,
            "currency:fixture-orb",
            limit=1000,
        )
        self.assertEqual(len(history), 1000)
        self.assertEqual(history[0]["divine_value"], 6.0)
        self.assertEqual(history[-1]["divine_value"], 1005.0)

    def test_latest_item_prices_uses_exact_keys_and_newest_observation(self) -> None:
        standard = League(id="Standard", name="Standard")
        self.storage.upsert_league(standard, current=False)
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=standard.id,
                    item_key="currency:exact-a",
                    name="Exact A",
                    category="Currency",
                    source="poe.ninja",
                    observed_at="2026-07-29T12:00:00Z",
                    chaos_value=100.0,
                    divine_value=1.0,
                ),
                PricePoint(
                    league_id=standard.id,
                    item_key="currency:exact-a",
                    name="Exact A",
                    category="Currency",
                    source="poe.ninja",
                    observed_at="2026-07-30T12:00:00Z",
                    chaos_value=200.0,
                    divine_value=2.0,
                ),
                PricePoint(
                    league_id=standard.id,
                    item_key="currency:exact-b",
                    name="Exact B",
                    category="Currency",
                    source="poe.ninja",
                    observed_at="2026-07-30T12:00:00Z",
                    chaos_value=300.0,
                    divine_value=3.0,
                ),
            ]
        )

        anchors = self.storage.latest_item_prices(standard.id)

        self.assertEqual(set(anchors), {"currency:exact-a", "currency:exact-b"})
        self.assertEqual(anchors["currency:exact-a"]["divine_value"], 2.0)
        self.assertEqual(
            anchors["currency:exact-a"]["observed_at"],
            "2026-07-30T12:00:00Z",
        )

    def test_daily_and_seasonal_curves_are_exact_confident_daily_points(
        self,
    ) -> None:
        item_key = "skillgem:awakened-enlighten-support"
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=self.league.id,
                    item_key=item_key,
                    name="Awakened Enlighten Support",
                    category="SkillGem",
                    source="poe.ninja",
                    observed_at="2026-07-24T21:00:00Z",
                    chaos_value=5000,
                    divine_value=30,
                    confidence=0.8,
                ),
                PricePoint(
                    league_id=self.league.id,
                    item_key=item_key,
                    name="Awakened Enlighten Support",
                    category="SkillGem",
                    source="poe.ninja",
                    observed_at="2026-07-25T18:00:00Z",
                    chaos_value=5200,
                    divine_value=32,
                    confidence=0.9,
                ),
                PricePoint(
                    league_id=self.league.id,
                    item_key=item_key,
                    name="Awakened Enlighten Support",
                    category="SkillGem",
                    source="poe.ninja",
                    observed_at="2026-07-25T19:00:00Z",
                    chaos_value=9999,
                    divine_value=99,
                    confidence=0.49,
                ),
                PricePoint(
                    league_id=self.league.id,
                    item_key=item_key,
                    name="Awakened Enlighten Support",
                    category="SkillGem",
                    source="poe.ninja",
                    observed_at="2026-07-25T21:00:00Z",
                    chaos_value=5600,
                    divine_value=35,
                    confidence=0.7,
                ),
            ]
        )
        daily = self.storage.daily_item_history(
            self.league.id,
            item_key,
            self.league.start_at,
        )
        self.assertEqual(
            [(row["league_day"], row["divine_value"]) for row in daily],
            [(1, 32.0), (2, 35.0)],
        )
        unfiltered_daily = self.storage.daily_item_history(
            self.league.id,
            item_key,
            self.league.start_at,
            minimum_confidence=0.0,
        )
        self.assertEqual(
            [
                (
                    row["league_day"],
                    row["divine_value"],
                    row["confidence"],
                )
                for row in unfiltered_daily
            ],
            [(1, 99.0, 0.49), (2, 35.0, 0.7)],
        )

        self.storage.upsert_league(
            League(
                id="Keepers",
                name="Keepers of the Flame",
                start_at="2025-10-31T19:00:00Z",
            )
        )
        self.storage.upsert_league(
            League(
                id="Mirage",
                name="Mirage",
                start_at="2026-03-06T19:00:00Z",
            )
        )
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.watch",
                    "source_item_id": "awakened-enlighten",
                    "item_key": item_key,
                    "name": "Awakened Enlighten Support",
                    "category": "SkillGem",
                },
                {
                    "source": "alternate",
                    "source_item_id": "awakened-enlighten-alt",
                    "item_key": item_key,
                    "name": "Awakened Enlighten Support",
                    "category": "SkillGem",
                },
            ]
        )
        self.storage.upsert_seasonal_prices(
            [
                {
                    "league_id": "Keepers",
                    "item_key": item_key,
                    "source": "poe.watch",
                    "source_item_id": "awakened-enlighten",
                    "league_day": 1,
                    "observed_at": "2025-10-31T20:00:00Z",
                    "divine_value": 20,
                    "confidence": 0.8,
                },
                {
                    "league_id": "Mirage",
                    "item_key": item_key,
                    "source": "poe.watch",
                    "source_item_id": "awakened-enlighten",
                    "league_day": 1,
                    "observed_at": "2026-03-06T20:00:00Z",
                    "divine_value": 40,
                    "confidence": 0.8,
                },
                {
                    "league_id": "Mirage",
                    "item_key": item_key,
                    "source": "alternate",
                    "source_item_id": "awakened-enlighten-alt",
                    "league_day": 1,
                    "observed_at": "2026-03-06T21:00:00Z",
                    "divine_value": 41,
                    "confidence": 0.9,
                },
                {
                    "league_id": "Mirage",
                    "item_key": item_key,
                    "source": "poe.watch",
                    "source_item_id": "awakened-enlighten",
                    "league_day": 2,
                    "observed_at": "2026-03-07T20:00:00Z",
                    "divine_value": 45,
                    "confidence": 0.49,
                },
            ]
        )
        seasonal = self.storage.seasonal_price_curve_rows(
            item_key,
            ["Keepers", "Mirage"],
        )
        self.assertEqual(len(seasonal), 2)
        self.assertEqual(
            [
                (
                    row["league_id"],
                    row["league_day"],
                    row["divine_value"],
                    row["source"],
                )
                for row in seasonal
            ],
            [
                ("Keepers", 1, 20.0, "poe.watch"),
                ("Mirage", 1, 41.0, "alternate"),
            ],
        )

    def test_v3_schema_migrates_an_existing_database_idempotently(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy-v1.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE legacy_marker(value TEXT NOT NULL);
                INSERT INTO legacy_marker(value) VALUES ('preserved');
                PRAGMA user_version = 1;
                """
            )
        finally:
            connection.close()

        migrated = Storage(legacy_path)
        migrated.initialize()
        connection = migrated.connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            marker = connection.execute(
                "SELECT value FROM legacy_marker"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(version, 3)
        self.assertEqual(marker, "preserved")
        self.assertTrue(
            {
                "historical_assets",
                "historical_fetch_state",
                "seasonal_prices",
                "meta_class_snapshots",
            }.issubset(tables)
        )

    def test_meta_class_snapshots_round_trip_and_nearest_day_lookup(self) -> None:
        first = self.storage.save_meta_class_snapshot(
            league_id=self.league.id,
            observed_at="2026-07-29T12:00:00Z",
            source="ggg-public-ladder",
            league_day=5,
            class_counts={"Elementalist": 60, "Necromancer": 40},
            sample_size=100,
            page_count=5,
            snapshot_ids=[4, 4, 7],
        )
        self.storage.save_meta_class_snapshot(
            league_id=self.league.id,
            observed_at="2026-08-20T12:00:00Z",
            source="ggg-public-ladder",
            league_day=27,
            class_counts={"Elementalist": 25, "Necromancer": 75},
            sample_size=100,
            page_count=5,
        )
        indexed = self.storage.save_meta_class_snapshot(
            league_id=self.league.id,
            observed_at="2026-07-30T12:00:00Z",
            source="poe.ninja-builds",
            league_day=6,
            class_counts={},
            class_shares={"Elementalist": 0.27, "Necromancer": 0.071},
            sample_size=124_381,
            page_count=1,
        )

        self.assertEqual(first["league_day"], 5)
        self.assertEqual(first["snapshot_ids"], [4, 7])
        self.assertAlmostEqual(first["class_shares"]["Elementalist"], 0.6)
        self.assertEqual(indexed["class_counts"], {})
        self.assertAlmostEqual(indexed["class_shares"]["Elementalist"], 0.27)
        exact = self.storage.get_meta_class_snapshot(
            self.league.id,
            "2026-07-29T12:00:00Z",
        )
        self.assertEqual(exact["class_counts"]["Necromancer"], 40)
        self.assertEqual(
            self.storage.latest_meta_class_snapshot(self.league.id)[
                "league_day"
            ],
            27,
        )
        self.assertEqual(
            self.storage.nearest_meta_class_snapshot(self.league.id, 6)[
                "league_day"
            ],
            5,
        )
        self.assertEqual(
            len(
                self.storage.list_meta_class_snapshots(
                    league_ids=[self.league.id]
                )
            ),
            2,
        )

    def test_historical_asset_catalog_and_fetch_state(self) -> None:
        assets = [
            {
                "source": "poe.watch",
                "source_item_id": "veiled",
                "item_key": "currency:veiled-orb",
                "name": "Veiled Orb",
                "category": "Currency",
                "source_category": "currency",
                "source_group": "stackable",
                "variant": {"kind": "base"},
                "current_daily": 80,
                "current_chaos": 210,
                "current_divine": 1.5,
                "eligible": True,
                "seen_at": "2026-07-30T01:00:00Z",
            },
            {
                "source": "poe.watch",
                "source_item_id": "doctor",
                "item_key": "divinationcard:the-doctor",
                "name": "The Doctor",
                "category": "DivinationCard",
                "source_category": "card",
                "current_daily": 120,
                "current_chaos": 1000,
                "current_divine": 7.0,
                "eligible": True,
                "seen_at": "2026-07-30T01:00:00Z",
            },
            {
                "source": "poe.watch",
                "source_item_id": "thin-market",
                "item_key": "currency:thin-market",
                "name": "Thin Market",
                "category": "Currency",
                "source_category": "currency",
                "current_daily": 999,
                "low_confidence": True,
                "eligible": False,
                "seen_at": "2026-07-30T01:00:00Z",
            },
        ]
        self.assertEqual(self.storage.upsert_historical_assets(assets), 3)
        eligible = self.storage.list_historical_assets()
        self.assertEqual(
            [asset["source_item_id"] for asset in eligible],
            ["doctor", "veiled"],
        )
        self.assertEqual(
            json.loads(eligible[1]["variant_json"]),
            {"kind": "base"},
        )
        self.assertEqual(eligible[0]["eligible"], 1)

        all_assets = self.storage.list_historical_assets(eligible_only=False)
        self.assertEqual(all_assets[0]["source_item_id"], "thin-market")

        refreshed = dict(assets[0])
        refreshed["current_daily"] = 140
        refreshed["current_divine"] = 1.7
        self.assertEqual(self.storage.upsert_historical_assets([refreshed]), 1)
        self.assertEqual(
            self.storage.list_historical_assets()[0]["source_item_id"],
            "veiled",
        )

        self.storage.set_history_fetch_state(
            source="poe.watch",
            league_id=self.league.id,
            source_item_id="veiled",
            status="failed",
            last_error="fixture failure",
        )
        self.assertFalse(
            self.storage.history_fetch_succeeded(
                "poe.watch", self.league.id, "veiled"
            )
        )
        self.storage.set_history_fetch_state(
            source="poe.watch",
            league_id=self.league.id,
            source_item_id="veiled",
            status="success",
            points_written=14,
        )
        self.assertTrue(
            self.storage.history_fetch_succeeded(
                "poe.watch", self.league.id, "veiled"
            )
        )
        self.storage.set_history_fetch_state(
            source="poe.watch",
            league_id=self.league.id,
            source_item_id="doctor",
            status="partial",
            points_written=10,
            last_error="Some league days use a validated Divine fallback.",
        )
        self.assertFalse(
            self.storage.history_fetch_succeeded(
                "poe.watch", self.league.id, "doctor"
            )
        )

        counts = self.storage.seasonal_status_counts()
        self.assertEqual(counts["catalog_assets"], 3)
        self.assertEqual(counts["eligible_assets"], 2)
        self.assertEqual(counts["completed_fetches"], 1)
        self.assertEqual(counts["usable_fetches"], 2)
        status_counts = self.storage.status_counts(self.league.id)
        self.assertEqual(status_counts["completed_fetches"], 1)
        self.assertEqual(status_counts["usable_fetches"], 2)

    def test_seasonal_prices_upsert_and_exact_horizon_returns(self) -> None:
        older_league = League(
            id="Older Softcore",
            name="Older Softcore",
            start_at="2026-03-01T20:00:00Z",
        )
        self.storage.upsert_league(older_league, current=False)
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.watch",
                    "source_item_id": "veiled",
                    "item_key": "currency:veiled-orb",
                    "name": "Veiled Orb",
                    "category": "Currency",
                    "source_category": "currency",
                    "current_daily": 100,
                    "eligible": True,
                }
            ]
        )

        def seasonal_row(
            league_id: str,
            league_day: int,
            divine_value: float,
            *,
            confidence: float = 0.8,
        ) -> dict[str, object]:
            return {
                "league_id": league_id,
                "item_key": "currency:veiled-orb",
                "source": "poe.watch",
                "source_item_id": "veiled",
                "league_day": league_day,
                "observed_at": f"2026-07-{league_day:02d}T20:00:00Z",
                "chaos_value": divine_value * 150,
                "divine_value": divine_value,
                "volume": 1000,
                "confidence": confidence,
                "details": {"fixture": True},
            }

        rows = [
            seasonal_row(self.league.id, 2, 1.0),
            seasonal_row(self.league.id, 8, 1.2),
            seasonal_row(self.league.id, 9, 1.5),
            seasonal_row(older_league.id, 2, 2.0),
            seasonal_row(older_league.id, 9, 1.5),
        ]
        self.assertEqual(self.storage.upsert_seasonal_prices(rows), 5)

        replacement = seasonal_row(
            self.league.id,
            2,
            1.1,
            confidence=3.0,
        )
        self.assertEqual(self.storage.upsert_seasonal_prices([replacement]), 1)

        seven_day = self.storage.seasonal_return_rows(
            2,
            7,
            item_keys=["currency:veiled-orb"],
        )
        self.assertEqual(len(seven_day), 2)
        by_league = {row["league_id"]: row for row in seven_day}
        self.assertAlmostEqual(
            by_league[self.league.id]["forward_return"],
            (1.5 / 1.1) - 1,
        )
        self.assertEqual(by_league[self.league.id]["entry_confidence"], 1.0)
        self.assertEqual(
            by_league[older_league.id]["league_name"],
            older_league.name,
        )
        self.assertAlmostEqual(
            by_league[older_league.id]["forward_return"],
            -0.25,
        )
        same_day = self.storage.seasonal_entry_rows(
            2,
            item_keys=["currency:veiled-orb"],
        )
        self.assertEqual(len(same_day), 2)
        same_day_by_league = {
            row["league_id"]: row for row in same_day
        }
        self.assertAlmostEqual(
            same_day_by_league[self.league.id]["entry_divine"],
            1.1,
        )
        self.assertEqual(
            same_day_by_league[self.league.id]["entry_confidence"],
            1.0,
        )
        self.assertEqual(
            self.storage.seasonal_entry_rows(2, item_keys=[]),
            [],
        )

        six_day = self.storage.seasonal_return_rows(2, 6)
        self.assertEqual(len(six_day), 1)
        self.assertEqual(six_day[0]["exit_day"], 8)
        self.assertEqual(
            self.storage.seasonal_return_rows(2, 7, item_keys=[]),
            [],
        )

        counts = self.storage.seasonal_status_counts()
        self.assertEqual(counts["seasonal_prices"], 5)
        self.assertEqual(counts["historical_leagues"], 2)
        filtered_status = self.storage.status_counts(self.league.id)
        self.assertEqual(filtered_status["seasonal_prices"], 5)
        self.assertEqual(filtered_status["historical_leagues"], 2)


if __name__ == "__main__":
    unittest.main()
