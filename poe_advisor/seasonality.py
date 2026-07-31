from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .historical import COMPLETED_LEAGUES
from .provenance import HISTORICAL_PRICE_SOURCES
from .storage import Storage


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _percentile(values: list[float], fraction: float) -> float:
    """Return a linearly interpolated percentile for a small league sample."""

    if not values:
        raise ValueError("A percentile requires at least one value.")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = _clamp(fraction, 0.0, 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _group_return_rows(
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Validate rows and keep at most one observation per item/league."""

    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        try:
            entry = float(row["entry_divine"])
            exit_value = float(row["exit_divine"])
            entry_confidence = float(row.get("entry_confidence") or 0.0)
            exit_confidence = float(row.get("exit_confidence") or 0.0)
        except (TypeError, ValueError, KeyError):
            continue
        if (
            not math.isfinite(entry)
            or not math.isfinite(exit_value)
            or entry <= 0
            or exit_value <= 0
            or min(entry_confidence, exit_confidence) < 0.5
        ):
            continue
        league_id = str(row.get("league_id") or "")
        if not league_id:
            continue
        grouped[str(row["item_key"])][league_id] = {
            "entry_price": entry,
            "log_return": math.log(exit_value / entry),
            "league_name": str(row.get("league_name") or league_id),
            "league_id": league_id,
            "league_start_at": str(row.get("league_start_at") or ""),
        }
    return grouped


def _group_entry_rows(
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Validate same-day rows and keep one price level per item/league."""

    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        try:
            entry = float(row["entry_divine"])
            entry_confidence = float(row.get("entry_confidence") or 0.0)
        except (TypeError, ValueError, KeyError):
            continue
        if (
            not math.isfinite(entry)
            or entry <= 0
            or entry_confidence < 0.5
        ):
            continue
        league_id = str(row.get("league_id") or "")
        if not league_id:
            continue
        grouped[str(row["item_key"])][league_id] = {
            "entry_price": entry,
            "league_name": str(row.get("league_name") or league_id),
            "league_id": league_id,
            "league_start_at": str(row.get("league_start_at") or ""),
        }
    return grouped


@dataclass(frozen=True, slots=True)
class SeasonalSignal:
    item_key: str
    status: str
    league_day: int
    horizon_days: int
    sample_leagues: int
    level_sample_leagues: int = 0
    average_entry_price: float | None = None
    recency_weighted_entry_price: float | None = None
    median_entry_price: float | None = None
    entry_dispersion: float = 0.0
    entry_mean_median_skew: float = 0.0
    level_confidence: float = 0.0
    median_log_return: float = 0.0
    median_return: float = 0.0
    recency_weighted_log_return: float = 0.0
    recency_weighted_return: float = 0.0
    dispersion: float = 0.0
    p25_return: float = 0.0
    p75_return: float = 0.0
    positive_rate: float = 0.0
    confidence: float = 0.0
    model_weight: float = 0.0
    leagues: tuple[str, ...] = ()
    league_weights: tuple[dict[str, Any], ...] = ()
    appreciation_status: str = "insufficient_leagues"
    appreciation_horizon_days: int = 21
    appreciation_sample_leagues: int = 0
    appreciation_median_log_return: float = 0.0
    appreciation_median_return: float = 0.0
    appreciation_recency_weighted_log_return: float = 0.0
    appreciation_recency_weighted_return: float = 0.0
    appreciation_dispersion: float = 0.0
    appreciation_positive_rate: float = 0.0
    appreciation_confidence: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "league_day": self.league_day,
            "horizon_days": self.horizon_days,
            "sample_leagues": self.sample_leagues,
            "historical_level_sample_leagues": self.level_sample_leagues,
            "historical_average_divine": self.average_entry_price,
            "historical_recency_weighted_divine": (
                self.recency_weighted_entry_price
            ),
            "historical_median_divine": self.median_entry_price,
            "historical_level_dispersion": self.entry_dispersion,
            "historical_mean_median_skew": self.entry_mean_median_skew,
            "historical_level_confidence": self.level_confidence,
            "historical_forward_return": self.median_return,
            "median_return": self.median_return,
            "recency_weighted_return": self.recency_weighted_return,
            "dispersion": self.dispersion,
            "p25_return": self.p25_return,
            "p75_return": self.p75_return,
            "positive_rate": self.positive_rate,
            "confidence": self.confidence,
            "model_weight": self.model_weight,
            "leagues": list(self.leagues),
            "seasonal_league_weights": [
                dict(observation) for observation in self.league_weights
            ],
            "appreciation_status": self.appreciation_status,
            "appreciation_horizon_days": self.appreciation_horizon_days,
            "appreciation_sample_leagues": self.appreciation_sample_leagues,
            "appreciation_median_return": self.appreciation_median_return,
            "appreciation_recency_weighted_return": (
                self.appreciation_recency_weighted_return
            ),
            "appreciation_dispersion": self.appreciation_dispersion,
            "appreciation_positive_rate": self.appreciation_positive_rate,
            "appreciation_confidence": self.appreciation_confidence,
        }


class SeasonalModel:
    """Item-specific prior from the same league day in completed leagues."""

    MIN_SAMPLE_LEAGUES = 3
    FULL_SAMPLE_LEAGUES = 4
    APPRECIATION_HORIZON_DAYS = 21
    APPRECIATION_MIN_POSITIVE_RATE = 0.60
    # A league one release older receives 72% as much influence as the next
    # newer league. After normalization, the estimate remains in price units
    # and missing observations do not act like zero-valued prices.
    RECENCY_DECAY_PER_LEAGUE = 0.72
    _COMPLETED_LEAGUE_AGE_RANK = {
        spec.league_id.casefold(): age_rank
        for age_rank, spec in enumerate(reversed(COMPLETED_LEAGUES))
    }

    def __init__(self, storage: Storage):
        self.storage = storage

    def signals(
        self,
        *,
        league_day: int,
        horizon: int,
        item_keys: Iterable[str] | None = None,
    ) -> dict[str, SeasonalSignal]:
        day = max(1, int(league_day))
        hold = max(1, int(horizon))
        requested_keys = (
            [str(key) for key in item_keys] if item_keys is not None else None
        )
        query_keys = (
            requested_keys
            if requested_keys is not None and len(requested_keys) <= 800
            else None
        )
        rows = self.storage.seasonal_return_rows(
            day,
            hold,
            # Some SQLite builds cap bound variables at 999. Once the current
            # poe.ninja universe is larger than that, the indexed day/horizon
            # join is cheaper and safer than a giant IN clause.
            item_keys=query_keys,
            sources=HISTORICAL_PRICE_SOURCES,
        )
        grouped = _group_return_rows(rows)
        level_grouped = _group_entry_rows(
            self.storage.seasonal_entry_rows(
                day,
                item_keys=query_keys,
                sources=HISTORICAL_PRICE_SOURCES,
            )
        )
        if hold == self.APPRECIATION_HORIZON_DAYS:
            appreciation_grouped = grouped
        else:
            appreciation_grouped = _group_return_rows(
                self.storage.seasonal_return_rows(
                    day,
                    self.APPRECIATION_HORIZON_DAYS,
                    item_keys=query_keys,
                    sources=HISTORICAL_PRICE_SOURCES,
                )
            )

        signals: dict[str, SeasonalSignal] = {}
        requested = set(requested_keys or [])
        signal_keys = (
            requested
            if requested_keys is not None
            else set(grouped) | set(level_grouped)
        )
        for item_key in signal_keys:
            # ISO-8601 UTC timestamps sort chronologically. The name/id
            # tie-breakers keep fixture data and legacy rows without a known
            # start timestamp deterministic.
            forward_rows = sorted(
                grouped.get(item_key, {}).values(),
                key=lambda row: (
                    str(row.get("league_start_at") or ""),
                    str(row.get("league_name") or ""),
                    str(row.get("league_id") or ""),
                ),
                reverse=True,
            )
            level_rows = sorted(
                level_grouped.get(item_key, {}).values(),
                key=lambda row: (
                    str(row.get("league_start_at") or ""),
                    str(row.get("league_name") or ""),
                    str(row.get("league_id") or ""),
                ),
                reverse=True,
            )
            entry_prices = [
                float(row["entry_price"]) for row in level_rows
            ]
            returns = [float(row["log_return"]) for row in forward_rows]
            sample_count = len(returns)
            level_sample_count = len(entry_prices)
            appreciation_rows = sorted(
                appreciation_grouped.get(item_key, {}).values(),
                key=lambda row: (
                    str(row.get("league_start_at") or ""),
                    str(row.get("league_name") or ""),
                    str(row.get("league_id") or ""),
                ),
                reverse=True,
            )
            appreciation_returns = [
                float(row["log_return"]) for row in appreciation_rows
            ]
            appreciation_sample_count = len(appreciation_returns)
            appreciation_known_age_ranks = [
                self._COMPLETED_LEAGUE_AGE_RANK.get(
                    str(row["league_id"]).casefold()
                )
                for row in appreciation_rows
            ]
            appreciation_age_ranks = [
                (
                    int(known_rank)
                    if known_rank is not None
                    else fallback_rank
                )
                for fallback_rank, known_rank in enumerate(
                    appreciation_known_age_ranks
                )
            ]
            appreciation_raw_weights = [
                self.RECENCY_DECAY_PER_LEAGUE**age_rank
                for age_rank in appreciation_age_ranks
            ]
            appreciation_weight_total = sum(appreciation_raw_weights)
            appreciation_normalized_weights = [
                weight / appreciation_weight_total
                for weight in appreciation_raw_weights
            ]
            appreciation_recency_weighted_log = (
                sum(
                    value * weight
                    for value, weight in zip(
                        appreciation_returns,
                        appreciation_normalized_weights,
                    )
                )
                if appreciation_returns
                else 0.0
            )
            appreciation_recency_weighted_return = math.expm1(
                appreciation_recency_weighted_log
            )
            appreciation_median_log = (
                statistics.median(appreciation_returns)
                if appreciation_returns
                else 0.0
            )
            appreciation_median_return = math.expm1(
                appreciation_median_log
            )
            appreciation_positive_rate = (
                sum(value > 0 for value in appreciation_returns)
                / appreciation_sample_count
                if appreciation_sample_count
                else 0.0
            )
            appreciation_dispersion = (
                1.4826
                * statistics.median(
                    abs(value - appreciation_median_log)
                    for value in appreciation_returns
                )
                if appreciation_returns
                else 0.0
            )
            appreciation_count_quality = _clamp(
                appreciation_sample_count / self.FULL_SAMPLE_LEAGUES,
                0.0,
                1.0,
            )
            appreciation_dispersion_quality = 1.0 / (
                1.0 + (appreciation_dispersion / 0.35) ** 2
            )
            appreciation_direction_agreement = (
                abs(appreciation_positive_rate - 0.5) * 2.0
            )
            appreciation_confidence = _clamp(
                appreciation_count_quality
                * appreciation_dispersion_quality
                * (0.70 + 0.30 * appreciation_direction_agreement),
                0.0,
                1.0,
            )
            if appreciation_sample_count < self.MIN_SAMPLE_LEAGUES:
                appreciation_status = "insufficient_leagues"
            elif (
                appreciation_median_return > 0
                and appreciation_recency_weighted_return > 0
                and appreciation_positive_rate
                >= self.APPRECIATION_MIN_POSITIVE_RATE
            ):
                appreciation_status = "appreciating"
            else:
                appreciation_status = "not_appreciating"
            average_entry_price = (
                statistics.fmean(entry_prices) if entry_prices else None
            )
            level_known_age_ranks = [
                self._COMPLETED_LEAGUE_AGE_RANK.get(
                    str(row["league_id"]).casefold()
                )
                for row in level_rows
            ]
            # Production leagues use their positions in the complete reviewed
            # calendar. Thus, if an item is absent from an intermediate
            # season, the gap is preserved instead of making old evidence look
            # one season newer. Unknown fixture/legacy league IDs fall back to
            # their deterministic newest-to-oldest order.
            level_age_ranks = [
                (
                    int(known_rank)
                    if known_rank is not None
                    else fallback_rank
                )
                for fallback_rank, known_rank in enumerate(
                    level_known_age_ranks
                )
            ]
            level_raw_weights = [
                self.RECENCY_DECAY_PER_LEAGUE**age_rank
                for age_rank in level_age_ranks
            ]
            level_weight_total = sum(level_raw_weights)
            level_normalized_weights = [
                weight / level_weight_total
                for weight in level_raw_weights
            ]
            forward_known_age_ranks = [
                self._COMPLETED_LEAGUE_AGE_RANK.get(
                    str(row["league_id"]).casefold()
                )
                for row in forward_rows
            ]
            forward_age_ranks = [
                (
                    int(known_rank)
                    if known_rank is not None
                    else fallback_rank
                )
                for fallback_rank, known_rank in enumerate(
                    forward_known_age_ranks
                )
            ]
            forward_raw_weights = [
                self.RECENCY_DECAY_PER_LEAGUE**age_rank
                for age_rank in forward_age_ranks
            ]
            forward_weight_total = sum(forward_raw_weights)
            forward_normalized_weights = [
                weight / forward_weight_total
                for weight in forward_raw_weights
            ]
            recency_weighted_log_return = (
                sum(
                    value * normalized_weight
                    for value, normalized_weight in zip(
                        returns,
                        forward_normalized_weights,
                    )
                )
                if returns
                else 0.0
            )
            recency_weighted_return = math.expm1(
                recency_weighted_log_return
            )
            recency_weighted_entry_price = (
                sum(
                    float(row["entry_price"]) * normalized_weight
                    for row, normalized_weight in zip(
                        level_rows,
                        level_normalized_weights,
                    )
                )
                if level_rows
                else None
            )
            league_weights = tuple(
                {
                    "league": str(row["league_name"]),
                    "league_id": str(row["league_id"]),
                    "start_at": (
                        str(row["league_start_at"]) or None
                    ),
                    "entry_divine": float(row["entry_price"]),
                    "age_rank": level_age_ranks[observation_index],
                    "raw_weight": level_raw_weights[observation_index],
                    "normalized_weight": level_normalized_weights[
                        observation_index
                    ],
                }
                for observation_index, row in enumerate(level_rows)
            )
            median_entry_price = (
                statistics.median(entry_prices) if entry_prices else None
            )
            entry_log_dispersion = 0.0
            entry_mean_median_skew = 0.0
            if entry_prices:
                entry_logs = [math.log(value) for value in entry_prices]
                median_entry_log = statistics.median(entry_logs)
                robust_log_dispersion = 1.4826 * statistics.median(
                    abs(value - median_entry_log) for value in entry_logs
                )
                if (
                    average_entry_price is not None
                    and median_entry_price is not None
                    and median_entry_price > 0
                ):
                    entry_mean_median_skew = abs(
                        math.log(average_entry_price / median_entry_price)
                    )
                # With three leagues, two similar prices and one catastrophic
                # outlier produce a zero MAD. Mean/median skew catches that
                # failure while leaving the requested arithmetic average
                # visible for audit.
                entry_log_dispersion = max(
                    robust_log_dispersion,
                    entry_mean_median_skew,
                )
            level_count_quality = _clamp(
                level_sample_count / self.FULL_SAMPLE_LEAGUES,
                0.0,
                1.0,
            )
            level_dispersion_quality = 1.0 / (
                1.0 + (entry_log_dispersion / 0.35) ** 2
            )
            level_confidence = _clamp(
                level_count_quality * level_dispersion_quality,
                0.0,
                1.0,
            )
            if sample_count < self.MIN_SAMPLE_LEAGUES:
                signals[item_key] = SeasonalSignal(
                    item_key=item_key,
                    status="insufficient_leagues",
                    league_day=day,
                    horizon_days=hold,
                    sample_leagues=sample_count,
                    level_sample_leagues=level_sample_count,
                    average_entry_price=average_entry_price,
                    recency_weighted_entry_price=(
                        recency_weighted_entry_price
                    ),
                    median_entry_price=median_entry_price,
                    entry_dispersion=entry_log_dispersion,
                    entry_mean_median_skew=entry_mean_median_skew,
                    level_confidence=level_confidence,
                    leagues=tuple(
                        sorted(str(row["league_name"]) for row in level_rows)
                    ),
                    league_weights=league_weights,
                    appreciation_status=appreciation_status,
                    appreciation_horizon_days=(
                        self.APPRECIATION_HORIZON_DAYS
                    ),
                    appreciation_sample_leagues=(
                        appreciation_sample_count
                    ),
                    appreciation_median_log_return=(
                        appreciation_median_log
                    ),
                    appreciation_median_return=(
                        appreciation_median_return
                    ),
                    appreciation_recency_weighted_log_return=(
                        appreciation_recency_weighted_log
                    ),
                    appreciation_recency_weighted_return=(
                        appreciation_recency_weighted_return
                    ),
                    appreciation_dispersion=appreciation_dispersion,
                    appreciation_positive_rate=(
                        appreciation_positive_rate
                    ),
                    appreciation_confidence=appreciation_confidence,
                    recency_weighted_log_return=(
                        recency_weighted_log_return
                    ),
                    recency_weighted_return=recency_weighted_return,
                )
                continue

            median_log = statistics.median(returns)
            absolute_deviations = [
                abs(value - median_log) for value in returns
            ]
            dispersion = 1.4826 * statistics.median(absolute_deviations)
            simple_returns = [math.expm1(value) for value in returns]
            positive_rate = sum(value > 0 for value in returns) / sample_count
            count_quality = _clamp(
                sample_count / self.FULL_SAMPLE_LEAGUES,
                0.0,
                1.0,
            )
            dispersion_quality = 1.0 / (1.0 + (dispersion / 0.20) ** 2)
            direction_agreement = abs(positive_rate - 0.5) * 2.0
            forward_confidence = _clamp(
                count_quality
                * dispersion_quality
                * (0.75 + 0.25 * direction_agreement),
                0.0,
                1.0,
            )
            # The price level is the primary cross-league evidence. Forward
            # returns at the same league day are secondary confirmation.
            confidence = _clamp(
                0.65 * level_confidence + 0.35 * forward_confidence,
                0.0,
                1.0,
            )
            # Summarize how much confidence the completed-league sample earns.
            # It is deliberately capped: a handful of past seasons is
            # evidence, not certainty.
            model_weight = _clamp(0.68 * confidence, 0.0, 0.65)
            signals[item_key] = SeasonalSignal(
                item_key=item_key,
                status=(
                    "unstable_level"
                    if entry_log_dispersion > math.log(4.0)
                    else (
                        "ok"
                        if sample_count >= self.FULL_SAMPLE_LEAGUES
                        else "provisional"
                    )
                ),
                league_day=day,
                horizon_days=hold,
                sample_leagues=sample_count,
                level_sample_leagues=level_sample_count,
                average_entry_price=average_entry_price,
                recency_weighted_entry_price=(
                    recency_weighted_entry_price
                ),
                median_entry_price=median_entry_price,
                entry_dispersion=entry_log_dispersion,
                entry_mean_median_skew=entry_mean_median_skew,
                level_confidence=level_confidence,
                median_log_return=median_log,
                median_return=math.expm1(median_log),
                recency_weighted_log_return=recency_weighted_log_return,
                recency_weighted_return=recency_weighted_return,
                dispersion=dispersion,
                p25_return=_percentile(simple_returns, 0.25),
                p75_return=_percentile(simple_returns, 0.75),
                positive_rate=positive_rate,
                confidence=confidence,
                model_weight=model_weight,
                leagues=tuple(
                    sorted(str(row["league_name"]) for row in level_rows)
                ),
                league_weights=league_weights,
                appreciation_status=appreciation_status,
                appreciation_horizon_days=self.APPRECIATION_HORIZON_DAYS,
                appreciation_sample_leagues=appreciation_sample_count,
                appreciation_median_log_return=appreciation_median_log,
                appreciation_median_return=appreciation_median_return,
                appreciation_recency_weighted_log_return=(
                    appreciation_recency_weighted_log
                ),
                appreciation_recency_weighted_return=(
                    appreciation_recency_weighted_return
                ),
                appreciation_dispersion=appreciation_dispersion,
                appreciation_positive_rate=appreciation_positive_rate,
                appreciation_confidence=appreciation_confidence,
            )
        return signals
