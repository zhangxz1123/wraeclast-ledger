from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from poe_advisor.historical import (
    BROADLY_COVERED_LEAGUES,
    COMPLETED_LEAGUES,
)
from poe_advisor.models import League, PricePoint, iso_utc
from poe_advisor.recommendation import RecommendationEngine
from poe_advisor.server import AdvisorApplication
from poe_advisor.storage import Storage


class ForecastRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.storage = Storage(
            Path(self.temporary_directory.name) / "forecast.sqlite3"
        )
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.live = League(
            id="Forecast Live",
            name="Forecast Live",
            start_at=iso_utc(self.now - timedelta(days=2, hours=1)),
        )
        self.storage.upsert_league(self.live, current=True)
        for spec in COMPLETED_LEAGUES:
            self.storage.upsert_league(spec.as_league(), current=False)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_current(
        self,
        key: str,
        name: str,
        values: list[float],
        *,
        category: str = "Currency",
        listing_count: int = 1,
        volume: float = 1.0,
        details: dict[str, object] | None = None,
    ) -> None:
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=self.live.id,
                    item_key=key,
                    name=name,
                    category=category,
                    source="poe.ninja",
                    observed_at=iso_utc(
                        self.now
                        - timedelta(days=len(values) - index - 1)
                    ),
                    chaos_value=value * 100,
                    divine_value=value,
                    listing_count=listing_count,
                    volume=volume,
                    confidence=0.2,
                    details=details or {},
                )
                for index, value in enumerate(values)
            ]
        )

    def add_target(
        self,
        key: str,
        name: str,
        league_id: str,
        horizon: int,
        value: float,
        *,
        category: str = "Currency",
        confidence: float = 0.9,
    ) -> None:
        spec = next(
            value
            for value in COMPLETED_LEAGUES
            if value.league_id == league_id
        )
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.ninja-history",
                    "source_item_id": key,
                    "item_key": key,
                    "name": name,
                    "category": category,
                    "eligible": True,
                }
            ]
        )
        self.storage.upsert_seasonal_prices(
            [
                {
                    "league_id": league_id,
                    "item_key": key,
                    "source": "poe.ninja-history",
                    "source_item_id": key,
                    "league_day": int(self.live.day or 1) + horizon,
                    "observed_at": spec.start_at,
                    "divine_value": value,
                    "confidence": confidence,
                }
            ]
        )

    def add_lifecycle_curve(
        self,
        key: str,
        name: str,
        league_id: str,
        weekly_values: list[float],
        *,
        category: str = "Currency",
    ) -> None:
        spec = next(
            value
            for value in COMPLETED_LEAGUES
            if value.league_id == league_id
        )
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.ninja-history",
                    "source_item_id": key,
                    "item_key": key,
                    "name": name,
                    "category": category,
                    "eligible": True,
                }
            ]
        )
        rows = []
        start = datetime.fromisoformat(spec.start_at.replace("Z", "+00:00"))
        for week, value in enumerate(weekly_values):
            for offset in (0, 1):
                league_day = week * 7 + offset + 1
                rows.append(
                    {
                        "league_id": league_id,
                        "item_key": key,
                        "source": "poe.ninja-history",
                        "source_item_id": key,
                        "league_day": league_day,
                        "observed_at": iso_utc(
                            start + timedelta(days=league_day - 1)
                        ),
                        "divine_value": value,
                        "confidence": 0.9,
                    }
                )
        self.storage.upsert_seasonal_prices(rows)

    def test_only_broad_leagues_contribute_and_sparse_history_is_not_gated(
        self,
    ) -> None:
        self.add_current("currency:one", "One", [1.0])
        self.add_current("currency:none", "No History", [2.0])
        self.add_target("currency:one", "One", "Mirage", 7, 1.5)
        self.add_target("currency:one", "One", "Affliction", 7, 999.0)
        self.add_target("currency:one", "One", "Necropolis", 7, 888.0)

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        self.assertEqual(payload["mode"], "forecast_ranking")
        one = next(row for row in payload["rankings"] if row["name"] == "One")
        none = next(
            row
            for row in payload["rankings"]
            if row["name"] == "No History"
        )
        self.assertEqual(one["historical_sample_leagues"], 1)
        self.assertEqual(
            one["forecast_7d"]["historical_leagues"],
            ["Mirage"],
        )
        self.assertAlmostEqual(one["historical_target_price_divine"], 1.5)
        self.assertAlmostEqual(one["expected_gain"], 0.5)
        self.assertIsNone(none["expected_gain"])
        self.assertGreater(none["rank"], one["rank"])
        self.assertNotIn("eligibility_status", one)
        self.assertEqual(payload["forecast_model"]["eligibility_gates"], [])

    def test_low_confidence_dump_outliers_are_audit_only(self) -> None:
        simplex = "basetype:simplex-amulet-85-crusader-variant-crusader"
        focused = "basetype:focused-amulet-84-elder-variant-elder"
        infested = "beast:infested-ursa"
        self.add_current(
            simplex,
            "Simplex Amulet",
            [0.01],
            category="BaseType",
            details={"variant": "Crusader"},
        )
        self.add_current(
            focused,
            "Focused Amulet",
            [0.02],
            category="BaseType",
            details={"variant": "Elder"},
        )
        self.add_current(
            infested,
            "Infested Ursa",
            [0.89],
            category="Beast",
            details={"baseType": "Plummeting Ursae|Ursae|The Wilds"},
        )

        # These reproduce the extreme Low rows in poe.ninja's Mirage dump.
        self.add_target(
            simplex,
            "Simplex Amulet",
            "Mirage",
            7,
            3.1848,
            category="BaseType",
            confidence=0.35,
        )
        self.add_target(
            focused,
            "Focused Amulet",
            "Mirage",
            7,
            5.308,
            category="BaseType",
            confidence=0.35,
        )
        self.add_target(
            infested,
            "Infested Ursa",
            "Mirage",
            7,
            1111.044,
            category="Beast",
            confidence=0.35,
        )
        # Focused Amulet still has a target from qualifying observations.
        self.add_target(
            focused,
            "Focused Amulet",
            "Mercenaries",
            7,
            0.02556299452221546,
            category="BaseType",
            confidence=0.65,
        )
        self.add_target(
            focused,
            "Focused Amulet",
            "Settlers",
            7,
            0.04838709677419355,
            category="BaseType",
            confidence=0.9,
        )

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )
        by_name = {row["name"]: row for row in payload["rankings"]}

        # Low data is a forecast-quality rule, not an item eligibility rule.
        self.assertEqual(
            set(by_name),
            {"Simplex Amulet", "Focused Amulet", "Infested Ursa"},
        )
        self.assertIsNone(by_name["Simplex Amulet"]["expected_gain"])
        self.assertEqual(
            by_name["Simplex Amulet"]["forecast_7d"],
            {"days": 7},
        )
        self.assertIsNone(by_name["Infested Ursa"]["expected_gain"])
        self.assertEqual(
            by_name["Infested Ursa"]["forecast_7d"],
            {"days": 7},
        )

        weights = (0.72**2, 0.72**3)
        expected_target = (
            0.02556299452221546 * weights[0]
            + 0.04838709677419355 * weights[1]
        ) / sum(weights)
        focused_forecast = by_name["Focused Amulet"]["forecast_7d"]
        self.assertAlmostEqual(
            focused_forecast["raw_historical_target_divine"],
            expected_target,
        )
        self.assertEqual(
            focused_forecast["historical_leagues"],
            ["Mercenaries", "Settlers"],
        )
        self.assertLess(
            focused_forecast["raw_historical_target_divine"],
            0.05,
        )
        self.assertEqual(payload["rankings"][0]["name"], "Focused Amulet")
        self.assertEqual(
            payload["forecast_model"]["historical_confidence_floor"],
            0.5,
        )

        # The original Low observations are still retained for local audit.
        raw_low = self.storage.seasonal_price_curve_rows(
            infested,
            ["Mirage"],
            minimum_confidence=0.0,
            sources=("poe.ninja-history",),
        )
        self.assertEqual(len(raw_low), 1)
        self.assertEqual(raw_low[0]["confidence"], 0.35)
        self.assertEqual(
            self.storage.seasonal_price_curve_rows(
                infested,
                ["Mirage"],
                minimum_confidence=0.5,
                sources=("poe.ninja-history",),
            ),
            [],
        )

    def test_sub_chaos_price_is_outside_the_requested_universe(
        self,
    ) -> None:
        self.add_current(
            "currency:jewellers-orb",
            "Jeweller's Orb",
            [0.001],
        )
        self.add_target(
            "currency:jewellers-orb",
            "Jeweller's Orb",
            "Mirage",
            7,
            10.0,
        )
        self.add_current(
            "currency:fracturing-orb",
            "Fracturing Orb",
            [1.0],
        )
        self.add_target(
            "currency:fracturing-orb",
            "Fracturing Orb",
            "Mirage",
            7,
            2.0,
        )
        self.add_current(
            "currency:one-chaos-floor",
            "One Chaos Floor",
            [0.01],
        )

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        names = {row["name"] for row in payload["rankings"]}
        self.assertNotIn("Jeweller's Orb", names)
        self.assertIn("Fracturing Orb", names)
        self.assertIn("One Chaos Floor", names)
        self.assertEqual(
            payload["investment_scope"][
                "excluded_below_one_chaos_items"
            ],
            1,
        )
        self.assertEqual(
            payload["investment_scope"]["excluded_item_count"],
            1,
        )

    def test_chromatic_orb_is_archived_but_not_ranked(self) -> None:
        key = "currency:chrome"
        self.add_current(key, "Chromatic Orb", [0.02])
        self.add_target(key, "Chromatic Orb", "Mirage", 7, 0.04)
        self.add_current(
            "currency:fracturing-orb",
            "Fracturing Orb",
            [1.0],
        )

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        names = {row["name"] for row in payload["rankings"]}
        self.assertNotIn("Chromatic Orb", names)
        self.assertIn("Fracturing Orb", names)
        scope = payload["investment_scope"]
        self.assertEqual(scope["excluded_low_end_currency_items"], 1)
        self.assertEqual(
            scope["excluded_low_end_currency_counts"],
            {"Chromatic Orb": 1},
        )
        self.assertEqual(scope["excluded_item_count"], 1)

        archived = self.storage.item_histories(
            self.live.id,
            days=30,
            sources=("poe.ninja",),
        )
        self.assertIn(key, archived)
        self.assertEqual(archived[key][-1]["name"], "Chromatic Orb")

    def test_log_blend_uses_actual_curve_and_caps_projection(self) -> None:
        self.add_current("currency:blend", "Blend", [1.0, 2.0, 4.0])
        self.add_target("currency:blend", "Blend", "Mirage", 3, 8.0)

        row = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=3,
            persist=False,
        )["rankings"][0]

        forecast = row["forecast_3d"]
        projection = forecast["current_curve_projection"]
        self.assertAlmostEqual(
            projection["log_slope_per_day"],
            math.log(2.0),
        )
        self.assertTrue(projection["was_capped"])
        self.assertAlmostEqual(projection["capped_gain"], 0.5)
        expected = math.exp(
            0.7 * math.log(2.0) + 0.3 * math.log(1.5)
        ) - 1.0
        self.assertAlmostEqual(forecast["expected_gain"], expected)
        self.assertEqual(forecast["blend"]["historical_weight"], 0.7)
        self.assertEqual(forecast["blend"]["current_curve_weight"], 0.3)

    def test_known_maven_decline_is_omitted_without_other_eligibility_gates(
        self,
    ) -> None:
        key = "fragment:the-mavens-writ"
        self.add_current(
            key,
            "The Maven's Writ",
            [4.0, 2.0, 1.0],
            category="Fragment",
            listing_count=0,
            volume=0,
        )
        self.add_target(
            key,
            "The Maven's Writ",
            "Mirage",
            7,
            0.5,
            category="Fragment",
            confidence=0.1,
        )

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )
        self.assertNotIn(
            "The Maven's Writ",
            {row["name"] for row in payload["rankings"]},
        )
        vetoes = payload["investment_scope"]["known_decline_vetoes"]
        self.assertEqual(len(vetoes), 1)
        self.assertEqual(vetoes[0]["name"], "The Maven's Writ")

    def test_recent_league_consensus_automatically_omits_chaos_orb(
        self,
    ) -> None:
        chaos_key = "currency:chaos-orb"
        self.add_current(chaos_key, "Chaos Orb", [0.01])
        self.add_current("currency:divine-orb", "Divine Orb", [1.0])
        declining = [1.0 * 0.95**week for week in range(12)]
        increasing = [0.5 * 1.03**week for week in range(12)]
        for league_id in ("Mirage", "Keepers"):
            self.add_lifecycle_curve(
                chaos_key,
                "Chaos Orb",
                league_id,
                declining,
            )
        for league_id in ("Mercenaries", "Settlers"):
            self.add_lifecycle_curve(
                chaos_key,
                "Chaos Orb",
                league_id,
                increasing,
            )

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        names = {row["name"] for row in payload["rankings"]}
        self.assertNotIn("Chaos Orb", names)
        # A routine currency is not excluded merely by name anymore.
        self.assertIn("Divine Orb", names)
        scope = payload["investment_scope"]
        self.assertEqual(scope["automatic_decline_items"], 1)
        veto = scope["automatic_decline_vetoes"][0]
        self.assertEqual(veto["name"], "Chaos Orb")
        self.assertGreaterEqual(veto["weighted_support"], 0.65)
        self.assertEqual(
            set(veto["declining_leagues"]),
            {"Mirage", "Keepers"},
        )

    def test_chaos_orb_without_direct_dump_curve_is_still_omitted(
        self,
    ) -> None:
        self.add_current("currency:chaos-orb", "Chaos Orb", [0.01])
        self.add_current("currency:divine-orb", "Divine Orb", [1.0])

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        names = {row["name"] for row in payload["rankings"]}
        self.assertNotIn("Chaos Orb", names)
        self.assertIn("Divine Orb", names)
        vetoes = payload["investment_scope"]["known_decline_vetoes"]
        self.assertEqual(len(vetoes), 1)
        self.assertEqual(vetoes[0]["name"], "Chaos Orb")
        self.assertEqual(
            vetoes[0]["code"],
            "divine_relative_reference_currency_decline",
        )

    def test_variant_absent_from_latest_successful_sync_is_omitted(
        self,
    ) -> None:
        fresh_key = "skillgem:awakened-still-listed"
        stale_key = "skillgem:awakened-vanished-variant"
        self.add_current(
            fresh_key,
            "Awakened Still Listed",
            [2.0],
            category="SkillGem",
        )
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=self.live.id,
                    item_key=stale_key,
                    name="Awakened Vanished Variant",
                    category="SkillGem",
                    source="poe.ninja",
                    observed_at=iso_utc(self.now - timedelta(days=1)),
                    chaos_value=100.0,
                    divine_value=1.0,
                    listing_count=1,
                    volume=1.0,
                    confidence=0.2,
                )
            ]
        )
        with self.storage.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sync_runs(
                    started_at, finished_at, status, league_id
                ) VALUES (?, ?, 'success', ?)
                """,
                (
                    iso_utc(self.now - timedelta(minutes=1)),
                    iso_utc(self.now),
                    self.live.id,
                ),
            )

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        names = {row["name"] for row in payload["rankings"]}
        self.assertIn("Awakened Still Listed", names)
        self.assertNotIn("Awakened Vanished Variant", names)
        self.assertEqual(
            payload["investment_scope"]["excluded_stale_current_items"],
            1,
        )

    def test_one_declining_league_does_not_trigger_automatic_omission(
        self,
    ) -> None:
        key = "currency:one-decline"
        self.add_current(key, "One Decline", [0.02])
        declining = [1.0 * 0.95**week for week in range(12)]
        stable = [1.0 for _ in range(12)]
        self.add_lifecycle_curve(key, "One Decline", "Mirage", declining)
        self.add_lifecycle_curve(key, "One Decline", "Keepers", stable)

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        self.assertIn(
            "One Decline",
            {row["name"] for row in payload["rankings"]},
        )
        self.assertEqual(
            payload["investment_scope"]["automatic_decline_items"],
            0,
        )

    def test_selected_horizon_changes_ranking(self) -> None:
        for key, name in (
            ("currency:a", "A"),
            ("currency:b", "B"),
        ):
            self.add_current(key, name, [1.0])
        self.add_target("currency:a", "A", "Mirage", 3, 3.0)
        self.add_target("currency:a", "A", "Mirage", 7, 1.1)
        self.add_target("currency:b", "B", "Mirage", 3, 1.2)
        self.add_target("currency:b", "B", "Mirage", 7, 4.0)

        engine = RecommendationEngine(self.storage)
        three = engine.generate(self.live, horizon=3, persist=False)
        seven = engine.generate(self.live, horizon=7, persist=False)
        self.assertEqual(three["rankings"][0]["name"], "A")
        self.assertEqual(seven["rankings"][0]["name"], "B")

    def test_current_curves_are_loaded_only_for_forecastable_items(
        self,
    ) -> None:
        for index in range(25):
            self.add_current(
                f"currency:no-target-{index}",
                f"No Target {index}",
                [1.0],
            )
        self.add_current("currency:target", "Target", [1.0, 1.1])
        self.add_target(
            "currency:target",
            "Target",
            "Mirage",
            7,
            2.0,
        )

        with patch.object(
            self.storage,
            "daily_item_history",
            wraps=self.storage.daily_item_history,
        ) as daily_history:
            payload = RecommendationEngine(self.storage).generate(
                self.live,
                horizon=7,
                persist=False,
            )

        self.assertEqual(daily_history.call_count, 1)
        self.assertEqual(payload["rankings"][0]["name"], "Target")
        null_row = next(
            row
            for row in payload["rankings"]
            if row["name"] == "No Target 0"
        )
        self.assertIsNone(null_row["expected_gain"])
        self.assertEqual(null_row["history"], [])

    def test_full_exact_variant_universe_is_not_truncated(self) -> None:
        for index in range(105):
            self.add_current(
                f"invitation:fixture-{index:03d}",
                f"Fixture {index:03d}",
                [1.0],
                category="Invitation",
            )
        self.add_current(
            "skillgem:awakened-enlighten-support-level-1",
            "Awakened Enlighten Support",
            [35.0],
            category="SkillGem",
            details={
                "variant": "1",
                "gemLevel": 1,
            },
        )
        self.add_current(
            "skillgem:awakened-enlighten-support-level-5-corrupted",
            "Awakened Enlighten Support",
            [135.0],
            category="SkillGem",
            details={
                "variant": "5c",
                "gemLevel": 5,
                "corrupted": True,
            },
        )

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        self.assertEqual(len(payload["rankings"]), 107)
        self.assertEqual(
            [row["rank"] for row in payload["rankings"]],
            list(range(1, 108)),
        )
        self.assertIsNone(payload["ranking_summary"]["limit"])
        self.assertEqual(payload["ranking_summary"]["returned"], 107)
        awakened = [
            row
            for row in payload["rankings"]
            if row["name"] == "Awakened Enlighten Support"
        ]
        self.assertEqual(len(awakened), 2)
        self.assertEqual(
            {row["trade_identity"]["variant"] for row in awakened},
            {"1", "5c"},
        )
        self.assertNotIn("forecast_horizons", awakened[0])
        self.assertNotIn("horizons", awakened[0])
        self.assertEqual(awakened[0]["forecast_3d"], {"days": 3})

    def test_requested_market_categories_are_archived_but_not_ranked(
        self,
    ) -> None:
        fixtures = (
            (
                "uniqueaccessory:headhunter",
                "Headhunter",
                "UniqueAccessory",
            ),
            (
                "uniquemap:fixture",
                "Unique Map Fixture",
                "Unique Map",
            ),
            (
                "uniquejewel:sublime-vision",
                "Sublime Vision",
                "UniqueJewel",
            ),
            (
                "uniquejewel:the-adorned",
                "The Adorned",
                "UniqueJewel",
            ),
            (
                "uniquejewel:watchers-eye",
                "Watcher's Eye",
                "UniqueJewel",
            ),
            (
                "uniquejewel:voices-not-exact",
                "Voices (3 passives)",
                "UniqueJewel",
            ),
            ("valdomap:fixture", "Valdo Fixture", "ValdoMap"),
            (
                "skillgem:enlighten-support",
                "Enlighten Support",
                "SkillGem",
            ),
            (
                "skillgem:awakened-without-space",
                "AwakenedFixture Support",
                "SkillGem",
            ),
            (
                "skillgem:awakened-enlighten-support",
                "Awakened Enlighten Support",
                "SkillGem",
            ),
            (
                "skillgem:awakened-case-fixture",
                "aWaKeNeD Case Fixture",
                "SkillGem",
            ),
            (
                "forbiddenjewel:unbreakable",
                "Unbreakable",
                "ForbiddenJewel",
            ),
            (
                "basetype:abyssal-axe-86-hunter-variant-hunter",
                "Abyssal Axe",
                "BaseType",
            ),
            (
                "basetype:replica-simplex-amulet-86",
                "Replica Simplex Amulet",
                "BaseType",
            ),
            (
                "basetype:simplex-amulet-86-hunter-variant-hunter",
                "Simplex Amulet",
                "BaseType",
            ),
            (
                "basetype:focused-amulet-86-shaper-variant-shaper",
                "Focused Amulet",
                "BaseType",
            ),
        )
        for key, name, category in fixtures:
            self.add_current(key, name, [1.0], category=category)
        self.add_current(
            "uniquejewel:voices-1-passive",
            "Voices",
            [10.0],
            category="UniqueJewel",
            details={"detailsId": "voices-large-cluster-jewel"},
        )
        self.add_current(
            "uniquejewel:voices-3-passives",
            "Voices",
            [2.0],
            category="UniqueJewel",
            details={
                "variant": "3 passives",
                "detailsId": "voices-3-passives-large-cluster-jewel",
            },
        )
        self.add_current(
            "uniquejewel:voices-3-passives-wrong-identity",
            "Voices",
            [3.0],
            category="UniqueJewel",
            details={
                "variant": "3 passives",
                "detailsId": "voices-5-passives-large-cluster-jewel",
            },
        )
        self.add_current(
            "uniquejewel:voices-5-passives",
            "Voices",
            [0.5],
            category="UniqueJewel",
            details={"variant": "5 passives"},
        )
        self.add_current(
            "uniquejewel:voices-7-passives",
            "Voices",
            [0.1],
            category="UniqueJewel",
            details={"variant": "7 passives"},
        )
        self.add_current(
            "uniquejewel:voices-unresolved",
            "Voices",
            [5.0],
            category="UniqueJewel",
        )

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        ranked_names = {row["name"] for row in payload["rankings"]}
        self.assertEqual(
            ranked_names,
            {
                "Awakened Enlighten Support",
                "aWaKeNeD Case Fixture",
                "Unbreakable",
                "Voices",
                "Sublime Vision",
                "The Adorned",
                "Watcher's Eye",
                "Simplex Amulet",
                "Focused Amulet",
            },
        )
        self.assertEqual(
            payload["investment_scope"]["excluded_category_counts"],
            {
                "BaseType": 2,
                "SkillGem": 2,
                "Unique Map": 1,
                "UniqueAccessory": 1,
                "UniqueJewel": 5,
                "ValdoMap": 1,
            },
        )
        self.assertIn(
            "SkillGem (except names beginning with 'Awakened ')",
            payload["investment_scope"]["excluded_categories"],
        )
        self.assertIn(
            "BaseType (except Simplex Amulet and Focused Amulet)",
            payload["investment_scope"]["excluded_categories"],
        )
        self.assertIn(
            (
                "Unique* (except 1-/3-passive Voices and the aggregate "
                "Sublime Vision, The Adorned, and Watcher's Eye markets)"
            ),
            payload["investment_scope"]["excluded_categories"],
        )
        self.assertEqual(
            self.storage.status_counts(self.live.id)["price_points"],
            len(fixtures) + 6,
        )
        rows_by_key = {row["key"]: row for row in payload["rankings"]}
        self.assertEqual(
            {
                key
                for key in rows_by_key
                if key.startswith("uniquejewel:voices-")
            },
            {
                "uniquejewel:voices-1-passive",
                "uniquejewel:voices-3-passives",
            },
        )
        self.assertEqual(
            rows_by_key["uniquejewel:voices-1-passive"]["trade_identity"][
                "variant"
            ],
            "1 passive",
        )
        self.assertEqual(
            rows_by_key["uniquejewel:voices-3-passives"][
                "market_scope_code"
            ],
            "exact_voices_3_passives",
        )
        for name in ("Sublime Vision", "The Adorned", "Watcher's Eye"):
            row = next(
                candidate
                for candidate in payload["rankings"]
                if candidate["name"] == name
            )
            self.assertEqual(
                row["market_scope_code"],
                "aggregate_roll_unresolved",
            )
            self.assertIn(
                "not the price of a specific roll",
                row["market_scope_caveat"],
            )

    def test_item_level_variants_have_distinct_full_identity(self) -> None:
        self.add_current(
            "basetype:simplex-amulet-86-hunter-variant-hunter",
            "Simplex Amulet",
            [1.0],
            category="BaseType",
            details={"variant": "Hunter"},
        )
        self.add_current(
            "clusterjewel:12-to-chaos-resistance-2-passives-84-variant-2-passives",
            "+12% to Chaos Resistance",
            [1.0],
            category="ClusterJewel",
            details={
                "variant": "2 passives",
                "baseType": "Small Cluster Jewel",
            },
        )
        self.add_current(
            "basetype:focused-amulet-6l-links-6",
            "Focused Amulet",
            [1.0],
            category="BaseType",
            details={"baseType": "Focused Amulet", "links": 6},
        )
        self.add_current(
            "wombgift:ancient-wombgift-84",
            "Ancient Wombgift",
            [1.0],
            category="Wombgift",
        )

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )
        identities = {
            row["name"]: row["trade_identity"]
            for row in payload["rankings"]
        }
        self.assertEqual(identities["Simplex Amulet"]["item_level"], 86)
        self.assertEqual(
            identities["+12% to Chaos Resistance"]["item_level"],
            84,
        )
        self.assertEqual(identities["Focused Amulet"]["links"], 6)
        self.assertEqual(
            identities["Ancient Wombgift"]["item_level"],
            84,
        )

    def test_history_comparison_contains_only_broad_leagues(self) -> None:
        key = "currency:curve"
        self.add_current(key, "Curve", [1.0, 1.1, 1.2])
        for spec in COMPLETED_LEAGUES:
            self.add_target(
                key,
                "Curve",
                spec.league_id,
                3,
                2.0 if spec in BROADLY_COVERED_LEAGUES else 999.0,
                confidence=(
                    0.1 if spec.league_id == "Mirage" else 0.9
                ),
            )
        application = object.__new__(AdvisorApplication)
        application.storage = self.storage
        comparison = AdvisorApplication._seasonal_comparison(
            application,
            league=self.live,
            item_key=key,
        )
        self.assertTrue(
            {
                row["league_id"] for row in comparison["past_leagues"]
            }.issubset(
                {spec.league_id for spec in BROADLY_COVERED_LEAGUES}
            )
        )
        self.assertEqual(
            {
                row["league_id"] for row in comparison["past_leagues"]
            },
            {
                spec.league_id
                for spec in BROADLY_COVERED_LEAGUES
            },
        )
        mirage_curve = next(
            row
            for row in comparison["past_leagues"]
            if row["league_id"] == "Mirage"
        )
        self.assertTrue(
            all(
                point["model_grade"] is False
                for point in mirage_curve["points"]
            )
        )
        self.assertEqual(
            comparison["calculation"]["historical_confidence_floor"],
            0.5,
        )
        target = comparison["forecast_horizons"]["3"]
        self.assertAlmostEqual(
            target["historical_target_price_divine"],
            2.0,
        )


if __name__ == "__main__":
    unittest.main()
