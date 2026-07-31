from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from poe_advisor.historical import (
    COMPLETED_LEAGUES,
    CROSS_LEAGUE_ANCHOR_CONFIDENCE_CAP,
    HistoricalBackfillService,
    _compact_assets,
    _cross_league_divine_fallbacks,
    _reject_cross_league_divine_outliers,
    _validate_divine_curve,
    league_day,
    parse_daily_history_points,
)
from poe_advisor.models import FetchResult, League, iso_utc, parse_datetime
from poe_advisor.normalization import normalize_poe_ninja
from poe_advisor.storage import Storage


def result(url: str, payload: Any) -> FetchResult:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return FetchResult(
        url=url,
        status=200,
        payload=payload,
        raw=raw,
        etag=None,
        last_modified=None,
        fetched_at="2026-07-30T08:00:00Z",
    )


class FakePoeWatchHistory:
    def __init__(self) -> None:
        self.history_calls: list[tuple[str, str]] = []
        self.compact_calls = 0

    def compact_url(self, league: str, *, all_items: bool = True) -> str:
        return f"fixture://compact/{league}/{all_items}"

    def fetch_compact(
        self, league: str, *, all_items: bool = True
    ) -> FetchResult:
        self.compact_calls += 1
        response = result(
            self.compact_url(league, all_items=all_items),
            {
                "items": [
                    {
                        "id": 56327,
                        "name": "Divine Orb",
                        "category": "currency",
                        "group": "currency",
                        "mean": 100,
                        "divine": 1,
                        "daily": 5000,
                        "lowConfidence": False,
                    },
                    {
                        "id": 135,
                        "name": "Frigid Fossil",
                        "category": "delve",
                        "group": "currency",
                        "mean": 5,
                        "divine": 0.05,
                        "daily": 500,
                        "lowConfidence": False,
                    },
                ]
            },
        )
        response.fetched_at = iso_utc()
        return response

    def fetch_history(
        self, league: str, item_id: int | str
    ) -> FetchResult:
        item_id = str(item_id)
        self.history_calls.append((league, item_id))
        spec = next(value for value in COMPLETED_LEAGUES if value.source_alias == league)
        start = parse_datetime(spec.start_at)
        assert start is not None
        rows = []
        for offset in range(20):
            timestamp = start + timedelta(days=offset, hours=1)
            divine_chaos = 100.0 + offset
            mean = (
                divine_chaos
                if item_id == "56327"
                else divine_chaos * (0.05 + offset * 0.002)
            )
            rows.append(
                {
                    "mean": mean,
                    "date": iso_utc(timestamp),
                    "id": int(item_id),
                    "volume": 1000,
                    "lowConfidence": False,
                }
            )
        return result(f"fixture://history/{league}/{item_id}", rows)


class CorruptDivineHistory(FakePoeWatchHistory):
    def fetch_history(
        self, league: str, item_id: int | str
    ) -> FetchResult:
        response = super().fetch_history(league, item_id)
        if str(item_id) != "56327" or league not in {
            "Settlers",
            "Mercenaries",
        }:
            return response
        payload = [dict(row) for row in response.payload]
        payload[4]["mean"] = 0.47
        payload[5]["mean"] = 0.03
        return result(response.url, payload)


class UnusableLeagueDivineHistory(FakePoeWatchHistory):
    def fetch_history(
        self, league: str, item_id: int | str
    ) -> FetchResult:
        response = super().fetch_history(league, item_id)
        if str(item_id) != "56327" or league != "Affliction":
            return response
        payload = [dict(row) for row in response.payload]
        for row in payload:
            row["mean"] = 0.03
        return result(response.url, payload)


class HistoricalNormalizationTests(unittest.TestCase):
    def test_reviewed_league_calendar_includes_supported_older_leagues(
        self,
    ) -> None:
        by_id = {spec.league_id: spec for spec in COMPLETED_LEAGUES}

        self.assertEqual(
            by_id["Affliction"].start_at,
            "2023-12-08T19:00:00Z",
        )
        self.assertEqual(
            by_id["Affliction"].end_at,
            by_id["Necropolis"].start_at,
        )
        self.assertEqual(
            by_id["Necropolis"].end_at,
            by_id["Settlers"].start_at,
        )
        starts = [
            parse_datetime(spec.start_at) for spec in COMPLETED_LEAGUES
        ]
        self.assertTrue(all(value is not None for value in starts))
        self.assertEqual(starts, sorted(starts))

    def test_league_day_uses_launch_time_not_utc_date(self) -> None:
        start = "2026-07-24T20:00:00Z"
        self.assertEqual(league_day("2026-07-24T20:00:00Z", start), 1)
        self.assertEqual(league_day("2026-07-25T19:59:59Z", start), 1)
        self.assertEqual(league_day("2026-07-25T20:00:00Z", start), 2)

    def test_active_league_open_end_sentinel_keeps_dated_history(self) -> None:
        points = parse_daily_history_points(
            [
                {
                    "mean": 100,
                    "date": "2026-07-24T21:00:00Z",
                    "volume": 20,
                    "lowConfidence": False,
                }
            ],
            "2026-07-24T20:00:00Z",
            "0001-01-01T00:00:00Z",
        )

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["league_day"], 1)
        self.assertEqual(points[0]["mean"], 100.0)

    def test_common_poe_watch_buckets_map_to_poe_ninja_families(self) -> None:
        assets = _compact_assets(
            {
                "items": [
                    {
                        "id": 1,
                        "name": "Timeless Eternal Empire Splinter",
                        "category": "maps",
                        "group": "currency",
                    },
                    {
                        "id": 2,
                        "name": "Tattoo of the Kitava Warrior",
                        "category": "currency",
                        "group": "currency",
                    },
                    {
                        "id": 3,
                        "name": "Writhing Invitation",
                        "category": "maps",
                        "group": "maps",
                    },
                    {
                        "id": 4,
                        "name": "Skittering Delirium Orb",
                        "category": "delirium",
                        "group": "currency",
                    },
                ]
            }
        )
        self.assertEqual(
            [asset["category"] for asset in assets],
            ["Fragment", "Tattoo", "Invitation", "DeliriumOrb"],
        )
        self.assertTrue(all(asset["eligible"] for asset in assets))

    def test_divine_curve_rejects_bad_units_and_jump_neighbors(self) -> None:
        quality = _validate_divine_curve(
            {
                1: 80.0,
                2: 95.0,
                3: 110.0,
                4: 120.0,
                5: 0.03,
                6: 1.94,
                7: 170.0,
                8: 180.0,
                9: 190.0,
                10: 200.0,
                11: 205.0,
                12: 210.0,
            }
        )

        self.assertTrue({4, 5, 6, 7}.issubset(quality.rejected_days))
        self.assertNotIn(5, quality.prices)
        self.assertNotIn(6, quality.prices)
        self.assertIn(8, quality.prices)
        self.assertTrue(any("outside" in issue for issue in quality.issues))
        self.assertTrue(any("adjacent jump" in issue for issue in quality.issues))

    def test_cross_league_fallback_is_exact_day_median_and_not_chained(
        self,
    ) -> None:
        curves = {
            "Settlers": {day: 100.0 + day for day in range(1, 10) if day != 6},
            "Mercenaries": {
                day: 120.0 + day for day in range(1, 10) if day != 6
            },
            "Keepers": {day: 140.0 + day for day in range(1, 10)},
            "Mirage": {day: 160.0 + day for day in range(1, 10)},
        }

        completed, details, counts = _cross_league_divine_fallbacks(curves)

        self.assertEqual(completed["Settlers"][6], 156.0)
        self.assertEqual(completed["Mercenaries"][6], 156.0)
        self.assertEqual(
            details["Settlers"][6]["kind"],
            "cross_league_day_median",
        )
        # Settlers cannot become a donor after its own fallback was created.
        self.assertEqual(
            details["Mercenaries"][6]["donor_leagues"],
            ["Keepers", "Mirage"],
        )
        self.assertEqual(counts["Settlers"], 1)
        self.assertEqual(counts["Mercenaries"], 1)

    def test_sparse_direct_anchor_is_rejected_only_by_agreeing_peers(
        self,
    ) -> None:
        curves = {
            "Affliction": {6: 9.55, 7: 155.0},
            "Keepers": {6: 150.0, 7: 160.0},
            "Mirage": {6: 184.24, 7: 300.0},
        }

        filtered, rejected = _reject_cross_league_divine_outliers(curves)

        self.assertNotIn(6, filtered["Affliction"])
        self.assertEqual(filtered["Keepers"][6], 150.0)
        self.assertEqual(filtered["Mirage"][6], 184.24)
        detail = rejected["Affliction"][6]
        self.assertEqual(detail["donor_leagues"], ["Keepers", "Mirage"])
        self.assertAlmostEqual(detail["donor_median"], 167.12)
        # Day 7 donors disagree too widely, so no league is guessed wrong.
        self.assertFalse(any(7 in days for days in rejected.values()))

    def test_forbidden_jewel_identity_matches_poe_ninja(self) -> None:
        assets = _compact_assets(
            {
                "items": [
                    {
                        "id": 9001,
                        "name": "Forbidden Flesh (Mastermind of Discord)",
                        "category": "jewels",
                        "group": "jewels",
                    },
                    {
                        "id": 9002,
                        "name": "Forbidden Flame (Mastermind of Discord)",
                        "category": "jewels",
                        "group": "jewels",
                    },
                ]
            }
        )
        ninja_points = normalize_poe_ninja(
            {
                "lines": [
                    {
                        "name": "Mastermind of Discord",
                        "detailsId": "forbidden-flesh-mastermind-of-discord",
                        "variant": "Forbidden Flesh",
                        "divineValue": 2.5,
                        "metadata": {
                            "baseClass": "Witch",
                            "ascendancy": "Elementalist",
                            "passiveName": "Mastermind of Discord",
                        },
                    },
                    {
                        "name": "Mastermind of Discord",
                        "detailsId": "forbidden-flame-mastermind-of-discord",
                        "variant": "Forbidden Flame",
                        "divineValue": 3.0,
                    },
                ]
            },
            league_id="Fixture League",
            category="ForbiddenJewel",
            observed_at="2026-07-29T12:00:00Z",
            snapshot_id=9,
        )

        self.assertEqual(
            [asset["item_key"] for asset in assets],
            [point.item_key for point in ninja_points],
        )
        self.assertEqual(
            [asset["name"] for asset in assets],
            ["Mastermind of Discord", "Mastermind of Discord"],
        )
        self.assertEqual(
            [asset["category"] for asset in assets],
            ["ForbiddenJewel", "ForbiddenJewel"],
        )
        self.assertTrue(all(asset["eligible"] for asset in assets))
        self.assertNotEqual(assets[0]["item_key"], assets[1]["item_key"])
        self.assertEqual(
            assets[0]["variant"]["variant"],
            "Forbidden Flesh",
        )
        self.assertEqual(
            ninja_points[0].details["metadata"]["ascendancy"],
            "Elementalist",
        )

    def test_skill_gem_identity_matches_exact_poe_ninja_variant(self) -> None:
        assets = _compact_assets(
            {
                "items": [
                    {
                        "id": 46480,
                        "name": "Awakened Enlighten Support",
                        "category": "gem",
                        "group": "supportgem",
                        "gemLevel": 1,
                        "gemQuality": 0,
                        "gemIsCorrupted": False,
                    },
                    {
                        "id": 46798,
                        "name": "Awakened Enlighten Support",
                        "category": "gem",
                        "group": "supportgem",
                        "gemLevel": 4,
                        "gemQuality": 0,
                        "gemIsCorrupted": True,
                    },
                ]
            }
        )
        ninja_points = normalize_poe_ninja(
            {
                "lines": [
                    {
                        "name": "Awakened Enlighten Support",
                        "detailsId": "awakened-enlighten-support-1",
                        "variant": "1",
                        "gemLevel": 1,
                        "divineValue": 37.0,
                    },
                    {
                        "name": "Awakened Enlighten Support",
                        "detailsId": "awakened-enlighten-support-4c",
                        "variant": "4c",
                        "corrupted": True,
                        "gemLevel": 4,
                        "divineValue": 14.0,
                    },
                ]
            },
            league_id="Fixture League",
            category="SkillGem",
            observed_at="2026-07-29T12:00:00Z",
            snapshot_id=9,
        )

        self.assertEqual(
            [asset["item_key"] for asset in assets],
            [point.item_key for point in ninja_points],
        )
        self.assertTrue(all(asset["eligible"] for asset in assets))
        self.assertEqual(
            [asset["category"] for asset in assets],
            ["SkillGem", "SkillGem"],
        )
        self.assertFalse(assets[0]["variant"]["gem_is_corrupted"])
        self.assertTrue(assets[1]["variant"]["gem_is_corrupted"])

    def test_skill_gem_current_match_never_falls_back_to_name_only(self) -> None:
        assets = _compact_assets(
            {
                "items": [
                    {
                        "id": 46480,
                        "name": "Awakened Enlighten Support",
                        "category": "gem",
                        "group": "supportgem",
                        "gemLevel": 1,
                        "gemQuality": 0,
                        "gemIsCorrupted": False,
                    },
                    {
                        "id": 46798,
                        "name": "Awakened Enlighten Support",
                        "category": "gem",
                        "group": "supportgem",
                        "gemLevel": 4,
                        "gemQuality": 0,
                        "gemIsCorrupted": True,
                    },
                ]
            }
        )
        current = [
            {
                "item_key": assets[0]["item_key"],
                "name": "Awakened Enlighten Support",
                "category": "SkillGem",
            }
        ]

        HistoricalBackfillService._match_current_items(assets, current)

        self.assertTrue(assets[0]["current_match"])
        self.assertFalse(assets[1]["current_match"])
        self.assertNotEqual(assets[0]["item_key"], assets[1]["item_key"])


class HistoricalBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.directory.name) / "history.sqlite3")
        self.current = League(
            id="Allflame",
            name="Allflame",
            start_at="2026-07-24T20:00:00Z",
        )
        self.storage.upsert_league(self.current, current=True)
        self.client = FakePoeWatchHistory()
        self.service = HistoricalBackfillService(
            self.storage,
            client=self.client,
            sleeper=lambda _: None,
            request_pause_seconds=0,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_all_reviewed_leagues_are_normalized_and_resumable(self) -> None:
        first = self.service.backfill(self.current, max_items=1)

        self.assertEqual(first["status"], "success")
        self.assertEqual(first["selected_items"], 1)
        expected_fetches = len(COMPLETED_LEAGUES) * 2
        self.assertEqual(first["histories_fetched"], expected_fetches)
        counts = self.storage.seasonal_status_counts()
        self.assertEqual(
            counts["historical_leagues"],
            len(COMPLETED_LEAGUES),
        )
        self.assertEqual(counts["completed_fetches"], expected_fetches)
        returns = self.storage.seasonal_return_rows(
            3,
            7,
            item_keys=["fossil:frigid-fossil"],
        )
        self.assertEqual(len(returns), len(COMPLETED_LEAGUES))
        self.assertTrue(
            all(row["exit_divine"] > row["entry_divine"] for row in returns)
        )

        calls_after_first = len(self.client.history_calls)
        second = self.service.backfill(self.current, max_items=1)
        self.assertEqual(second["status"], "success")
        self.assertEqual(second["selected_items"], 0)
        self.assertEqual(len(self.client.history_calls), calls_after_first)
        self.assertEqual(self.client.compact_calls, 1)
        self.assertTrue(second["catalog_cache_reused"])

    def test_candidate_selection_advances_before_retrying_failures(
        self,
    ) -> None:
        assets = [
            {
                "source": "poe.watch",
                "source_item_id": "retry",
                "item_key": "test:retry",
                "name": "Popular failed item",
                "category": "ForbiddenJewel",
                "eligible": True,
                "current_match": True,
                "daily_volume": 10_000,
                "current_chaos": 100,
            },
            {
                "source": "poe.watch",
                "source_item_id": "complete",
                "item_key": "test:complete",
                "name": "Already archived item",
                "category": "Currency",
                "eligible": True,
                "current_match": True,
                "daily_volume": 5_000,
                "current_chaos": 50,
            },
            {
                "source": "poe.watch",
                "source_item_id": "fresh",
                "item_key": "test:fresh",
                "name": "Never attempted item",
                "category": "Currency",
                "eligible": True,
                "current_match": True,
                "daily_volume": 1,
                "current_chaos": 1,
            },
        ]
        self.storage.upsert_historical_assets(assets)
        for index, spec in enumerate(COMPLETED_LEAGUES):
            self.storage.upsert_league(spec.as_league(), current=False)
            self.storage.set_history_fetch_state(
                source="poe.watch",
                league_id=spec.league_id,
                source_item_id="retry",
                status="failed" if index % 2 == 0 else "missing",
            )
            self.storage.set_history_fetch_state(
                source="poe.watch",
                league_id=spec.league_id,
                source_item_id="complete",
                status="success" if index % 2 == 0 else "partial",
            )

        selected, complete = self.service._select_candidates(
            assets,
            max_items=1,
        )

        self.assertEqual(
            [asset["source_item_id"] for asset in selected],
            ["fresh"],
        )
        self.assertEqual(complete, 1)

    def test_unusable_direct_curve_is_not_requarantined_each_pass(
        self,
    ) -> None:
        client = UnusableLeagueDivineHistory()
        service = HistoricalBackfillService(
            self.storage,
            client=client,
            sleeper=lambda _: None,
            request_pause_seconds=0,
        )

        first = service.backfill(self.current, max_items=1)
        connection = self.storage.connect()
        try:
            first_affliction_rows = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM seasonal_prices
                WHERE league_id = 'Affliction'
                  AND source_item_id = '135'
                """
            ).fetchone()["count"]
        finally:
            connection.close()
        affliction_anchor_calls = sum(
            league == "Affliction" and item_id == "56327"
            for league, item_id in client.history_calls
        )

        second = service.backfill(self.current, max_items=1)
        connection = self.storage.connect()
        try:
            second_affliction_rows = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM seasonal_prices
                WHERE league_id = 'Affliction'
                  AND source_item_id = '135'
                """
            ).fetchone()["count"]
            anchor_state = connection.execute(
                """
                SELECT status
                FROM historical_fetch_state
                WHERE league_id = 'Affliction'
                  AND source_item_id = '56327'
                """
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(first["status"], "partial")
        self.assertGreater(first_affliction_rows, 0)
        self.assertEqual(second_affliction_rows, first_affliction_rows)
        self.assertEqual(anchor_state["status"], "partial")
        self.assertEqual(
            sum(
                league == "Affliction" and item_id == "56327"
                for league, item_id in client.history_calls
            ),
            affliction_anchor_calls,
        )
        self.assertEqual(second["seasonal_rows_quarantined"], 0)

    def test_bad_existing_anchor_days_are_repaired_with_auditable_fallback(
        self,
    ) -> None:
        for spec in COMPLETED_LEAGUES:
            self.storage.upsert_league(spec.as_league(), current=False)
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.watch",
                    "source_item_id": "56327",
                    "item_key": "currency:divine-orb",
                    "name": "Divine Orb",
                    "category": "Currency",
                    "source_category": "currency",
                    "eligible": True,
                },
                {
                    "source": "poe.watch",
                    "source_item_id": "135",
                    "item_key": "fossil:frigid-fossil",
                    "name": "Frigid Fossil",
                    "category": "Fossil",
                    "source_category": "delve",
                    "eligible": True,
                },
            ]
        )
        legacy_snapshot_id, _ = self.storage.add_snapshot(
            source="poe.watch",
            endpoint="fixture://legacy-corrupt-divine",
            league_id="Settlers",
            category="Currency",
            fetched_at="2026-07-30T08:00:00Z",
            status_code=200,
            raw=b'{"preserve":"raw provider evidence"}',
        )
        settlers = next(
            spec for spec in COMPLETED_LEAGUES if spec.league_id == "Settlers"
        )
        start = parse_datetime(settlers.start_at)
        assert start is not None
        anchor_rows = []
        for day in range(1, 13):
            price = 100.0 + day
            if day == 5:
                price = 0.47
            if day == 6:
                price = 0.03
            anchor_rows.append(
                {
                    "league_id": "Settlers",
                    "item_key": "currency:divine-orb",
                    "source": "poe.watch",
                    "source_item_id": "56327",
                    "league_day": day,
                    "observed_at": iso_utc(
                        start + timedelta(days=day - 1, hours=1)
                    ),
                    "chaos_value": price,
                    "divine_value": 1.0,
                    "confidence": 0.8,
                    "snapshot_id": legacy_snapshot_id,
                }
            )
        self.storage.upsert_seasonal_prices(
            [
                *anchor_rows,
                {
                    "league_id": "Settlers",
                    "item_key": "fossil:frigid-fossil",
                    "source": "poe.watch",
                    "source_item_id": "135",
                    "league_day": 6,
                    "observed_at": iso_utc(start + timedelta(days=5, hours=1)),
                    "chaos_value": 6.3,
                    "divine_value": 210.0,
                    "confidence": 0.8,
                    "snapshot_id": legacy_snapshot_id,
                    "details": {"legacy_bad_divine_chaos": 0.03},
                },
            ]
        )
        for source_item_id in ("56327", "135"):
            self.storage.set_history_fetch_state(
                source="poe.watch",
                league_id="Settlers",
                source_item_id=source_item_id,
                status="success",
                points_written=12,
            )

        corrupt_client = CorruptDivineHistory()
        service = HistoricalBackfillService(
            self.storage,
            client=corrupt_client,
            sleeper=lambda _: None,
            request_pause_seconds=0,
        )
        summary = service.backfill(self.current, max_items=1)

        self.assertEqual(summary["status"], "partial")
        self.assertGreater(summary["seasonal_rows_quarantined"], 0)
        self.assertIn("Settlers", summary["normalization_partial_leagues"])
        self.assertGreater(
            summary["normalization_fallback_days"]["Settlers"], 0
        )
        connection = self.storage.connect()
        try:
            repaired = connection.execute(
                """
                SELECT divine_value, confidence, details_json
                FROM seasonal_prices
                WHERE league_id = 'Settlers'
                  AND item_key = 'fossil:frigid-fossil'
                  AND league_day = 6
                """
            ).fetchone()
            state = connection.execute(
                """
                SELECT status
                FROM historical_fetch_state
                WHERE league_id = 'Settlers' AND source_item_id = '135'
                """
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(repaired)
        self.assertAlmostEqual(repaired["divine_value"], 0.06)
        self.assertLessEqual(
            repaired["confidence"], CROSS_LEAGUE_ANCHOR_CONFIDENCE_CAP
        )
        details = json.loads(repaired["details_json"])
        self.assertEqual(
            details["divine_anchor_kind"],
            "cross_league_day_median",
        )
        self.assertGreaterEqual(len(details["divine_anchor_donors"]), 2)
        self.assertEqual(state["status"], "partial")
        self.assertEqual(
            self.storage.read_snapshot(legacy_snapshot_id),
            b'{"preserve":"raw provider evidence"}',
        )


if __name__ == "__main__":
    unittest.main()
