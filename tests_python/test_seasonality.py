from __future__ import annotations

import math
import unittest
from typing import Any

from poe_advisor.provenance import HISTORICAL_PRICE_SOURCES
from poe_advisor.seasonality import SeasonalModel


class FakeSeasonalStorage:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        entry_rows: list[dict[str, Any]] | None = None,
    ):
        self.rows = rows
        self.calls: list[tuple[int, int, list[str] | None]] = []
        self.entry_rows = entry_rows if entry_rows is not None else rows
        self.entry_calls: list[tuple[int, list[str] | None]] = []

    def seasonal_return_rows(
        self,
        league_day: int,
        horizon: int,
        item_keys: list[str] | None = None,
        *,
        sources: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        self.return_sources = sources
        self.calls.append((league_day, horizon, item_keys))
        allowed = set(item_keys or [])
        return [
            row
            for row in self.rows
            if not allowed or str(row["item_key"]) in allowed
        ]

    def seasonal_entry_rows(
        self,
        league_day: int,
        item_keys: list[str] | None = None,
        *,
        sources: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        self.entry_sources = sources
        self.entry_calls.append((league_day, item_keys))
        allowed = set(item_keys or [])
        return [
            row
            for row in self.entry_rows
            if not allowed or str(row["item_key"]) in allowed
        ]


def row(
    *,
    league: str,
    ratio: float,
    entry: float = 1.0,
    item_key: str = "currency:mirror-shard",
    confidence: float = 0.9,
    start_at: str = "",
) -> dict[str, Any]:
    return {
        "league_id": league,
        "league_name": league,
        "league_start_at": start_at,
        "item_key": item_key,
        "entry_divine": entry,
        "exit_divine": entry * ratio,
        "entry_confidence": confidence,
        "exit_confidence": confidence,
    }


class SeasonalModelTests(unittest.TestCase):
    def test_four_league_median_is_item_specific_and_league_aligned(self) -> None:
        storage = FakeSeasonalStorage(
            [
                row(league="Settlers", ratio=1.10),
                row(league="Mercenaries", ratio=1.20),
                row(league="Keepers", ratio=1.30),
                row(league="Mirage", ratio=0.90),
                row(
                    league="Mirage",
                    ratio=9.0,
                    item_key="currency:unrelated",
                ),
            ]
        )
        signal = SeasonalModel(storage).signals(
            league_day=12,
            horizon=7,
            item_keys=["currency:mirror-shard"],
        )["currency:mirror-shard"]

        self.assertEqual(
            storage.calls,
            [
                (12, 7, ["currency:mirror-shard"]),
                (12, 21, ["currency:mirror-shard"]),
            ],
        )
        self.assertEqual(
            storage.entry_calls,
            [(12, ["currency:mirror-shard"])],
        )
        self.assertEqual(storage.return_sources, HISTORICAL_PRICE_SOURCES)
        self.assertEqual(storage.entry_sources, HISTORICAL_PRICE_SOURCES)
        self.assertEqual(signal.status, "ok")
        self.assertEqual(signal.sample_leagues, 4)
        expected_median = math.expm1(
            (math.log(1.10) + math.log(1.20)) / 2
        )
        self.assertAlmostEqual(signal.median_return, expected_median)
        self.assertEqual(signal.positive_rate, 0.75)
        self.assertEqual(signal.appreciation_status, "appreciating")
        self.assertEqual(signal.appreciation_horizon_days, 21)
        self.assertEqual(signal.appreciation_sample_leagues, 4)
        self.assertGreater(signal.recency_weighted_return, 0)
        self.assertEqual(signal.average_entry_price, 1.0)
        self.assertEqual(signal.median_entry_price, 1.0)
        self.assertEqual(signal.entry_dispersion, 0.0)
        self.assertEqual(signal.level_confidence, 1.0)
        self.assertGreater(signal.model_weight, 0)
        self.assertLessEqual(signal.model_weight, 0.65)

    def test_same_day_price_level_preserves_unweighted_audit_and_tracks_spread(
        self,
    ) -> None:
        storage = FakeSeasonalStorage(
            [
                row(league="Settlers", entry=1.0, ratio=1.10),
                row(league="Mercenaries", entry=1.5, ratio=1.10),
                row(league="Keepers", entry=2.0, ratio=1.10),
                row(league="Mirage", entry=3.5, ratio=1.10),
            ]
        )

        signal = SeasonalModel(storage).signals(
            league_day=9,
            horizon=7,
            item_keys=["currency:mirror-shard"],
        )["currency:mirror-shard"]

        self.assertEqual(signal.status, "ok")
        self.assertAlmostEqual(signal.average_entry_price, 2.0)
        self.assertIsNotNone(signal.recency_weighted_entry_price)
        self.assertAlmostEqual(signal.median_entry_price, 1.75)
        self.assertGreater(signal.entry_dispersion, 0)
        self.assertLess(signal.level_confidence, 1)
        serialized = signal.as_dict()
        self.assertAlmostEqual(
            serialized["historical_average_divine"],
            2.0,
        )
        self.assertEqual(
            serialized["historical_recency_weighted_divine"],
            signal.recency_weighted_entry_price,
        )
        self.assertIn("historical_level_dispersion", serialized)

    def test_recency_weighting_preserves_missing_completed_league_gaps(
        self,
    ) -> None:
        storage = FakeSeasonalStorage(
            [
                row(
                    league="Affliction",
                    entry=10.0,
                    ratio=1.10,
                    start_at="2023-12-08T19:00:00Z",
                ),
                row(
                    league="Settlers",
                    entry=4.0,
                    ratio=1.10,
                    start_at="2024-07-26T20:00:00Z",
                ),
                row(
                    league="Mirage",
                    entry=1.0,
                    ratio=1.10,
                    start_at="2026-03-06T19:00:00Z",
                ),
            ]
        )

        signal = SeasonalModel(storage).signals(
            league_day=9,
            horizon=7,
            item_keys=["currency:mirror-shard"],
        )["currency:mirror-shard"]

        decay = SeasonalModel.RECENCY_DECAY_PER_LEAGUE
        expected = (
            1.0 + 4.0 * decay**3 + 10.0 * decay**5
        ) / (1.0 + decay**3 + decay**5)
        self.assertAlmostEqual(signal.average_entry_price, 5.0)
        self.assertAlmostEqual(
            signal.recency_weighted_entry_price,
            expected,
        )
        self.assertEqual(
            [entry["league"] for entry in signal.league_weights],
            ["Mirage", "Settlers", "Affliction"],
        )
        self.assertEqual(
            [entry["age_rank"] for entry in signal.league_weights],
            [0, 3, 5],
        )
        self.assertAlmostEqual(
            sum(
                entry["normalized_weight"]
                for entry in signal.league_weights
            ),
            1.0,
        )

    def test_same_day_level_uses_league_without_requested_horizon_pair(
        self,
    ) -> None:
        level_rows = [
            row(league="Affliction", entry=6.0, ratio=1.01),
            row(league="Necropolis", entry=5.0, ratio=1.02),
            row(league="Settlers", entry=4.0, ratio=9.99),
            row(league="Mercenaries", entry=3.0, ratio=1.04),
            row(league="Keepers", entry=2.0, ratio=1.05),
            row(league="Mirage", entry=1.0, ratio=1.06),
        ]
        forward_rows = [
            observation
            for observation in level_rows
            if observation["league_id"] != "Settlers"
        ]
        storage = FakeSeasonalStorage(
            forward_rows,
            entry_rows=level_rows,
        )

        signal = SeasonalModel(storage).signals(
            league_day=7,
            horizon=7,
            item_keys=["currency:mirror-shard"],
        )["currency:mirror-shard"]

        decay = SeasonalModel.RECENCY_DECAY_PER_LEAGUE
        expected_level = sum(
            price * decay**age_rank
            for age_rank, price in enumerate([1, 2, 3, 4, 5, 6])
        ) / sum(decay**age_rank for age_rank in range(6))
        forward_ratios_by_age = {
            0: 1.06,
            1: 1.05,
            2: 1.04,
            4: 1.02,
            5: 1.01,
        }
        expected_forward = math.expm1(
            sum(
                math.log(ratio) * decay**age_rank
                for age_rank, ratio in forward_ratios_by_age.items()
            )
            / sum(
                decay**age_rank for age_rank in forward_ratios_by_age
            )
        )

        self.assertEqual(signal.status, "ok")
        self.assertEqual(signal.level_sample_leagues, 6)
        self.assertEqual(signal.sample_leagues, 5)
        self.assertAlmostEqual(signal.average_entry_price, 3.5)
        self.assertAlmostEqual(
            signal.recency_weighted_entry_price,
            expected_level,
        )
        self.assertAlmostEqual(
            signal.recency_weighted_return,
            expected_forward,
        )
        self.assertEqual(len(signal.league_weights), 6)
        settlers = next(
            observation
            for observation in signal.league_weights
            if observation["league_id"] == "Settlers"
        )
        self.assertEqual(settlers["age_rank"], 3)
        self.assertEqual(settlers["entry_divine"], 4.0)
        self.assertEqual(
            signal.as_dict()["historical_level_sample_leagues"],
            6,
        )

    def test_missing_or_low_confidence_leagues_fail_closed(self) -> None:
        storage = FakeSeasonalStorage(
            [
                row(league="Settlers", ratio=1.10),
                row(league="Mercenaries", ratio=1.20),
                row(league="Keepers", ratio=1.30, confidence=0.2),
            ]
        )
        signal = SeasonalModel(storage).signals(
            league_day=5,
            horizon=3,
            item_keys=["currency:mirror-shard"],
        )["currency:mirror-shard"]

        self.assertEqual(signal.status, "insufficient_leagues")
        self.assertEqual(signal.sample_leagues, 2)
        self.assertEqual(signal.model_weight, 0)

    def test_one_extreme_price_level_outlier_is_marked_unstable(self) -> None:
        storage = FakeSeasonalStorage(
            [
                row(league="Mercenaries", entry=90.0, ratio=1.10),
                row(league="Keepers", entry=1.3, ratio=1.10),
                row(league="Mirage", entry=1.5, ratio=1.10),
            ]
        )

        signal = SeasonalModel(storage).signals(
            league_day=6,
            horizon=7,
            item_keys=["currency:mirror-shard"],
        )["currency:mirror-shard"]

        self.assertEqual(signal.sample_leagues, 3)
        self.assertEqual(signal.status, "unstable_level")
        self.assertGreater(signal.average_entry_price, 30)
        self.assertLess(signal.median_entry_price, 2)
        self.assertGreater(signal.entry_dispersion, math.log(4))
        self.assertGreater(signal.entry_mean_median_skew, math.log(4))
        self.assertLess(signal.level_confidence, 0.1)

    def test_one_observation_per_league_prevents_overweighting(self) -> None:
        storage = FakeSeasonalStorage(
            [
                row(league="Settlers", ratio=1.01),
                row(league="Settlers", ratio=1.50),
                row(league="Mercenaries", ratio=1.10),
                row(league="Keepers", ratio=1.10),
            ]
        )
        signal = SeasonalModel(storage).signals(
            league_day=8,
            horizon=14,
            item_keys=["currency:mirror-shard"],
        )["currency:mirror-shard"]

        self.assertEqual(signal.sample_leagues, 3)
        self.assertEqual(signal.status, "provisional")


if __name__ == "__main__":
    unittest.main()
