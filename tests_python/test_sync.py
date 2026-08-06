from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from poe_advisor.clients import PoeNinjaClient
from poe_advisor.models import (
    DataSourceError,
    FetchResult,
    League,
    PricePoint,
    iso_utc,
)
from poe_advisor.storage import Storage
from poe_advisor.sync import (
    DEFAULT_EXCHANGE_CATEGORIES,
    DEFAULT_ITEM_CATEGORIES,
    SyncService,
    _poe_ninja_exchange_history_points,
    _poe_ninja_exchange_item_history_points,
    _poe_ninja_stash_history_points,
)


def fetch_result(url: str, payload: Any) -> FetchResult:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return FetchResult(
        url=url,
        status=200,
        payload=payload,
        raw=raw,
        etag='"fixture"',
        last_modified=None,
        fetched_at="2026-07-30T06:00:00Z",
    )


class FakePoeNinja:
    SOURCE = "poe.ninja"

    def __init__(self, *, fail_prices: bool = False):
        self.fail_prices = fail_prices
        self.history_calls: list[tuple[str, str, str, str]] = []

    def league_url(self) -> str:
        return "fixture://poe-ninja/leagues"

    def list_leagues(self, **_: Any) -> FetchResult:
        return fetch_result(self.league_url(), [{"name": "Fixture Softcore"}])

    def exchange_url(self, league: str, category: str) -> str:
        return f"fixture://poe-ninja/{league}/exchange/{category}"

    def stash_item_url(self, league: str, category: str) -> str:
        return f"fixture://poe-ninja/{league}/item/{category}"

    def fetch_exchange(self, league: str, category: str, **_: Any) -> FetchResult:
        if self.fail_prices:
            raise DataSourceError("fixture price outage")
        return fetch_result(
            self.exchange_url(league, category),
            {
                "lines": [
                    {
                        "id": "orb-of-alchemy",
                        "name": "Orb of Alchemy",
                        "detailsId": "orb-of-alchemy",
                        "divineValue": 0.01,
                        "chaosValue": 2.0,
                        "volumePrimaryValue": 500,
                    }
                ]
            },
        )

    def fetch_stash_item(
        self, league: str, category: str, **_: Any
    ) -> FetchResult:
        if category == "SkillGem":
            return fetch_result(
                self.stash_item_url(league, category),
                {
                    "lines": [
                        {
                            "id": 95714,
                            "name": "Awakened Enlighten Support",
                            "detailsId": "awakened-enlighten-support-1",
                            "variant": "1",
                            "gemLevel": 1,
                            "gemQuality": 0,
                            "corrupted": False,
                            "divineValue": 37.0,
                            "chaosValue": 6660.0,
                            "listingCount": 15,
                            "category": "SkillGem",
                        }
                    ]
                },
            )
        return self.fetch_exchange(league, category)

    def exchange_details_url(
        self, league: str, category: str, item_id: int | str
    ) -> str:
        return f"fixture://poe-ninja/{league}/details/{category}/{item_id}"

    def stash_item_history_url(
        self, league: str, category: str, item_id: int | str
    ) -> str:
        return f"fixture://poe-ninja/{league}/history/{category}/{item_id}"

    def fetch_exchange_details(
        self, league: str, category: str, item_id: int | str, **_: Any
    ) -> FetchResult:
        self.history_calls.append(("exchange", league, category, str(item_id)))
        rates = [100.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0]
        history = [
            {
                "timestamp": f"2026-07-{25 + index:02d}T00:00:00Z",
                "rate": rate,
                "volumePrimaryValue": 1000 + index,
            }
            for index, rate in enumerate(rates)
        ]
        payload = {
            "item": {
                "id": "divine",
                "name": "Divine Orb",
                "detailsId": "divine-orb",
            },
            "pairs": [{"id": "chaos", "history": history}],
        }
        response = fetch_result(
            self.exchange_details_url(league, category, item_id),
            payload,
        )
        response.fetched_at = "2026-07-31T06:00:00Z"
        return response

    def fetch_stash_item_history(
        self, league: str, category: str, item_id: int | str, **_: Any
    ) -> FetchResult:
        self.history_calls.append(("stash-item", league, category, str(item_id)))
        payload = [
            {"count": 20 + index, "value": 3000 + index * 600, "daysAgo": 6 - index}
            for index in range(7)
        ]
        response = fetch_result(
            self.stash_item_history_url(league, category, item_id),
            payload,
        )
        response.fetched_at = "2026-07-31T06:00:00Z"
        return response


class InvalidDivineHistoryPoeNinja(FakePoeNinja):
    def fetch_exchange_details(
        self, league: str, category: str, item_id: int | str, **kwargs: Any
    ) -> FetchResult:
        response = super().fetch_exchange_details(
            league, category, item_id, **kwargs
        )
        if str(item_id) != "divine-orb":
            return response
        for point in response.payload["pairs"][0]["history"]:
            point["rate"] = 0.03
        response.raw = json.dumps(response.payload).encode("utf-8")
        return response


class FakePoeWatch:
    SOURCE = "poe.watch"

    def __init__(self, *, fail_compact: bool = False):
        self.fail_compact = fail_compact
        self.history_calls: list[tuple[str, str]] = []

    def leagues_url(self) -> str:
        return "fixture://poe-watch/leagues"

    def list_leagues(self, **_: Any) -> FetchResult:
        return fetch_result(
            self.leagues_url(),
            [
                {
                    "name": "Fixture Softcore",
                    "start_date": "2026-07-24T20:00:00Z",
                }
            ],
        )

    def compact_url(self, league: str, *, all_items: bool = True) -> str:
        return f"fixture://poe-watch/{league}/compact/{all_items}"

    def fetch_compact(
        self, league: str, *, all_items: bool = True, **_: Any
    ) -> FetchResult:
        if self.fail_compact:
            raise DataSourceError("fixture compact outage")
        return fetch_result(
            self.compact_url(league, all_items=all_items),
            {
                "items": [
                    {
                        "id": 56327,
                        "name": "Divine Orb",
                        "category": "currency",
                        "group": "currency",
                        "mean": 200,
                        "divine": 1,
                        "daily": 5000,
                    },
                    {
                        "id": 9001,
                        "name": "Forbidden Flesh (Shaper of Storms)",
                        "category": "jewels",
                        "group": "jewels",
                        "mean": 300,
                        "divine": 1.5,
                        "daily": 20,
                    },
                    {
                        "id": 9002,
                        "name": "Forbidden Flame (Shaper of Storms)",
                        "category": "jewels",
                        "group": "jewels",
                        "mean": 400,
                        "divine": 2,
                        "daily": 15,
                    },
                    {
                        "id": 46480,
                        "name": "Awakened Enlighten Support",
                        "category": "gem",
                        "group": "supportgem",
                        "mean": 6660,
                        "divine": 37,
                        "daily": 15,
                        "gemLevel": 1,
                        "gemQuality": 0,
                        "gemIsCorrupted": False,
                    },
                ]
            },
        )

    def history_url(self, league: str, item_id: int | str) -> str:
        return f"fixture://poe-watch/{league}/history/{item_id}"

    def fetch_history(
        self,
        league: str,
        item_id: int | str,
        **_: Any,
    ) -> FetchResult:
        source_item_id = str(item_id)
        self.history_calls.append((league, source_item_id))
        if source_item_id == "56327":
            rows = [
                {
                    "mean": 100.0,
                    "date": "2026-07-24T21:00:00Z",
                    "id": 56327,
                    "volume": 500,
                    "lowConfidence": False,
                },
                {
                    "mean": 110.0,
                    "date": "2026-07-25T21:00:00Z",
                    "id": 56327,
                    "volume": 500,
                    "lowConfidence": False,
                },
                {
                    "mean": 120.0,
                    "date": "2026-07-26T21:00:00Z",
                    "id": 56327,
                    "volume": 500,
                    "lowConfidence": False,
                },
            ]
        elif source_item_id == "46480":
            rows = [
                {
                    "mean": 3300.0,
                    "date": "2026-07-25T22:00:00Z",
                    "id": 46480,
                    "volume": 20,
                    "lowConfidence": False,
                },
                {
                    "mean": 3600.0,
                    "date": "2026-07-26T22:00:00Z",
                    "id": 46480,
                    "volume": 30,
                    "lowConfidence": False,
                },
            ]
        else:
            rows = []
        response = fetch_result(self.history_url(league, item_id), rows)
        response.fetched_at = iso_utc()
        return response


class InvalidDivineHistoryPoeWatch(FakePoeWatch):
    def fetch_history(
        self,
        league: str,
        item_id: int | str,
        **kwargs: Any,
    ) -> FetchResult:
        response = super().fetch_history(league, item_id, **kwargs)
        if str(item_id) != "56327":
            return response
        invalid = [dict(row, mean=0.03) for row in response.payload]
        replacement = fetch_result(response.url, invalid)
        replacement.fetched_at = response.fetched_at
        return replacement


class FakeSkillTree:
    SOURCE = "ggg-skilltree-export"

    def export_url(self) -> str:
        return "fixture://ggg/skilltree-export"

    def fetch_export(self, **_: Any) -> FetchResult:
        return fetch_result(
            self.export_url(),
            {
                "classes": [
                    {
                        "name": "Witch",
                        "ascendancies": [
                            {
                                "id": "Elementalist",
                                "name": "Elementalist",
                            }
                        ],
                    }
                ],
                "nodes": {
                    "42": {
                        "skill": 42,
                        "name": "Shaper of Storms",
                        "isNotable": True,
                        "ascendancyName": "Elementalist",
                    }
                },
            },
        )


class FakeGGG:
    SOURCE = "ggg-currency-exchange"
    leagues_configured = False

    def currency_exchange_url(self, cursor: int | None = None) -> str:
        suffix = f"/{cursor}" if cursor is not None else ""
        return f"fixture://ggg/currency-exchange{suffix}"


class SyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.storage = Storage(
            Path(self.temporary_directory.name) / "sync.sqlite3"
        )
        self.storage.set_setting("exchange_categories", ["Currency"])
        self.storage.set_setting("item_categories", [])

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_exchange_history_uses_launch_windows_and_rejects_conflicts(
        self,
    ) -> None:
        league = League(
            id="Mirage",
            name="Mirage",
            start_at="2026-03-06T19:00:00Z",
        )
        history = [
            {
                "timestamp": "2026-03-06T19:00:00Z",
                "rate": 100.0,
                "volumePrimaryValue": 10,
            },
            {
                "timestamp": "2026-03-07T19:00:00Z",
                "rate": 110.0,
                "volumePrimaryValue": 11,
            },
        ]
        payload = {"pairs": [{"id": "chaos", "history": history}]}
        points = _poe_ninja_exchange_history_points(payload, league)
        self.assertEqual([point["league_day"] for point in points], [1, 2])

        open_ended = League(
            id="Mirage",
            name="Mirage",
            start_at="2026-03-06T19:00:00Z",
            end_at="0001-01-01T00:00:00Z",
        )
        points = _poe_ninja_exchange_history_points(payload, open_ended)
        self.assertEqual([point["league_day"] for point in points], [1, 2])

        zero_volume = {
            "timestamp": "2026-03-06T19:00:00Z",
            "rate": 22.25,
            "volumePrimaryValue": 0,
        }
        points = _poe_ninja_exchange_history_points(
            {"pairs": [{"id": "chaos", "history": [zero_volume, history[1]]}]},
            league,
        )
        self.assertEqual([point["league_day"] for point in points], [1, 2])
        self.assertEqual(points[0]["mean"], 22.25)
        self.assertEqual(points[0]["volume"], 0.0)
        self.assertEqual(points[0]["confidence"], 0.65)
        self.assertEqual(points[0]["quote_currency"], "chaos")

        conflict = {
            "timestamp": "2026-03-06T19:00:00Z",
            "rate": 200.0,
            "volumePrimaryValue": 10,
        }
        for rows in (history + [conflict], [conflict] + history):
            with self.assertRaises(DataSourceError):
                _poe_ninja_exchange_history_points(
                    {"pairs": [{"id": "chaos", "history": rows}]},
                    league,
                )

    def test_exchange_item_history_prefers_direct_divine_then_chaos(self) -> None:
        league = League(
            id="Allflame",
            name="Allflame",
            start_at="2026-07-24T20:00:00Z",
        )
        mirror_payload = {
            "pairs": [
                {"id": "chaos", "history": []},
                {
                    "id": "divine",
                    "history": [
                        {
                            "timestamp": "2026-07-25T00:00:00Z",
                            "rate": 100.0,
                            "volumePrimaryValue": 0,
                        },
                        {
                            "timestamp": "2026-07-26T00:00:00Z",
                            "rate": 139.9,
                            "volumePrimaryValue": 119457,
                        },
                    ],
                },
            ]
        }

        mirror_points = _poe_ninja_exchange_item_history_points(
            mirror_payload,
            league,
        )

        self.assertEqual(
            [point["league_day"] for point in mirror_points],
            [1, 2],
        )
        self.assertEqual(
            [point["mean"] for point in mirror_points],
            [100.0, 139.9],
        )
        self.assertTrue(
            all(point["quote_currency"] == "divine" for point in mirror_points)
        )
        self.assertEqual(mirror_points[0]["confidence"], 0.65)

        divine_chaos_payload = {
            "pairs": [
                {
                    "id": "chaos",
                    "history": [
                        {
                            "timestamp": "2026-07-25T00:00:00Z",
                            "rate": 100.0,
                            "volumePrimaryValue": 500,
                        }
                    ],
                },
                {"id": "divine", "history": []},
            ]
        }
        chaos_points = _poe_ninja_exchange_item_history_points(
            divine_chaos_payload,
            league,
        )
        self.assertEqual([point["mean"] for point in chaos_points], [100.0])
        self.assertEqual(chaos_points[0]["quote_currency"], "chaos")
        self.assertEqual(chaos_points[0]["confidence"], 0.95)

    def test_stash_history_rejects_conflicting_duplicate_day(self) -> None:
        league = League(
            id="Mirage",
            name="Mirage",
            start_at="2026-03-06T19:00:00Z",
        )
        first = {"daysAgo": 1, "value": 20.0, "count": 10}
        conflict = {"daysAgo": 1, "value": 40.0, "count": 10}
        for rows in ([first, conflict], [conflict, first]):
            with self.assertRaises(DataSourceError):
                _poe_ninja_stash_history_points(
                    rows,
                    league,
                    "2026-03-08T06:00:00Z",
                )
        self.assertEqual(
            _poe_ninja_stash_history_points(
                [{"daysAgo": 1, "value": 20.0, "count": 0}],
                league,
                "2026-03-08T06:00:00Z",
            ),
            [],
        )

    def service(
        self,
        *,
        fail_prices: bool = False,
        poe_ninja: FakePoeNinja | None = None,
        poe_watch: FakePoeWatch | None = None,
    ) -> SyncService:
        return SyncService(
            self.storage,
            poe_ninja=(poe_ninja or FakePoeNinja(fail_prices=fail_prices)),
            poe_watch=(
                poe_watch
                or FakePoeWatch(fail_compact=fail_prices)
            ),
            ggg=FakeGGG(),
            skill_tree=FakeSkillTree(),
            allow_demo_seed=False,
        )

    def test_default_categories_cover_documented_poe1_types(self) -> None:
        self.assertEqual(
            DEFAULT_EXCHANGE_CATEGORIES,
            [
                "Currency",
                "Fragment",
                "Runegraft",
                "AllflameEmber",
                "Tattoo",
                "Omen",
                "DjinnCoin",
                "Ducat",
                "EnshroudingCrystal",
                "DivinationCard",
                "Artifact",
                "Oil",
                "DeliriumOrb",
                "Scarab",
                "Astrolabe",
                "Fossil",
                "Resonator",
                "Essence",
            ],
        )
        self.assertEqual(
            DEFAULT_ITEM_CATEGORIES,
            [
                "Wombgift",
                "Incubator",
                "UniqueWeapon",
                "UniqueArmour",
                "UniqueAccessory",
                "UniqueFlask",
                "UniqueJewel",
                "ForbiddenJewel",
                "ShrineBelt",
                "UniqueTincture",
                "UniqueRelic",
                "SkillGem",
                "ImbuedGem",
                "ClusterJewel",
                "Map",
                "BlightedMap",
                "BlightRavagedMap",
                "UniqueMap",
                "ValdoMap",
                "Invitation",
                "Memory",
                "IncursionTemple",
                "BaseType",
                "Beast",
                "Vial",
            ],
        )

    def test_poe_ninja_detail_history_urls_preserve_exact_identity(self) -> None:
        client = PoeNinjaClient(base_url="https://poe.ninja")
        self.assertEqual(
            client.exchange_details_url(
                "Allflame",
                "Currency",
                "chromatic-orb",
            ),
            (
                "https://poe.ninja/poe1/api/economy/exchange/current/"
                "details?league=Allflame&type=Currency&id=chromatic-orb"
            ),
        )
        self.assertEqual(
            client.stash_item_history_url("Allflame", "SkillGem", 95714),
            (
                "https://poe.ninja/poe1/api/economy/stash/current/item/"
                "history?league=Allflame&type=SkillGem&id=95714"
            ),
        )

    def test_fresh_database_sync_persists_league_metadata_before_snapshot(self) -> None:
        result = self.service().sync(backfill_hours=0)

        self.assertTrue(result["ok"])
        self.assertGreater(result["stats"]["poe_ninja_parity_rows"], 0)
        league = self.storage.get_current_league()
        self.assertIsNotNone(league)
        self.assertEqual(league.start_at, "2026-07-24T20:00:00Z")
        self.assertGreater(
            self.storage.status_counts(league.id)["price_points"],
            0,
        )

    def test_one_click_sync_persists_standard_prices_as_optional_anchor(self) -> None:
        result = self.service().sync(backfill_hours=0)

        self.assertTrue(result["ok"])
        self.assertTrue(result["stats"]["standard_anchor_available"])
        self.assertGreater(result["stats"]["standard_rows_written"], 0)
        standard = self.storage.get_league("Standard")
        self.assertIsNotNone(standard)
        anchors = self.storage.latest_item_prices("Standard")
        point = anchors["currency:orb-of-alchemy"]
        self.assertEqual(point["name"], "Orb of Alchemy")
        self.assertEqual(point["divine_value"], 0.01)
        self.assertEqual(point["source"], "poe.ninja")

    def test_one_click_sync_stores_exact_forbidden_variants_with_meta(self) -> None:
        result = self.service().sync(backfill_hours=0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["forbidden_variants"], 2)
        self.assertEqual(result["stats"]["forbidden_variants_mapped"], 2)
        league = self.storage.get_current_league()
        assert league is not None
        histories = self.storage.item_histories(league.id, days=90)
        exact = [
            rows[-1]
            for key, rows in histories.items()
            if key.startswith("forbiddenjewel:")
        ]
        self.assertEqual(
            sorted(row["name"] for row in exact),
            [
                "Forbidden Flame (Shaper of Storms)",
                "Forbidden Flesh (Shaper of Storms)",
            ],
        )
        self.assertEqual(
            {row["details"]["metadata"]["ascendancy"] for row in exact},
            {"Elementalist"},
        )
        self.assertEqual(
            {row["details"]["metadata"]["baseClass"] for row in exact},
            {"Witch"},
        )
        self.assertTrue(
            all(row["details"]["exactVariant"] for row in exact)
        )

    def test_ranked_current_history_uses_exact_days_and_keeps_fresher_local(
        self,
    ) -> None:
        self.storage.set_setting("item_categories", ["SkillGem"])
        service = self.service()
        result = service.sync(backfill_hours=0)
        self.assertTrue(result["ok"])
        league = self.storage.get_current_league()
        assert league is not None
        item_key = (
            "skillgem:awakened-enlighten-support-1-variant-1-"
            "corrupted-false-gemlevel-1-gemquality-0"
        )
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=league.id,
                    item_key=item_key,
                    name="Awakened Enlighten Support",
                    category="SkillGem",
                    source="poe.ninja",
                    observed_at="2026-07-26T12:00:00Z",
                    chaos_value=5500.0,
                    divine_value=50.0,
                    confidence=0.9,
                )
            ]
        )

        # Keep this dated fixture independent of the wall clock. Without the
        # freeze, every UTC rollover after its final July 31 history bucket
        # correctly adds another missing league day and makes the assertion
        # below time-dependent.
        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 7, 31, 6, tzinfo=timezone.utc),
        ):
            summary = service.sync_current_item_histories(
                league,
                [item_key, "unique:missing-ranked-item"],
                max_items=100,
            )

        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["requested_items"], 2)
        self.assertEqual(summary["matched_items"], 1)
        self.assertEqual(summary["unmatched_items"], 1)
        self.assertEqual(
            summary["unmatched"],
            [
                {
                    "item_key": "unique:missing-ranked-item",
                    "reason": "missing_poe_ninja_history_identity",
                }
            ],
        )
        self.assertEqual(summary["fetched_items"], 1)
        self.assertEqual(summary["failed_items"], 0)
        self.assertGreaterEqual(summary["snapshots_written"], 2)
        self.assertGreaterEqual(summary["rows_written"], 2)
        self.assertIn(
            ("exchange", league.id, "Currency", "divine-orb"),
            service.poe_ninja.history_calls,
        )
        self.assertIn(
            ("stash-item", league.id, "SkillGem", "95714"),
            service.poe_ninja.history_calls,
        )
        coverage = summary["coverage"][item_key]
        # The first poe.ninja midnight bucket is four hours after launch and
        # therefore remains in the first 24-hour league window.
        self.assertEqual(coverage["first_observed_day"], 1)
        self.assertEqual(coverage["missing_days"], [])
        self.assertEqual(coverage["normalized_days"], list(range(1, 8)))
        self.assertEqual(coverage["missing_divine_anchor_days"], [])

        daily = self.storage.daily_item_history(
            league.id,
            item_key,
            league.start_at,
            minimum_confidence=0.0,
        )
        by_day = {int(point["league_day"]): point for point in daily}
        self.assertEqual(by_day[1]["divine_value"], 3000.0 / 100.0)
        self.assertEqual(by_day[1]["source"], "poe.ninja")
        # The later local observation on July 26 wins over that date's
        # midnight poe.ninja history bucket.
        self.assertEqual(by_day[2]["divine_value"], 50.0)
        self.assertEqual(by_day[2]["source"], "poe.ninja")
        self.assertEqual(by_day[3]["divine_value"], 4200.0 / 130.0)

        archived = self.storage.current_item_history_archive(
            league.id,
            item_key,
        )
        assert archived is not None
        self.assertTrue(archived["durable"])
        self.assertEqual(archived["provider_first_observed_day"], 1)
        self.assertEqual(archived["normalized_days"], list(range(1, 8)))
        self.assertEqual(archived["interpolation"], "none")
        connection = self.storage.connect()
        try:
            raw_categories = {
                row["category"]
                for row in connection.execute(
                    """
                    SELECT category
                    FROM raw_snapshots
                    WHERE source = 'poe.ninja' AND league_id = ?
                      AND category IN (
                          'current-divine-history',
                          'current-item-history'
                      )
                    """,
                    (league.id,),
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(
            raw_categories,
            {"current-divine-history", "current-item-history"},
        )
        history_state = self.storage.get_source_state(
            "poe.ninja-current-history",
            f"{league.id}:ranked-current-history",
            league.id,
            "ranked-current-history",
        )
        self.assertIsNotNone(history_state)
        self.assertEqual(history_state["status"], "partial")

        calls_after_first = len(service.poe_ninja.history_calls)
        # Public archives remove every raw response. Durable exact coverage
        # must still make the full-universe backfill one-time and avoid even a
        # Divine-history request on the next daily update.
        with self.storage.transaction() as connection:
            connection.execute(
                """
                UPDATE price_points
                SET snapshot_id = NULL
                WHERE snapshot_id IN (
                    SELECT id FROM raw_snapshots
                    WHERE league_id = ?
                      AND category IN (
                          'current-divine-history',
                          'current-item-history'
                      )
                )
                """,
                (league.id,),
            )
            connection.execute(
                """
                DELETE FROM raw_snapshots
                WHERE league_id = ?
                  AND category IN (
                      'current-divine-history',
                      'current-item-history'
                  )
                """,
                (league.id,),
            )
        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 7, 31, 6, tzinfo=timezone.utc),
        ):
            cached = service.sync_current_item_histories(
                league,
                [item_key],
                max_items=100,
            )
        self.assertEqual(cached["status"], "success")
        self.assertEqual(cached["cached_items"], 1)
        self.assertEqual(cached["already_backfilled_items"], 1)
        self.assertTrue(cached["coverage"][item_key]["already_backfilled"])
        self.assertEqual(
            len(service.poe_ninja.history_calls),
            calls_after_first,
        )

        # A parser/normalization version bump must invalidate otherwise
        # complete durable coverage exactly once, even after raw responses
        # have been removed from the compact public archive.
        with self.storage.transaction() as connection:
            connection.execute(
                """
                UPDATE current_item_history_coverage
                SET metadata_json = json_set(
                    metadata_json, '$.normalization_version', 1
                )
                WHERE league_id = ? AND item_key = ?
                  AND provider = 'poe.ninja'
                """,
                (league.id, item_key),
            )
        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 7, 31, 6, tzinfo=timezone.utc),
        ):
            migrated = service.sync_current_item_histories(
                league,
                [item_key],
                max_items=100,
            )
        self.assertEqual(migrated["status"], "success")
        self.assertEqual(migrated["already_backfilled_items"], 0)
        migrated_archive = self.storage.current_item_history_archive(
            league.id,
            item_key,
            provider="poe.ninja",
        )
        assert migrated_archive is not None
        self.assertEqual(migrated_archive["normalization_version"], 2)
        calls_after_version_migration = len(service.poe_ninja.history_calls)
        self.assertEqual(calls_after_version_migration, calls_after_first + 2)

        # A durable marker is only a cache hit while every normalized day is
        # still present. If archive corruption or an interrupted migration
        # drops a row, the next run must re-fetch and repair the curve.
        with self.storage.transaction() as connection:
            connection.execute(
                """
                DELETE FROM price_points
                WHERE league_id = ? AND item_key = ?
                  AND source = 'poe.ninja'
                  AND json_extract(details_json, '$.history_backfill') = 1
                  AND json_extract(details_json, '$.league_day') = 4
                """,
                (league.id, item_key),
            )
        damaged = self.storage.current_item_history_archive(
            league.id,
            item_key,
            provider="poe.ninja",
        )
        assert damaged is not None
        self.assertFalse(damaged["normalized_price_days_complete"])
        self.assertEqual(damaged["missing_normalized_price_days"], [4])
        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 7, 31, 6, tzinfo=timezone.utc),
        ):
            repaired = service.sync_current_item_histories(
                league,
                [item_key],
                max_items=100,
            )
        self.assertEqual(repaired["status"], "success")
        self.assertEqual(repaired["already_backfilled_items"], 0)
        self.assertEqual(
            len(service.poe_ninja.history_calls),
            calls_after_version_migration,
        )
        restored = self.storage.current_item_history_archive(
            league.id,
            item_key,
            provider="poe.ninja",
        )
        assert restored is not None
        self.assertTrue(restored["normalized_price_days_complete"])

    def test_ranked_history_rechecks_an_unexpected_closed_day_gap(self) -> None:
        class HealingPoeNinja(FakePoeNinja):
            def __init__(self) -> None:
                super().__init__()
                self.heal = False

            def fetch_exchange_details(
                self,
                league: str,
                category: str,
                item_id: int | str,
                **kwargs: Any,
            ) -> FetchResult:
                if not self.heal:
                    return super().fetch_exchange_details(
                        league,
                        category,
                        item_id,
                        **kwargs,
                    )
                self.history_calls.append(
                    ("exchange", league, category, str(item_id))
                )
                response = fetch_result(
                    self.exchange_details_url(league, category, item_id),
                    {
                        "item": {
                            "id": "divine",
                            "name": "Divine Orb",
                            "detailsId": "divine-orb",
                        },
                        "pairs": [
                            {
                                "id": "chaos",
                                "history": [
                                    {
                                        "timestamp": (
                                            datetime(2026, 7, 25, tzinfo=timezone.utc)
                                            + timedelta(days=index)
                                        ).isoformat().replace("+00:00", "Z"),
                                        "rate": 100.0 + index * 10.0,
                                        "volumePrimaryValue": 1000 + index,
                                    }
                                    for index in range(10)
                                ],
                            }
                        ],
                    },
                )
                response.fetched_at = "2026-08-03T18:00:00Z"
                return response

            def fetch_stash_item_history(
                self,
                league: str,
                category: str,
                item_id: int | str,
                **_: Any,
            ) -> FetchResult:
                if not self.heal:
                    return super().fetch_stash_item_history(
                        league,
                        category,
                        item_id,
                    )
                self.history_calls.append(
                    ("stash-item", league, category, str(item_id))
                )
                response = fetch_result(
                    self.stash_item_history_url(league, category, item_id),
                    [
                        {
                            "count": 20 + index,
                            "value": 3000 + index * 600,
                            "daysAgo": 9 - index,
                        }
                        for index in range(10)
                    ],
                )
                response.fetched_at = "2026-08-03T18:00:00Z"
                return response

        self.storage.set_setting("item_categories", ["SkillGem"])
        client = HealingPoeNinja()
        service = self.service(poe_ninja=client)
        result = service.sync(backfill_hours=0)
        self.assertTrue(result["ok"])
        league = self.storage.get_current_league()
        assert league is not None
        item_key = (
            "skillgem:awakened-enlighten-support-1-variant-1-"
            "corrupted-false-gemlevel-1-gemquality-0"
        )

        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 7, 31, 6, tzinfo=timezone.utc),
        ):
            initial = service.sync_current_item_histories(
                league,
                [item_key],
                max_items=100,
            )
        self.assertEqual(initial["status"], "success")
        calls_after_initial = len(client.history_calls)

        # Simulate successful overviews on league days 8 and 10 with a missed
        # scheduled run on closed day 9. The seven-day age check alone would
        # accept this curve, but the missing available day must force a detail
        # refresh so poe.ninja can heal it.
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=league.id,
                    item_key=item_key,
                    name="Awakened Enlighten Support",
                    category="SkillGem",
                    source="poe.ninja",
                    observed_at="2026-08-01T12:00:00Z",
                    chaos_value=6800.0,
                    divine_value=40.0,
                    confidence=0.9,
                    details={"poe_ninja_id": "95714"},
                ),
                PricePoint(
                    league_id=league.id,
                    item_key=item_key,
                    name="Awakened Enlighten Support",
                    category="SkillGem",
                    source="poe.ninja",
                    observed_at="2026-08-03T12:00:00Z",
                    chaos_value=7200.0,
                    divine_value=42.0,
                    confidence=0.9,
                    details={"poe_ninja_id": "95714"},
                ),
            ]
        )
        client.heal = True
        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 8, 3, 18, tzinfo=timezone.utc),
        ):
            repaired = service.sync_current_item_histories(
                league,
                [item_key],
                max_items=100,
            )

        self.assertEqual(repaired["status"], "success")
        self.assertEqual(repaired["gap_recheck_items"], 1)
        self.assertEqual(repaired["gap_recheck_days"], 1)
        self.assertEqual(
            repaired["coverage"][item_key]["rechecked_missing_days"],
            [9],
        )
        self.assertEqual(len(client.history_calls), calls_after_initial + 2)
        daily = self.storage.daily_item_history(
            league.id,
            item_key,
            league.start_at,
            minimum_confidence=0.0,
            sources=("poe.ninja",),
        )
        self.assertEqual(
            [int(point["league_day"]) for point in daily],
            list(range(1, 11)),
        )

    def test_only_provider_confirmed_closed_gap_can_remain_cached(self) -> None:
        self.storage.set_setting("item_categories", ["SkillGem"])
        client = FakePoeNinja()
        service = self.service(poe_ninja=client)
        self.assertTrue(service.sync(backfill_hours=0)["ok"])
        league = self.storage.get_current_league()
        assert league is not None
        item_key = (
            "skillgem:awakened-enlighten-support-1-variant-1-"
            "corrupted-false-gemlevel-1-gemquality-0"
        )
        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 7, 31, 6, tzinfo=timezone.utc),
        ):
            self.assertEqual(
                service.sync_current_item_histories(
                    league,
                    [item_key],
                    max_items=100,
                )["status"],
                "success",
            )
        calls_after_initial = len(client.history_calls)

        with self.storage.transaction() as connection:
            connection.execute(
                """
                UPDATE current_item_history_coverage
                SET missing_days_json = '[8]',
                    missing_divine_anchor_days_json = '[]',
                    metadata_json = json_set(
                        metadata_json, '$.checked_through_day', 9
                    )
                WHERE league_id = ? AND item_key = ?
                  AND provider = 'poe.ninja'
                """,
                (league.id, item_key),
            )
        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 8, 2, 18, tzinfo=timezone.utc),
        ):
            confirmed_gap = service.sync_current_item_histories(
                league,
                [item_key],
                max_items=100,
            )
        self.assertEqual(confirmed_gap["status"], "success")
        self.assertEqual(confirmed_gap["already_backfilled_items"], 1)
        self.assertEqual(confirmed_gap["gap_recheck_items"], 0)
        self.assertEqual(len(client.history_calls), calls_after_initial)

        # The same missing stored day is repairable when the item existed but
        # its Divine conversion anchor was absent. It must not be exempted as
        # a provider-confirmed no-trade day.
        with self.storage.transaction() as connection:
            connection.execute(
                """
                UPDATE current_item_history_coverage
                SET missing_days_json = '[]',
                    missing_divine_anchor_days_json = '[8]'
                WHERE league_id = ? AND item_key = ?
                  AND provider = 'poe.ninja'
                """,
                (league.id, item_key),
            )
        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 8, 2, 18, tzinfo=timezone.utc),
        ):
            missing_anchor = service.sync_current_item_histories(
                league,
                [item_key],
                max_items=100,
            )
        self.assertEqual(missing_anchor["gap_recheck_items"], 1)
        self.assertEqual(missing_anchor["gap_recheck_days"], 1)
        self.assertEqual(len(client.history_calls), calls_after_initial + 2)

    def test_ranked_exchange_history_uses_direct_divine_without_chaos_normalization(
        self,
    ) -> None:
        class MirrorPoeNinja(FakePoeNinja):
            def fetch_exchange_details(
                self,
                league: str,
                category: str,
                item_id: int | str,
                **_: Any,
            ) -> FetchResult:
                if str(item_id) != "mirror-of-kalandra":
                    return super().fetch_exchange_details(
                        league,
                        category,
                        item_id,
                    )
                self.history_calls.append(
                    ("exchange", league, category, str(item_id))
                )
                response = fetch_result(
                    self.exchange_details_url(league, category, item_id),
                    {
                        "item": {
                            "id": "mirror",
                            "name": "Mirror of Kalandra",
                            "detailsId": "mirror-of-kalandra",
                        },
                        "pairs": [
                            {"id": "chaos", "history": []},
                            {
                                "id": "divine",
                                "history": [
                                    {
                                        "timestamp": "2026-07-25T00:00:00Z",
                                        "rate": 100.0,
                                        "volumePrimaryValue": 0,
                                    },
                                    {
                                        "timestamp": "2026-07-26T00:00:00Z",
                                        "rate": 139.9,
                                        "volumePrimaryValue": 119457,
                                    },
                                ],
                            },
                        ],
                    },
                )
                response.fetched_at = "2026-07-31T06:00:00Z"
                return response

        client = MirrorPoeNinja()
        service = self.service(poe_ninja=client)
        result = service.sync(backfill_hours=0)
        self.assertTrue(result["ok"])
        league = self.storage.get_current_league()
        assert league is not None
        item_key = "currency:mirror-of-kalandra"
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=league.id,
                    item_key=item_key,
                    name="Mirror of Kalandra",
                    category="Currency",
                    source="poe.ninja",
                    observed_at="2026-07-30T06:00:00Z",
                    chaos_value=65000.0,
                    divine_value=300.0,
                    confidence=0.95,
                    details={"detailsId": "mirror-of-kalandra"},
                )
            ]
        )

        with patch(
            "poe_advisor.models.utc_now",
            return_value=datetime(2026, 7, 31, 6, tzinfo=timezone.utc),
        ):
            summary = service.sync_current_item_histories(
                league,
                [item_key],
                max_items=100,
            )

        self.assertEqual(summary["failed_items"], 0)
        self.assertEqual(summary["coverage"][item_key]["normalized_days"], [1, 2])
        self.assertEqual(
            summary["coverage"][item_key]["missing_divine_anchor_days"],
            [],
        )
        self.assertIn(
            ("exchange", league.id, "Currency", "divine-orb"),
            client.history_calls,
        )
        self.assertIn(
            ("exchange", league.id, "Currency", "mirror-of-kalandra"),
            client.history_calls,
        )
        histories = self.storage.item_histories(
            league.id,
            days=90,
            item_key=item_key,
            sources=("poe.ninja",),
        )[item_key]
        backfilled = [
            point
            for point in histories
            if point["details"].get("history_backfill") is True
        ]
        self.assertEqual(
            [point["divine_value"] for point in backfilled],
            [100.0, 139.9],
        )
        self.assertEqual(backfilled[0]["chaos_value"], None)
        self.assertEqual(backfilled[0]["volume"], 0.0)
        self.assertEqual(backfilled[0]["confidence"], 0.65)
        self.assertEqual(backfilled[0]["details"]["quote_currency"], "divine")
        self.assertEqual(
            backfilled[0]["details"]["normalization"],
            "direct same-league poe.ninja Divine pair history",
        )

    def test_ranked_current_history_reports_request_limit(self) -> None:
        service = self.service()
        result = service.sync(backfill_hours=0)
        self.assertTrue(result["ok"])
        league = self.storage.get_current_league()
        assert league is not None

        summary = service.sync_current_item_histories(
            league,
            ["missing:a", "missing:b", "missing:c"],
            max_items=2,
        )

        self.assertEqual(summary["input_items"], 3)
        self.assertEqual(summary["requested_items"], 2)
        self.assertEqual(summary["omitted_items"], 1)
        self.assertEqual(summary["status"], "partial")
        self.assertTrue(
            any("exceeded" in warning for warning in summary["warnings"])
        )

    def test_ranked_current_history_zero_limit_disables_requests(self) -> None:
        service = self.service()
        result = service.sync(backfill_hours=0)
        self.assertTrue(result["ok"])
        league = self.storage.get_current_league()
        assert league is not None
        history_calls_before = list(service.poe_ninja.history_calls)

        summary = service.sync_current_item_histories(
            league,
            ["missing:a", "missing:b"],
            max_items=0,
        )

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["requested_items"], 0)
        self.assertEqual(summary["input_items"], 2)
        self.assertEqual(summary["omitted_items"], 2)
        self.assertEqual(
            summary["message"],
            "No ranked item histories were requested.",
        )
        self.assertEqual(service.poe_ninja.history_calls, history_calls_before)

    def test_exchange_history_recovers_identity_from_canonical_item_key(self) -> None:
        service = self.service()
        result = service.sync(backfill_hours=0)
        self.assertTrue(result["ok"])
        league = self.storage.get_current_league()
        assert league is not None
        self.storage.set_setting(
            "exchange_categories",
            ["Currency", "DivinationCard"],
        )
        item_key = "divinationcard:the-visionary"
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=league.id,
                    item_key=item_key,
                    name="The Visionary",
                    category="DivinationCard",
                    source="poe.ninja",
                    observed_at="2026-07-30T06:00:00Z",
                    chaos_value=10.0,
                    divine_value=0.1,
                    listing_count=10,
                    confidence=0.9,
                    details={},
                )
            ]
        )

        summary = service.sync_current_item_histories(
            league,
            [item_key],
        )

        self.assertEqual(summary["matched_items"], 1)
        self.assertEqual(summary["unmatched_items"], 0)
        self.assertEqual(summary["derived_exchange_identity_items"], 1)
        self.assertIn(
            ("exchange", league.id, "DivinationCard", "the-visionary"),
            service.poe_ninja.history_calls,
        )
        self.assertEqual(
            summary["coverage"][item_key]["identity_source"],
            "canonical poe.ninja item-key suffix",
        )

    def test_ranked_current_history_fails_closed_on_bad_divine_curve(
        self,
    ) -> None:
        self.storage.set_setting("item_categories", ["SkillGem"])
        service = self.service(poe_ninja=InvalidDivineHistoryPoeNinja())
        result = service.sync(backfill_hours=0)
        self.assertTrue(result["ok"])
        league = self.storage.get_current_league()
        assert league is not None
        item_key = (
            "skillgem:awakened-enlighten-support-1-variant-1-"
            "corrupted-false-gemlevel-1-gemquality-0"
        )

        summary = service.sync_current_item_histories(
            league,
            [item_key],
        )

        self.assertEqual(summary["status"], "failed")
        self.assertIn("Divine Orb normalization rejected", summary["message"])
        connection = self.storage.connect()
        try:
            current_history_rows = connection.execute(
                """
                SELECT COUNT(*)
                FROM price_points
                WHERE league_id = ? AND item_key = ?
                  AND source = 'poe.ninja'
                  AND json_extract(details_json, '$.history_backfill') = 1
                """,
                (league.id, item_key),
            ).fetchone()[0]
            raw_divine = connection.execute(
                """
                SELECT metadata_json
                FROM raw_snapshots
                WHERE league_id = ?
                  AND category = 'current-divine-history'
                ORDER BY id DESC
                LIMIT 1
                """,
                (league.id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(current_history_rows, 0)
        self.assertIsNotNone(raw_divine)
        metadata = json.loads(raw_divine["metadata_json"])
        self.assertFalse(metadata["valid"])
        self.assertIn("normalization rejected", metadata["validation_error"])

    def test_total_price_outage_does_not_advance_freshness(self) -> None:
        league = League(
            id="Fixture Softcore",
            name="Fixture Softcore",
            start_at="2026-07-24T20:00:00Z",
        )
        self.storage.upsert_league(league)
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=league.id,
                    item_key="currency:orb-of-alchemy",
                    name="Orb of Alchemy",
                    category="Currency",
                    source="poe.ninja",
                    observed_at="2026-07-28T06:00:00Z",
                    chaos_value=2.0,
                    divine_value=0.01,
                )
            ]
        )
        run_id = self.storage.start_sync_run(league.id)
        self.storage.finish_sync_run(
            run_id,
            status="success",
            rows_written=1,
            snapshots_written=1,
            message="prior success",
            warnings=[],
        )
        prior_freshness = self.storage.last_sync_at(league.id)

        result = self.service(fail_prices=True).sync(backfill_hours=0)

        self.assertFalse(result["ok"])
        self.assertIn("freshness timestamp was not advanced", result["message"])
        self.assertEqual(
            self.storage.last_sync_at(league.id),
            prior_freshness,
        )


if __name__ == "__main__":
    unittest.main()
