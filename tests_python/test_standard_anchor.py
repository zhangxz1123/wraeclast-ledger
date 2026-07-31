from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any

from poe_advisor.models import League, iso_utc, utc_now
from poe_advisor.provenance import (
    CURRENT_PRICE_SOURCES,
    STANDARD_PRICE_SOURCES,
)
from poe_advisor.recommendation import RecommendationEngine
from poe_advisor.seasonality import SeasonalSignal


ITEM_KEY = "currency:standard-anchor-fixture"


class FixtureStorage:
    def __init__(self) -> None:
        now = utc_now()
        self.rows = [
            {
                "item_key": ITEM_KEY,
                "name": "Standard Anchor Fixture",
                "category": "Currency",
                "source": "poe.ninja",
                "observed_at": iso_utc(now - timedelta(days=11 - index)),
                "chaos_value": 100.0,
                "divine_value": 0.5,
                "listing_count": 1000,
                "volume": 10000,
                "confidence": 0.95,
                "details": {},
            }
            for index in range(12)
        ]
        self.standard: dict[str, dict[str, Any]] = {}

    def item_histories(
        self,
        league_id: str,
        *,
        days: int,
        sources: tuple[str, ...] | None = None,
    ) -> dict[str, list]:
        del league_id, days
        self.requested_current_sources = sources
        return {ITEM_KEY: self.rows}

    def latest_item_prices(
        self,
        league_id: str,
        *,
        sources: tuple[str, ...] | None = None,
    ) -> dict[str, dict[str, Any]]:
        self.requested_anchor_league = league_id
        self.requested_anchor_sources = sources
        return self.standard

    def save_recommendations(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("persist=False must not write recommendations")


class FixtureSeasonalModel:
    MIN_SAMPLE_LEAGUES = 3
    FULL_SAMPLE_LEAGUES = 4
    RECENCY_DECAY_PER_LEAGUE = 0.72

    def signals(self, **_: Any) -> dict[str, SeasonalSignal]:
        return {
            ITEM_KEY: SeasonalSignal(
                item_key=ITEM_KEY,
                status="ok",
                league_day=10,
                horizon_days=7,
                sample_leagues=4,
                average_entry_price=1.0,
                recency_weighted_entry_price=1.0,
                median_entry_price=1.0,
                entry_dispersion=0.03,
                level_confidence=0.95,
                median_return=0.20,
                recency_weighted_return=0.20,
                dispersion=0.03,
                p25_return=0.15,
                p75_return=0.25,
                positive_rate=1.0,
                confidence=0.95,
                model_weight=0.95,
                leagues=("A", "B", "C", "D"),
                appreciation_status="appreciating",
                appreciation_horizon_days=21,
                appreciation_sample_leagues=4,
                appreciation_median_return=0.40,
                appreciation_recency_weighted_return=0.40,
                appreciation_positive_rate=1.0,
                appreciation_confidence=0.95,
            )
        }


class FixtureMetaService:
    def latest_profile(self, league_id: str) -> None:
        del league_id
        return None


class StandardAnchorRecommendationTests(unittest.TestCase):
    def test_anchor_is_exact_context_and_does_not_change_short_term_result(
        self,
    ) -> None:
        storage = FixtureStorage()
        engine = RecommendationEngine(
            storage,  # type: ignore[arg-type]
            meta_service=FixtureMetaService(),  # type: ignore[arg-type]
        )
        engine.seasonal_model = FixtureSeasonalModel()  # type: ignore[assignment]
        now = utc_now()
        league = League(
            id="Current Fixture",
            name="Current Fixture",
            start_at=iso_utc(now - timedelta(days=9)),
        )

        without_anchor = engine.generate(
            league,
            budget=100,
            horizon=7,
            persist=False,
        )
        self.assertEqual(len(without_anchor["recommendations"]), 1)
        baseline = without_anchor["recommendations"][0]

        storage.standard = {
            ITEM_KEY: {
                "item_key": ITEM_KEY,
                "name": "Standard Anchor Fixture",
                "category": "Currency",
                "observed_at": "2026-07-30T06:00:00Z",
                "chaos_value": 400.0,
                "divine_value": 2.0,
                "listing_count": 500,
                "volume": 1000,
                "confidence": 0.9,
                "source": "poe.ninja",
            },
            # A similar name under a different key must never be matched.
            "currency:standard-anchor-fixture-other-variant": {
                "item_key": "currency:standard-anchor-fixture-other-variant",
                "name": "Standard Anchor Fixture",
                "category": "Currency",
                "observed_at": "2026-07-30T06:00:00Z",
                "chaos_value": 999.0,
                "divine_value": 9.99,
                "source": "poe.ninja",
            },
        }
        with_anchor = engine.generate(
            league,
            budget=100,
            horizon=7,
            persist=False,
        )
        anchored = with_anchor["recommendations"][0]

        for field in (
            "rank",
            "allocation_divine",
            "quantity",
            "expected_return_pct",
            "target_divine",
            "entry_ceiling_divine",
            "stop_divine",
        ):
            self.assertEqual(anchored[field], baseline[field], field)
        self.assertEqual(anchored["standard_anchor_divine"], 2.0)
        self.assertEqual(anchored["standard_anchor_gap"], 0.75)
        self.assertEqual(anchored["standard_anchor_ratio"], 4.0)
        self.assertEqual(
            anchored["standard_anchor_observed_at"],
            "2026-07-30T06:00:00Z",
        )
        self.assertEqual(anchored["standard_anchor_source"], "poe.ninja")
        self.assertIn("does not affect short-term ranking", anchored["factors"][-1])
        self.assertTrue(with_anchor["standard_model"]["available"])
        self.assertFalse(
            with_anchor["standard_model"]["affects_short_term_ranking"]
        )
        self.assertFalse(
            with_anchor["standard_model"]["affects_expected_return"]
        )
        self.assertEqual(with_anchor["standard_model"]["matched_items"], 1)
        self.assertEqual(storage.requested_anchor_league, "Standard")
        self.assertEqual(
            storage.requested_current_sources,
            CURRENT_PRICE_SOURCES,
        )
        self.assertEqual(
            storage.requested_anchor_sources,
            STANDARD_PRICE_SOURCES,
        )


if __name__ == "__main__":
    unittest.main()
