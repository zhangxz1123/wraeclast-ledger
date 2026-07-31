from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from typing import Any

from .historical import (
    BROADLY_COVERED_LEAGUE_IDS,
    BROADLY_COVERED_LEAGUES,
    COMPLETED_LEAGUES,
    league_day as calculate_league_day,
)
from .meta import (
    LADDER_SAMPLE_CAVEAT,
    LADDER_SOURCE_LABEL,
    POE_NINJA_META_CAVEAT,
    POE_NINJA_META_SOURCE,
    POE_NINJA_META_SOURCE_LABEL,
    MetaService,
)
from .models import (
    STANDARD_LEAGUE_ID,
    League,
    iso_utc,
    parse_datetime,
    utc_now,
)
from .provenance import (
    CURRENT_PRICE_SOURCES,
    HISTORICAL_PRICE_SOURCES,
    STANDARD_PRICE_SOURCES,
    production_price_provenance,
)
from .seasonality import SeasonalModel, SeasonalSignal
from .storage import Storage


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _safe_log(value: float) -> float:
    return math.log(max(value, 1e-12))


def _linear_fit(values: list[float]) -> tuple[float, float]:
    """Return a robust intercept and Theil-Sen log slope."""

    if len(values) < 2:
        return _safe_log(values[-1] if values else 1.0), 0.0
    logs = [_safe_log(value) for value in values]
    slopes = [
        (logs[right] - logs[left]) / (right - left)
        for left in range(len(logs) - 1)
        for right in range(left + 1, len(logs))
    ]
    slope = statistics.median(slopes)
    intercept = statistics.median(
        value - slope * index for index, value in enumerate(logs)
    )
    return intercept, slope


def _daily_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use the most recent real observation per UTC date."""

    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = str(row["observed_at"])[:10]
        prior = by_day.get(day)
        if prior is None or str(row["observed_at"]) > str(prior["observed_at"]):
            by_day[day] = row
    return [by_day[key] for key in sorted(by_day)]


def _league_phase(day: int | None) -> tuple[str, str]:
    if day is None:
        return "unknown", "League start time is unavailable"
    if day <= 3:
        return "launch", "launch phase"
    if day <= 10:
        return "early", "early-league progression"
    if day <= 28:
        return "mid", "mid-league crafting and build demand"
    return "late", "late-league scarcity and endgame demand"


def _phase_prior(phase: str, category: str) -> float:
    category = category.lower()
    priors = {
        "launch": {
            "currency": 0.08,
            "essence": 0.09,
            "scarab": -0.02,
            "divinationcard": -0.04,
        },
        "early": {
            "currency": 0.06,
            "essence": 0.08,
            "fragment": 0.04,
            "skillgem": 0.02,
        },
        "mid": {
            "currency": 0.02,
            "essence": 0.04,
            "fragment": 0.06,
            "scarab": 0.06,
            "divinationcard": 0.05,
            "uniquejewel": 0.04,
        },
        "late": {
            "currency": 0.01,
            "fragment": 0.03,
            "divinationcard": 0.08,
            "skillgem": 0.06,
            "uniquejewel": 0.05,
        },
    }
    for needle, value in priors.get(phase, {}).items():
        if needle in category:
            return value
    return 0.0


def _liquidity_score(row: dict[str, Any]) -> float:
    listing = row.get("listing_count")
    volume = row.get("volume")
    scores: list[float] = []
    if listing is not None:
        scores.append(_clamp(math.log10(max(1, float(listing))) / 3.0, 0, 1))
    if volume is not None:
        scores.append(_clamp(math.log10(max(1, float(volume))) / 4.0, 0, 1))
    return statistics.fmean(scores) if scores else 0.35


def _label(score: float) -> str:
    if score >= 0.72:
        return "High"
    if score >= 0.46:
        return "Medium"
    return "Low"


def _round_trip_friction(category: str) -> float:
    value = category.lower()
    if value == "currency":
        return 0.03
    if any(
        needle in value
        for needle in (
            "essence",
            "fragment",
            "scarab",
            "fossil",
            "oil",
            "resonator",
            "delirium",
        )
    ):
        return 0.05
    if "divination" in value:
        return 0.07
    if "unique" in value:
        return 0.12
    return 0.07


def _minimum_net_return(horizon: int) -> float:
    if horizon <= 3:
        return 0.04
    if horizon <= 7:
        return 0.07
    return 0.12


MAX_PRIORITY_RANKINGS = 100
FORECAST_HORIZONS = (3, 7, 14)
HISTORICAL_FORECAST_WEIGHT = 0.70
CURRENT_CURVE_FORECAST_WEIGHT = 0.30
HISTORICAL_MODEL_CONFIDENCE_FLOOR = 0.5
CURRENT_CURVE_LOOKBACK_POINTS = 7
CURRENT_PROJECTION_GAIN_FLOOR = -0.50
CURRENT_PROJECTION_GAIN_CEILING = 0.50
MINIMUM_INVESTMENT_PRICE_CHAOS = 1.0

# Structural decline is measured from weekly median Divine-relative prices.
# The rule is deliberately conservative: one noisy league is never enough to
# remove an item, and newer broadly covered leagues receive more influence.
DECLINE_CURVE_MAXIMUM_DAY = 120
DECLINE_WEEK_DAYS = 7
DECLINE_MINIMUM_POINTS_PER_WEEK = 2
DECLINE_MINIMUM_WEEKS = 12
DECLINE_MINIMUM_DAY_SPAN = 70
DECLINE_MAXIMUM_WEEKLY_GAIN = -0.02
DECLINE_MINIMUM_NEGATIVE_PAIR_FRACTION = 0.70
DECLINE_MAXIMUM_EARLY_LATE_RATIO = 0.80
DECLINE_MINIMUM_LEAGUE_VOTES = 2
DECLINE_MINIMUM_WEIGHTED_SUPPORT = 0.65

# These markets remain in the local archive for research and fast queries, but
# they are intentionally outside the investment ranking. Their repeatable
# supply and tiny unit values make them better suited to bulk arbitrage than
# multi-day appreciation positions.
EXCLUDED_INVESTMENT_CATEGORIES = (
    "Essence",
    "Fossil",
    "Oil",
    "Resonator",
    "Scarab",
    "DeliriumOrb",
    "Artifact",
    "Incubator",
)
_EXCLUDED_INVESTMENT_CATEGORY_KEYS = frozenset(
    re.sub(r"[^a-z0-9]+", "", value.casefold())
    for value in EXCLUDED_INVESTMENT_CATEGORIES
)

# Structural priors are explicit and auditable. They override a noisy
# point-in-time historical sample when the asset's economic lifecycle is
# known to be unsuitable for appreciation investing.
KNOWN_DECLINING_LIFECYCLES: dict[str, dict[str, str]] = {
    "chaosorb": {
        "name": "Chaos Orb",
        "code": "divine_relative_reference_currency_decline",
        "reason": (
            "Known structural-decline lifecycle: Chaos Orb is the dump's "
            "quote currency, so poe.ninja has no direct Chaos/Chaos history "
            "to classify automatically. Its Divine-relative purchasing "
            "power characteristically falls as a trade league matures, so "
            "it remains archived but is outside the appreciation ranking."
        ),
    },
    "themavenswrit": {
        "name": "The Maven's Writ",
        "code": "expanding_boss_access_supply",
        "reason": (
            "Known structural-decline lifecycle: Maven's Writ is a "
            "consumable boss-access item whose farmed supply expands through "
            "the league. It is never treated as an appreciation investment, "
            "even when a noisy same-day historical sample looks positive."
        ),
    },
}


def _normalized_asset_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _category_is_excluded(category: str) -> bool:
    return _normalized_asset_token(category) in (
        _EXCLUDED_INVESTMENT_CATEGORY_KEYS
    )


def _known_declining_lifecycle(
    item_key: str,
    name: str,
) -> dict[str, str] | None:
    name_token = _normalized_asset_token(name)
    key_token = _normalized_asset_token(item_key.split(":", 1)[-1])
    return KNOWN_DECLINING_LIFECYCLES.get(
        name_token,
        KNOWN_DECLINING_LIFECYCLES.get(key_token),
    )


def _item_level_from_identity(
    item_key: str,
    category: str,
    details: dict[str, Any],
) -> int | None:
    """Recover the exact poe.ninja item-level variant without changing keys."""

    if category not in {"BaseType", "ClusterJewel", "Wombgift"}:
        return None
    raw_level = details.get("itemLevel")
    if raw_level is None:
        raw_level = details.get("item_level")
    if raw_level is None:
        raw_level = details.get("levelRequired")
    if raw_level is not None:
        try:
            item_level = int(raw_level)
        except (TypeError, ValueError):
            item_level = 0
        if 1 <= item_level <= 100:
            return item_level

    identity = str(item_key).split(":", 1)[-1].casefold()
    variant = str(details.get("variant") or "").strip().casefold()
    variant_slug = re.sub(r"[^a-z0-9]+", "-", variant).strip("-")
    variant_suffix = f"-variant-{variant_slug}" if variant_slug else ""
    if variant_suffix and identity.endswith(variant_suffix):
        identity = identity[: -len(variant_suffix)]
    if variant_slug and identity.endswith(f"-{variant_slug}"):
        identity = identity[: -(len(variant_slug) + 1)]
    match = re.search(r"-(\d{1,3})$", identity)
    if not match:
        return None
    item_level = int(match.group(1))
    return item_level if 1 <= item_level <= 100 else None


def _weighted_median(
    values: list[tuple[float, float]],
) -> float | None:
    eligible = [
        (float(value), float(weight))
        for value, weight in values
        if math.isfinite(float(value))
        and math.isfinite(float(weight))
        and float(weight) > 0
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda pair: pair[0])
    midpoint = sum(weight for _, weight in eligible) / 2.0
    cumulative = 0.0
    for value, weight in eligible:
        cumulative += weight
        if cumulative >= midpoint:
            return value
    return eligible[-1][0]


def _historical_decline_assessments(
    rows: list[dict[str, Any]],
    *,
    league_weights: dict[str, float],
) -> dict[str, dict[str, Any]]:
    """Classify persistent completed-league depreciation by exact item key.

    Daily markets are noisy, so each league is reduced to weekly median
    log-prices. A robust Theil-Sen slope, the share of negative pairwise
    slopes, and the early/late price ratio must all agree. Finally, at least
    two broadly covered leagues and 65% of their available recency weight must
    vote for decline.
    """

    grouped: defaultdict[
        str, defaultdict[str, list[tuple[int, float]]]
    ] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        item_key = str(row.get("item_key") or "").strip()
        league_id = str(row.get("league_id") or "").strip()
        try:
            league_day = int(row.get("league_day") or 0)
            price = float(row.get("divine_value") or 0.0)
        except (TypeError, ValueError):
            continue
        if (
            not item_key
            or league_id not in league_weights
            or league_day < 1
            or league_day > DECLINE_CURVE_MAXIMUM_DAY
            or not math.isfinite(price)
            or price <= 0
        ):
            continue
        grouped[item_key][league_id].append((league_day, price))

    assessments: dict[str, dict[str, Any]] = {}
    for item_key, league_points in grouped.items():
        profiles: list[dict[str, Any]] = []
        for league_id, points in league_points.items():
            weekly: defaultdict[int, list[tuple[int, float]]] = defaultdict(
                list
            )
            for league_day, price in points:
                weekly[(league_day - 1) // DECLINE_WEEK_DAYS].append(
                    (league_day, price)
                )
            weekly_points = []
            for bucket in sorted(weekly):
                bucket_points = weekly[bucket]
                if len(bucket_points) < DECLINE_MINIMUM_POINTS_PER_WEEK:
                    continue
                weekly_points.append(
                    (
                        float(
                            statistics.median(
                                day for day, _ in bucket_points
                            )
                        ),
                        statistics.median(
                            _safe_log(price) for _, price in bucket_points
                        ),
                    )
                )
            span_days = (
                weekly_points[-1][0] - weekly_points[0][0]
                if len(weekly_points) >= 2
                else 0.0
            )
            if (
                len(weekly_points) < DECLINE_MINIMUM_WEEKS
                or span_days < DECLINE_MINIMUM_DAY_SPAN
            ):
                continue
            pairwise_slopes = [
                (right_log - left_log) / (right_day - left_day)
                for left_index, (left_day, left_log) in enumerate(
                    weekly_points[:-1]
                )
                for right_day, right_log in weekly_points[left_index + 1 :]
                if right_day > left_day
            ]
            if not pairwise_slopes:
                continue
            log_slope_per_day = statistics.median(pairwise_slopes)
            weekly_gain = math.expm1(
                log_slope_per_day * DECLINE_WEEK_DAYS
            )
            negative_pair_fraction = sum(
                slope < 0 for slope in pairwise_slopes
            ) / len(pairwise_slopes)
            quartile_size = max(2, len(weekly_points) // 4)
            early_log_price = statistics.median(
                point[1] for point in weekly_points[:quartile_size]
            )
            late_log_price = statistics.median(
                point[1] for point in weekly_points[-quartile_size:]
            )
            early_late_ratio = math.exp(late_log_price - early_log_price)
            declines = (
                weekly_gain <= DECLINE_MAXIMUM_WEEKLY_GAIN
                and negative_pair_fraction
                >= DECLINE_MINIMUM_NEGATIVE_PAIR_FRACTION
                and early_late_ratio <= DECLINE_MAXIMUM_EARLY_LATE_RATIO
            )
            profiles.append(
                {
                    "league_id": league_id,
                    "weekly_gain": weekly_gain,
                    "negative_pair_fraction": negative_pair_fraction,
                    "early_late_ratio": early_late_ratio,
                    "weekly_points": len(weekly_points),
                    "span_days": span_days,
                    "declines": declines,
                    "weight": float(league_weights[league_id]),
                }
            )

        profiles.sort(
            key=lambda profile: (
                -float(profile["weight"]),
                str(profile["league_id"]),
            )
        )
        total_weight = sum(float(profile["weight"]) for profile in profiles)
        declining_profiles = [
            profile for profile in profiles if profile["declines"]
        ]
        declining_weight = sum(
            float(profile["weight"]) for profile in declining_profiles
        )
        support = declining_weight / total_weight if total_weight > 0 else 0.0
        aggregate_weekly_gain = _weighted_median(
            [
                (float(profile["weekly_gain"]), float(profile["weight"]))
                for profile in profiles
            ]
        )
        aggregate_early_late_ratio = _weighted_median(
            [
                (
                    float(profile["early_late_ratio"]),
                    float(profile["weight"]),
                )
                for profile in profiles
            ]
        )
        historical_decline = (
            len(declining_profiles) >= DECLINE_MINIMUM_LEAGUE_VOTES
            and support >= DECLINE_MINIMUM_WEIGHTED_SUPPORT
            and aggregate_weekly_gain is not None
            and aggregate_weekly_gain <= DECLINE_MAXIMUM_WEEKLY_GAIN
            and aggregate_early_late_ratio is not None
            and aggregate_early_late_ratio
            <= DECLINE_MAXIMUM_EARLY_LATE_RATIO
        )
        assessments[item_key] = {
            "historical_decline": historical_decline,
            "weighted_support": support,
            "sample_leagues": len(profiles),
            "declining_leagues": [
                str(profile["league_id"])
                for profile in declining_profiles
            ],
            "aggregate_weekly_gain": aggregate_weekly_gain,
            "aggregate_early_late_ratio": aggregate_early_late_ratio,
            "profiles": profiles,
        }
    return assessments


def _current_curve_projection(
    points: list[dict[str, Any]],
    horizon: int,
) -> dict[str, Any]:
    """Project a current curve with a Theil-Sen log slope.

    League-day coordinates are used directly, so missing calendar days do not
    get mistaken for consecutive observations. The raw projection remains
    visible while the component used in the blend is capped to a 50% loss or
    gain over the requested horizon.
    """

    usable = [
        {
            "league_day": int(point["league_day"]),
            "divine_value": float(point["divine_value"]),
        }
        for point in points
        if int(point.get("league_day") or 0) >= 1
        and float(point.get("divine_value") or 0.0) > 0
    ][-CURRENT_CURVE_LOOKBACK_POINTS:]
    slopes = [
        (
            _safe_log(right["divine_value"])
            - _safe_log(left["divine_value"])
        )
        / (right["league_day"] - left["league_day"])
        for left_index, left in enumerate(usable[:-1])
        for right in usable[left_index + 1 :]
        if right["league_day"] > left["league_day"]
    ]
    slope = statistics.median(slopes) if slopes else None
    projected_log_gain = (
        slope * int(horizon) if slope is not None else None
    )
    raw_gain = (
        math.expm1(_clamp(projected_log_gain, -700.0, 700.0))
        if projected_log_gain is not None
        else None
    )
    capped_gain = (
        _clamp(
            raw_gain,
            CURRENT_PROJECTION_GAIN_FLOOR,
            CURRENT_PROJECTION_GAIN_CEILING,
        )
        if raw_gain is not None
        else None
    )
    return {
        "point_count": len(usable),
        "league_days": [point["league_day"] for point in usable],
        "points": usable,
        "log_slope_per_day": slope,
        "projected_log_gain": projected_log_gain,
        "raw_gain": raw_gain,
        "capped_gain": capped_gain,
        "was_capped": (
            raw_gain is not None
            and capped_gain is not None
            and not math.isclose(raw_gain, capped_gain, rel_tol=1e-12)
        ),
    }


class RecommendationEngine:
    """Conservative, explainable signal model and priority ranker."""

    def __init__(
        self,
        storage: Storage,
        meta_service: MetaService | None = None,
    ):
        self.storage = storage
        self.seasonal_model = SeasonalModel(storage)
        self.meta_service = meta_service or MetaService(storage)

    def generate(
        self,
        league: League,
        *,
        budget: float = 100.0,
        horizon: int = 7,
        persist: bool = True,
    ) -> dict[str, Any]:
        budget = _clamp(float(budget), 1.0, 100000.0)
        horizon = int(_clamp(float(horizon), 1, 30))
        if (
            not league.is_demo
            and callable(
                getattr(self.storage, "daily_item_history", None)
            )
            and callable(
                getattr(self.storage, "seasonal_entry_rows", None)
            )
        ):
            return self._generate_forecast_ranking(
                league,
                budget=budget,
                horizon=horizon,
                persist=persist,
            )
        histories = self.storage.item_histories(
            league.id,
            days=max(45, horizon * 5),
            sources=None if league.is_demo else CURRENT_PRICE_SOURCES,
        )
        # Standard is a long-horizon reference only. It is loaded separately
        # and never enters the score, expected return, vetoes, or allocator.
        standard_prices = (
            {}
            if league.is_demo
            else self.storage.latest_item_prices(
                STANDARD_LEAGUE_ID,
                sources=STANDARD_PRICE_SOURCES,
            )
        )
        seasonal_signals: dict[str, SeasonalSignal] = {}
        if not league.is_demo and league.day is not None:
            investable_history_keys: list[str] = []
            for key, key_rows in histories.items():
                if key in {"currency:divine-orb", "currency:chaos-orb"}:
                    continue
                daily_key_rows = _daily_rows(key_rows)
                if not daily_key_rows:
                    continue
                key_latest = daily_key_rows[-1]
                if _category_is_excluded(str(key_latest["category"])):
                    continue
                if _known_declining_lifecycle(
                    key,
                    str(key_latest["name"]),
                ):
                    continue
                investable_history_keys.append(key)
            seasonal_signals = self.seasonal_model.signals(
                league_day=league.day,
                horizon=horizon,
                item_keys=investable_history_keys,
            )
        phase, phase_description = _league_phase(league.day)
        candidates: list[dict[str, Any]] = []
        watchlist: list[dict[str, Any]] = []
        meta_signals: dict[str, dict[str, Any]] = {}
        excluded_category_counts: defaultdict[str, int] = defaultdict(int)
        known_decline_items = 0
        appreciation_rejections = 0

        for item_key, raw_rows in histories.items():
            rows = _daily_rows(raw_rows)
            if not rows:
                continue
            latest = rows[-1]
            category = str(latest["category"])
            if _category_is_excluded(category):
                excluded_category_counts[category] += 1
                continue
            current = float(latest["divine_value"])
            if current <= 0:
                continue
            if current < 0.001:
                # Sub-chaos markets are too granular for dependable manual
                # position sizing and are especially vulnerable to ratio noise.
                continue
            if latest["name"].lower() in {"divine orb", "chaos orb"}:
                continue
            lifecycle = _known_declining_lifecycle(
                item_key,
                str(latest["name"]),
            )
            if lifecycle is not None:
                known_decline_items += 1
                watchlist.insert(
                    0,
                    {
                        "key": item_key,
                        "name": latest["name"],
                        "category": latest["category"],
                        "price_divine": round(current, 5),
                        "reason": lifecycle["reason"],
                        "lifecycle_status": "known_decline",
                        "lifecycle_veto_code": lifecycle["code"],
                        "lifecycle_veto_reason": lifecycle["reason"],
                    },
                )
                continue
            latest_observed = parse_datetime(str(latest.get("observed_at") or ""))
            if (
                not league.is_demo
                and (
                    latest_observed is None
                    or (utc_now() - latest_observed).total_seconds() > 36 * 3600
                )
            ):
                watchlist.append(
                    {
                        "key": item_key,
                        "name": latest["name"],
                        "category": latest["category"],
                        "price_divine": round(current, 5),
                        "reason": (
                            "The newest local observation is more than 36 "
                            "hours old; sync this category before sizing it."
                        ),
                    }
                )
                continue
            if (
                latest.get("source") == "ggg-currency-exchange"
                and latest.get("details", {}).get("identifier_resolved") is False
            ):
                watchlist.append(
                    {
                        "key": item_key,
                        "name": latest["name"],
                        "category": latest["category"],
                        "price_divine": round(current, 5),
                        "reason": (
                            "The official feed exposes only an internal "
                            "identifier for this market; it is excluded until "
                            "that identifier can be mapped to an exact item."
                        ),
                    }
                )
                continue
            if re.search(r"\d$", str(latest["name"]).strip()):
                watchlist.append(
                    {
                        "key": item_key,
                        "name": latest["name"],
                        "category": latest["category"],
                        "price_divine": round(current, 5),
                        "reason": "The source identifier could not be matched to an exact tradeable variant.",
                    }
                )
                continue
            seasonal = seasonal_signals.get(item_key)
            if not league.is_demo and (
                seasonal is None
                or seasonal.status not in {"ok", "provisional"}
            ):
                sample_count = seasonal.sample_leagues if seasonal else 0
                if seasonal and seasonal.status == "unstable_level":
                    reason = (
                        "The completed-league same-day prices disagree too "
                        "sharply for their arithmetic average to be a safe "
                        "fair-value anchor. The item stays on the watchlist "
                        "until the historical source or coverage improves."
                    )
                else:
                    reason = (
                        "Item-specific seasonality is required for live "
                        f"recommendations; only {sample_count} comparable "
                        "completed leagues have an exact entry and exit. "
                        "Continue the historical archive backfill."
                    )
                watchlist.append(
                    {
                        "key": item_key,
                        "name": latest["name"],
                        "category": latest["category"],
                        "price_divine": round(current, 5),
                        "reason": reason,
                        "seasonal_status": (
                            seasonal.status
                            if seasonal
                            else "insufficient_leagues"
                        ),
                        "seasonal_sample_leagues": sample_count,
                        "historical_level_dispersion": (
                            seasonal.entry_dispersion
                            if seasonal
                            else None
                        ),
                    }
                )
                continue
            minimum_current_days = (
                1
                if seasonal and seasonal.status == "ok"
                else (2 if seasonal else 5)
            )
            if len(rows) < minimum_current_days:
                watchlist.append(
                    {
                        "key": item_key,
                        "name": latest["name"],
                        "category": latest["category"],
                        "price_divine": round(current, 5),
                        "reason": (
                            f"Only {len(rows)} locally observed daily prices; "
                            f"at least {minimum_current_days} are required for "
                            "this level of completed-league coverage."
                        ),
                        "seasonal_status": (
                            seasonal.status
                            if seasonal
                            else "demo_unavailable"
                        ),
                        "seasonal_sample_leagues": (
                            seasonal.sample_leagues if seasonal else 0
                        ),
                        "seasonal_recency_weighted_return_pct": (
                            round(
                                seasonal.recency_weighted_return * 100,
                                1,
                            )
                            if seasonal
                            else None
                        ),
                        "appreciation_status": (
                            seasonal.appreciation_status
                            if seasonal
                            else "demo_unavailable"
                        ),
                        "appreciation_horizon_days": (
                            seasonal.appreciation_horizon_days
                            if seasonal
                            else SeasonalModel.APPRECIATION_HORIZON_DAYS
                        ),
                        "appreciation_sample_leagues": (
                            seasonal.appreciation_sample_leagues
                            if seasonal
                            else 0
                        ),
                        "appreciation_recency_weighted_return_pct": (
                            round(
                                seasonal.appreciation_recency_weighted_return
                                * 100,
                                1,
                            )
                            if seasonal
                            else None
                        ),
                    }
                )
                continue

            values = [float(row["divine_value"]) for row in rows[-30:]]
            # Keep the recent evaluation window out of the fair-value fit.
            # Otherwise a short-lived drawdown teaches the baseline that the
            # drawdown itself is fair value.
            if len(values) >= 5:
                holdout = min(
                    max(1, horizon // 2),
                    max(1, len(values) - 5),
                )
                training = values[:-holdout][-21:]
                intercept, slope = _linear_fit(training)
                predicted_fair = math.exp(
                    intercept + slope * (len(training) + holdout)
                )
                predicted_fair = _clamp(
                    predicted_fair,
                    current * 0.7,
                    current * 1.35,
                )
                discount = (predicted_fair - current) / current
            else:
                # A complete, low-dispersion seasonal sample can support a
                # first-day idea. Until current observations accumulate, the
                # current-league fair-value and trend components stay neutral.
                slope = 0.0
                predicted_fair = current
                discount = 0.0

            returns = [
                _safe_log(values[index] / values[index - 1])
                for index in range(1, len(values))
                if values[index - 1] > 0
            ]
            volatility = (
                statistics.pstdev(returns[-14:])
                if len(returns) >= 2
                else (0.12 if seasonal else 0.25)
            )
            lookback_index = max(0, len(values) - 1 - min(horizon, len(values) - 1))
            momentum = values[-1] / values[lookback_index] - 1
            median = statistics.median(values[-14:])
            absolute_deviations = [
                abs(value - median) for value in values[-14:]
            ]
            mad = statistics.median(absolute_deviations) or current * 0.03
            z_score = (current - median) / max(mad * 1.4826, current * 0.015)
            mean_reversion = _clamp(-z_score / 3.0, -1.0, 1.0)
            liquidity = _liquidity_score(latest)
            generic_phase_prior = _phase_prior(
                phase, str(latest["category"])
            )
            # Once an item-specific prior exists, the generic category prior
            # is retained only as a small regularizer to avoid counting the
            # same seasonal intuition twice.
            phase_prior = (
                generic_phase_prior
                if seasonal is None
                else generic_phase_prior * 0.15
            )
            trend_component = _clamp(slope * horizon, -0.25, 0.30)
            momentum_quality = _clamp(momentum, -0.25, 0.20)
            overextension_penalty = max(0.0, z_score - 1.25) * 0.08
            current_signal_score = (
                0.30 * _clamp(discount, -0.35, 0.45)
                + 0.23 * trend_component
                + 0.18 * mean_reversion
                + 0.11 * momentum_quality
                + 0.13 * liquidity
                + phase_prior
                - 0.45 * min(volatility, 0.35)
                - overextension_penalty
            )
            current_gross_expected_return = _clamp(
                0.45 * max(0.0, discount)
                + 0.30 * trend_component
                + phase_prior * 0.25
                - volatility * math.sqrt(horizon) * 0.18,
                -0.15,
                0.30,
            )
            friction = _round_trip_friction(str(latest["category"]))
            seasonal_weight = seasonal.model_weight if seasonal else 0.0
            historical_average = (
                seasonal.average_entry_price if seasonal else None
            )
            historical_recency_weighted = (
                seasonal.recency_weighted_entry_price
                if seasonal
                else None
            )
            # Recency weighting is the primary same-day fair-value estimate.
            # The equal-weight arithmetic mean remains exposed for audit.
            historical_fair_value = historical_recency_weighted
            historical_median = (
                seasonal.median_entry_price if seasonal else None
            )
            meta_signal: dict[str, Any] = {
                "status": "not_applicable",
                "multiplier": 1.0,
            }
            details = latest.get("details")
            metadata = (
                details.get("metadata")
                if isinstance(details, dict)
                else None
            )
            ascendancy = (
                str(metadata.get("ascendancy") or "").strip()
                if isinstance(metadata, dict)
                else ""
            )
            if (
                not league.is_demo
                and str(latest["category"]).casefold() == "forbiddenjewel"
                and ascendancy
            ):
                if ascendancy not in meta_signals:
                    meta_signals[ascendancy] = (
                        self.meta_service.ascendancy_multiplier(
                            league,
                            COMPLETED_LEAGUES,
                            ascendancy,
                        )
                    )
                meta_signal = meta_signals[ascendancy]
                if (
                    meta_signal.get("status") == "ok"
                    and historical_fair_value is not None
                ):
                    historical_fair_value *= float(
                        meta_signal.get("multiplier") or 1.0
                    )
            historical_discount: float | None = None
            historical_upside = 0.0
            if historical_fair_value is not None and historical_fair_value > 0:
                historical_discount = (
                    historical_fair_value - current
                ) / historical_fair_value
                historical_upside = historical_fair_value / current - 1.0

            if seasonal and historical_discount is not None:
                valuation_signal = _clamp(
                    historical_discount,
                    -0.50,
                    0.75,
                )
                forward_confirmation = _clamp(
                    seasonal.recency_weighted_return,
                    -0.35,
                    0.50,
                )
                # Same-day valuation is the primary score. The subsequent
                # move in past leagues confirms timing; current-league trend
                # and liquidity remain smaller safeguards.
                score = (
                    0.62
                    * valuation_signal
                    * (0.55 + 0.45 * seasonal.level_confidence)
                    + 0.20
                    * forward_confirmation
                    * seasonal.confidence
                    + 0.18 * current_signal_score
                )
                level_weight = 0.55 + 0.10 * seasonal.level_confidence
                forward_weight = 0.25 + 0.10 * seasonal.confidence
                current_weight = max(
                    0.0,
                    1.0 - level_weight - forward_weight,
                )
                capture_rate = _clamp(
                    0.30 + 0.025 * horizon,
                    0.35,
                    0.65,
                )
                valuation_return = (
                    _clamp(historical_upside, -0.50, 1.00)
                    * capture_rate
                )
                blended_gross_return = (
                    level_weight * valuation_return
                    + forward_weight * seasonal.recency_weighted_return
                    + current_weight * current_gross_expected_return
                )
                disagreement = abs(
                    seasonal.recency_weighted_return
                    - current_gross_expected_return
                )
                predictive_dispersion = math.sqrt(
                    (
                        current_weight
                        * volatility
                        * math.sqrt(horizon)
                    )
                    ** 2
                    + (forward_weight * seasonal.dispersion) ** 2
                    + (
                        level_weight
                        * seasonal.entry_dispersion
                        * capture_rate
                    )
                    ** 2
                    + (forward_weight * disagreement * 0.25) ** 2
                    + (
                        level_weight
                        * abs(
                            math.log(
                                max(
                                    0.01,
                                    float(
                                        meta_signal.get("multiplier")
                                        or 1.0
                                    ),
                                )
                            )
                        )
                        * (
                            1.0
                            - float(meta_signal.get("confidence") or 0.0)
                        )
                    )
                    ** 2
                )
            else:
                score = current_signal_score
                blended_gross_return = current_gross_expected_return
                predictive_dispersion = volatility * math.sqrt(horizon)
            history_cap = (
                0.08
                + 0.01 * min(len(rows), 14)
                + (0.04 if seasonal and seasonal.status == "ok" else 0.0)
            )
            expected_return = min(
                blended_gross_return - friction,
                history_cap,
            )
            risk_adjusted_edge = (
                expected_return - 0.20 * predictive_dispersion
            )
            seasonal_veto = bool(
                seasonal
                and seasonal.p75_return <= -max(0.03, friction)
                and seasonal.median_return <= -0.08
            )
            forward_timing_veto = bool(
                seasonal
                and (
                    seasonal.median_return < 0
                    or seasonal.recency_weighted_return <= friction
                )
            )
            appreciation_veto = bool(
                seasonal
                and seasonal.appreciation_status != "appreciating"
            )
            if appreciation_veto:
                appreciation_rejections += 1
            valuation_veto = bool(
                seasonal
                and (
                    historical_discount is None
                    or historical_discount <= 0
                )
            )
            recent_values = values[-4:]
            recent_return = (
                _safe_log(recent_values[-1] / recent_values[0])
                if len(recent_values) >= 4 and recent_values[0] > 0
                else 0.0
            )
            falling_knife = (
                len(recent_values) >= 4
                and recent_return
                < -1.5 * max(volatility, 0.015) * math.sqrt(3)
            )
            source_confidence = statistics.fmean(
                float(row.get("confidence") or 0.5) for row in rows[-7:]
            )
            history_confidence = _clamp(len(rows) / 21.0, 0.25, 1.0)
            confidence_score = _clamp(
                0.36 * source_confidence
                + 0.30 * history_confidence
                + 0.28 * liquidity
                + 0.06 * (1 - min(1.0, volatility / 0.20)),
                0,
                1,
            )
            if seasonal:
                confidence_score = _clamp(
                    0.52 * confidence_score
                    + 0.48 * seasonal.confidence,
                    0,
                    1,
                )
            if historical_discount is not None:
                valuation_direction = (
                    "below"
                    if historical_discount >= 0
                    else "above"
                )
                if (
                    meta_signal.get("status") == "ok"
                    and historical_average is not None
                ):
                    valuation_comparison = (
                        f"Current {current:.5g} div vs "
                        f"{historical_fair_value:.5g}-div meta-adjusted fair "
                        f"value ({historical_recency_weighted:.5g}-div "
                        f"recency-weighted prior-league day-{league.day} "
                        f"value ×"
                        f"{float(meta_signal['multiplier']):.2f}): "
                        f"{abs(historical_discount) * 100:.1f}% "
                        f"{valuation_direction}; unweighted mean "
                        f"{historical_average:.5g} div"
                    )
                else:
                    valuation_comparison = (
                        f"Current {current:.5g} div vs "
                        f"{historical_fair_value:.5g}-div recency-weighted "
                        f"prior-league day-{league.day} value: "
                        f"{abs(historical_discount) * 100:.1f}% "
                        f"{valuation_direction}; unweighted mean "
                        f"{historical_average:.5g} div"
                    )
            else:
                valuation_comparison = (
                    "offline demo has no same-day prior-league valuation"
                )
            standard_anchor = standard_prices.get(item_key)
            standard_anchor_divine: float | None = None
            standard_anchor_gap: float | None = None
            standard_anchor_ratio: float | None = None
            if standard_anchor is not None:
                anchor_value = float(standard_anchor["divine_value"])
                if anchor_value > 0:
                    standard_anchor_divine = anchor_value
                    standard_anchor_gap = (
                        anchor_value - current
                    ) / anchor_value
                    standard_anchor_ratio = anchor_value / current
            reason_bits = [
                valuation_comparison,
                (
                    f"{seasonal.recency_weighted_return * 100:+.1f}% "
                    f"recency-weighted next-{horizon}-day move "
                    f"({seasonal.median_return * 100:+.1f}% median; "
                    f"{seasonal.positive_rate * 100:.0f}% positive across "
                    f"{seasonal.sample_leagues} prior leagues)"
                    if seasonal
                    else "current-league history drives this demo idea"
                ),
                (
                    f"{seasonal.appreciation_recency_weighted_return * 100:+.1f}% "
                    f"recency-weighted next-"
                    f"{seasonal.appreciation_horizon_days}-day appreciation "
                    f"profile ({seasonal.appreciation_median_return * 100:+.1f}% "
                    f"median; "
                    f"{seasonal.appreciation_positive_rate * 100:.0f}% "
                    f"positive)"
                    if seasonal
                    else "medium-term appreciation profile unavailable in demo"
                ),
                f"{slope * 100:+.2f}% log trend per day",
                f"{_label(liquidity).lower()} liquidity",
                phase_description,
                f"{friction * 100:.0f}% round-trip friction deducted",
            ]
            if standard_anchor_divine is not None:
                reason_bits.append(
                    f"Standard long-term anchor "
                    f"{standard_anchor_divine:.5g} div "
                    f"({standard_anchor_gap * 100:+.1f}% gap by Standard "
                    "value), shown as context only and excluded from the "
                    "short-term score"
                )
            if meta_signal.get("status") == "ok":
                meta_sample_name = (
                    "poe.ninja indexed-build"
                    if meta_signal.get("source") == POE_NINJA_META_SOURCE
                    else "top-ladder"
                )
                reason_bits.insert(
                    1,
                    (
                        f"{ascendancy} {meta_sample_name} share "
                        f"{float(meta_signal['current_share']) * 100:.1f}% "
                        f"vs {float(meta_signal['historical_share']) * 100:.1f}% "
                        f"historical, applying a capped "
                        f"×{float(meta_signal['multiplier']):.2f} "
                        f"demand adjustment"
                    ),
                )
            record = {
                "key": item_key,
                "name": latest["name"],
                "category": latest["category"],
                "price_divine": current,
                "price_chaos": (
                    float(latest["chaos_value"])
                    if latest.get("chaos_value") is not None
                    else None
                ),
                "score": score,
                "expected_return": expected_return,
                "confidence_score": confidence_score,
                "liquidity_score": liquidity,
                "market_volume": latest.get("volume"),
                "listing_count": latest.get("listing_count"),
                "volatility": volatility,
                "risk_adjusted_edge": risk_adjusted_edge,
                "rationale": "; ".join(reason_bits) + ".",
                "falling_knife": falling_knife,
                "historical_fair_value_divine": historical_fair_value,
                "historical_average_divine": historical_average,
                "historical_recency_weighted_divine": (
                    historical_recency_weighted
                ),
                "historical_median_divine": historical_median,
                "historical_discount": historical_discount,
                "standard_anchor_divine": standard_anchor_divine,
                "standard_anchor_gap": standard_anchor_gap,
                "standard_anchor_ratio": standard_anchor_ratio,
                "standard_anchor_observed_at": (
                    standard_anchor.get("observed_at")
                    if standard_anchor is not None
                    else None
                ),
                "standard_anchor_source": (
                    standard_anchor.get("source")
                    if standard_anchor is not None
                    else None
                ),
                "historical_level_dispersion": (
                    seasonal.entry_dispersion if seasonal else None
                ),
                "historical_mean_median_skew": (
                    seasonal.entry_mean_median_skew
                    if seasonal
                    else None
                ),
                "historical_level_confidence": (
                    seasonal.level_confidence if seasonal else 0.0
                ),
                "historical_level_sample_leagues": (
                    seasonal.level_sample_leagues if seasonal else 0
                ),
                "historical_forward_return": (
                    seasonal.median_return if seasonal else None
                ),
                "historical_recency_weighted_forward_return": (
                    seasonal.recency_weighted_return
                    if seasonal
                    else None
                ),
                "meta_status": meta_signal.get("status"),
                "meta_ascendancy": ascendancy or None,
                "meta_multiplier": float(
                    meta_signal.get("multiplier") or 1.0
                ),
                "meta_current_share": meta_signal.get("current_share"),
                "meta_historical_share": meta_signal.get(
                    "historical_share"
                ),
                "meta_current_sample_size": int(
                    meta_signal.get("current_sample_size") or 0
                ),
                "meta_historical_sample_size": int(
                    meta_signal.get("historical_sample_size") or 0
                ),
                "meta_historical_league_count": int(
                    meta_signal.get("historical_league_count") or 0
                ),
                "meta_baseline_quality": meta_signal.get(
                    "baseline_quality"
                ),
                "meta_confidence": float(
                    meta_signal.get("confidence") or 0.0
                ),
                "meta_source": meta_signal.get("source"),
                "meta_caveat": meta_signal.get("caveat"),
                "seasonal_status": (
                    seasonal.status if seasonal else "demo_unavailable"
                ),
                "seasonal_sample_leagues": (
                    seasonal.sample_leagues if seasonal else 0
                ),
                "seasonal_median_return": (
                    seasonal.median_return if seasonal else None
                ),
                "seasonal_positive_rate": (
                    seasonal.positive_rate if seasonal else None
                ),
                "seasonal_recency_weighted_return": (
                    seasonal.recency_weighted_return
                    if seasonal
                    else None
                ),
                "seasonal_confidence": (
                    seasonal.confidence if seasonal else 0.0
                ),
                "seasonal_weight": seasonal_weight,
                "seasonal_leagues": (
                    list(seasonal.leagues) if seasonal else []
                ),
                "seasonal_league_weights": (
                    [
                        dict(observation)
                        for observation in seasonal.league_weights
                    ]
                    if seasonal
                    else []
                ),
                "appreciation_status": (
                    seasonal.appreciation_status
                    if seasonal
                    else "demo_unavailable"
                ),
                "appreciation_horizon_days": (
                    seasonal.appreciation_horizon_days
                    if seasonal
                    else SeasonalModel.APPRECIATION_HORIZON_DAYS
                ),
                "appreciation_sample_leagues": (
                    seasonal.appreciation_sample_leagues
                    if seasonal
                    else 0
                ),
                "appreciation_median_return": (
                    seasonal.appreciation_median_return
                    if seasonal
                    else None
                ),
                "appreciation_recency_weighted_return": (
                    seasonal.appreciation_recency_weighted_return
                    if seasonal
                    else None
                ),
                "appreciation_positive_rate": (
                    seasonal.appreciation_positive_rate
                    if seasonal
                    else None
                ),
                "appreciation_confidence": (
                    seasonal.appreciation_confidence
                    if seasonal
                    else 0.0
                ),
                "history": [
                    {
                        "date": str(row["observed_at"])[:10],
                        "value": round(float(row["divine_value"]), 5),
                    }
                    for row in rows[-30:]
                ],
            }
            if (
                expected_return >= _minimum_net_return(horizon)
                and score > 0.015
                and liquidity >= 0.24
                and confidence_score >= 0.38
                and not falling_knife
                and risk_adjusted_edge > 0
                and not seasonal_veto
                and not forward_timing_veto
                and not appreciation_veto
                and not valuation_veto
            ):
                candidates.append(record)
            else:
                watchlist.append(
                    {
                        "key": item_key,
                        "name": latest["name"],
                        "category": latest["category"],
                        "price_divine": round(current, 5),
                        "reason": (
                            (
                                "Recent downside exceeds the falling-knife "
                                "guardrail."
                                if falling_knife
                                else (
                                    "The upper quartile of comparable "
                                    "completed-league outcomes still shows a "
                                    "material loss over the requested horizon."
                                    if seasonal_veto
                                    else (
                                        "The item is undervalued, but its "
                                        "same-day-aligned forward evidence does "
                                        f"not clear {friction * 100:.0f}% "
                                        "round-trip friction after recent "
                                        "leagues receive more weight; wait for "
                                        "a better historical entry day."
                                        if forward_timing_veto
                                        else (
                                            "The item lacks consistent positive "
                                            f"{SeasonalModel.APPRECIATION_HORIZON_DAYS}-day "
                                            "appreciation evidence at this "
                                            "league day; it is excluded from "
                                            "the appreciation-only ranking."
                                            if appreciation_veto
                                            else (
                                                "The current price is not below "
                                                "the recency-weighted same-day "
                                                "prior-league fair value."
                                                if valuation_veto
                                                else (
                                                    "Signal is not strong enough "
                                                    "after same-day valuation, "
                                                    "forward-history confirmation, "
                                                    "friction, liquidity, volatility, "
                                                    "and current-price adjustments."
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        ),
                        "seasonal_status": (
                            seasonal.status
                            if seasonal
                            else "demo_unavailable"
                        ),
                        "seasonal_sample_leagues": (
                            seasonal.sample_leagues if seasonal else 0
                        ),
                        "seasonal_recency_weighted_return_pct": (
                            round(
                                seasonal.recency_weighted_return * 100,
                                1,
                            )
                            if seasonal
                            else None
                        ),
                        "appreciation_status": (
                            seasonal.appreciation_status
                            if seasonal
                            else "demo_unavailable"
                        ),
                        "appreciation_horizon_days": (
                            seasonal.appreciation_horizon_days
                            if seasonal
                            else SeasonalModel.APPRECIATION_HORIZON_DAYS
                        ),
                        "appreciation_sample_leagues": (
                            seasonal.appreciation_sample_leagues
                            if seasonal
                            else 0
                        ),
                        "appreciation_recency_weighted_return_pct": (
                            round(
                                seasonal.appreciation_recency_weighted_return
                                * 100,
                                1,
                            )
                            if seasonal
                            else None
                        ),
                    }
                )

        def ranking_key(item: dict[str, Any]) -> tuple[float, float]:
            historical_discount = item.get("historical_discount")
            if historical_discount is None:
                primary = item["score"] * item["confidence_score"]
            else:
                # Rank live ideas first by the same-day cross-league valuation
                # gap, tempered (but not replaced) by level reliability.
                primary = float(historical_discount) * (
                    0.50
                    + 0.50
                    * float(item.get("historical_level_confidence") or 0.0)
                )
            secondary = item["score"] * item["confidence_score"]
            item["priority_score"] = primary
            item["priority_tiebreaker"] = secondary
            return primary, secondary

        candidates.sort(key=ranking_key, reverse=True)
        # The official exchange and poe.ninja can describe the same fungible
        # asset under different source identifiers. Keep the strongest signal
        # for a human-readable asset/category pair so the ranking never shows
        # the same market twice.
        selected: list[dict[str, Any]] = []
        seen_assets: set[tuple[str, str]] = set()
        for candidate in candidates:
            signature = (
                re.sub(r"[^a-z0-9]+", "", str(candidate["name"]).lower()),
                str(candidate["category"]).lower(),
            )
            if signature in seen_assets:
                continue
            seen_assets.add(signature)
            selected.append(candidate)
        recommendations = self._rank(selected)
        research_rankings = self._rank_research_candidates(
            watchlist=watchlist,
            histories=histories,
            seasonal_signals=seasonal_signals,
            qualified=recommendations,
        )
        rankings = [
            dict(item)
            for item in (recommendations + research_rankings)[
                :MAX_PRIORITY_RANKINGS
            ]
        ]
        for index, item in enumerate(rankings, start=1):
            item["rank"] = index
        latest_profile = getattr(self.meta_service, "latest_profile", None)
        latest_meta = (
            latest_profile(league.id)
            if callable(latest_profile)
            else None
        )
        latest_meta_source = (
            str(latest_meta.get("source"))
            if latest_meta
            else POE_NINJA_META_SOURCE
        )
        if latest_meta_source == POE_NINJA_META_SOURCE:
            latest_meta_label = POE_NINJA_META_SOURCE_LABEL
            latest_meta_caveat = POE_NINJA_META_CAVEAT
        else:
            latest_meta_label = LADDER_SOURCE_LABEL
            latest_meta_caveat = LADDER_SAMPLE_CAVEAT
        matched_standard = [
            standard_prices[key]
            for key in histories
            if key in standard_prices
        ]
        payload = {
            "mode": "priority_ranking",
            "allocation_mode": "none",
            "generated_at": iso_utc(),
            "price_provenance": (
                {
                    "policy": "offline-demo-fixture",
                    "golden_provider": None,
                    "fail_closed": True,
                    "current_price_sources": ["demo"],
                    "historical_price_sources": [],
                    "standard_price_sources": [],
                    "source_labels": {
                        "demo": "Illustrative offline fixture data"
                    },
                }
                if league.is_demo
                else production_price_provenance()
            ),
            "league": {
                "id": league.id,
                "name": league.name,
                "start_at": league.start_at,
                "day": league.day,
                "phase": phase,
                "demo": league.is_demo,
            },
            "budget": round(budget, 2),
            "budget_affects_ranking": False,
            "horizon": horizon,
            # Retained as nullable compatibility fields for older API
            # consumers. Ranking mode intentionally makes no allocation.
            "reserve": None,
            "invested": None,
            "confidence_note": (
                "Live ideas compare the current Divine price with the item's "
                "recency-weighted price on the same league day in at least "
                "three completed leagues. Each league back receives 72% of "
                "the next newer league's weight; the equal-weight mean remains "
                "visible for audit. The requested-horizon move must clear "
                "round-trip friction after the same recency weighting, and a "
                "separate 21-day profile must show broad appreciation. "
                "Known structurally declining assets and low-end consumable "
                "markets are outside the ranking. Exact Forbidden "
                "Jewels can receive a capped poe.ninja indexed-build demand "
                "adjustment when comparable profiles are available; the "
                "official ladder is fallback-only. The top-100 response puts "
                "fully qualified ideas first, then labels incomplete or "
                "failed signals as research rather than recommendations. "
                "They are probabilistic research aids, not guaranteed profit. "
                + (
                    "This run uses illustrative offline demo data, not live prices."
                    if league.is_demo
                    else "Prices can move before a trade fills; verify in game."
                )
            ),
            "seasonal_model": {
                "required_for_live_ideas": not league.is_demo,
                "minimum_sample_leagues": SeasonalModel.MIN_SAMPLE_LEAGUES,
                "full_sample_leagues": SeasonalModel.FULL_SAMPLE_LEAGUES,
                "items_with_usable_prior": sum(
                    signal.status in {"ok", "provisional"}
                    for signal in seasonal_signals.values()
                ),
                "items_evaluated": len(seasonal_signals),
                "items_with_appreciation_evidence": sum(
                    signal.appreciation_status == "appreciating"
                    for signal in seasonal_signals.values()
                ),
                "alignment": (
                    "exact league-day price level, plus the requested "
                    "forward hold horizon and a 21-day appreciation check"
                ),
                "price_level_estimator": "recency-weighted arithmetic mean",
                "requested_forward_estimator": (
                    "recency-weighted geometric return; must clear "
                    "category round-trip friction"
                ),
                "recency_decay_per_league": (
                    SeasonalModel.RECENCY_DECAY_PER_LEAGUE
                ),
                "appreciation_horizon_days": (
                    SeasonalModel.APPRECIATION_HORIZON_DAYS
                ),
                "appreciation_minimum_sample_leagues": (
                    SeasonalModel.MIN_SAMPLE_LEAGUES
                ),
                "appreciation_minimum_positive_rate": (
                    SeasonalModel.APPRECIATION_MIN_POSITIVE_RATE
                ),
            },
            "investment_scope": {
                "strategy": "appreciation_only",
                "output": "priority_ranking_without_allocation",
                "archive_policy": (
                    "All fetched prices remain stored locally; exclusions "
                    "apply only to recommendations and the watchlist."
                ),
                "excluded_categories": list(
                    EXCLUDED_INVESTMENT_CATEGORIES
                ),
                "excluded_category_items": sum(
                    excluded_category_counts.values()
                ),
                "excluded_category_counts": dict(
                    sorted(excluded_category_counts.items())
                ),
                "known_decline_assets": [
                    lifecycle["name"]
                    for lifecycle in KNOWN_DECLINING_LIFECYCLES.values()
                ],
                "known_decline_items": known_decline_items,
                "appreciation_rejections": appreciation_rejections,
            },
            "standard_model": {
                "available": bool(standard_prices),
                "league": STANDARD_LEAGUE_ID,
                "source": "poe.ninja",
                "role": "long_term_context_only",
                "match_method": "exact_normalized_item_key",
                "affects_short_term_ranking": False,
                "affects_expected_return": False,
                "matched_items": len(matched_standard),
                "current_items": len(histories),
                "latest_observed_at": max(
                    (
                        str(row["observed_at"])
                        for row in standard_prices.values()
                    ),
                    default=None,
                ),
            },
            "meta_model": {
                "available": latest_meta is not None,
                "source": latest_meta_source,
                "source_label": latest_meta_label,
                "scope": "exact Forbidden Flesh and Forbidden Flame variants",
                "sample_size": (
                    int(latest_meta["sample_size"]) if latest_meta else 0
                ),
                "league_day": (
                    int(latest_meta["league_day"]) if latest_meta else None
                ),
                "ascendancies_evaluated": len(meta_signals),
                "caveat": latest_meta_caveat,
            },
            "ranking_summary": {
                "limit": MAX_PRIORITY_RANKINGS,
                "returned": len(rankings),
                "qualified_returned": sum(
                    bool(item.get("eligible_for_recommendation"))
                    for item in rankings
                ),
                "research_returned": sum(
                    not bool(item.get("eligible_for_recommendation"))
                    for item in rankings
                ),
                "qualified_total": len(recommendations),
                "research_total": len(research_rankings),
                "watchlist_total": len(watchlist),
                "ordering": (
                    "Qualified recommendations first; research candidates "
                    "second, ordered by availability and strength of the "
                    "same-day historical valuation signal."
                ),
            },
            "rankings": rankings,
            "recommendations": recommendations,
            # Legacy compact watchlist; the new rankings are built from the
            # complete, untruncated research pool above.
            "watchlist": watchlist[:12],
        }
        if persist:
            self.storage.save_recommendations(
                league.id, budget, horizon, payload
            )
        return payload

    def _generate_forecast_ranking(
        self,
        league: League,
        *,
        budget: float,
        horizon: int,
        persist: bool,
    ) -> dict[str, Any]:
        """Rank exact current markets after explicit universe exclusions.

        This is the active live model. It has no recommendation eligibility
        tier, liquidity gate, or price cap. Small-consumable categories,
        sub-chaos markets, unresolved source identifiers, and assets with a
        persistent completed-league decline lifecycle are omitted before the
        remaining exact variants are ranked by their selected forecast.
        """

        selected_horizon = (
            horizon
            if horizon in FORECAST_HORIZONS
            else min(
                FORECAST_HORIZONS,
                key=lambda value: (abs(value - horizon), value),
            )
        )
        current_day = max(1, int(league.day or 1))
        histories = self.storage.item_histories(
            league.id,
            days=max(90, current_day + 14),
            sources=CURRENT_PRICE_SOURCES,
        )
        standard_prices = self.storage.latest_item_prices(
            STANDARD_LEAGUE_ID,
            sources=STANDARD_PRICE_SOURCES,
        )
        broad_id_set = frozenset(BROADLY_COVERED_LEAGUE_IDS)
        newest_first = list(reversed(BROADLY_COVERED_LEAGUES))
        age_rank = {
            spec.league_id: index
            for index, spec in enumerate(newest_first)
        }
        raw_league_weight = {
            spec.league_id: SeasonalModel.RECENCY_DECAY_PER_LEAGUE**index
            for index, spec in enumerate(newest_first)
        }
        excluded_category_counts: defaultdict[str, int] = defaultdict(int)
        excluded_below_one_chaos_items = 0
        excluded_unknown_chaos_items = 0
        excluded_stale_current_items = 0
        unresolved_items = 0
        current_source_verified_at = self.storage.latest_source_success_at(
            "poe.ninja",
            league.id,
        )
        latest_sync_window = self.storage.latest_successful_sync_window(
            league.id
        )
        latest_sync_started_at = parse_datetime(
            str((latest_sync_window or {}).get("started_at") or "")
        )

        chaos_unit_divine: float | None = None
        chaos_rows = _daily_rows(histories.get("currency:chaos-orb", []))
        if chaos_rows:
            candidate = float(chaos_rows[-1].get("divine_value") or 0.0)
            if math.isfinite(candidate) and candidate > 0:
                chaos_unit_divine = candidate

        current_items: dict[str, dict[str, Any]] = {}
        for item_key, raw_rows in histories.items():
            rows = _daily_rows(raw_rows)
            if not rows:
                continue
            latest = rows[-1]
            observed_at = parse_datetime(str(latest.get("observed_at") or ""))
            league_start = parse_datetime(league.start_at)
            if (
                observed_at is None
                or league_start is None
                or calculate_league_day(observed_at, league_start)
                < current_day - 1
                or (
                    latest_sync_started_at is not None
                    and observed_at < latest_sync_started_at
                )
            ):
                excluded_stale_current_items += 1
                continue
            category = str(latest["category"])
            if _category_is_excluded(category):
                excluded_category_counts[category] += 1
                continue
            current = float(latest.get("divine_value") or 0.0)
            if not math.isfinite(current) or current <= 0:
                continue
            details = latest.get("details")
            if (
                str(latest.get("source") or "")
                == "ggg-currency-exchange"
                and isinstance(details, dict)
                and details.get("identifier_resolved") is False
            ):
                unresolved_items += 1
                continue
            try:
                direct_chaos = float(latest.get("chaos_value") or 0.0)
            except (TypeError, ValueError):
                direct_chaos = 0.0
            current_chaos = (
                direct_chaos
                if math.isfinite(direct_chaos) and direct_chaos > 0
                else (
                    current / chaos_unit_divine
                    if chaos_unit_divine is not None
                    else None
                )
            )
            if current_chaos is None or not math.isfinite(current_chaos):
                excluded_unknown_chaos_items += 1
                continue
            if current_chaos < MINIMUM_INVESTMENT_PRICE_CHAOS:
                excluded_below_one_chaos_items += 1
                continue
            current_items[item_key] = {
                "latest": latest,
                "current": current,
                "current_chaos": current_chaos,
            }

        # Keep peak memory bounded now that official poe.ninja dumps contain
        # tens of millions of exact daily rows. Each item's assessment is
        # independent, so batches can be reduced and merged without changing
        # the classification result.
        decline_assessments: dict[str, dict[str, Any]] = {}
        lifecycle_item_keys = sorted(current_items)
        for offset in range(0, len(lifecycle_item_keys), 250):
            lifecycle_rows = self.storage.seasonal_lifecycle_rows(
                lifecycle_item_keys[offset : offset + 250],
                BROADLY_COVERED_LEAGUE_IDS,
                minimum_league_day=1,
                maximum_league_day=DECLINE_CURVE_MAXIMUM_DAY,
                minimum_confidence=HISTORICAL_MODEL_CONFIDENCE_FLOOR,
                sources=HISTORICAL_PRICE_SOURCES,
            )
            decline_assessments.update(
                _historical_decline_assessments(
                    lifecycle_rows,
                    league_weights=raw_league_weight,
                )
            )
        automatic_decline_vetoes: list[dict[str, Any]] = []
        known_decline_vetoes: list[dict[str, Any]] = []
        for item_key in list(current_items):
            current_item = current_items[item_key]
            latest = current_item["latest"]
            assessment = decline_assessments.get(item_key)
            if assessment and assessment["historical_decline"]:
                automatic_decline_vetoes.append(
                    {
                        "key": item_key,
                        "name": str(latest["name"]),
                        "weighted_support": assessment[
                            "weighted_support"
                        ],
                        "sample_leagues": assessment["sample_leagues"],
                        "declining_leagues": assessment[
                            "declining_leagues"
                        ],
                        "aggregate_weekly_gain": assessment[
                            "aggregate_weekly_gain"
                        ],
                        "aggregate_early_late_ratio": assessment[
                            "aggregate_early_late_ratio"
                        ],
                    }
                )
                current_items.pop(item_key)
                continue
            known_lifecycle = _known_declining_lifecycle(
                item_key,
                str(latest["name"]),
            )
            if known_lifecycle is not None:
                known_decline_vetoes.append(
                    {
                        "key": item_key,
                        "name": str(latest["name"]),
                        "code": known_lifecycle["code"],
                        "reason": known_lifecycle["reason"],
                    }
                )
                current_items.pop(item_key)

        item_keys = list(current_items)

        historical_rows_by_horizon: dict[
            int, dict[str, list[dict[str, Any]]]
        ] = {}
        for forecast_horizon in FORECAST_HORIZONS:
            grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
            rows = self.storage.seasonal_entry_rows(
                current_day + forecast_horizon,
                item_keys,
                minimum_confidence=HISTORICAL_MODEL_CONFIDENCE_FLOOR,
                sources=HISTORICAL_PRICE_SOURCES,
            )
            for row in rows:
                if str(row["league_id"]) in broad_id_set:
                    grouped[str(row["item_key"])].append(row)
            historical_rows_by_horizon[forecast_horizon] = dict(grouped)

        same_day_rows: defaultdict[str, list[dict[str, Any]]] = defaultdict(
            list
        )
        for row in self.storage.seasonal_entry_rows(
            current_day,
            item_keys,
            minimum_confidence=HISTORICAL_MODEL_CONFIDENCE_FLOOR,
            sources=HISTORICAL_PRICE_SOURCES,
        ):
            if str(row["league_id"]) in broad_id_set:
                same_day_rows[str(row["item_key"])].append(row)

        # A current-curve projection can only affect an item that has a
        # historical future target at one of the three forecast horizons.
        # Loading daily curves for every current market caused thousands of
        # individual SQLite queries and made the initial dashboard request take
        # more than a minute. Null-forecast rows still remain in the filtered
        # universe; their full curve is loaded on demand by /api/history.
        forecastable_keys = {
            item_key
            for rows_by_item in historical_rows_by_horizon.values()
            for item_key in rows_by_item
        }
        current_curves = {
            item_key: self.storage.daily_item_history(
                league.id,
                item_key,
                league.start_at,
                minimum_confidence=0.0,
                sources=CURRENT_PRICE_SOURCES,
            )
            for item_key in forecastable_keys
        }

        def weighted_level(
            rows: list[dict[str, Any]],
        ) -> tuple[float | None, list[dict[str, Any]]]:
            observations = []
            for row in rows:
                league_id = str(row["league_id"])
                price = float(row.get("entry_divine") or 0.0)
                confidence = float(row.get("entry_confidence") or 0.0)
                if (
                    league_id not in raw_league_weight
                    or price <= 0
                    or confidence < HISTORICAL_MODEL_CONFIDENCE_FLOOR
                ):
                    continue
                observations.append(
                    {
                        "league_id": league_id,
                        "league_name": row.get("league_name"),
                        "league_day": int(row["entry_day"]),
                        "price_divine": price,
                        "confidence": confidence,
                        "source": row.get("source"),
                        "source_item_id": row.get("source_item_id"),
                        "age_rank": age_rank[league_id],
                        "raw_weight": raw_league_weight[league_id],
                    }
                )
            observations.sort(key=lambda row: int(row["age_rank"]))
            weight_total = sum(
                float(row["raw_weight"]) for row in observations
            )
            if weight_total <= 0:
                return None, observations
            for observation in observations:
                observation["normalized_weight"] = (
                    float(observation["raw_weight"]) / weight_total
                )
            return (
                sum(
                    float(row["price_divine"])
                    * float(row["normalized_weight"])
                    for row in observations
                ),
                observations,
            )

        rows_for_ranking: list[dict[str, Any]] = []
        meta_signals: dict[str, dict[str, Any]] = {}
        for item_key, current_item in current_items.items():
            latest = current_item["latest"]
            current = float(current_item["current"])
            current_chaos = float(current_item["current_chaos"])
            curve = current_curves.get(item_key, [])
            details = (
                latest.get("details")
                if isinstance(latest.get("details"), dict)
                else {}
            )
            item_level = _item_level_from_identity(
                item_key,
                str(latest["category"]),
                details,
            )
            metadata = (
                details.get("metadata")
                if isinstance(details.get("metadata"), dict)
                else {}
            )
            ascendancy = str(metadata.get("ascendancy") or "").strip()
            meta_signal: dict[str, Any] = {
                "status": "not_applicable",
                "multiplier": 1.0,
            }
            if (
                str(latest["category"]).casefold() == "forbiddenjewel"
                and ascendancy
            ):
                if ascendancy not in meta_signals:
                    meta_signals[ascendancy] = (
                        self.meta_service.ascendancy_multiplier(
                            league,
                            BROADLY_COVERED_LEAGUES,
                            ascendancy,
                        )
                    )
                meta_signal = meta_signals[ascendancy]
            meta_multiplier = (
                float(meta_signal.get("multiplier") or 1.0)
                if meta_signal.get("status") == "ok"
                else 1.0
            )
            if not math.isfinite(meta_multiplier) or meta_multiplier <= 0:
                meta_multiplier = 1.0
            same_day_price, same_day_observations = weighted_level(
                same_day_rows.get(item_key, [])
            )
            horizon_forecasts: dict[str, dict[str, Any]] = {}

            for forecast_horizon in FORECAST_HORIZONS:
                historical_target, historical_observations = weighted_level(
                    historical_rows_by_horizon[forecast_horizon].get(
                        item_key,
                        [],
                    )
                )
                meta_adjusted_target = (
                    historical_target * meta_multiplier
                    if historical_target is not None
                    else None
                )
                historical_gain = (
                    meta_adjusted_target / current - 1.0
                    if meta_adjusted_target is not None
                    else None
                )
                current_projection = _current_curve_projection(
                    curve,
                    forecast_horizon,
                )
                current_gain = current_projection["capped_gain"]
                use_current_curve = (
                    historical_gain is not None
                    and int(current_projection["point_count"]) >= 2
                    and current_gain is not None
                )
                if historical_gain is None:
                    expected_gain = None
                elif use_current_curve:
                    expected_gain = math.exp(
                        HISTORICAL_FORECAST_WEIGHT
                        * math.log1p(historical_gain)
                        + CURRENT_CURVE_FORECAST_WEIGHT
                        * math.log1p(float(current_gain))
                    ) - 1.0
                else:
                    expected_gain = historical_gain

                horizon_forecasts[str(forecast_horizon)] = {
                    "days": forecast_horizon,
                    "expected_gain": expected_gain,
                    "expected_gain_pct": (
                        expected_gain * 100.0
                        if expected_gain is not None
                        else None
                    ),
                    "expected_price_divine": (
                        current * (1.0 + expected_gain)
                        if expected_gain is not None
                        else None
                    ),
                    "raw_historical_target_divine": historical_target,
                    "historical_target_price_divine": meta_adjusted_target,
                    "historical_target_divine": meta_adjusted_target,
                    "meta_adjusted_historical_target_divine": (
                        meta_adjusted_target
                    ),
                    "meta_multiplier": meta_multiplier,
                    "meta_signal": meta_signal,
                    "historical_target_gain": historical_gain,
                    "historical_target_gain_pct": (
                        historical_gain * 100.0
                        if historical_gain is not None
                        else None
                    ),
                    "historical_sample_leagues": len(
                        historical_observations
                    ),
                    "historical_leagues": [
                        observation["league_id"]
                        for observation in historical_observations
                    ],
                    "sample_leagues": [
                        observation["league_id"]
                        for observation in historical_observations
                    ],
                    "sample_league_names": [
                        observation["league_name"]
                        for observation in historical_observations
                    ],
                    "historical_observations": historical_observations,
                    "current_curve_gain_pct": (
                        current_projection["capped_gain"] * 100.0
                        if current_projection["capped_gain"] is not None
                        else None
                    ),
                    "current_curve_projection": {
                        **current_projection,
                        "raw_gain_pct": (
                            current_projection["raw_gain"] * 100.0
                            if current_projection["raw_gain"] is not None
                            else None
                        ),
                        "capped_gain_pct": (
                            current_projection["capped_gain"] * 100.0
                            if current_projection["capped_gain"] is not None
                            else None
                        ),
                    },
                    "blend": {
                        "method": "weighted_log_return",
                        "historical_weight": (
                            HISTORICAL_FORECAST_WEIGHT
                            if use_current_curve
                            else (
                                1.0
                                if historical_gain is not None
                                else None
                            )
                        ),
                        "current_curve_weight": (
                            CURRENT_CURVE_FORECAST_WEIGHT
                            if use_current_curve
                            else (
                                0.0
                                if historical_gain is not None
                                else None
                            )
                        ),
                        "current_curve_used": use_current_curve,
                    },
                }

            selected = horizon_forecasts[str(selected_horizon)]
            standard_anchor = standard_prices.get(item_key)
            standard_value = (
                float(standard_anchor["divine_value"])
                if standard_anchor is not None
                and float(standard_anchor.get("divine_value") or 0.0) > 0
                else None
            )
            liquidity = _liquidity_score(latest)
            selected_expected = selected["expected_gain"]
            rationale = (
                f"{selected_horizon}-day gross forecast "
                + (
                    f"{selected_expected * 100:+.1f}%"
                    if selected_expected is not None
                    else "unavailable because no Medium/High-confidence "
                    "broad-league target-day price is stored"
                )
                + (
                    f"; historical target "
                    f"{selected['historical_target_price_divine']:.5g} div "
                    f"from {selected['historical_sample_leagues']} broad "
                    f"league(s)"
                    if selected["historical_target_price_divine"] is not None
                    else ""
                )
                + (
                    f"; {ascendancy} meta multiplier "
                    f"{meta_multiplier:.3g}x"
                    if meta_multiplier != 1.0
                    else ""
                )
                + (
                    f"; current curve component "
                    f"{selected['current_curve_projection']['capped_gain'] * 100:+.1f}%"
                    if selected["blend"]["current_curve_used"]
                    else "; current curve component not blended"
                )
                + "."
            )
            row = {
                "key": item_key,
                "curve_key": item_key,
                "name": latest["name"],
                "category": latest["category"],
                "trade_identity": {
                    "variant": details.get("variant"),
                    "base_type": (
                        details.get("baseType")
                        or details.get("base_type")
                        or details.get("base")
                    ),
                    "gem_level": (
                        details.get("gemLevel")
                        if details.get("gemLevel") is not None
                        else details.get("gem_level")
                    ),
                    "gem_quality": (
                        details.get("gemQuality")
                        if details.get("gemQuality") is not None
                        else details.get("gem_quality")
                    ),
                    "item_level": item_level,
                    "links": details.get("links"),
                    "map_tier": (
                        details.get("mapTier")
                        if details.get("mapTier") is not None
                        else details.get("map_tier")
                    ),
                    "corrupted": details.get("corrupted"),
                    "passive_name": (
                        details.get("passiveName")
                        or details.get("passive_name")
                        or metadata.get("passiveName")
                    ),
                },
                "price_divine": current,
                "current_price_divine": current,
                "price_chaos": current_chaos,
                "current_observed_at": latest.get("observed_at"),
                "current_source": latest.get("source"),
                "market_volume": latest.get("volume"),
                "listing_count": latest.get("listing_count"),
                "liquidity": liquidity,
                "liquidity_score": liquidity,
                "historical_same_day_price_divine": same_day_price,
                "weighted_historical_price_divine": same_day_price,
                "meta_adjusted_same_day_price_divine": (
                    same_day_price * meta_multiplier
                    if same_day_price is not None
                    else None
                ),
                "meta_multiplier": meta_multiplier,
                "meta_signal": meta_signal,
                "historical_same_day_sample_leagues": len(
                    same_day_observations
                ),
                "historical_same_day_observations": same_day_observations,
                "historical_discount_pct": (
                    (same_day_price - current) / same_day_price * 100.0
                    if same_day_price is not None
                    and same_day_price > 0
                    else None
                ),
                "standard_anchor_divine": standard_value,
                "standard_anchor_source": (
                    standard_anchor.get("source")
                    if standard_anchor is not None
                    else None
                ),
                "standard_anchor_gap_pct": (
                    (standard_value - current) / standard_value * 100.0
                    if standard_value is not None
                    else None
                ),
                "selected_horizon_days": selected_horizon,
                "expected_gain": selected_expected,
                "expected_gain_pct": (
                    selected_expected * 100.0
                    if selected_expected is not None
                    else None
                ),
                "expected_return": selected_expected,
                "expected_return_pct": (
                    selected_expected * 100.0
                    if selected_expected is not None
                    else None
                ),
                "expected_price_divine": selected["expected_price_divine"],
                "target_divine": selected["expected_price_divine"],
                "historical_target_price_divine": selected[
                    "historical_target_price_divine"
                ],
                "historical_target_divine": selected[
                    "historical_target_divine"
                ],
                "historical_target_gain_pct": selected[
                    "historical_target_gain_pct"
                ],
                "historical_sample_leagues": selected[
                    "historical_sample_leagues"
                ],
                "current_curve_projection_gain_pct": selected[
                    "current_curve_projection"
                ]["capped_gain_pct"],
                "current_curve_point_count": selected[
                    "current_curve_projection"
                ]["point_count"],
                "current_curve_log_slope_per_day": selected[
                    "current_curve_projection"
                ]["log_slope_per_day"],
                "forecast_3d": horizon_forecasts["3"],
                "forecast_7d": horizon_forecasts["7"],
                "forecast_14d": horizon_forecasts["14"],
                "forecast_horizons": horizon_forecasts,
                "expected_gain_3d": horizon_forecasts["3"][
                    "expected_gain"
                ],
                "expected_gain_3d_pct": horizon_forecasts["3"][
                    "expected_gain_pct"
                ],
                "expected_gain_7d": horizon_forecasts["7"][
                    "expected_gain"
                ],
                "expected_gain_7d_pct": horizon_forecasts["7"][
                    "expected_gain_pct"
                ],
                "expected_gain_14d": horizon_forecasts["14"][
                    "expected_gain"
                ],
                "expected_gain_14d_pct": horizon_forecasts["14"][
                    "expected_gain_pct"
                ],
                "horizons": horizon_forecasts,
                "history": [
                    {
                        "league_day": int(point["league_day"]),
                        "date": str(point["observed_at"])[:10],
                        "value": float(point["divine_value"]),
                        "confidence": float(point["confidence"]),
                        "source": point["source"],
                    }
                    for point in curve
                ],
                "rationale": rationale,
            }
            rows_for_ranking.append(row)

        # Current histories are already canonicalized by exact item key. Keep
        # distinct variants even when they share a display name: gem level,
        # quality, corruption, and other variants can have very different
        # prices. Exact-key deduplication only protects against an accidental
        # duplicate row without collapsing those investable identities.
        unique_rows: list[dict[str, Any]] = []
        seen_item_keys: set[str] = set()
        for row in sorted(
            rows_for_ranking,
            key=lambda item: (
                str(item["name"]).casefold(),
                str(item["category"]).casefold(),
                str(item["key"]),
            ),
        ):
            item_key = str(row["key"])
            if item_key in seen_item_keys:
                continue
            seen_item_keys.add(item_key)
            unique_rows.append(row)

        unique_rows.sort(
            key=lambda item: (
                item["expected_gain"] is None,
                (
                    -float(item["expected_gain"])
                    if item["expected_gain"] is not None
                    else 0.0
                ),
                str(item["name"]).casefold(),
                str(item["key"]),
            )
        )
        total_universe = len(unique_rows)
        rankings = unique_rows
        compact_row_keys = (
            "key",
            "curve_key",
            "name",
            "category",
            "trade_identity",
            "price_divine",
            "current_price_divine",
            "price_chaos",
            "current_observed_at",
            "current_source",
            "historical_same_day_price_divine",
            "meta_adjusted_same_day_price_divine",
            "meta_multiplier",
            "meta_signal",
            "historical_discount_pct",
            "standard_anchor_divine",
            "standard_anchor_source",
            "standard_anchor_gap_pct",
            "selected_horizon_days",
            "expected_gain",
            "expected_gain_pct",
            "expected_return",
            "expected_return_pct",
            "expected_price_divine",
            "target_divine",
            "historical_target_price_divine",
            "historical_target_divine",
            "historical_target_gain_pct",
            "historical_sample_leagues",
            "current_curve_projection_gain_pct",
            "current_curve_point_count",
            "current_curve_log_slope_per_day",
            "forecast_3d",
            "forecast_7d",
            "forecast_14d",
            "history",
            "rank",
            "priority_score",
        )
        retain_null_keys = {
            "expected_gain",
            "priority_score",
            "history",
        }
        for rank, row in enumerate(rankings, start=1):
            row["rank"] = rank
            row["priority_score"] = row["expected_gain"]
            # The browser accepts several legacy forecast aliases, but sending
            # every alias for thousands of rows multiplies the payload many
            # times over. Keep the canonical forecast_3d/7d/14d objects and
            # compact entirely missing horizons to their day identifier.
            row.pop("forecast_horizons", None)
            row.pop("horizons", None)
            row.pop("expected_gain_3d", None)
            row.pop("expected_gain_3d_pct", None)
            row.pop("expected_gain_7d", None)
            row.pop("expected_gain_7d_pct", None)
            row.pop("expected_gain_14d", None)
            row.pop("expected_gain_14d_pct", None)
            row.pop("historical_same_day_observations", None)
            for forecast_horizon in FORECAST_HORIZONS:
                forecast_key = f"forecast_{forecast_horizon}d"
                forecast = row[forecast_key]
                if (
                    forecast.get("expected_gain") is None
                    and forecast.get("historical_target_price_divine") is None
                ):
                    row[forecast_key] = {"days": forecast_horizon}
            compact_row = {
                key: row[key]
                for key in compact_row_keys
                if key in row
                and (
                    row[key] is not None
                    or key in retain_null_keys
                )
            }
            compact_row["trade_identity"] = {
                key: value
                for key, value in compact_row.get(
                    "trade_identity",
                    {},
                ).items()
                if value is not None
            }
            # Price curves are served lazily by /api/history when a row is
            # opened. Keeping a second copy on every list row would make the
            # full paginated universe unnecessarily large.
            compact_row["history"] = []
            row.clear()
            row.update(compact_row)

        payload = {
            "mode": "forecast_ranking",
            "allocation_mode": "none",
            "generated_at": iso_utc(),
            "price_provenance": production_price_provenance(),
            "league": {
                "id": league.id,
                "name": league.name,
                "start_at": league.start_at,
                "day": league.day,
                "phase": _league_phase(league.day)[0],
                "demo": False,
            },
            "budget": round(budget, 2),
            "budget_affects_ranking": False,
            "horizon": selected_horizon,
            "available_horizons": list(FORECAST_HORIZONS),
            "reserve": None,
            "invested": None,
            "confidence_note": (
                "Within the investment universe, this is a gross-price "
                "forecast rather than a pass/fail recommendation. Sub-chaos "
                "markets and persistent completed-league decliners are omitted. "
                "For each horizon, the model compares today's current-league "
                "price with the recency-weighted target-day price in Settlers, "
                "Mercenaries, Keepers, and Mirage. Only poe.ninja historical "
                "observations graded Medium or High (normalized confidence "
                "at least 0.5) enter that target or the displayed weighted "
                "curve; Low observations remain archived for audit. When at "
                "least two "
                "current-league days exist, it blends 70% historical target "
                "and 30% robust current-curve projection in log-return space. "
                "Forbidden Jewel historical targets are adjusted by the "
                "current-versus-historical ascendancy-share multiplier when "
                "a valid meta snapshot is available. "
                "Missing historical target days remain null."
            ),
            "forecast_model": {
                "historical_leagues": list(
                    BROADLY_COVERED_LEAGUE_IDS
                ),
                "historical_league_calendar_newest_first": [
                    {
                        "league_id": spec.league_id,
                        "league_name": spec.name,
                        "start_at": spec.start_at,
                        "age_rank": age_rank[spec.league_id],
                        "raw_weight": raw_league_weight[spec.league_id],
                    }
                    for spec in newest_first
                ],
                "recency_decay_per_league": (
                    SeasonalModel.RECENCY_DECAY_PER_LEAGUE
                ),
                "historical_target_estimator": (
                    "recency-weighted arithmetic target-day Divine price over "
                    "poe.ninja Medium/High observations only"
                ),
                "historical_confidence_floor": (
                    HISTORICAL_MODEL_CONFIDENCE_FLOOR
                ),
                "low_confidence_history": "retained locally; audit-only",
                "current_curve_estimator": (
                    "Theil-Sen log-price slope over up to seven exact current "
                    "league-day observations"
                ),
                "current_projection_gain_cap": {
                    "minimum": CURRENT_PROJECTION_GAIN_FLOOR,
                    "maximum": CURRENT_PROJECTION_GAIN_CEILING,
                },
                "blend": {
                    "space": "log_return",
                    "historical_weight": HISTORICAL_FORECAST_WEIGHT,
                    "current_curve_weight": CURRENT_CURVE_FORECAST_WEIGHT,
                    "minimum_current_curve_points": 2,
                    "historical_required": True,
                },
                "gross_gain": True,
                "friction_deducted": False,
                "eligibility_gates": [],
                "universe_filters": [
                    "excluded small-consumable categories",
                    "current price below one Chaos Orb",
                    "current poe.ninja observation older than one league day",
                    "current item absent from the latest successful or partial poe.ninja sync",
                    "persistent broad-league structural decline",
                    "unresolved source identity",
                ],
            },
            "investment_scope": {
                "strategy": "filtered_forecast_ranking",
                "excluded_categories": list(
                    EXCLUDED_INVESTMENT_CATEGORIES
                ),
                "excluded_category_items": sum(
                    excluded_category_counts.values()
                ),
                "excluded_category_counts": dict(
                    sorted(excluded_category_counts.items())
                ),
                "minimum_price_chaos": MINIMUM_INVESTMENT_PRICE_CHAOS,
                "excluded_below_one_chaos_items": (
                    excluded_below_one_chaos_items
                ),
                "excluded_unknown_chaos_items": (
                    excluded_unknown_chaos_items
                ),
                "excluded_stale_current_items": excluded_stale_current_items,
                "current_source_verified_at": current_source_verified_at,
                "automatic_decline_items": len(automatic_decline_vetoes),
                "automatic_decline_vetoes": sorted(
                    automatic_decline_vetoes,
                    key=lambda item: (
                        str(item["name"]).casefold(),
                        str(item["key"]),
                    ),
                ),
                "excluded_item_count": (
                    sum(excluded_category_counts.values())
                    + excluded_below_one_chaos_items
                    + excluded_unknown_chaos_items
                    + excluded_stale_current_items
                    + len(automatic_decline_vetoes)
                    + len(known_decline_vetoes)
                    + unresolved_items
                ),
                "unresolved_identity_items": unresolved_items,
                "known_decline_vetoes": known_decline_vetoes,
                "decline_model": {
                    "currency": "Divine Orb",
                    "historical_leagues": list(
                        BROADLY_COVERED_LEAGUE_IDS
                    ),
                    "maximum_league_day": DECLINE_CURVE_MAXIMUM_DAY,
                    "weekly_median": True,
                    "minimum_points_per_week": (
                        DECLINE_MINIMUM_POINTS_PER_WEEK
                    ),
                    "minimum_weeks_per_league": DECLINE_MINIMUM_WEEKS,
                    "minimum_day_span": DECLINE_MINIMUM_DAY_SPAN,
                    "maximum_weekly_gain": DECLINE_MAXIMUM_WEEKLY_GAIN,
                    "minimum_negative_pair_fraction": (
                        DECLINE_MINIMUM_NEGATIVE_PAIR_FRACTION
                    ),
                    "maximum_early_late_ratio": (
                        DECLINE_MAXIMUM_EARLY_LATE_RATIO
                    ),
                    "minimum_declining_leagues": (
                        DECLINE_MINIMUM_LEAGUE_VOTES
                    ),
                    "minimum_recency_weighted_support": (
                        DECLINE_MINIMUM_WEIGHTED_SUPPORT
                    ),
                    "recency_decay_per_league": (
                        SeasonalModel.RECENCY_DECAY_PER_LEAGUE
                    ),
                },
            },
            "ranking_summary": {
                "limit": None,
                "returned": len(rankings),
                "universe_total": total_universe,
                "pagination": {
                    "mode": "client",
                    "default_page_size": 50,
                },
                "with_selected_horizon_forecast": sum(
                    row["expected_gain"] is not None
                    for row in unique_rows
                ),
                "without_selected_horizon_forecast": sum(
                    row["expected_gain"] is None
                    for row in unique_rows
                ),
                "ordering": (
                    f"Expected gross {selected_horizon}-day gain descending; "
                    "missing forecasts last."
                ),
            },
            "rankings": rankings,
            "recommendations": rankings,
            "watchlist": [],
        }
        if persist:
            self.storage.save_recommendations(
                league.id,
                budget,
                selected_horizon,
                payload,
            )
        return payload

    def _rank(
        self,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Format already-qualified candidates as a pure priority ranking."""

        results: list[dict[str, Any]] = []
        for item in candidates:
            category = str(item["category"])
            price = float(item["price_divine"])
            volatility = float(item["volatility"])
            expected = float(item["expected_return"])
            if (
                item.get("meta_status") == "ok"
                and item.get("historical_recency_weighted_divine") is not None
            ):
                valuation_factor = (
                    f"Same-day valuation: {item['price_divine']:.5g} div "
                    f"vs {item['historical_recency_weighted_divine']:.5g} "
                    f"div recency-weighted history, "
                    f"{item['historical_average_divine']:.5g} div "
                    f"unweighted mean, and "
                    f"{item['historical_fair_value_divine']:.5g} div "
                    f"meta-adjusted fair value "
                    f"({item['historical_discount'] * 100:+.1f}% gap)."
                )
            elif item.get("historical_recency_weighted_divine") is not None:
                valuation_factor = (
                    f"Same-day valuation: {item['price_divine']:.5g} div "
                    f"vs {item['historical_recency_weighted_divine']:.5g} "
                    f"div recency-weighted history "
                    f"(unweighted mean "
                    f"{item['historical_average_divine']:.5g} div; "
                    f"{item['historical_discount'] * 100:+.1f}% gap)."
                )
            else:
                valuation_factor = (
                    "No completed-league valuation is used in demo mode."
                )
            factors = [
                valuation_factor,
                (
                    f"Forward confirmation: "
                    f"{item['seasonal_recency_weighted_return'] * 100:+.1f}% "
                    f"recency-weighted, "
                    f"{item['seasonal_median_return'] * 100:+.1f}% median; "
                    f"{item['seasonal_positive_rate'] * 100:.0f}% positive "
                    f"across {item['seasonal_sample_leagues']} leagues."
                    if (
                        item.get("seasonal_median_return") is not None
                        and item.get("seasonal_recency_weighted_return")
                        is not None
                    )
                    else "Current-league history drives the demo forecast."
                ),
                (
                    f"Appreciation regime: "
                    f"{item['appreciation_recency_weighted_return'] * 100:+.1f}% "
                    f"recency-weighted and "
                    f"{item['appreciation_median_return'] * 100:+.1f}% "
                    f"median over {item['appreciation_horizon_days']} days; "
                    f"{item['appreciation_positive_rate'] * 100:.0f}% "
                    f"positive across "
                    f"{item['appreciation_sample_leagues']} leagues."
                    if (
                        item.get("appreciation_median_return") is not None
                        and item.get(
                            "appreciation_recency_weighted_return"
                        )
                        is not None
                    )
                    else "No completed-league appreciation regime is used "
                    "in demo mode."
                ),
                (
                    "Priority ranking only: budget, unit price, category "
                    "concentration, and position sizing do not affect this "
                    "item's inclusion or rank."
                ),
            ]
            if item.get("standard_anchor_divine") is not None:
                factors.append(
                    f"Long-term context: Standard is "
                    f"{float(item['standard_anchor_divine']):.5g} div "
                    f"({float(item['standard_anchor_gap']) * 100:+.1f}% "
                    "gap by Standard value). This anchor does not affect "
                    "short-term ranking or expected return."
                )
            if item.get("meta_status") == "ok":
                meta_sample_name = (
                    "poe.ninja indexed-build sample"
                    if item.get("meta_source") == POE_NINJA_META_SOURCE
                    else "top-ladder sample"
                )
                factors.insert(
                    1,
                    (
                        f"Meta demand: {item['meta_ascendancy']} is "
                        f"{float(item['meta_current_share']) * 100:.1f}% of "
                        f"the current {meta_sample_name} versus "
                        f"{float(item['meta_historical_share']) * 100:.1f}% "
                        f"in the historical baseline; fair value ×"
                        f"{float(item['meta_multiplier']):.2f}."
                    ),
                )
            result = {
                "rank": len(results) + 1,
                "ranking_tier": 1,
                "eligibility_status": "qualified",
                "eligible_for_recommendation": True,
                "qualification_reason": None,
                "priority_score": round(
                    float(item.get("priority_score") or 0.0) * 100,
                    2,
                ),
                "key": item["key"],
                "curve_key": item["key"],
                "name": item["name"],
                "category": category,
                "price_divine": round(price, 5),
                "current_price_divine": round(price, 5),
                "weighted_historical_price_divine": (
                    round(
                        float(item["historical_recency_weighted_divine"]),
                        5,
                    )
                    if item.get("historical_recency_weighted_divine")
                    is not None
                    else None
                ),
                "price_chaos": (
                    round(float(item["price_chaos"]), 2)
                    if item["price_chaos"] is not None
                    else None
                ),
                "allocation_divine": None,
                "quantity": None,
                "position_unit_cap": None,
                "allocation_exception": None,
                "high_ticket_single_unit": False,
                "expected_return_pct": round(expected * 100, 1),
                "confidence": round(float(item["confidence_score"]), 3),
                "confidence_label": _label(float(item["confidence_score"])),
                "confidence_score": round(float(item["confidence_score"]), 3),
                "liquidity": round(float(item["liquidity_score"]), 3),
                "liquidity_label": _label(float(item["liquidity_score"])),
                "liquidity_score": round(float(item["liquidity_score"]), 3),
                "entry_ceiling_divine": round(
                    price * (1.02 + 0.015 * item["confidence_score"]), 5
                ),
                "target_divine": round(price * (1 + expected), 5),
                "stop_divine": round(
                    price * (1 - _clamp(0.08 + volatility * 1.5, 0.08, 0.24)),
                    5,
                ),
                "rationale": item["rationale"],
                "factors": factors,
                "historical_fair_value_divine": (
                    round(float(item["historical_fair_value_divine"]), 5)
                    if item.get("historical_fair_value_divine") is not None
                    else None
                ),
                "historical_average_divine": (
                    round(float(item["historical_average_divine"]), 5)
                    if item.get("historical_average_divine") is not None
                    else None
                ),
                "historical_recency_weighted_divine": (
                    round(
                        float(item["historical_recency_weighted_divine"]),
                        5,
                    )
                    if item.get("historical_recency_weighted_divine")
                    is not None
                    else None
                ),
                "historical_median_divine": (
                    round(float(item["historical_median_divine"]), 5)
                    if item.get("historical_median_divine") is not None
                    else None
                ),
                "historical_discount": (
                    round(float(item["historical_discount"]), 6)
                    if item.get("historical_discount") is not None
                    else None
                ),
                "historical_discount_pct": (
                    round(float(item["historical_discount"]) * 100, 1)
                    if item.get("historical_discount") is not None
                    else None
                ),
                "standard_anchor_divine": (
                    round(float(item["standard_anchor_divine"]), 5)
                    if item.get("standard_anchor_divine") is not None
                    else None
                ),
                "standard_anchor_gap": (
                    round(float(item["standard_anchor_gap"]), 6)
                    if item.get("standard_anchor_gap") is not None
                    else None
                ),
                "standard_anchor_ratio": (
                    round(float(item["standard_anchor_ratio"]), 6)
                    if item.get("standard_anchor_ratio") is not None
                    else None
                ),
                "standard_anchor_observed_at": item.get(
                    "standard_anchor_observed_at"
                ),
                "standard_anchor_source": item.get(
                    "standard_anchor_source"
                ),
                "historical_level_dispersion": (
                    round(float(item["historical_level_dispersion"]), 6)
                    if item.get("historical_level_dispersion") is not None
                    else None
                ),
                "historical_level_dispersion_pct": (
                    round(
                        float(item["historical_level_dispersion"]) * 100,
                        1,
                    )
                    if item.get("historical_level_dispersion") is not None
                    else None
                ),
                "historical_level_confidence": round(
                    float(item.get("historical_level_confidence") or 0.0),
                    3,
                ),
                "historical_level_sample_leagues": int(
                    item.get("historical_level_sample_leagues") or 0
                ),
                "historical_mean_median_skew": (
                    round(
                        float(item["historical_mean_median_skew"]),
                        6,
                    )
                    if item.get("historical_mean_median_skew") is not None
                    else None
                ),
                "historical_forward_return_pct": (
                    round(float(item["historical_forward_return"]) * 100, 1)
                    if item.get("historical_forward_return") is not None
                    else None
                ),
                "historical_recency_weighted_forward_return_pct": (
                    round(
                        float(
                            item[
                                "historical_recency_weighted_forward_return"
                            ]
                        )
                        * 100,
                        1,
                    )
                    if item.get(
                        "historical_recency_weighted_forward_return"
                    )
                    is not None
                    else None
                ),
                "meta_status": item.get("meta_status"),
                "meta_ascendancy": item.get("meta_ascendancy"),
                "meta_multiplier": round(
                    float(item.get("meta_multiplier") or 1.0),
                    4,
                ),
                "meta_current_share": item.get("meta_current_share"),
                "meta_historical_share": item.get(
                    "meta_historical_share"
                ),
                "meta_current_sample_size": int(
                    item.get("meta_current_sample_size") or 0
                ),
                "meta_historical_sample_size": int(
                    item.get("meta_historical_sample_size") or 0
                ),
                "meta_historical_league_count": int(
                    item.get("meta_historical_league_count") or 0
                ),
                "meta_baseline_quality": item.get(
                    "meta_baseline_quality"
                ),
                "meta_confidence": round(
                    float(item.get("meta_confidence") or 0.0),
                    3,
                ),
                "meta_source": item.get("meta_source"),
                "meta_caveat": item.get("meta_caveat"),
                "seasonal_status": item.get("seasonal_status"),
                "seasonal_sample_leagues": item.get(
                    "seasonal_sample_leagues", 0
                ),
                "seasonal_median_return_pct": (
                    round(float(item["seasonal_median_return"]) * 100, 1)
                    if item.get("seasonal_median_return") is not None
                    else None
                ),
                "seasonal_recency_weighted_return_pct": (
                    round(
                        float(item["seasonal_recency_weighted_return"])
                        * 100,
                        1,
                    )
                    if item.get("seasonal_recency_weighted_return")
                    is not None
                    else None
                ),
                "seasonal_positive_rate": item.get(
                    "seasonal_positive_rate"
                ),
                "seasonal_confidence": round(
                    float(item.get("seasonal_confidence") or 0.0),
                    3,
                ),
                "seasonal_weight": round(
                    float(item.get("seasonal_weight") or 0.0),
                    3,
                ),
                "seasonal_leagues": item.get("seasonal_leagues", []),
                "seasonal_league_weights": [
                    {
                        **observation,
                        "entry_divine": round(
                            float(observation["entry_divine"]),
                            5,
                        ),
                        "raw_weight": round(
                            float(observation["raw_weight"]),
                            6,
                        ),
                        "normalized_weight": round(
                            float(observation["normalized_weight"]),
                            6,
                        ),
                    }
                    for observation in item.get(
                        "seasonal_league_weights",
                        [],
                    )
                ],
                "appreciation_status": item.get("appreciation_status"),
                "appreciation_horizon_days": int(
                    item.get("appreciation_horizon_days")
                    or SeasonalModel.APPRECIATION_HORIZON_DAYS
                ),
                "appreciation_sample_leagues": int(
                    item.get("appreciation_sample_leagues") or 0
                ),
                "appreciation_median_return_pct": (
                    round(
                        float(item["appreciation_median_return"]) * 100,
                        1,
                    )
                    if item.get("appreciation_median_return") is not None
                    else None
                ),
                "appreciation_recency_weighted_return_pct": (
                    round(
                        float(
                            item["appreciation_recency_weighted_return"]
                        )
                        * 100,
                        1,
                    )
                    if item.get(
                        "appreciation_recency_weighted_return"
                    )
                    is not None
                    else None
                ),
                "appreciation_positive_rate": item.get(
                    "appreciation_positive_rate"
                ),
                "appreciation_confidence": round(
                    float(item.get("appreciation_confidence") or 0.0),
                    3,
                ),
                "history": item["history"],
            }
            results.append(result)
        return results

    def _rank_research_candidates(
        self,
        *,
        watchlist: list[dict[str, Any]],
        histories: dict[str, list[dict[str, Any]]],
        seasonal_signals: dict[str, SeasonalSignal],
        qualified: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build an explicitly non-qualified research tier.

        These rows make incomplete or failed signals inspectable without
        relabelling them as recommendations. Structurally declining assets,
        excluded bulk-consumable categories, and unresolved source identifiers
        stay outside the investment ranking.
        """

        seen_assets = {
            (
                _normalized_asset_token(str(item["name"])),
                _normalized_asset_token(str(item["category"])),
            )
            for item in qualified
        }
        research: list[dict[str, Any]] = []
        for watch in watchlist:
            key = str(watch.get("key") or "")
            name = str(watch.get("name") or "")
            category = str(watch.get("category") or "")
            reason = str(watch.get("reason") or "Signal review is required.")
            if not key or not name or not category:
                continue
            if _category_is_excluded(category):
                continue
            if (
                watch.get("lifecycle_status") == "known_decline"
                or _known_declining_lifecycle(key, name) is not None
            ):
                continue
            reason_token = reason.casefold()
            if (
                "internal identifier" in reason_token
                or "exact tradeable variant" in reason_token
            ):
                continue
            signature = (
                _normalized_asset_token(name),
                _normalized_asset_token(category),
            )
            if signature in seen_assets:
                continue
            rows = _daily_rows(histories.get(key, []))
            if not rows:
                continue
            latest = rows[-1]
            current = float(latest.get("divine_value") or 0.0)
            if current <= 0:
                continue
            seen_assets.add(signature)

            seasonal = seasonal_signals.get(key)
            weighted = (
                seasonal.recency_weighted_entry_price
                if seasonal
                else None
            )
            historical_average = (
                seasonal.average_entry_price if seasonal else None
            )
            historical_median = (
                seasonal.median_entry_price if seasonal else None
            )
            historical_discount: float | None = None
            if weighted is not None and weighted > 0:
                historical_discount = (weighted - current) / weighted
            level_confidence = (
                seasonal.level_confidence if seasonal else 0.0
            )
            priority = (
                historical_discount * (0.50 + 0.50 * level_confidence)
                if historical_discount is not None
                else None
            )
            source_confidence = statistics.fmean(
                float(row.get("confidence") or 0.5)
                for row in rows[-7:]
            )
            liquidity = _liquidity_score(latest)
            research_confidence = _clamp(
                0.55 * source_confidence
                + 0.25 * level_confidence
                + 0.20 * liquidity,
                0.0,
                1.0,
            )
            seasonal_status = (
                seasonal.status
                if seasonal
                else str(
                    watch.get("seasonal_status")
                    or "insufficient_leagues"
                )
            )
            research.append(
                {
                    "rank": 0,
                    "ranking_tier": 2,
                    "eligibility_status": "research",
                    "eligible_for_recommendation": False,
                    "qualification_reason": reason,
                    "reason": reason,
                    "rationale": reason,
                    "factors": [
                        (
                            f"Current price: {current:.5g} div; "
                            + (
                                f"same-day recency-weighted history: "
                                f"{weighted:.5g} div."
                                if weighted is not None
                                else (
                                    "same-day recency-weighted history is "
                                    "not yet available."
                                )
                            )
                        ),
                        (
                            "Research tier only: this item failed or has not "
                            "yet completed at least one recommendation gate."
                        ),
                    ],
                    "priority_score": (
                        round(priority * 100, 2)
                        if priority is not None
                        else None
                    ),
                    "key": key,
                    "curve_key": key,
                    "name": name,
                    "category": category,
                    "price_divine": round(current, 5),
                    "current_price_divine": round(current, 5),
                    "price_chaos": (
                        round(float(latest["chaos_value"]), 2)
                        if latest.get("chaos_value") is not None
                        else None
                    ),
                    "weighted_historical_price_divine": (
                        round(float(weighted), 5)
                        if weighted is not None
                        else None
                    ),
                    "historical_fair_value_divine": (
                        round(float(weighted), 5)
                        if weighted is not None
                        else None
                    ),
                    "historical_recency_weighted_divine": (
                        round(float(weighted), 5)
                        if weighted is not None
                        else None
                    ),
                    "historical_average_divine": (
                        round(float(historical_average), 5)
                        if historical_average is not None
                        else None
                    ),
                    "historical_median_divine": (
                        round(float(historical_median), 5)
                        if historical_median is not None
                        else None
                    ),
                    "historical_discount": (
                        round(historical_discount, 6)
                        if historical_discount is not None
                        else None
                    ),
                    "historical_discount_pct": (
                        round(historical_discount * 100, 1)
                        if historical_discount is not None
                        else None
                    ),
                    "historical_level_confidence": round(
                        level_confidence,
                        3,
                    ),
                    "historical_level_sample_leagues": (
                        seasonal.level_sample_leagues if seasonal else 0
                    ),
                    "historical_level_dispersion": (
                        round(seasonal.entry_dispersion, 6)
                        if seasonal
                        else None
                    ),
                    "seasonal_status": seasonal_status,
                    "seasonal_sample_leagues": (
                        seasonal.sample_leagues if seasonal else 0
                    ),
                    "seasonal_recency_weighted_return_pct": (
                        round(
                            seasonal.recency_weighted_return * 100,
                            1,
                        )
                        if seasonal
                        else None
                    ),
                    "appreciation_status": (
                        seasonal.appreciation_status
                        if seasonal
                        else str(
                            watch.get("appreciation_status")
                            or "insufficient_leagues"
                        )
                    ),
                    "appreciation_horizon_days": (
                        seasonal.appreciation_horizon_days
                        if seasonal
                        else SeasonalModel.APPRECIATION_HORIZON_DAYS
                    ),
                    "appreciation_sample_leagues": (
                        seasonal.appreciation_sample_leagues
                        if seasonal
                        else 0
                    ),
                    "appreciation_recency_weighted_return_pct": (
                        round(
                            seasonal.appreciation_recency_weighted_return
                            * 100,
                            1,
                        )
                        if seasonal
                        else None
                    ),
                    "confidence": round(research_confidence, 3),
                    "confidence_label": _label(research_confidence),
                    "confidence_score": round(research_confidence, 3),
                    "liquidity": round(liquidity, 3),
                    "liquidity_label": _label(liquidity),
                    "liquidity_score": round(liquidity, 3),
                    "expected_return_pct": None,
                    "entry_ceiling_divine": None,
                    "target_divine": None,
                    "stop_divine": None,
                    "allocation_divine": None,
                    "quantity": None,
                    "position_unit_cap": None,
                    "allocation_exception": None,
                    "high_ticket_single_unit": False,
                    "history": [
                        {
                            "date": str(row["observed_at"])[:10],
                            "value": round(
                                float(row["divine_value"]),
                                5,
                            ),
                        }
                        for row in rows[-30:]
                    ],
                    "_research_has_history": weighted is not None,
                    "_research_priority": (
                        priority if priority is not None else -1.0
                    ),
                }
            )

        research.sort(
            key=lambda item: (
                bool(item["_research_has_history"]),
                float(item["_research_priority"]),
                int(item["historical_level_sample_leagues"]),
                float(item["confidence_score"]),
                float(item["liquidity_score"]),
            ),
            reverse=True,
        )
        for index, item in enumerate(research, start=1):
            item["rank"] = index
            item.pop("_research_has_history", None)
            item.pop("_research_priority", None)
        return research

    def _allocate(
        self,
        candidates: list[dict[str, Any]],
        budget: float,
        league_day: int | None,
    ) -> list[dict[str, Any]]:
        """Compatibility alias for callers from the former allocator.

        Budget and league phase are intentionally ignored in ranking mode.
        """

        del budget, league_day
        return self._rank(candidates)
