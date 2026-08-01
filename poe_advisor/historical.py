from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Callable, Iterable

from .clients import PoeWatchClient
from .forbidden import (
    cached_passive_map,
    enrich_forbidden_assets,
    forbidden_price_points,
)
from .models import League, iso_utc, parse_datetime
from .normalization import canonical_key, slugify


DIVINE_ORB_SOURCE_ITEM_ID = "56327"
HISTORY_SOURCE = PoeWatchClient.SOURCE
MIN_DIVINE_CHAOS = 5.0
MAX_DIVINE_CHAOS = 5_000.0
MAX_ADJACENT_DIVINE_RATIO = 4.0
# Cross-league economies can legitimately diverge much more than adjacent
# days within one league. Reserve this guard for unmistakable wrong-unit data.
MAX_CROSS_LEAGUE_DIVINE_RATIO = 8.0
MAX_CONSENSUS_DONOR_SPREAD = 1.5
MIN_USABLE_DIVINE_POINTS = 7
CROSS_LEAGUE_ANCHOR_CONFIDENCE_CAP = 0.52


@dataclass(frozen=True, slots=True)
class HistoricalLeagueSpec:
    league_id: str
    name: str
    source_alias: str
    start_at: str
    end_at: str

    def as_league(self) -> League:
        return League(
            id=self.league_id,
            name=self.name,
            start_at=self.start_at,
            end_at=self.end_at,
        )


COMPLETED_LEAGUES: tuple[HistoricalLeagueSpec, ...] = (
    HistoricalLeagueSpec(
        league_id="Affliction",
        name="Affliction",
        source_alias="Affliction",
        start_at="2023-12-08T19:00:00Z",
        end_at="2024-03-29T19:00:00Z",
    ),
    HistoricalLeagueSpec(
        league_id="Necropolis",
        name="Necropolis",
        source_alias="Necropolis",
        start_at="2024-03-29T19:00:00Z",
        end_at="2024-07-26T20:00:00Z",
    ),
    HistoricalLeagueSpec(
        league_id="Settlers",
        name="Settlers",
        source_alias="Settlers",
        start_at="2024-07-26T20:00:00Z",
        end_at="2025-06-13T20:00:00Z",
    ),
    HistoricalLeagueSpec(
        league_id="Mercenaries",
        name="Mercenaries",
        source_alias="Mercenaries",
        start_at="2025-06-13T20:00:00Z",
        end_at="2025-10-31T19:00:00Z",
    ),
    HistoricalLeagueSpec(
        league_id="Keepers",
        name="Keepers of the Flame",
        source_alias="Keepers",
        start_at="2025-10-31T19:00:00Z",
        end_at="2026-03-06T19:00:00Z",
    ),
    HistoricalLeagueSpec(
        league_id="Mirage",
        name="Mirage",
        source_alias="Mirage",
        start_at="2026-03-06T19:00:00Z",
        end_at="2026-07-24T20:00:00Z",
    ),
)
HISTORICAL_LEAGUES = COMPLETED_LEAGUES

# These four leagues have broad item coverage in the local archive. Forecasts
# deliberately ignore the much thinner Affliction and Necropolis samples so a
# handful of surviving rows cannot distort a cross-league target.
BROADLY_COVERED_LEAGUE_IDS = (
    "Settlers",
    "Mercenaries",
    "Keepers",
    "Mirage",
)
BROADLY_COVERED_LEAGUES: tuple[HistoricalLeagueSpec, ...] = tuple(
    spec
    for spec in COMPLETED_LEAGUES
    if spec.league_id in BROADLY_COVERED_LEAGUE_IDS
)


_CATEGORY_MAP = {
    "currency": "Currency",
    "fragment": "Fragment",
    "fragments": "Fragment",
    "card": "DivinationCard",
    "divinationcard": "DivinationCard",
    "divinationcards": "DivinationCard",
    "oil": "Oil",
    "oils": "Oil",
    "deliriumorb": "DeliriumOrb",
    "deliriumorbs": "DeliriumOrb",
    "delirium": "DeliriumOrb",
    "scarab": "Scarab",
    "scarabs": "Scarab",
    "fossil": "Fossil",
    "fossils": "Fossil",
    "resonator": "Resonator",
    "resonators": "Resonator",
    "essence": "Essence",
    "essences": "Essence",
    "invitation": "Invitation",
    "invitations": "Invitation",
    "incubator": "Incubator",
    "incubators": "Incubator",
    "artifact": "Artifact",
    "artifacts": "Artifact",
    "tattoo": "Tattoo",
    "tattoos": "Tattoo",
    "omen": "Omen",
    "omens": "Omen",
    "gem": "SkillGem",
    "gems": "SkillGem",
}
ELIGIBLE_CATEGORIES = frozenset(_CATEGORY_MAP.values()) | {"ForbiddenJewel"}


_FORBIDDEN_JEWEL_NAME = re.compile(
    r"^Forbidden\s+(Flesh|Flame)\s+\((.+)\)$",
    re.IGNORECASE,
)


def _forbidden_jewel_parts(name: str) -> tuple[str, str, str] | None:
    """Translate poe.watch's jewel name into poe.ninja's item identity."""

    match = _FORBIDDEN_JEWEL_NAME.fullmatch(name.strip())
    if match is None:
        return None
    jewel_kind = match.group(1).title()
    passive_name = match.group(2).strip()
    if not passive_name:
        return None
    variant = f"Forbidden {jewel_kind}"
    details_id = f"forbidden-{jewel_kind.lower()}-{slugify(passive_name)}"
    return passive_name, variant, details_id


def _skill_gem_parts(
    name: str,
    row: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Mirror poe.ninja's exact level/quality/corruption gem identity.

    poe.watch exposes these fields separately while poe.ninja folds them into
    both ``detailsId`` and ``variant``. A name-only key would merge materially
    different markets (for example, uncorrupted level 1 and corrupted level
    4), so incomplete rows deliberately remain ineligible.
    """

    try:
        gem_level = int(row.get("gemLevel"))
        gem_quality = int(row.get("gemQuality") or 0)
    except (TypeError, ValueError):
        return None
    if gem_level < 1 or gem_quality < 0:
        return None
    corrupted = _boolean(
        row.get("gemIsCorrupted", row.get("corrupted", False))
    )
    variant = str(gem_level)
    if gem_quality:
        variant += f"/{gem_quality}"
    if corrupted:
        variant += "c"

    details_id = f"{slugify(name)}-{variant}"
    identity_parts = [
        details_id,
        f"variant={variant}",
    ]
    if corrupted:
        identity_parts.append("corrupted=True")
    identity_parts.append(f"gemLevel={gem_level}")
    if gem_quality:
        identity_parts.append(f"gemQuality={gem_quality}")
    identity = "-".join(identity_parts)
    return (
        canonical_key(name, "SkillGem", identity),
        {
            "variant": variant,
            "detailsId": details_id,
            "gem_level": gem_level,
            "gem_quality": gem_quality,
            "gem_is_corrupted": corrupted,
        },
    )


def league_day(observed_at: str | datetime, league_start: str | datetime) -> int:
    """Return the one-based 24-hour league window containing an observation."""

    observed = _as_datetime(observed_at)
    start = _as_datetime(league_start)
    if observed is None or start is None:
        raise ValueError("observed_at and league_start must be valid timestamps")
    elapsed_days = int((observed - start).total_seconds() // 86_400)
    return elapsed_days + 1


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and math.isfinite(float(value)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parsed = parse_datetime(text)
        if parsed is None:
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except (TypeError, ValueError):
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _positive_float(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("volume", "count", "value", "mean"):
            number = _positive_float(value.get(key))
            if number is not None:
                return number
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _source_item_id(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _canonical_category(
    source_category: Any, source_group: Any, name: str
) -> str:
    if _forbidden_jewel_parts(name) is not None:
        return "ForbiddenJewel"
    category_slug = slugify(str(source_category or "")).replace("-", "")
    group_slug = slugify(str(source_group or "")).replace("-", "")
    name_slug = slugify(name)
    # poe.watch groups several exact poe.ninja exchange families under broad
    # "currency" or "maps" buckets. Names are stable enough for these
    # fungible families and avoid conflating them with generic Currency.
    named_families = (
        ("runegraft-", "Runegraft"),
        ("tattoo-", "Tattoo"),
        ("omen-", "Omen"),
        ("allflame-ember-", "AllflameEmber"),
    )
    for prefix, category in named_families:
        if name_slug.startswith(prefix):
            return category
    if "delirium-orb" in name_slug:
        return "DeliriumOrb"
    if name_slug.endswith("-incubator"):
        return "Incubator"
    if name_slug.endswith("-artifact"):
        return "Artifact"
    if "invitation" in name_slug:
        return "Invitation"
    if category_slug == "maps" and group_slug == "currency":
        return "Fragment"
    if category_slug in {"delve", "fossil", "fossils", "resonator", "resonators"}:
        if (
            "resonator" in slugify(name)
            or "resonator" in group_slug
            or category_slug in {"resonator", "resonators"}
        ):
            return "Resonator"
        return "Fossil"
    return _CATEGORY_MAP.get(category_slug, str(source_category or "Other"))


def _extract_collection(payload: Any, *keys: str) -> list[dict[str, Any]]:
    candidate = payload
    if isinstance(payload, dict):
        candidate = None
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                candidate = value
                break
        if candidate is None and all(isinstance(value, dict) for value in payload.values()):
            candidate = list(payload.values())
    if not isinstance(candidate, list):
        return []
    return [value for value in candidate if isinstance(value, dict)]


def _compact_assets(payload: Any) -> list[dict[str, Any]]:
    rows = _extract_collection(payload, "items", "data", "result")
    assets: list[dict[str, Any]] = []
    for row in rows:
        source_id = _source_item_id(
            row.get("id", row.get("itemId", row.get("item_id", "")))
        )
        source_name = str(row.get("name") or row.get("type") or "").strip()
        if not source_id or not source_name:
            continue
        source_category = str(row.get("category") or "").strip()
        source_group = str(row.get("group") or row.get("groupName") or "").strip()
        category = _canonical_category(source_category, source_group, source_name)
        forbidden_parts = _forbidden_jewel_parts(source_name)
        gem_parts = (
            _skill_gem_parts(source_name, row)
            if category == "SkillGem"
            else None
        )
        if forbidden_parts is not None:
            name, jewel_variant, details_id = forbidden_parts
            # poe.ninja includes the jewel base as a variant suffix in its
            # canonical identity. Mirror that exact construction so a
            # historical Flesh row cannot collide with its Flame pair.
            identity = f"{details_id}-variant={jewel_variant}"
            item_key = canonical_key(name, category, identity)
            exact_variant: dict[str, Any] = {
                "source_name": source_name,
                "variant": jewel_variant,
                "passiveName": name,
                "detailsId": details_id,
            }
        elif gem_parts is not None:
            name = source_name
            item_key, exact_variant = gem_parts
        else:
            name = source_name
            item_key = canonical_key(name, category)
            exact_variant = {}
        daily_volume = _positive_float(
            row.get("daily", row.get("volume", row.get("count")))
        )
        current_chaos = _positive_float(
            row.get("mean", row.get("chaosValue", row.get("price")))
        )
        current_divine = _positive_float(row.get("divine"))
        current_divine = _positive_float(
            row.get("divine", row.get("divineValue"))
        )
        low_confidence = _boolean(
            row.get("lowConfidence", row.get("low_confidence", False))
        )
        eligible = category in ELIGIBLE_CATEGORIES and (
            category != "SkillGem" or gem_parts is not None
        )
        assets.append(
            {
                "source": PoeWatchClient.SOURCE,
                "source_item_id": source_id,
                "item_key": item_key,
                "name": name,
                "category": category,
                "source_category": source_category,
                "source_group": source_group,
                "group_name": source_group,
                "eligible": eligible,
                "current_daily": daily_volume,
                "daily_volume": daily_volume,
                "current_chaos": current_chaos,
                "current_divine": current_divine,
                "current_divine": current_divine,
                "low_confidence": low_confidence,
                "variant": {
                    "source_category": source_category,
                    "source_group": source_group,
                    **exact_variant,
                    "minimum_chaos": row.get("min"),
                    "maximum_chaos": row.get("max"),
                    "change": row.get("change"),
                    "base": row.get("base"),
                    "links": row.get("links", row.get("linkCount")),
                    "gem_level": row.get("gemLevel"),
                    "gem_quality": row.get("gemQuality"),
                    "map_tier": row.get("mapTier"),
                },
            }
        )
    return assets


def _history_rows(payload: Any) -> list[dict[str, Any]]:
    return _extract_collection(payload, "history", "data", "items", "result")


@dataclass(frozen=True, slots=True)
class DivineCurveQuality:
    """A fail-closed view of a raw Chaos-per-Divine history."""

    prices: dict[int, float]
    rejected_days: frozenset[int]
    issues: tuple[str, ...]

    @property
    def partial(self) -> bool:
        return bool(self.rejected_days)


def _day_summary(days: Iterable[int], *, limit: int = 8) -> str:
    ordered = sorted(set(int(day) for day in days))
    shown = ", ".join(str(day) for day in ordered[:limit])
    if len(ordered) > limit:
        shown += f", +{len(ordered) - limit} more"
    return shown


def _validate_divine_curve(
    curve: dict[int, float],
    *,
    minimum_points: int = MIN_USABLE_DIVINE_POINTS,
    reject_adjacent_jumps: bool = True,
) -> DivineCurveQuality:
    """Reject implausible anchor days instead of poisoning derived prices.

    poe.watch documents ``mean`` as a Chaos-equivalent price, but some archived
    Divine Orb histories contain values such as ``0.03`` between otherwise
    normal 100-200 Chaos observations. Those values are present in the raw
    provider response, so identity checks alone cannot catch the corruption.
    Both an absolute sanity band and an adjacent-day discontinuity guard are
    deliberately conservative. A rejected day is never interpolated.
    """

    prices: dict[int, float] = {}
    rejected: set[int] = set()
    absolute_rejections: set[int] = set()
    for raw_day, raw_price in curve.items():
        try:
            day = int(raw_day)
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if day < 1 or not math.isfinite(price) or price <= 0:
            if day >= 1:
                rejected.add(day)
            continue
        prices[day] = price
        if price < MIN_DIVINE_CHAOS or price > MAX_DIVINE_CHAOS:
            absolute_rejections.add(day)
            rejected.add(day)

    jump_days: set[int] = set()
    if reject_adjacent_jumps:
        ordered = sorted(prices.items())
        for (left_day, left_price), (right_day, right_price) in zip(
            ordered, ordered[1:]
        ):
            if right_day != left_day + 1:
                continue
            ratio = max(left_price, right_price) / min(left_price, right_price)
            if ratio > MAX_ADJACENT_DIVINE_RATIO:
                # It is impossible to know which side of an untrusted
                # provider discontinuity is wrong without a second source, so
                # fail closed on both. Official poe.ninja direct pairs opt out
                # because that provider is the golden source for this model.
                jump_days.update((left_day, right_day))
                rejected.update((left_day, right_day))

    usable = {
        day: price for day, price in prices.items() if day not in rejected
    }
    required_points = max(1, int(minimum_points))
    if len(usable) < required_points:
        raise ValueError(
            "Divine Orb normalization rejected: only "
            f"{len(usable)} trustworthy league-days remain"
        )
    span = max(usable) - min(usable) + 1
    if span < required_points:
        raise ValueError(
            "Divine Orb normalization rejected: trustworthy observations "
            f"span only {span} league-days"
        )

    issues: list[str] = []
    if absolute_rejections:
        issues.append(
            f"{len(absolute_rejections)} values outside "
            f"{MIN_DIVINE_CHAOS:g}-{MAX_DIVINE_CHAOS:g} Chaos "
            f"(days {_day_summary(absolute_rejections)})"
        )
    if jump_days:
        issues.append(
            f"{len(jump_days)} days touched a >"
            f"{MAX_ADJACENT_DIVINE_RATIO:g}x adjacent jump "
            f"(days {_day_summary(jump_days)})"
        )
    return DivineCurveQuality(
        prices=usable,
        rejected_days=frozenset(rejected),
        issues=tuple(issues),
    )


def parse_daily_history_points(
    payload: Any,
    league_start_at: str | datetime,
    league_end_at: str | datetime | None = None,
) -> list[dict[str, Any]]:
    """Consolidate a dated poe.watch history into exact league-day points.

    The newest provider observation within each league day is retained.
    Missing days remain absent; this function never interpolates or assigns a
    synthetic launch-day value.
    """

    start = _as_datetime(league_start_at)
    end = _as_datetime(league_end_at)
    if start is None:
        raise ValueError("A valid league start is required for item history")
    # Some active-league feeds use year 1 as an open-ended sentinel.
    if end is not None and end <= start:
        end = None
    by_day: dict[int, dict[str, Any]] = {}
    for row in _history_rows(payload):
        mean = _positive_float(
            row.get("mean", row.get("price", row.get("chaosValue")))
        )
        observed = _as_datetime(
            row.get(
                "date",
                row.get("timestamp", row.get("observed_at", row.get("time"))),
            )
        )
        if (
            mean is None
            or observed is None
            or observed < start
            or (end is not None and observed >= end)
        ):
            continue
        day = league_day(observed, start)
        if day < 1:
            continue
        volume = _positive_float(
            row.get("volume", row.get("daily", row.get("count")))
        )
        low_confidence = _boolean(
            row.get("lowConfidence", row.get("low_confidence", False))
        )
        confidence = 0.4 if low_confidence else 0.8
        if volume is not None:
            confidence = min(
                0.95,
                max(confidence, 0.45 + math.log10(volume + 1.0) / 8.0),
            )
        point = {
            "league_day": day,
            "observed_at": iso_utc(observed),
            "mean": mean,
            "volume": volume,
            "confidence": confidence,
        }
        previous = by_day.get(day)
        if previous is None or point["observed_at"] > previous["observed_at"]:
            by_day[day] = point
    return [by_day[day] for day in sorted(by_day)]


def _cross_league_divine_fallbacks(
    curves: dict[str, dict[int, float]],
    specs: Iterable[HistoricalLeagueSpec] = COMPLETED_LEAGUES,
) -> tuple[
    dict[str, dict[int, float]],
    dict[str, dict[int, dict[str, Any]]],
    dict[str, int],
]:
    """Fill missing anchors from other leagues without chaining estimates."""

    direct = {
        str(league_id): {
            int(day): float(price) for day, price in curve.items()
        }
        for league_id, curve in curves.items()
    }
    completed = {
        league_id: dict(curve) for league_id, curve in direct.items()
    }
    details: dict[str, dict[int, dict[str, Any]]] = {
        league_id: {
            day: {
                "kind": "poe_watch_direct",
                "donor_leagues": [league_id],
                "donor_values": [price],
            }
            for day, price in curve.items()
        }
        for league_id, curve in direct.items()
    }
    fallback_counts: dict[str, int] = {}
    all_days = sorted(
        {day for curve in direct.values() for day in curve}
    )
    for spec in specs:
        league_id = spec.league_id
        target = completed.setdefault(league_id, {})
        target_details = details.setdefault(league_id, {})
        start = _as_datetime(spec.start_at)
        end = _as_datetime(spec.end_at)
        assert start is not None and end is not None
        maximum_day = max(
            1, math.ceil((end - start).total_seconds() / 86400.0)
        )
        count = 0
        for day in all_days:
            if day > maximum_day or day in target:
                continue
            donors = sorted(
                (
                    donor_league,
                    donor_curve[day],
                )
                for donor_league, donor_curve in direct.items()
                if donor_league != league_id and day in donor_curve
            )
            if len(donors) < 2:
                continue
            value = float(median(price for _, price in donors))
            target[day] = value
            target_details[day] = {
                "kind": "cross_league_day_median",
                "donor_leagues": [name for name, _ in donors],
                "donor_values": [price for _, price in donors],
            }
            count += 1
        fallback_counts[league_id] = count
    return completed, details, fallback_counts


def _reject_cross_league_divine_outliers(
    curves: dict[str, dict[int, float]],
) -> tuple[
    dict[str, dict[int, float]],
    dict[str, dict[int, dict[str, Any]]],
]:
    """Reject a direct anchor only when independent peer leagues agree.

    Sparse poe.watch curves can contain an isolated wrong-unit value without
    an adjacent observation, so the within-league jump guard cannot see it.
    Peer values are taken only from the original, locally validated direct
    curves. Two or more donors must agree within a narrow band before a target
    more than eight times away is rejected. Rejected or fallback-derived values
    never become donors during the same pass.
    """

    direct = {
        str(league_id): {
            int(day): float(price) for day, price in curve.items()
        }
        for league_id, curve in curves.items()
    }
    filtered = {
        league_id: dict(curve) for league_id, curve in direct.items()
    }
    rejected: dict[str, dict[int, dict[str, Any]]] = {}
    for league_id, curve in direct.items():
        for day, price in curve.items():
            donors = sorted(
                (
                    donor_league,
                    donor_curve[day],
                )
                for donor_league, donor_curve in direct.items()
                if donor_league != league_id and day in donor_curve
            )
            if len(donors) < 2:
                continue
            donor_values = [value for _, value in donors]
            donor_spread = max(donor_values) / min(donor_values)
            if donor_spread > MAX_CONSENSUS_DONOR_SPREAD:
                continue
            donor_median = float(median(donor_values))
            deviation = max(price, donor_median) / min(price, donor_median)
            if deviation <= MAX_CROSS_LEAGUE_DIVINE_RATIO:
                continue
            filtered[league_id].pop(day, None)
            rejected.setdefault(league_id, {})[day] = {
                "kind": "cross_league_direct_outlier",
                "rejected_value": price,
                "donor_median": donor_median,
                "donor_leagues": [name for name, _ in donors],
                "donor_values": donor_values,
                "donor_spread_ratio": donor_spread,
                "deviation_ratio": deviation,
            }
    return filtered, rejected


class HistoricalBackfillService:
    """Resumable historical ingestion for common poe.ninja-style markets."""

    def __init__(
        self,
        storage: Any,
        client: PoeWatchClient | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        request_pause_seconds: float = 0.05,
    ):
        self.storage = storage
        self.client = client or PoeWatchClient()
        self.sleeper = sleeper
        self.request_pause_seconds = max(0.0, request_pause_seconds)
        self._run_lock = threading.Lock()
        self._is_syncing = False
        self._progress: dict[str, Any] = {
            "status": "idle",
            "completed": 0,
            "total": 0,
        }
        self._last_summary: dict[str, Any] | None = None

    @property
    def is_syncing(self) -> bool:
        return self._is_syncing

    def progress(self) -> dict[str, Any]:
        return dict(self._progress)

    @property
    def last_summary(self) -> dict[str, Any] | None:
        return dict(self._last_summary) if self._last_summary else None

    def backfill(
        self,
        current_league: League | dict[str, Any] | str,
        max_items: int = 80,
    ) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {
                "status": "busy",
                "message": "A historical backfill is already running.",
                "progress": self.progress(),
            }
        self._is_syncing = True
        started_at = iso_utc()
        summary: dict[str, Any] = {
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "catalog_items": 0,
            "eligible_items": 0,
            "matched_items": 0,
            "selected_items": 0,
            "already_complete_items": 0,
            "histories_fetched": 0,
            "histories_skipped": 0,
            "histories_failed": 0,
            "seasonal_rows_written": 0,
            "seasonal_rows_quarantined": 0,
            "assets_written": 0,
            "snapshots_written": 0,
            "normalization_partial_leagues": [],
            "normalization_fallback_days": {},
            "normalization_consensus_rejections": {},
            "warnings": [],
            "warnings_suppressed": 0,
            "leagues": [spec.league_id for spec in COMPLETED_LEAGUES],
        }
        try:
            current_id, current_name = self._current_league_identity(current_league)
            for spec in COMPLETED_LEAGUES:
                self.storage.upsert_league(spec.as_league(), current=False)

            self._progress = {
                "status": "catalog",
                "completed": 0,
                "total": 1,
                "message": f"Downloading the full {current_name} poe.watch catalog",
            }
            cached_catalog = None
            compact_url = getattr(self.client, "compact_url", None)
            if callable(compact_url):
                endpoint = compact_url(current_name, all_items=True)
                cached_catalog = self.storage.latest_snapshot(
                    source=PoeWatchClient.SOURCE,
                    endpoint=endpoint,
                    league_id=current_id,
                )
                cached_at = (
                    _as_datetime(cached_catalog.get("fetched_at"))
                    if cached_catalog
                    else None
                )
                if (
                    cached_at is None
                    or (
                        datetime.now(timezone.utc) - cached_at
                    ).total_seconds()
                    > 12 * 3600
                ):
                    cached_catalog = None

            if cached_catalog is not None:
                compact_payload = json.loads(
                    cached_catalog["raw"].decode("utf-8")
                )
                snapshot_id = int(cached_catalog["id"])
                compact_fetched_at = str(cached_catalog["fetched_at"])
                summary["catalog_cache_reused"] = True
            else:
                compact = self.client.fetch_compact(
                    current_name, all_items=True
                )
                snapshot_id, created = self.storage.add_snapshot(
                    source=PoeWatchClient.SOURCE,
                    endpoint=compact.url,
                    league_id=current_id,
                    category="full-catalog",
                    fetched_at=compact.fetched_at,
                    status_code=compact.status,
                    raw=compact.raw,
                    etag=compact.etag,
                    last_modified=compact.last_modified,
                    metadata={"all": True, "provider_league": current_name},
                )
                summary["snapshots_written"] += int(created)
                compact_payload = compact.payload
                compact_fetched_at = compact.fetched_at
                summary["catalog_cache_reused"] = False

            assets = _compact_assets(compact_payload)
            if not assets:
                raise ValueError("poe.watch compact response did not contain any items")

            cached_mapping = cached_passive_map(self.storage)
            if cached_mapping is not None:
                passive_map, mapping_snapshot_id, mapping_endpoint = (
                    cached_mapping
                )
                coverage = enrich_forbidden_assets(
                    assets,
                    passive_map,
                    mapping_snapshot_id=mapping_snapshot_id,
                    mapping_endpoint=mapping_endpoint,
                )
                summary["forbidden_variants_mapped"] = coverage["mapped"]
                summary["forbidden_variants_unmapped"] = (
                    coverage["total"] - coverage["mapped"]
                )

            current_items = self._load_current_items(current_id)
            self._match_current_items(assets, current_items)
            summary["catalog_items"] = len(assets)
            summary["eligible_items"] = sum(
                1 for asset in assets if asset["eligible"]
            )
            summary["matched_items"] = sum(
                1 for asset in assets if asset.get("current_match")
            )
            summary["assets_written"] = int(
                self.storage.upsert_historical_assets(assets) or 0
            )
            current_forbidden = forbidden_price_points(
                assets,
                league_id=current_id,
                observed_at=compact_fetched_at,
                snapshot_id=snapshot_id,
            )
            summary["current_forbidden_rows_written"] = (
                self.storage.insert_price_points(current_forbidden)
                if current_forbidden
                else 0
            )
            divine_source_item_id = self._resolve_divine_source_item_id(assets)
            summary["divine_source_item_id"] = divine_source_item_id
            self._progress = {
                "status": "history",
                "completed": 0,
                "total": len(COMPLETED_LEAGUES),
                "message": "Preparing historical Divine Orb normalization",
            }

            divine_curves: dict[str, dict[int, float]] = {}
            partial_normalizations: set[str] = set()
            for spec in COMPLETED_LEAGUES:
                try:
                    stored_curve = self._stored_divine_curve(
                        spec.league_id, divine_source_item_id
                    )
                    fetch_status = self._history_fetch_status(
                        spec.league_id, divine_source_item_id
                    )
                    curve_quality: DivineCurveQuality | None = None
                    if not stored_curve and fetch_status == "partial":
                        # The raw provider curve was already found unusable.
                        # Reuse that audited state and rebuild exact days from
                        # independent leagues; repeatedly fetching and
                        # quarantining it would erase accumulated fallback
                        # history on every resumable pass.
                        curve = {}
                        partial_normalizations.add(spec.league_id)
                        summary["histories_skipped"] += 1
                    elif stored_curve:
                        curve_quality = _validate_divine_curve(stored_curve)
                        if curve_quality.partial:
                            reason = "; ".join(curve_quality.issues)
                            removed = self._quarantine_normalization_days(
                                spec.league_id,
                                curve_quality.rejected_days,
                                reason,
                                divine_source_item_id,
                            )
                            summary["seasonal_rows_quarantined"] += removed
                            self._add_warning(
                                summary,
                                f"{spec.name} / Divine Orb: quarantined "
                                f"{removed} derived rows; {reason}",
                            )
                    if not stored_curve and fetch_status == "partial":
                        pass
                    elif (
                        curve_quality is not None
                        and curve_quality.prices
                        and self._history_fetch_usable(
                            spec.league_id, divine_source_item_id
                        )
                    ):
                        curve = curve_quality.prices
                        if (
                            curve_quality.partial
                            or fetch_status == "partial"
                        ):
                            partial_normalizations.add(spec.league_id)
                        summary["histories_skipped"] += 1
                    else:
                        fetched_quality = self._fetch_divine_curve(
                            spec, divine_source_item_id, summary
                        )
                        curve = fetched_quality.prices
                        if fetched_quality.partial:
                            partial_normalizations.add(spec.league_id)
                except Exception as error:
                    curve = {}
                    removed = self._quarantine_league_normalization(
                        spec.league_id, str(error)
                    )
                    summary["seasonal_rows_quarantined"] += removed
                    summary["histories_failed"] += 1
                    self._set_fetch_state(
                        league_id=spec.league_id,
                        source_item_id=divine_source_item_id,
                        status="partial",
                        points_written=0,
                        last_error=(
                            f"{error}; exact-day cross-league fallback "
                            "required"
                        ),
                    )
                    partial_normalizations.add(spec.league_id)
                    self._add_warning(
                        summary,
                        f"{spec.name} / Divine Orb: {error}",
                    )
                divine_curves[spec.league_id] = curve
                self._advance_progress(
                    f"Prepared Divine Orb normalization for {spec.name}"
                )

            (
                divine_curves,
                consensus_rejections,
            ) = _reject_cross_league_divine_outliers(divine_curves)
            summary["normalization_consensus_rejections"] = {
                league_id: {
                    str(day): details
                    for day, details in sorted(rejections.items())
                }
                for league_id, rejections in consensus_rejections.items()
            }
            for league_id, rejections in consensus_rejections.items():
                if not rejections:
                    continue
                partial_normalizations.add(league_id)
                day_text = _day_summary(rejections)
                reason = (
                    "cross-league Divine consensus rejected direct anchor "
                    f"days {day_text}"
                )
                removed = self._quarantine_normalization_days(
                    league_id,
                    rejections,
                    reason,
                    divine_source_item_id,
                )
                summary["seasonal_rows_quarantined"] += removed
                self._add_warning(
                    summary,
                    f"{league_id} / Divine Orb: quarantined {removed} "
                    f"derived rows; {reason}",
                )

            (
                divine_curves,
                divine_anchor_details,
                fallback_counts,
            ) = _cross_league_divine_fallbacks(divine_curves)
            for league_id, count in fallback_counts.items():
                if count:
                    partial_normalizations.add(league_id)
            summary["normalization_fallback_days"] = {
                league_id: count
                for league_id, count in fallback_counts.items()
                if count
            }
            summary["normalization_partial_leagues"] = sorted(
                partial_normalizations
            )
            selection_assets = self._persisted_assets(assets)
            selected, complete_count = self._select_candidates(
                selection_assets,
                max(1, min(int(max_items), 2000)),
                divine_source_item_id=divine_source_item_id,
                available_leagues={
                    league_id
                    for league_id, curve in divine_curves.items()
                    if curve
                },
            )
            summary["selected_items"] = len(selected)
            summary["already_complete_items"] = complete_count
            self._progress = {
                **self._progress,
                "total": len(COMPLETED_LEAGUES)
                + len(COMPLETED_LEAGUES) * len(selected),
            }

            for asset in selected:
                for spec in COMPLETED_LEAGUES:
                    source_item_id = str(asset["source_item_id"])
                    if not divine_curves.get(spec.league_id):
                        summary["histories_skipped"] += 1
                        self._advance_progress(
                            f"Skipped {asset['name']} in {spec.name}: "
                            "Divine Orb normalization unavailable"
                        )
                        continue
                    if self._history_fetch_usable(
                        spec.league_id, source_item_id
                    ):
                        summary["histories_skipped"] += 1
                        self._advance_progress(
                            f"Already archived {asset['name']} in {spec.name}"
                        )
                        continue
                    try:
                        written = self._fetch_asset_history(
                            spec,
                            asset,
                            divine_curves.get(spec.league_id, {}),
                            summary,
                            normalization_partial=(
                                spec.league_id in partial_normalizations
                            ),
                            normalization_details=divine_anchor_details.get(
                                spec.league_id, {}
                            ),
                        )
                        summary["seasonal_rows_written"] += written
                    except Exception as error:
                        summary["histories_failed"] += 1
                        self._set_fetch_state(
                            league_id=spec.league_id,
                            source_item_id=source_item_id,
                            status="failed",
                            points_written=0,
                            last_error=str(error),
                        )
                        self._add_warning(
                            summary,
                            f"{spec.name} / {asset['name']}: {error}",
                        )
                    self._advance_progress(
                        f"Archived {asset['name']} in {spec.name}"
                    )

            if summary["histories_failed"]:
                summary["status"] = "partial"
                summary["message"] = (
                    "Historical archive updated with some source failures; "
                    "the next run will retry them."
                )
            elif partial_normalizations:
                summary["status"] = "partial"
                summary["message"] = (
                    "Historical archive updated. Implausible Divine Orb "
                    "league-days were quarantined and rebuilt with an "
                    "auditable, low-confidence same-day fallback."
                )
            else:
                summary["status"] = "success"
                summary["message"] = (
                    "Historical archive updated. Successful item/league pairs "
                    "will be skipped on the next run."
                )
            return summary
        except Exception as error:
            summary["status"] = "failed"
            summary["message"] = str(error)
            self._add_warning(summary, str(error))
            return summary
        finally:
            summary["finished_at"] = iso_utc()
            self._last_summary = dict(summary)
            self._progress = {
                **self._progress,
                "status": summary["status"],
                "message": summary.get("message", ""),
            }
            self._is_syncing = False
            self._run_lock.release()

    @staticmethod
    def _current_league_identity(
        current_league: League | dict[str, Any] | str,
    ) -> tuple[str, str]:
        if isinstance(current_league, League):
            return current_league.id, current_league.name
        if isinstance(current_league, dict):
            league_id = str(
                current_league.get("id") or current_league.get("name") or ""
            ).strip()
            league_name = str(
                current_league.get("name") or current_league.get("id") or ""
            ).strip()
        else:
            league_id = str(current_league).strip()
            league_name = league_id
        if not league_id or not league_name:
            raise ValueError("A current trade league is required for backfill")
        return league_id, league_name

    def _load_current_items(
        self, league_id: str
    ) -> list[dict[str, Any]]:
        method = getattr(self.storage, "list_current_market_items", None)
        if callable(method):
            try:
                return [dict(row) for row in method(league_id)]
            except (AttributeError, TypeError):
                pass
        connect = getattr(self.storage, "connect", None)
        if not callable(connect):
            return []
        connection = connect()
        try:
            rows = connection.execute(
                """
                SELECT item_key, name, category, MAX(observed_at) AS observed_at
                FROM price_points
                WHERE league_id = ?
                GROUP BY item_key, name, category
                """,
                (league_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _match_current_items(
        assets: list[dict[str, Any]], current_items: Iterable[dict[str, Any]]
    ) -> None:
        by_key: dict[str, dict[str, Any]] = {}
        by_identity: dict[tuple[str, str], dict[str, Any]] = {}
        for item in current_items:
            key = str(item.get("item_key") or "")
            name = str(item.get("name") or "")
            category = str(item.get("category") or "")
            if key:
                by_key[key] = item
            if name and category:
                by_identity[(name.casefold(), category.casefold())] = item
        for asset in assets:
            match = by_key.get(str(asset["item_key"]))
            # Variant-sensitive families must match their full normalized key.
            # Falling back to name/category would silently attach every gem
            # level, quality, and corruption state to one arbitrary market.
            if (
                match is None
                and str(asset.get("category") or "").casefold()
                != "skillgem"
            ):
                match = by_identity.get(
                    (
                        str(asset["name"]).casefold(),
                        str(asset["category"]).casefold(),
                    )
                )
            asset["current_match"] = bool(match)
            if match and match.get("item_key"):
                asset["item_key"] = str(match["item_key"])

    @staticmethod
    def _resolve_divine_source_item_id(
        assets: Iterable[dict[str, Any]],
    ) -> str:
        """Resolve the anchor from the current catalog and require uniqueness.

        poe.watch only exposes compact catalogs for active leagues. Its
        historical endpoint still accepts completed leagues, but there is no
        completed-league compact endpoint from which to discover a different
        per-league ID. The current all-items catalog is therefore the identity
        authority; curve quality is validated independently below.
        """

        matches = [
            asset
            for asset in assets
            if str(asset.get("name") or "").strip().casefold()
            == "divine orb"
            and str(asset.get("category") or "").strip().casefold()
            == "currency"
        ]
        if len(matches) != 1:
            raise ValueError(
                "poe.watch catalog must contain exactly one Currency / "
                f"Divine Orb row; found {len(matches)}"
            )
        source_item_id = str(matches[0].get("source_item_id") or "").strip()
        if not source_item_id:
            raise ValueError("poe.watch Divine Orb row has no source item ID")
        current_chaos = _positive_float(matches[0].get("current_chaos"))
        if (
            current_chaos is not None
            and (
                current_chaos < MIN_DIVINE_CHAOS
                or current_chaos > MAX_DIVINE_CHAOS
            )
        ):
            raise ValueError(
                "poe.watch current Divine Orb catalog price is outside the "
                "normalization sanity band"
            )
        return source_item_id

    def _select_candidates(
        self,
        assets: list[dict[str, Any]],
        max_items: int,
        *,
        divine_source_item_id: str = DIVINE_ORB_SOURCE_ITEM_ID,
        available_leagues: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        eligible = [
            asset
            for asset in assets
            if asset.get("eligible")
            and str(asset.get("source_item_id")) != divine_source_item_id
        ]
        matched = [asset for asset in eligible if asset.get("current_match")]
        unmatched = [asset for asset in eligible if not asset.get("current_match")]
        ranked = self._balanced(matched) + self._balanced(unmatched)
        required_specs = [
            spec
            for spec in COMPLETED_LEAGUES
            if available_leagues is None
            or spec.league_id in available_leagues
        ]
        if not required_specs:
            return [], 0

        never_attempted: list[dict[str, Any]] = []
        retries: list[dict[str, Any]] = []
        complete = 0
        for asset in ranked:
            item_id = str(asset["source_item_id"])
            statuses = [
                self._history_fetch_status(spec.league_id, item_id)
                for spec in required_specs
            ]
            if all(self._history_status_usable(status) for status in statuses):
                complete += 1
                continue
            if any(status is None for status in statuses):
                never_attempted.append(asset)
                if len(never_attempted) >= max_items:
                    break
            else:
                retries.append(asset)
        retry_slots = max(0, max_items - len(never_attempted))
        return never_attempted + retries[:retry_slots], complete

    def _persisted_assets(
        self, latest_assets: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        method = getattr(self.storage, "list_historical_assets", None)
        if not callable(method):
            return latest_assets
        try:
            stored = [dict(row) for row in method(eligible_only=True)]
        except (AttributeError, TypeError):
            return latest_assets
        latest_by_id = {
            str(asset["source_item_id"]): asset for asset in latest_assets
        }
        result: list[dict[str, Any]] = []
        for asset in stored:
            if str(asset.get("source")) != PoeWatchClient.SOURCE:
                continue
            source_id = str(asset.get("source_item_id") or "")
            latest = latest_by_id.get(source_id)
            if latest is None:
                continue
            asset["current_match"] = bool(latest.get("current_match"))
            asset["daily_volume"] = asset.get("current_daily")
            asset["group_name"] = asset.get("source_group")
            result.append(asset)
        return result or latest_assets

    @staticmethod
    def _balanced(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for asset in assets:
            grouped[str(asset["category"])].append(asset)
        queues: dict[str, deque[dict[str, Any]]] = {}
        for category, values in grouped.items():
            values.sort(
                key=lambda value: (
                    bool(value.get("low_confidence")),
                    -float(value.get("daily_volume") or 0.0),
                    -float(value.get("current_chaos") or 0.0),
                    str(value.get("name") or "").casefold(),
                )
            )
            queues[category] = deque(values)
        result: list[dict[str, Any]] = []
        categories = sorted(queues)
        while categories:
            remaining: list[str] = []
            for category in categories:
                queue = queues[category]
                if queue:
                    result.append(queue.popleft())
                if queue:
                    remaining.append(category)
            categories = remaining
        return result

    def _fetch_divine_curve(
        self,
        spec: HistoricalLeagueSpec,
        source_item_id: str,
        summary: dict[str, Any],
    ) -> DivineCurveQuality:
        result = self.client.fetch_history(
            spec.source_alias, source_item_id
        )
        self._pause()
        snapshot_id, created = self._store_history_snapshot(
            result, spec, source_item_id, "Currency"
        )
        summary["snapshots_written"] += int(created)
        response_ids = {
            _source_item_id(row.get("id"))
            for row in _history_rows(result.payload)
            if row.get("id") is not None
        }
        if response_ids and response_ids != {source_item_id}:
            raise ValueError(
                "Divine Orb history identity mismatch: requested "
                f"{source_item_id}, received {sorted(response_ids)}"
            )
        points = self._parsed_history_points(result.payload, spec)
        raw_curve = {
            int(point["league_day"]): float(point["mean"]) for point in points
        }
        quality = _validate_divine_curve(raw_curve)
        quality_error = "; ".join(quality.issues) if quality.issues else None
        if quality.partial:
            removed = self._quarantine_normalization_days(
                spec.league_id,
                quality.rejected_days,
                quality_error or "Divine Orb normalization was partial",
                source_item_id,
            )
            summary["seasonal_rows_quarantined"] += removed
            self._add_warning(
                summary,
                f"{spec.name} / Divine Orb: rejected "
                f"{len(quality.rejected_days)} source days; {quality_error}",
            )
        rows = [
            {
                "league_id": spec.league_id,
                "item_key": canonical_key("Divine Orb", "Currency"),
                "source_item_id": source_item_id,
                "league_day": point["league_day"],
                "observed_at": point["observed_at"],
                "chaos_value": point["mean"],
                "divine_value": 1.0,
                "volume": point["volume"],
                "confidence": point["confidence"],
                "snapshot_id": snapshot_id,
                "source": HISTORY_SOURCE,
                "details": {"normalization_reference": True},
            }
            for point in points
            if int(point["league_day"]) in quality.prices
        ]
        if not rows:
            raise ValueError(
                f"{spec.source_alias} Divine Orb history has no usable rows"
            )
        written = int(self.storage.upsert_seasonal_prices(rows) or 0)
        summary["seasonal_rows_written"] += written
        summary["histories_fetched"] += 1
        self._set_fetch_state(
            league_id=spec.league_id,
            source_item_id=source_item_id,
            status="partial" if quality.partial else "success",
            points_written=len(rows),
            last_error=quality_error,
        )
        return quality

    def _fetch_asset_history(
        self,
        spec: HistoricalLeagueSpec,
        asset: dict[str, Any],
        divine_curve: dict[int, float],
        summary: dict[str, Any],
        *,
        normalization_partial: bool = False,
        normalization_details: dict[int, dict[str, Any]] | None = None,
    ) -> int:
        if not divine_curve:
            raise ValueError("Divine Orb normalization history is unavailable")
        item_id = str(asset["source_item_id"])
        result = self.client.fetch_history(spec.source_alias, item_id)
        self._pause()
        snapshot_id, created = self._store_history_snapshot(
            result, spec, item_id, str(asset["category"])
        )
        summary["snapshots_written"] += int(created)
        points = self._parsed_history_points(result.payload, spec)
        rows: list[dict[str, Any]] = []
        for point in points:
            point_day = int(point["league_day"])
            divine_chaos = self._nearest_divine(divine_curve, point_day)
            if divine_chaos is None:
                continue
            anchor = (normalization_details or {}).get(point_day, {})
            anchor_kind = str(anchor.get("kind") or "poe_watch_direct")
            confidence_cap = (
                CROSS_LEAGUE_ANCHOR_CONFIDENCE_CAP
                if anchor_kind == "cross_league_day_median"
                else (0.4 if asset.get("low_confidence") else 0.95)
            )
            rows.append(
                {
                    "league_id": spec.league_id,
                    "item_key": str(asset["item_key"]),
                    "source_item_id": item_id,
                    "league_day": int(point["league_day"]),
                    "observed_at": str(point["observed_at"]),
                    "chaos_value": float(point["mean"]),
                    "divine_value": float(point["mean"]) / divine_chaos,
                    "volume": point["volume"],
                    "confidence": min(
                        float(point["confidence"]),
                        confidence_cap,
                        0.4 if asset.get("low_confidence") else 0.95,
                    ),
                    "snapshot_id": snapshot_id,
                    "source": HISTORY_SOURCE,
                    "details": {
                        "source_category": asset.get("source_category"),
                        "source_group": asset.get("source_group"),
                        "divine_chaos": divine_chaos,
                        "divine_anchor_kind": anchor_kind,
                        "divine_anchor_donors": anchor.get(
                            "donor_leagues", [spec.league_id]
                        ),
                        "divine_anchor_donor_values": anchor.get(
                            "donor_values", [divine_chaos]
                        ),
                    },
                }
            )
        if not rows:
            raise ValueError("history has no rows with a nearby Divine Orb price")
        written = int(self.storage.upsert_seasonal_prices(rows) or 0)
        self._set_fetch_state(
            league_id=spec.league_id,
            source_item_id=item_id,
            status="partial" if normalization_partial else "success",
            points_written=len(rows),
            last_error=(
                "Some league-days use a cross-league median Divine anchor"
                if normalization_partial
                else None
            ),
        )
        summary["histories_fetched"] += 1
        return written

    def _parsed_history_points(
        self, payload: Any, spec: HistoricalLeagueSpec
    ) -> list[dict[str, Any]]:
        return parse_daily_history_points(
            payload,
            spec.start_at,
            spec.end_at,
        )

    def _stored_divine_curve(
        self,
        league_id: str,
        source_item_id: str = DIVINE_ORB_SOURCE_ITEM_ID,
    ) -> dict[int, float]:
        connect = getattr(self.storage, "connect", None)
        if not callable(connect):
            return {}
        connection = connect()
        try:
            rows = connection.execute(
                """
                SELECT league_day, chaos_value
                FROM seasonal_prices
                WHERE source = ? AND league_id = ? AND source_item_id = ?
                  AND chaos_value > 0
                ORDER BY league_day
                """,
                (HISTORY_SOURCE, league_id, source_item_id),
            ).fetchall()
            return {
                int(row["league_day"]): float(row["chaos_value"]) for row in rows
            }
        except Exception:
            return {}
        finally:
            connection.close()

    @staticmethod
    def _nearest_divine(
        curve: dict[int, float], target_day: int
    ) -> float | None:
        # Do not borrow a neighbor. A missing day can be an explicitly
        # quarantined provider outlier; using day +/- 1 would silently
        # reintroduce the bad normalization.
        exact = curve.get(target_day)
        if exact is not None and exact > 0:
            return exact
        return None

    def _store_history_snapshot(
        self,
        result: Any,
        spec: HistoricalLeagueSpec,
        source_item_id: str,
        category: str,
    ) -> tuple[int, bool]:
        return self.storage.add_snapshot(
            source=PoeWatchClient.SOURCE,
            endpoint=result.url,
            league_id=spec.league_id,
            category=category,
            fetched_at=result.fetched_at,
            status_code=result.status,
            raw=result.raw,
            etag=result.etag,
            last_modified=result.last_modified,
            metadata={
                "provider_league": spec.source_alias,
                "source_item_id": source_item_id,
                "kind": "item-history",
            },
        )

    def _history_fetch_succeeded(
        self, league_id: str, source_item_id: str
    ) -> bool:
        method = getattr(self.storage, "history_fetch_succeeded", None)
        if not callable(method):
            return False
        return bool(
            method(PoeWatchClient.SOURCE, league_id, str(source_item_id))
        )

    def _history_fetch_status(
        self, league_id: str, source_item_id: str
    ) -> str | None:
        connect = getattr(self.storage, "connect", None)
        if not callable(connect):
            return (
                "success"
                if self._history_fetch_succeeded(league_id, source_item_id)
                else None
            )
        connection = connect()
        try:
            row = connection.execute(
                """
                SELECT status
                FROM historical_fetch_state
                WHERE source = ? AND league_id = ? AND source_item_id = ?
                LIMIT 1
                """,
                (HISTORY_SOURCE, league_id, str(source_item_id)),
            ).fetchone()
            return str(row["status"]).strip().casefold() if row else None
        except Exception:
            return None
        finally:
            connection.close()

    def _history_fetch_usable(
        self, league_id: str, source_item_id: str
    ) -> bool:
        status = self._history_fetch_status(league_id, source_item_id)
        return self._history_status_usable(status)

    @staticmethod
    def _history_status_usable(status: str | None) -> bool:
        return status in {
            "success",
            "succeeded",
            "complete",
            "completed",
            "partial",
        }

    def _quarantine_normalization_days(
        self,
        league_id: str,
        rejected_days: Iterable[int],
        reason: str,
        divine_source_item_id: str = DIVINE_ORB_SOURCE_ITEM_ID,
    ) -> int:
        """Delete unsafe derived rows while retaining every raw snapshot."""

        days = sorted(
            {
                int(day)
                for day in rejected_days
                if int(day) >= 1
            }
        )
        transaction = getattr(self.storage, "transaction", None)
        if not days or not callable(transaction):
            return 0
        placeholders = ",".join("?" for _ in days)
        now = iso_utc()
        with transaction() as connection:
            before = connection.total_changes
            connection.execute(
                f"""
                DELETE FROM seasonal_prices
                WHERE source = ? AND league_id = ?
                  AND league_day IN ({placeholders})
                """,
                [HISTORY_SOURCE, league_id, *days],
            )
            removed = connection.total_changes - before
            # Non-anchor histories must be rebuilt so the safe cross-league
            # fallback can replace the quarantined league-days.
            connection.execute(
                """
                UPDATE historical_fetch_state
                SET status = 'failed',
                    last_error = ?,
                    updated_at = ?
                WHERE source = ? AND league_id = ?
                  AND source_item_id <> ?
                """,
                (
                    f"Divine normalization repair required: {reason}",
                    now,
                    HISTORY_SOURCE,
                    league_id,
                    str(divine_source_item_id),
                ),
            )
            connection.execute(
                """
                UPDATE historical_fetch_state
                SET status = 'partial',
                    points_written = (
                        SELECT COUNT(*)
                        FROM seasonal_prices AS price
                        WHERE price.source = historical_fetch_state.source
                          AND price.league_id =
                              historical_fetch_state.league_id
                          AND price.source_item_id =
                              historical_fetch_state.source_item_id
                    ),
                    last_error = ?,
                    updated_at = ?
                WHERE source = ? AND league_id = ?
                  AND source_item_id = ?
                """,
                (
                    reason,
                    now,
                    HISTORY_SOURCE,
                    league_id,
                    str(divine_source_item_id),
                ),
            )
            return int(removed)

    def _quarantine_league_normalization(
        self, league_id: str, reason: str
    ) -> int:
        transaction = getattr(self.storage, "transaction", None)
        if not callable(transaction):
            return 0
        with transaction() as connection:
            before = connection.total_changes
            connection.execute(
                """
                DELETE FROM seasonal_prices
                WHERE source = ? AND league_id = ?
                """,
                (HISTORY_SOURCE, league_id),
            )
            removed = connection.total_changes - before
            connection.execute(
                """
                UPDATE historical_fetch_state
                SET status = 'failed',
                    points_written = 0,
                    last_error = ?,
                    updated_at = ?
                WHERE source = ? AND league_id = ?
                """,
                (
                    f"Divine normalization unavailable: {reason}",
                    iso_utc(),
                    HISTORY_SOURCE,
                    league_id,
                ),
            )
            return int(removed)

    def _set_fetch_state(
        self,
        *,
        league_id: str,
        source_item_id: str,
        status: str,
        points_written: int,
        last_error: str | None,
    ) -> None:
        method = getattr(self.storage, "set_history_fetch_state", None)
        if not callable(method):
            return
        method(
            source=PoeWatchClient.SOURCE,
            league_id=league_id,
            source_item_id=str(source_item_id),
            status=status,
            points_written=points_written,
            last_error=last_error,
        )

    def _pause(self) -> None:
        if self.request_pause_seconds:
            self.sleeper(self.request_pause_seconds)

    def _advance_progress(self, message: str) -> None:
        self._progress = {
            **self._progress,
            "completed": int(self._progress.get("completed", 0)) + 1,
            "message": message,
        }

    @staticmethod
    def _add_warning(
        summary: dict[str, Any], warning: str, *, limit: int = 50
    ) -> None:
        warnings = summary["warnings"]
        if len(warnings) < limit:
            warnings.append(warning)
        else:
            summary["warnings_suppressed"] += 1
