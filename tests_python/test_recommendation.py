from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from poe_advisor.demo import DEMO_ASSETS, seed_demo
from poe_advisor.historical import BROADLY_COVERED_LEAGUES
from poe_advisor.models import League, PricePoint, iso_utc, utc_now
from poe_advisor.recommendation import RecommendationEngine
from poe_advisor.storage import Storage


class RecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.storage = Storage(
            Path(self.temporary_directory.name) / "recommendations.sqlite3"
        )
        self.seed_result = seed_demo(
            self.storage,
            days=45,
            now=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )
        self.league = self.storage.get_current_league()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_demo_seed_is_complete_deterministic_and_labelled(self) -> None:
        self.assertIsNotNone(self.league)
        self.assertTrue(self.league.is_demo)
        self.assertEqual(self.seed_result["assets"], len(DEMO_ASSETS))
        self.assertEqual(
            self.seed_result["rows_written"],
            len(DEMO_ASSETS) * 45,
        )
        counts = self.storage.status_counts(self.league.id)
        self.assertEqual(counts["snapshots"], 1)
        self.assertEqual(counts["price_points"], len(DEMO_ASSETS) * 45)
        snapshot = self.storage.read_snapshot(1)
        self.assertIn(b"offline-demo-fixture", snapshot)
        self.assertIn(b"not live market prices", snapshot)

    def test_recommendations_are_numeric_priority_ranked_without_allocation(
        self,
    ) -> None:
        payload = RecommendationEngine(self.storage).generate(
            self.league,
            budget=100,
            horizon=7,
            persist=False,
        )

        self.assertEqual(payload["budget"], 100.0)
        self.assertEqual(payload["horizon"], 7)
        self.assertTrue(payload["league"]["demo"])
        self.assertIn("not live prices", payload["confidence_note"])
        self.assertGreater(len(payload["recommendations"]), 0)
        self.assertEqual(payload["mode"], "priority_ranking")
        self.assertEqual(payload["allocation_mode"], "none")
        self.assertFalse(payload["budget_affects_ranking"])
        self.assertIsNone(payload["invested"])
        self.assertIsNone(payload["reserve"])
        self.assertLessEqual(len(payload["rankings"]), 100)
        self.assertEqual(payload["ranking_summary"]["limit"], 100)
        self.assertEqual(
            payload["ranking_summary"]["returned"],
            len(payload["rankings"]),
        )
        qualified_prefix = payload["rankings"][
            : len(payload["recommendations"])
        ]
        self.assertEqual(
            [idea["key"] for idea in qualified_prefix],
            [idea["key"] for idea in payload["recommendations"]],
        )
        self.assertTrue(
            all(
                idea["eligibility_status"] == "qualified"
                and idea["eligible_for_recommendation"]
                for idea in qualified_prefix
            )
        )

        for expected_rank, recommendation in enumerate(
            payload["recommendations"],
            start=1,
        ):
            with self.subTest(name=recommendation["name"]):
                self.assertEqual(recommendation["rank"], expected_rank)
                self.assertIsNone(recommendation["quantity"])
                self.assertIsNone(recommendation["allocation_divine"])
                self.assertIsNone(recommendation["position_unit_cap"])
                self.assertEqual(
                    recommendation["curve_key"],
                    recommendation["key"],
                )
                self.assertEqual(
                    recommendation["current_price_divine"],
                    recommendation["price_divine"],
                )
                for key in (
                    "price_divine",
                    "priority_score",
                    "expected_return_pct",
                    "confidence",
                    "confidence_score",
                    "liquidity",
                    "liquidity_score",
                    "entry_ceiling_divine",
                    "target_divine",
                    "stop_divine",
                ):
                    self.assertIsInstance(recommendation[key], (int, float), key)
                self.assertGreaterEqual(recommendation["expected_return_pct"], 7)
                self.assertGreater(recommendation["target_divine"], recommendation["price_divine"])
                self.assertGreater(recommendation["entry_ceiling_divine"], recommendation["price_divine"])
                self.assertLess(recommendation["stop_divine"], recommendation["price_divine"])
                self.assertGreaterEqual(recommendation["confidence_score"], 0)
                self.assertLessEqual(recommendation["confidence_score"], 1)
                self.assertGreaterEqual(recommendation["liquidity_score"], 0)
                self.assertLessEqual(recommendation["liquidity_score"], 1)
                self.assertNotIn(
                    recommendation["name"].lower(),
                    {"chaos orb", "divine orb"},
                )
                self.assertGreaterEqual(len(recommendation["history"]), 5)

    def test_budget_and_horizon_are_clamped(self) -> None:
        payload = RecommendationEngine(self.storage).generate(
            self.league,
            budget=-50,
            horizon=99,
            persist=False,
        )
        self.assertEqual(payload["budget"], 1.0)
        self.assertEqual(payload["horizon"], 30)
        self.assertFalse(payload["budget_affects_ranking"])
        self.assertIsNone(payload["reserve"])
        self.assertIsNone(payload["invested"])

    def test_partial_sync_membership_cutoff_rejects_stale_current_rows(
        self,
    ) -> None:
        now = utc_now()
        live = League(
            id="Partial Membership Fixture",
            name="Partial Membership Fixture",
            start_at=iso_utc(now - timedelta(days=7)),
        )
        self.storage.upsert_league(live, current=True)
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=live.id,
                    item_key="basetype:stale-variant",
                    name="Stale Variant",
                    category="BaseType",
                    source="poe.ninja",
                    observed_at=iso_utc(now - timedelta(minutes=30)),
                    chaos_value=200.0,
                    divine_value=1.0,
                    listing_count=10,
                    volume=10.0,
                    confidence=0.95,
                ),
                PricePoint(
                    league_id=live.id,
                    item_key="basetype:refreshed-variant",
                    name="Refreshed Variant",
                    category="BaseType",
                    source="poe.ninja",
                    observed_at=iso_utc(now),
                    chaos_value=200.0,
                    divine_value=1.0,
                    listing_count=10,
                    volume=10.0,
                    confidence=0.95,
                ),
            ]
        )
        successful_run = self.storage.start_sync_run(live.id)
        self.storage.finish_sync_run(
            successful_run,
            status="success",
            rows_written=2,
            snapshots_written=1,
            message="older complete fixture sync",
            warnings=[],
        )
        partial_run = self.storage.start_sync_run(live.id)
        self.storage.finish_sync_run(
            partial_run,
            status="partial",
            rows_written=1,
            snapshots_written=1,
            message="newer partial fixture sync",
            warnings=["one category failed"],
        )
        with self.storage.transaction() as connection:
            connection.execute(
                """
                UPDATE sync_runs
                SET started_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    iso_utc(now - timedelta(minutes=60)),
                    iso_utc(now - timedelta(minutes=50)),
                    successful_run,
                ),
            )
            connection.execute(
                """
                UPDATE sync_runs
                SET started_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    iso_utc(now - timedelta(minutes=10)),
                    iso_utc(now - timedelta(minutes=5)),
                    partial_run,
                ),
            )

        payload = RecommendationEngine(self.storage).generate(
            live,
            budget=100,
            horizon=7,
            persist=False,
        )

        self.assertEqual(
            [row["key"] for row in payload["rankings"]],
            ["basetype:refreshed-variant"],
        )
        self.assertEqual(
            payload["investment_scope"]["excluded_stale_current_items"],
            1,
        )

    def test_budget_does_not_filter_or_rerank_candidates(self) -> None:
        engine = RecommendationEngine(self.storage)
        small_budget = engine.generate(
            self.league,
            budget=1,
            horizon=7,
            persist=False,
        )
        large_budget = engine.generate(
            self.league,
            budget=100000,
            horizon=7,
            persist=False,
        )

        self.assertEqual(
            [
                (idea["key"], idea["rank"], idea["priority_score"])
                for idea in small_budget["rankings"]
            ],
            [
                (idea["key"], idea["rank"], idea["priority_score"])
                for idea in large_budget["rankings"]
            ],
        )
        self.assertEqual(small_budget["budget"], 1.0)
        self.assertEqual(large_budget["budget"], 100000.0)
        self.assertFalse(small_budget["budget_affects_ranking"])

    def test_combined_priority_ranking_is_capped_at_one_hundred(self) -> None:
        engine = RecommendationEngine(self.storage)
        research = [
            {
                "rank": 0,
                "key": f"currency:research-{index}",
                "name": f"Research {index}",
                "category": "Currency",
                "eligibility_status": "research",
                "eligible_for_recommendation": False,
            }
            for index in range(120)
        ]
        with patch.object(
            engine,
            "_rank_research_candidates",
            return_value=research,
        ):
            payload = engine.generate(
                self.league,
                budget=100,
                horizon=7,
                persist=False,
            )

        self.assertEqual(len(payload["rankings"]), 100)
        self.assertEqual(payload["rankings"][-1]["rank"], 100)
        self.assertEqual(payload["ranking_summary"]["research_total"], 120)
        self.assertEqual(payload["ranking_summary"]["returned"], 100)

    def test_live_idea_requires_and_exposes_item_specific_seasonality(self) -> None:
        now = utc_now()
        live = League(
            id="Live Fixture",
            name="Live Fixture",
            start_at=iso_utc(now - timedelta(days=30, hours=1)),
        )
        self.storage.upsert_league(live, current=True)
        item_key = "currency:mirror-shard"
        current_points = []
        for index in range(12):
            current_points.append(
                PricePoint(
                    league_id=live.id,
                    item_key=item_key,
                    name="Mirror Shard",
                    category="Currency",
                    source="poe.ninja",
                    observed_at=iso_utc(now - timedelta(days=11 - index)),
                    chaos_value=100.0,
                    divine_value=0.98 if index == 11 else 1.0,
                    listing_count=1000,
                    volume=10000,
                    confidence=0.95,
                )
            )
        self.storage.insert_price_points(current_points)
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.ninja-history",
                    "source_item_id": "fixture-1",
                    "item_key": item_key,
                    "name": "Mirror Shard",
                    "category": "Currency",
                    "current_daily": 10000,
                    "eligible": True,
                }
            ]
        )
        target_day = live.day
        self.assertIsNotNone(target_day)
        for index, spec in enumerate(BROADLY_COVERED_LEAGUES):
            entry_divine = 1.6 - 0.2 * index
            historical = spec.as_league()
            self.storage.upsert_league(historical, current=False)
            self.storage.upsert_seasonal_prices(
                [
                    {
                        "league_id": historical.id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "fixture-1",
                        "league_day": target_day,
                        "observed_at": f"202{index + 1}-02-01T20:00:00Z",
                        "chaos_value": entry_divine * 100.0,
                        "divine_value": entry_divine,
                        "volume": 1000,
                        "confidence": 0.9,
                    },
                    {
                        "league_id": historical.id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "fixture-1",
                        "league_day": target_day + 7,
                        "observed_at": f"202{index + 1}-02-08T20:00:00Z",
                        "chaos_value": entry_divine * 130.0,
                        "divine_value": entry_divine * 1.3,
                        "volume": 1000,
                        "confidence": 0.9,
                    },
                    {
                        "league_id": historical.id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "fixture-1",
                        "league_day": target_day + 21,
                        "observed_at": f"202{index + 1}-02-22T20:00:00Z",
                        "chaos_value": entry_divine * 150.0,
                        "divine_value": entry_divine * 1.5,
                        "volume": 1000,
                        "confidence": 0.9,
                    },
                ]
            )

        payload = RecommendationEngine(self.storage).generate(
            live,
            budget=100,
            horizon=7,
            persist=False,
        )

        self.assertEqual(payload["mode"], "forecast_ranking")
        self.assertEqual(len(payload["recommendations"]), 1)
        idea = payload["recommendations"][0]
        self.assertEqual(idea["historical_sample_leagues"], 4)
        decay = payload["forecast_model"]["recency_decay_per_league"]
        expected_target = (
            1.0 * 1.3
            + 1.2 * 1.3 * decay
            + 1.4 * 1.3 * decay**2
            + 1.6 * 1.3 * decay**3
        ) / (1.0 + decay + decay**2 + decay**3)
        self.assertAlmostEqual(
            idea["historical_target_price_divine"],
            expected_target,
            places=5,
        )
        self.assertEqual(
            [
                entry["age_rank"]
                for entry in idea["forecast_7d"][
                    "historical_observations"
                ]
            ],
            [0, 1, 2, 3],
        )
        self.assertGreater(idea["expected_gain"], 0)

    def test_larger_same_day_undervaluation_ranks_ahead_of_faster_momentum(
        self,
    ) -> None:
        now = utc_now()
        live = League(
            id="Valuation Ranking",
            name="Valuation Ranking",
            start_at=iso_utc(now - timedelta(days=30, hours=1)),
        )
        self.storage.upsert_league(live, current=True)
        items = [
            (
                "currency:deep-discount",
                "Deep Discount Asset",
                "asset-a",
                2.0,
                1.05,
            ),
            (
                "currency:fast-forward",
                "Fast Forward Asset",
                "asset-b",
                1.1,
                1.50,
            ),
        ]
        current_points = []
        for item_key, name, _, _, _ in items:
            for index in range(12):
                current_points.append(
                    PricePoint(
                        league_id=live.id,
                        item_key=item_key,
                        name=name,
                        category="Currency",
                        source="poe.ninja",
                        observed_at=iso_utc(
                            now - timedelta(days=11 - index)
                        ),
                        chaos_value=100.0,
                        divine_value=1.0,
                        listing_count=1000,
                        volume=10000,
                        confidence=0.95,
                    )
                )
        self.storage.insert_price_points(current_points)
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.ninja-history",
                    "source_item_id": source_id,
                    "item_key": item_key,
                    "name": name,
                    "category": "Currency",
                    "current_daily": 10000,
                    "eligible": True,
                }
                for item_key, name, source_id, _, _ in items
            ]
        )
        target_day = live.day
        self.assertIsNotNone(target_day)
        for index, spec in enumerate(BROADLY_COVERED_LEAGUES):
            historical = spec.as_league()
            self.storage.upsert_league(historical, current=False)
            rows = []
            for item_key, _, source_id, entry, forward_ratio in items:
                rows.extend(
                    [
                        {
                            "league_id": historical.id,
                            "item_key": item_key,
                            "source": "poe.ninja-history",
                            "source_item_id": source_id,
                            "league_day": target_day,
                            "observed_at": (
                                f"202{index + 1}-02-01T20:00:00Z"
                            ),
                            "chaos_value": entry * 100,
                            "divine_value": entry,
                            "volume": 1000,
                            "confidence": 0.9,
                        },
                        {
                            "league_id": historical.id,
                            "item_key": item_key,
                            "source": "poe.ninja-history",
                            "source_item_id": source_id,
                            "league_day": target_day + 7,
                            "observed_at": (
                                f"202{index + 1}-02-08T20:00:00Z"
                            ),
                            "chaos_value": entry * forward_ratio * 100,
                            "divine_value": entry * forward_ratio,
                            "volume": 1000,
                            "confidence": 0.9,
                        },
                        {
                            "league_id": historical.id,
                            "item_key": item_key,
                            "source": "poe.ninja-history",
                            "source_item_id": source_id,
                            "league_day": target_day + 21,
                            "observed_at": (
                                f"202{index + 1}-02-22T20:00:00Z"
                            ),
                            "chaos_value": entry * 140,
                            "divine_value": entry * 1.4,
                            "volume": 1000,
                            "confidence": 0.9,
                        },
                    ]
                )
            self.storage.upsert_seasonal_prices(rows)

        payload = RecommendationEngine(self.storage).generate(
            live,
            budget=100,
            horizon=7,
            persist=False,
        )

        self.assertGreaterEqual(len(payload["recommendations"]), 2)
        self.assertEqual(
            payload["recommendations"][0]["name"],
            "Deep Discount Asset",
        )
        self.assertEqual(
            payload["recommendations"][1]["name"],
            "Fast Forward Asset",
        )
        self.assertGreater(
            payload["recommendations"][0]["historical_discount_pct"],
            payload["recommendations"][1]["historical_discount_pct"],
        )
        self.assertGreater(
            payload["recommendations"][0]["expected_gain"],
            payload["recommendations"][1]["expected_gain"],
        )

    def test_negative_forward_history_waits_despite_large_undervaluation(
        self,
    ) -> None:
        now = utc_now()
        live = League(
            id="Negative Forward Fixture",
            name="Negative Forward Fixture",
            start_at=iso_utc(now - timedelta(days=30, hours=1)),
        )
        self.storage.upsert_league(live, current=True)
        item_key = "currency:early-entry-trap"
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=live.id,
                    item_key=item_key,
                    name="Early Entry Trap",
                    category="Currency",
                    source="poe.ninja",
                    observed_at=iso_utc(now - timedelta(days=11 - index)),
                    chaos_value=20.0,
                    divine_value=0.2,
                    listing_count=1000,
                    volume=10000,
                    confidence=0.95,
                )
                for index in range(12)
            ]
        )
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.ninja-history",
                    "source_item_id": "negative-forward",
                    "item_key": item_key,
                    "name": "Early Entry Trap",
                    "category": "Currency",
                    "current_daily": 10000,
                    "eligible": True,
                }
            ]
        )
        target_day = live.day
        self.assertIsNotNone(target_day)
        for index, spec in enumerate(BROADLY_COVERED_LEAGUES):
            past = spec.as_league()
            self.storage.upsert_league(past, current=False)
            self.storage.upsert_seasonal_prices(
                [
                    {
                        "league_id": past.id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "negative-forward",
                        "league_day": target_day,
                        "observed_at": f"202{index + 1}-02-01T20:00:00Z",
                        "chaos_value": 100.0,
                        "divine_value": 1.0,
                        "volume": 1000,
                        "confidence": 0.9,
                    },
                    {
                        "league_id": past.id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "negative-forward",
                        "league_day": target_day + 7,
                        "observed_at": f"202{index + 1}-02-08T20:00:00Z",
                        "chaos_value": 10.0,
                        "divine_value": 0.1,
                        "volume": 1000,
                        "confidence": 0.9,
                    },
                    {
                        "league_id": past.id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "negative-forward",
                        "league_day": target_day + 21,
                        "observed_at": f"202{index + 1}-02-22T20:00:00Z",
                        "chaos_value": 140.0,
                        "divine_value": 1.4,
                        "volume": 1000,
                        "confidence": 0.9,
                    },
                ]
            )

        payload = RecommendationEngine(self.storage).generate(
            live,
            budget=100,
            horizon=7,
            persist=False,
        )

        item = next(
            row
            for row in payload["rankings"]
            if row["name"] == "Early Entry Trap"
        )
        self.assertLess(item["expected_gain"], 0)
        self.assertEqual(item["historical_sample_leagues"], 4)
        self.assertNotIn("eligibility_status", item)
        self.assertEqual(item["current_price_divine"], 0.2)
        self.assertEqual(item["curve_key"], "currency:early-entry-trap")

    def test_small_consumables_are_hidden_and_known_decline_is_vetoed(
        self,
    ) -> None:
        now = utc_now()
        live = League(
            id="Investment Scope Fixture",
            name="Investment Scope Fixture",
            start_at=iso_utc(now - timedelta(days=6)),
        )
        self.storage.upsert_league(live, current=True)
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=live.id,
                    item_key="oil:golden-oil",
                    name="Golden Oil",
                    category="Oil",
                    source="poe.ninja",
                    observed_at=iso_utc(now),
                    chaos_value=100.0,
                    divine_value=0.5,
                    listing_count=1000,
                    volume=10000,
                    confidence=0.95,
                ),
                PricePoint(
                    league_id=live.id,
                    item_key="currency:the-mavens-writ",
                    name="The Maven's Writ",
                    category="Currency",
                    source="poe.ninja",
                    observed_at=iso_utc(now),
                    chaos_value=200.0,
                    divine_value=1.0,
                    listing_count=1000,
                    volume=10000,
                    confidence=0.95,
                ),
            ]
        )

        payload = RecommendationEngine(self.storage).generate(
            live,
            budget=100,
            horizon=7,
            persist=False,
        )

        visible_names = {
            row["name"]
            for row in (
                payload["recommendations"] + payload["watchlist"]
            )
        }
        self.assertNotIn("Golden Oil", visible_names)
        self.assertNotIn("The Maven's Writ", visible_names)
        scope = payload["investment_scope"]
        self.assertEqual(scope["strategy"], "filtered_forecast_ranking")
        self.assertEqual(scope["excluded_category_items"], 1)
        self.assertEqual(scope["excluded_category_counts"], {"Oil": 1})
        self.assertEqual(len(scope["known_decline_vetoes"]), 1)
        self.assertEqual(
            scope["known_decline_vetoes"][0]["name"],
            "The Maven's Writ",
        )
        ranked_names = {row["name"] for row in payload["rankings"]}
        self.assertNotIn("Golden Oil", ranked_names)
        self.assertNotIn("The Maven's Writ", ranked_names)
        self.assertEqual(
            self.storage.status_counts(live.id)["price_points"],
            2,
        )

    def test_rank_formatter_does_not_size_cheap_items(self) -> None:
        candidate = {
            "key": "oil:cheap-bulk",
            "name": "Cheap Bulk",
            "category": "Oil",
            "price_divine": 0.001,
            "price_chaos": 0.2,
            "score": 0.5,
            "expected_return": 0.15,
            "confidence_score": 0.9,
            "liquidity_score": 0.9,
            "market_volume": 100000,
            "listing_count": None,
            "volatility": 0.02,
            "rationale": "Fixture.",
            "historical_fair_value_divine": 0.005,
            "historical_average_divine": 0.005,
            "historical_median_divine": 0.005,
            "historical_discount": 0.8,
            "historical_level_dispersion": 0.0,
            "historical_mean_median_skew": 0.0,
            "historical_level_confidence": 1.0,
            "historical_forward_return": 0.10,
            "meta_status": "not_applicable",
            "seasonal_status": "ok",
            "seasonal_sample_leagues": 4,
            "seasonal_median_return": 0.10,
            "seasonal_positive_rate": 1.0,
            "seasonal_confidence": 1.0,
            "seasonal_weight": 1.0,
            "seasonal_leagues": ["A", "B", "C", "D"],
            "history": [],
        }

        recommendations = RecommendationEngine(self.storage)._allocate(
            [candidate],
            budget=100,
            league_day=6,
        )

        self.assertEqual(len(recommendations), 1)
        self.assertIsNone(recommendations[0]["quantity"])
        self.assertIsNone(recommendations[0]["position_unit_cap"])
        self.assertIsNone(recommendations[0]["allocation_divine"])
        self.assertIn(
            "Priority ranking only",
            recommendations[0]["factors"][-1],
        )

    def test_price_and_category_caps_do_not_remove_ranked_candidates(
        self,
    ) -> None:
        candidate = {
            "key": "skillgem:awakened-enlighten-support",
            "name": "Awakened Enlighten Support",
            "category": "SkillGem",
            "price_divine": 37.0,
            "price_chaos": 7400.0,
            "score": 0.85,
            "expected_return": 0.25,
            "confidence_score": 0.9,
            "liquidity_score": 0.8,
            "market_volume": 100,
            "listing_count": 50,
            "volatility": 0.02,
            "rationale": "Strong fixture that already passed every gate.",
            "historical_fair_value_divine": 55.0,
            "historical_average_divine": 52.0,
            "historical_median_divine": 54.0,
            "historical_discount": 0.327,
            "historical_level_dispersion": 0.1,
            "historical_mean_median_skew": 0.04,
            "historical_level_confidence": 0.9,
            "historical_forward_return": 0.2,
            "meta_status": "not_applicable",
            "seasonal_status": "ok",
            "seasonal_sample_leagues": 4,
            "seasonal_median_return": 0.2,
            "seasonal_recency_weighted_return": 0.24,
            "seasonal_positive_rate": 0.75,
            "seasonal_confidence": 0.9,
            "seasonal_weight": 1.0,
            "seasonal_leagues": ["A", "B", "C", "D"],
            "appreciation_status": "ok",
            "appreciation_horizon_days": 21,
            "appreciation_sample_leagues": 4,
            "appreciation_median_return": 0.3,
            "appreciation_recency_weighted_return": 0.35,
            "appreciation_positive_rate": 0.75,
            "appreciation_confidence": 0.9,
            "history": [],
        }

        over_limit = {
            **candidate,
            "key": "skillgem:over-limit",
            "name": "Over Limit",
            "price_divine": 450.0,
        }
        second_same_category = {
            **candidate,
            "key": "skillgem:second-high-ticket",
            "name": "Second High Ticket Gem",
            "score": 0.8,
        }
        recommendations = RecommendationEngine(self.storage)._allocate(
            [over_limit, candidate, second_same_category],
            budget=1,
            league_day=6,
        )

        self.assertEqual(
            [idea["name"] for idea in recommendations],
            [
                "Over Limit",
                "Awakened Enlighten Support",
                "Second High Ticket Gem",
            ],
        )
        self.assertEqual(recommendations[0]["price_divine"], 450.0)
        self.assertTrue(
            all(idea["allocation_divine"] is None for idea in recommendations)
        )

    def test_forbidden_jewel_applies_current_meta_multiplier_to_targets(
        self,
    ) -> None:
        now = utc_now()
        live = League(
            id="Meta Fixture",
            name="Meta Fixture",
            start_at=iso_utc(now - timedelta(days=20, hours=1)),
        )
        self.storage.upsert_league(live, current=True)
        item_key = (
            "forbiddenjewel:forbidden-flesh-mastermind-of-discord-"
            "variant-forbidden-flesh"
        )
        self.storage.insert_price_points(
            [
                PricePoint(
                    league_id=live.id,
                    item_key=item_key,
                    name="Mastermind of Discord",
                    category="ForbiddenJewel",
                    source="poe.ninja",
                    observed_at=iso_utc(now - timedelta(days=11 - index)),
                    chaos_value=80.0,
                    divine_value=0.8,
                    listing_count=500,
                    volume=1000,
                    confidence=0.95,
                    details={
                        "metadata": {
                            "baseClass": "Witch",
                            "ascendancy": "Elementalist",
                            "passiveName": "Mastermind of Discord",
                        }
                    },
                )
                for index in range(12)
            ]
        )
        self.storage.upsert_historical_assets(
            [
                {
                    "source": "poe.ninja-history",
                    "source_item_id": "forbidden-fixture",
                    "item_key": item_key,
                    "name": "Mastermind of Discord",
                    "category": "ForbiddenJewel",
                    "current_daily": 1000,
                    "eligible": True,
                }
            ]
        )
        target_day = live.day
        self.assertIsNotNone(target_day)
        for index, spec in enumerate(BROADLY_COVERED_LEAGUES):
            past = spec.as_league()
            self.storage.upsert_league(past, current=False)
            self.storage.upsert_seasonal_prices(
                [
                    {
                        "league_id": past.id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "forbidden-fixture",
                        "league_day": target_day,
                        "observed_at": f"202{index + 1}-02-01T20:00:00Z",
                        "chaos_value": 100.0,
                        "divine_value": 1.0,
                        "volume": 1000,
                        "confidence": 0.9,
                    },
                    {
                        "league_id": past.id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "forbidden-fixture",
                        "league_day": target_day + 7,
                        "observed_at": f"202{index + 1}-02-08T20:00:00Z",
                        "chaos_value": 120.0,
                        "divine_value": 1.2,
                        "volume": 1000,
                        "confidence": 0.9,
                    },
                    {
                        "league_id": past.id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "forbidden-fixture",
                        "league_day": target_day + 21,
                        "observed_at": f"202{index + 1}-02-22T20:00:00Z",
                        "chaos_value": 140.0,
                        "divine_value": 1.4,
                        "volume": 1000,
                        "confidence": 0.9,
                    },
                ]
            )

        class StubMetaService:
            def ascendancy_multiplier(
                self,
                current_league,
                historical_leagues,
                ascendancy,
            ):
                del current_league, historical_leagues
                self.ascendancy = ascendancy
                return {
                    "status": "ok",
                    "ascendancy": ascendancy,
                    "current_share": 0.30,
                    "current_sample_size": 500,
                    "historical_share": 0.10,
                    "historical_sample_size": 2000,
                    "historical_league_count": 4,
                    "baseline_quality": "same-day",
                    "multiplier": 1.25,
                    "confidence": 1.0,
                    "source": "fixture",
                    "caveat": "fixture",
                }

        meta_service = StubMetaService()
        payload = RecommendationEngine(
            self.storage,
            meta_service=meta_service,
        ).generate(live, budget=100, horizon=7, persist=False)

        self.assertEqual(meta_service.ascendancy, "Elementalist")
        self.assertEqual(len(payload["recommendations"]), 1)
        idea = payload["recommendations"][0]
        self.assertAlmostEqual(
            idea["historical_same_day_price_divine"],
            1.0,
        )
        self.assertAlmostEqual(
            idea["historical_target_price_divine"],
            1.5,
        )
        self.assertAlmostEqual(
            idea["forecast_7d"]["raw_historical_target_divine"],
            1.2,
        )
        self.assertAlmostEqual(
            idea["meta_adjusted_same_day_price_divine"],
            1.25,
        )
        self.assertAlmostEqual(
            idea["forecast_7d"]["meta_adjusted_historical_target_divine"],
            1.5,
        )
        expected = math.exp(0.7 * math.log(1.875)) - 1.0
        self.assertAlmostEqual(idea["expected_gain"], expected)
        self.assertEqual(idea["meta_multiplier"], 1.25)
        self.assertEqual(idea["meta_signal"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
