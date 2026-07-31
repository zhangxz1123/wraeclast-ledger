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
        self.now = datetime(2026, 7, 30, 20, tzinfo=timezone.utc)
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
        start = datetime.fromisoformat(
            str(self.live.start_at).replace("Z", "+00:00")
        )
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=self.live.id,
                    item_key=key,
                    name=name,
                    category=category,
                    source="poe.ninja",
                    observed_at=iso_utc(
                        start + timedelta(days=index, hours=2)
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
                    "source": "poe.watch",
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
                    "source": "poe.watch",
                    "source_item_id": key,
                    "league_day": int(self.live.day or 1) + horizon,
                    "observed_at": spec.start_at,
                    "divine_value": value,
                    "confidence": confidence,
                }
            ]
        )

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

    def test_routine_base_currency_is_outside_the_requested_universe(
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

        payload = RecommendationEngine(self.storage).generate(
            self.live,
            horizon=7,
            persist=False,
        )

        names = {row["name"] for row in payload["rankings"]}
        self.assertNotIn("Jeweller's Orb", names)
        self.assertIn("Fracturing Orb", names)
        self.assertEqual(
            payload["investment_scope"]["excluded_routine_currency_items"],
            1,
        )
        self.assertEqual(
            payload["investment_scope"]["excluded_item_count"],
            1,
        )

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

    def test_negative_falling_maven_and_low_liquidity_remain_ranked(
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
        maven = next(
            row
            for row in payload["rankings"]
            if row["name"] == "The Maven's Writ"
        )
        self.assertLess(maven["expected_gain"], 0)
        self.assertEqual(maven["historical_sample_leagues"], 1)
        self.assertEqual(
            payload["investment_scope"]["known_decline_vetoes"],
            [],
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
                f"unique:fixture-{index:03d}",
                f"Fixture {index:03d}",
                [1.0],
                category="UniqueAccessory",
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
            {spec.league_id for spec in BROADLY_COVERED_LEAGUES},
        )
        self.assertEqual(
            comparison["calculation"]["historical_confidence_floor"],
            0.0,
        )
        target = comparison["forecast_horizons"]["3"]
        self.assertAlmostEqual(
            target["historical_target_price_divine"],
            2.0,
        )


if __name__ == "__main__":
    unittest.main()
