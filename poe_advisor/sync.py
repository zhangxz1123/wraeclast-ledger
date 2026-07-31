from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .clients import (
    GGGClient,
    GGGSkillTreeClient,
    PoeNinjaClient,
    PoeWatchClient,
)
from .demo import seed_demo
from .forbidden import (
    FORBIDDEN_CATALOG_CATEGORY,
    SKILL_TREE_CATEGORY,
    PassiveMap,
    enrich_forbidden_assets,
    forbidden_price_points,
    parse_passive_map,
)
from .historical import (
    _compact_assets,
    _validate_divine_curve,
    league_day,
    parse_daily_history_points,
)
from .models import (
    STANDARD_LEAGUE_ID,
    DataSourceError,
    FetchResult,
    League,
    PricePoint,
    iso_utc,
    parse_datetime,
)
from .normalization import (
    assert_poe_ninja_chaos_parity,
    normalize_ggg_markets,
    normalize_poe_ninja,
)
from .storage import Storage


DEFAULT_EXCHANGE_CATEGORIES = [
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
]
DEFAULT_ITEM_CATEGORIES = [
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
]

STANDARD_ANCHOR_SOURCE = "poe.ninja-standard"
CURRENT_HISTORY_PRICE_SOURCE = "poe.watch-history"
POE_NINJA_CURRENT_HISTORY_STATE_SOURCE = "poe.ninja-current-history"
CURRENT_HISTORY_ITEM_CATEGORY = "current-item-history"
CURRENT_HISTORY_DIVINE_CATEGORY = "current-divine-history"
CURRENT_HISTORY_CATALOG_CATEGORY = "current-history-catalog"
CURRENT_HISTORY_MAX_ITEMS = 2_000
CURRENT_HISTORY_REQUEST_PAUSE_SECONDS = 0.03


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _poe_ninja_exchange_history_points(
    payload: Any,
    league: League,
    *,
    pair_ids: tuple[str, ...] = ("chaos", "chaos-orb"),
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("pairs"), list):
        raise DataSourceError("poe.ninja exchange details had no pairs array")
    accepted = {value.casefold() for value in pair_ids}
    pair = next(
        (
            row
            for row in payload["pairs"]
            if isinstance(row, dict)
            and str(row.get("id") or "").casefold() in accepted
        ),
        None,
    )
    if pair is None or not isinstance(pair.get("history"), list):
        raise DataSourceError(
            "poe.ninja exchange details had no dated Chaos pair history"
        )
    start = parse_datetime(league.start_at)
    end = parse_datetime(league.end_at)
    if start is None:
        raise DataSourceError("League start timestamp is unavailable")
    if end is not None and end < start:
        # poe.ninja uses 0001-01-01 as an open-ended league sentinel. It is
        # not a real completion date and must not discard every current-day
        # history observation.
        end = None
    by_day: dict[int, dict[str, Any]] = {}
    for row in pair["history"]:
        if not isinstance(row, dict):
            continue
        observed = parse_datetime(str(row.get("timestamp") or ""))
        rate = _positive_float(row.get("rate"))
        volume = _positive_float(row.get("volumePrimaryValue"))
        if (
            observed is None
            or rate is None
            or volume is None
            or observed.date() < start.date()
        ):
            continue
        if end is not None and observed.date() > end.date():
            continue
        day = league_day(observed, start)
        candidate = {
            "league_day": day,
            "observed_at": iso_utc(observed),
            "mean": rate,
            "volume": volume,
            "confidence": 0.95,
        }
        previous = by_day.get(day)
        if (
            previous is not None
            and candidate["observed_at"] == previous["observed_at"]
            and candidate["mean"] != previous["mean"]
        ):
            raise DataSourceError(
                "poe.ninja exchange history contained conflicting values for "
                f"{candidate['observed_at']}"
            )
        if previous is None or (
            str(candidate["observed_at"]),
            float(candidate.get("volume") or 0.0),
        ) > (
            str(previous["observed_at"]),
            float(previous.get("volume") or 0.0),
        ):
            by_day[day] = candidate
    return [by_day[day] for day in sorted(by_day)]


def _poe_ninja_stash_history_points(
    payload: Any,
    league: League,
    fetched_at: str,
) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise DataSourceError("poe.ninja stash item history was not an array")
    start = parse_datetime(league.start_at)
    fetched = parse_datetime(fetched_at)
    end = parse_datetime(league.end_at)
    if start is None or fetched is None:
        raise DataSourceError("League start or history fetch timestamp is invalid")
    if end is not None and end < start:
        end = None
    utc_midnight = datetime(
        fetched.year,
        fetched.month,
        fetched.day,
        tzinfo=timezone.utc,
    )
    by_day: dict[int, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            days_ago = int(row.get("daysAgo"))
        except (TypeError, ValueError):
            continue
        value = _positive_float(row.get("value"))
        if days_ago < 0 or value is None:
            continue
        observed = utc_midnight - timedelta(days=days_ago)
        if observed.date() < start.date() or (
            end is not None and observed.date() > end.date()
        ):
            continue
        day = league_day(observed, start)
        count = _positive_float(row.get("count"))
        if count is None:
            continue
        candidate = {
            "league_day": day,
            "observed_at": iso_utc(observed),
            "mean": value,
            "volume": count,
            "listing_count": int(count),
            "confidence": 0.95 if count >= 10 else 0.8,
        }
        previous = by_day.get(day)
        # Stash history supplies day offsets, not intraday timestamps. If an
        # upstream duplicate appears, prefer the larger listing sample so the
        # choice is deterministic and independent of response order.
        if previous is not None and candidate["mean"] != previous["mean"]:
            raise DataSourceError(
                "poe.ninja stash history contained conflicting values for "
                f"{candidate['observed_at']}"
            )
        if previous is None or float(candidate.get("volume") or 0.0) > float(
            previous.get("volume") or 0.0
        ):
            by_day[day] = candidate
    return [by_day[day] for day in sorted(by_day)]


class SyncAlreadyRunning(RuntimeError):
    pass


def _league_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("leagues", "items", "data"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
    return []


def _is_softcore_trade(name: str, row: dict[str, Any]) -> bool:
    lowered = name.lower()
    if lowered in {"standard", "hardcore"}:
        return False
    if any(
        marker in lowered
        for marker in ("hardcore", " hc", "ssf", "solo self-found", "ruthless")
    ):
        return False
    if row.get("hardcore") is True or row.get("isHardcore") is True:
        return False
    return True


def choose_softcore_league(payload: Any) -> League:
    rows = _league_rows(payload)
    candidates: list[League] = []
    for row in rows:
        name = str(row.get("name") or row.get("id") or "").strip()
        if not name or not _is_softcore_trade(name, row):
            continue
        league_id = str(row.get("id") or name)
        start_at = (
            row.get("start_at")
            or row.get("startAt")
            or row.get("start_date")
            or row.get("startDate")
        )
        end_at = (
            row.get("end_at")
            or row.get("endAt")
            or row.get("end_date")
            or row.get("endDate")
        )
        candidates.append(
            League(
                id=league_id,
                name=name,
                start_at=str(start_at) if start_at else None,
                end_at=str(end_at) if end_at else None,
            )
        )
    if not candidates:
        raise DataSourceError(
            "No current Path of Exile 1 softcore trade league was found"
        )
    # poe.ninja documents the first entry as the active temporary league.
    return candidates[0]


def enrich_league(league: League, payload: Any) -> League:
    for row in _league_rows(payload):
        row_name = str(row.get("name") or row.get("id") or "")
        if row_name not in {league.name, league.id}:
            continue
        start = (
            row.get("start_at")
            or row.get("startAt")
            or row.get("start_date")
            or row.get("startDate")
        )
        end = (
            row.get("end_at")
            or row.get("endAt")
            or row.get("end_date")
            or row.get("endDate")
        )
        if start:
            league.start_at = str(start)
        if end:
            league.end_at = str(end)
        break
    return league


class SyncService:
    def __init__(
        self,
        storage: Storage,
        *,
        poe_ninja: PoeNinjaClient | None = None,
        ggg: GGGClient | None = None,
        poe_watch: PoeWatchClient | None = None,
        skill_tree: GGGSkillTreeClient | None = None,
        allow_demo_seed: bool = True,
    ):
        self.storage = storage
        self.poe_ninja = poe_ninja or PoeNinjaClient()
        self.ggg = ggg or GGGClient()
        self.poe_watch = poe_watch or PoeWatchClient()
        self.skill_tree = skill_tree or GGGSkillTreeClient()
        self.allow_demo_seed = allow_demo_seed
        self._lock = threading.Lock()
        self._current_history_lock = threading.Lock()

    @property
    def is_syncing(self) -> bool:
        return self._lock.locked()

    def sync_current_item_histories(
        self,
        league: League,
        item_keys: list[str],
        *,
        max_items: int = CURRENT_HISTORY_MAX_ITEMS,
    ) -> dict[str, Any]:
        """Backfill authoritative dated curves from poe.ninja detail APIs."""

        requested_keys = list(
            dict.fromkeys(
                str(item_key).strip()
                for item_key in item_keys
                if str(item_key).strip()
            )
        )[: max(1, min(int(max_items), CURRENT_HISTORY_MAX_ITEMS))]
        summary: dict[str, Any] = {
            "status": "running",
            "source": PoeNinjaClient.SOURCE,
            "price_source": PoeNinjaClient.SOURCE,
            "state_source": POE_NINJA_CURRENT_HISTORY_STATE_SOURCE,
            "requested_items": len(requested_keys),
            "matched_items": 0,
            "unmatched_items": 0,
            "ambiguous_items": 0,
            "unmatched": [],
            "fetched_items": 0,
            "cached_items": 0,
            "failed_items": 0,
            "rows_written": 0,
            "snapshots_written": 0,
            "assets_written": 0,
            "coverage": {},
            "warnings": [],
        }
        if not self._current_history_lock.acquire(blocking=False):
            summary.update(
                status="busy",
                message="A current-league item-history backfill is already running.",
            )
            return summary
        try:
            if league.is_demo:
                summary.update(
                    status="skipped",
                    message="Current history is unavailable in demo mode.",
                )
                return summary
            if not league.start_at:
                summary.update(
                    status="skipped",
                    message=(
                        "Current history needs a verified league start timestamp."
                    ),
                )
                return summary
            if not requested_keys:
                summary.update(
                    status="success",
                    message="No ranked item histories were requested.",
                )
                return summary

            exchange_types = {
                str(category).casefold()
                for category in self.storage.get_setting(
                    "exchange_categories",
                    DEFAULT_EXCHANGE_CATEGORIES,
                )
            }
            selected: list[dict[str, Any]] = []
            for item_key in requested_keys:
                rows = self.storage.item_histories(
                    league.id,
                    days=max(90, int(league.day or 1) + 14),
                    item_key=item_key,
                    sources=(PoeNinjaClient.SOURCE,),
                ).get(item_key, [])
                latest = rows[-1] if rows else None
                details = (
                    latest.get("details")
                    if isinstance(latest, dict)
                    and isinstance(latest.get("details"), dict)
                    else {}
                )
                category = str(latest.get("category") or "") if latest else ""
                history_kind = (
                    "exchange"
                    if category.casefold() in exchange_types
                    else "stash-item"
                )
                source_item_id = (
                    details.get("detailsId")
                    if history_kind == "exchange"
                    else details.get("poe_ninja_id")
                )
                if latest is None or source_item_id in (None, "") or not category:
                    summary["unmatched_items"] += 1
                    if len(summary["unmatched"]) < 20:
                        summary["unmatched"].append(
                            {
                                "item_key": item_key,
                                "reason": "missing_poe_ninja_history_identity",
                            }
                        )
                    continue
                selected.append(
                    {
                        "item_key": item_key,
                        "name": str(latest["name"]),
                        "category": category,
                        "source_item_id": str(source_item_id),
                        "history_kind": history_kind,
                    }
                )
            summary["matched_items"] = len(selected)
            if summary["unmatched_items"]:
                self._current_history_warning(
                    summary,
                    f"{summary['unmatched_items']} of {summary['requested_items']} "
                    "ranked keys lacked an exact poe.ninja history identity.",
                )
            if not selected:
                summary.update(
                    status="partial",
                    message=(
                        "No ranked item had an exact poe.ninja detail-history "
                        "identity; no histories were attached."
                    ),
                )
                return summary

            divine_record = self._poe_ninja_current_history_response(
                league,
                category="Currency",
                source_item_id="divine-orb",
                history_kind="exchange",
                snapshot_category=CURRENT_HISTORY_DIVINE_CATEGORY,
                summary=summary,
            )
            try:
                divine_points = _poe_ninja_exchange_history_points(
                    divine_record["payload"],
                    league,
                )
                raw_divine_curve = {
                    int(point["league_day"]): float(point["mean"])
                    for point in divine_points
                }
                divine_quality = _validate_divine_curve(
                    raw_divine_curve,
                    minimum_points=1,
                    reject_adjacent_jumps=False,
                )
            except Exception as error:
                self.storage.update_snapshot_metadata(
                    int(divine_record["snapshot_id"]),
                    {
                        "kind": "current-divine-history",
                        "provider": PoeNinjaClient.SOURCE,
                        "source_item_id": "divine-orb",
                        "valid": False,
                        "validation_error": str(error),
                        "interpolation": "none",
                    },
                )
                raise
            divine_snapshot_id = self._store_poe_ninja_current_history_record(
                divine_record,
                league,
                CURRENT_HISTORY_DIVINE_CATEGORY,
                {
                    "kind": "current-divine-history",
                    "provider": PoeNinjaClient.SOURCE,
                    "source_item_id": "divine-orb",
                    "provider_observed_days": sorted(raw_divine_curve),
                    "usable_days": sorted(divine_quality.prices),
                    "rejected_days": sorted(divine_quality.rejected_days),
                    "quality_issues": list(divine_quality.issues),
                    "normalization": "Chaos per Divine, exact dated pair history",
                },
                summary,
            )
            divine_points_by_day = {
                int(point["league_day"]): point
                for point in divine_points
                if int(point["league_day"]) in divine_quality.prices
            }
            current_day = max(1, int(league.day or 1))

            for asset in selected:
                item_key = str(asset["item_key"])
                source_item_id = str(asset["source_item_id"])
                try:
                    record = self._poe_ninja_current_history_response(
                        league,
                        category=str(asset["category"]),
                        source_item_id=source_item_id,
                        history_kind=str(asset["history_kind"]),
                        snapshot_category=CURRENT_HISTORY_ITEM_CATEGORY,
                        summary=summary,
                    )
                    if asset["history_kind"] == "exchange":
                        item_points = _poe_ninja_exchange_history_points(
                            record["payload"],
                            league,
                        )
                    else:
                        item_points = _poe_ninja_stash_history_points(
                            record["payload"],
                            league,
                            str(record["fetched_at"]),
                        )
                    item_points = [
                        point
                        for point in item_points
                        if 1 <= int(point["league_day"]) <= current_day
                    ]
                    provider_days = {
                        int(point["league_day"]) for point in item_points
                    }
                    normalized_days = sorted(
                        provider_days.intersection(divine_quality.prices)
                    )
                    missing_provider_days = [
                        day
                        for day in range(1, current_day + 1)
                        if day not in provider_days
                    ]
                    missing_divine_days = sorted(
                        provider_days.difference(divine_quality.prices)
                    )
                    metadata = {
                        "kind": "current-item-history",
                        "provider": PoeNinjaClient.SOURCE,
                        "provider_league": league.id,
                        **asset,
                        "provider_observed_days": sorted(provider_days),
                        "provider_first_observed_day": (
                            min(provider_days) if provider_days else None
                        ),
                        "provider_last_observed_day": (
                            max(provider_days) if provider_days else None
                        ),
                        "provider_missing_days": missing_provider_days,
                        "normalized_days": normalized_days,
                        "missing_divine_anchor_days": missing_divine_days,
                        "interpolation": "none",
                    }
                    snapshot_id = self._store_poe_ninja_current_history_record(
                        record,
                        league,
                        CURRENT_HISTORY_ITEM_CATEGORY,
                        metadata,
                        summary,
                    )
                    points: list[PricePoint] = []
                    for point in item_points:
                        point_day = int(point["league_day"])
                        divine_point = divine_points_by_day.get(point_day)
                        if divine_point is None:
                            continue
                        divine_chaos = divine_quality.prices[point_day]
                        points.append(
                            PricePoint(
                                league_id=league.id,
                                item_key=item_key,
                                name=str(asset["name"]),
                                category=str(asset["category"]),
                                source=PoeNinjaClient.SOURCE,
                                observed_at=str(point["observed_at"]),
                                chaos_value=float(point["mean"]),
                                divine_value=float(point["mean"]) / divine_chaos,
                                listing_count=point.get("listing_count"),
                                volume=point.get("volume"),
                                confidence=min(
                                    float(point["confidence"]),
                                    float(divine_point["confidence"]),
                                    0.95,
                                ),
                                details={
                                    "history_backfill": True,
                                    "provider": PoeNinjaClient.SOURCE,
                                    "source_item_id": source_item_id,
                                    "history_kind": asset["history_kind"],
                                    **(
                                        {"detailsId": source_item_id}
                                        if asset["history_kind"] == "exchange"
                                        else {"poe_ninja_id": source_item_id}
                                    ),
                                    "league_day": point_day,
                                    "divine_chaos": divine_chaos,
                                    "normalization": (
                                        "exact same-league, same-day poe.ninja "
                                        "Divine/Chaos pair"
                                    ),
                                    "interpolated": False,
                                    "divine_snapshot_id": divine_snapshot_id,
                                },
                                snapshot_id=snapshot_id,
                            )
                        )
                    if item_points and not points:
                        raise DataSourceError(
                            "no item day had an exact poe.ninja Divine/Chaos anchor"
                        )
                    summary["rows_written"] += (
                        self.storage.insert_price_points(points) if points else 0
                    )
                    summary["coverage"][item_key] = {
                        "source": PoeNinjaClient.SOURCE,
                        "source_item_id": source_item_id,
                        "first_observed_day": (
                            min(provider_days) if provider_days else None
                        ),
                        "last_observed_day": (
                            max(provider_days) if provider_days else None
                        ),
                        "observed_days": sorted(provider_days),
                        "missing_days": missing_provider_days,
                        "normalized_days": normalized_days,
                        "missing_divine_anchor_days": missing_divine_days,
                        "interpolation": "none",
                    }
                    summary[
                        "cached_items" if record["cached"] else "fetched_items"
                    ] += 1
                except Exception as error:
                    summary["failed_items"] += 1
                    self._current_history_warning(
                        summary,
                        f"{asset['name']} ({source_item_id}): {error}",
                    )

            if divine_quality.partial:
                self._current_history_warning(
                    summary,
                    "poe.ninja Divine/Chaos history contained rejected days: "
                    + "; ".join(divine_quality.issues),
                )
            summary["status"] = (
                "partial"
                if summary["failed_items"]
                or summary["unmatched_items"]
                or divine_quality.partial
                else "success"
            )
            summary["message"] = (
                "Current ranked-item archive updated from authoritative dated "
                "poe.ninja detail histories; genuine missing days were preserved."
            )
            self.storage.update_source_state(
                source=POE_NINJA_CURRENT_HISTORY_STATE_SOURCE,
                endpoint=f"{league.id}:ranked-current-history",
                league_id=league.id,
                category="ranked-current-history",
                status=summary["status"],
                detail=(
                    f"{summary['matched_items']} exact ranked identities; "
                    f"{summary['unmatched_items']} unmatched; "
                    f"{summary['rows_written']} normalized daily rows; "
                    f"{summary['failed_items']} failures."
                ),
                success=True,
            )
            return summary
        except Exception as error:
            summary["status"] = "failed"
            summary["message"] = (
                f"Current ranked-item history stopped safely: {error}"
            )
            self._current_history_warning(summary, str(error))
            self.storage.update_source_state(
                source=POE_NINJA_CURRENT_HISTORY_STATE_SOURCE,
                endpoint=f"{league.id}:ranked-current-history",
                league_id=league.id,
                category="ranked-current-history",
                status="unavailable",
                detail=str(error),
            )
            return summary
        finally:
            self._current_history_lock.release()

    def _sync_current_item_histories_poe_watch_archive(
        self,
        league: League,
        item_keys: list[str],
        *,
        max_items: int = CURRENT_HISTORY_MAX_ITEMS,
    ) -> dict[str, Any]:
        """Backfill exact dated curves for a bounded ranked-item set.

        poe.ninja's interval sparkline is not dated and is therefore never
        expanded into synthetic observations. Instead, this uses poe.watch's
        exact item-history endpoint and an exact same-day Divine Orb history.
        Missing item days or Divine anchors remain gaps.
        """

        requested_keys = list(
            dict.fromkeys(
                str(item_key).strip()
                for item_key in item_keys
                if str(item_key).strip()
            )
        )
        limit = max(1, min(int(max_items), CURRENT_HISTORY_MAX_ITEMS))
        requested_keys = requested_keys[:limit]
        summary: dict[str, Any] = {
            "status": "running",
            "source": PoeWatchClient.SOURCE,
            "price_source": CURRENT_HISTORY_PRICE_SOURCE,
            "requested_items": len(requested_keys),
            "matched_items": 0,
            "unmatched_items": 0,
            "ambiguous_items": 0,
            "unmatched": [],
            "fetched_items": 0,
            "cached_items": 0,
            "failed_items": 0,
            "rows_written": 0,
            "snapshots_written": 0,
            "assets_written": 0,
            "coverage": {},
            "warnings": [],
        }
        if not self._current_history_lock.acquire(blocking=False):
            summary.update(
                {
                    "status": "busy",
                    "message": (
                        "A current-league item-history backfill is already "
                        "running."
                    ),
                }
            )
            return summary
        try:
            if league.is_demo:
                summary.update(
                    {
                        "status": "skipped",
                        "message": "Current history is unavailable in demo mode.",
                    }
                )
                return summary
            if not league.start_at:
                summary.update(
                    {
                        "status": "skipped",
                        "message": (
                            "Current history needs a verified league start "
                            "timestamp."
                        ),
                    }
                )
                return summary
            if not requested_keys:
                summary.update(
                    {
                        "status": "success",
                        "message": "No ranked item histories were requested.",
                    }
                )
                return summary

            assets = self._current_history_assets(league, summary)
            assets_by_key: dict[str, list[dict[str, Any]]] = {}
            for asset in assets:
                assets_by_key.setdefault(str(asset["item_key"]), []).append(asset)

            selected: list[dict[str, Any]] = []
            for item_key in requested_keys:
                exact = assets_by_key.get(item_key, [])
                if len(exact) == 1:
                    selected.append(exact[0])
                elif not exact:
                    summary["unmatched_items"] += 1
                    if len(summary["unmatched"]) < 20:
                        summary["unmatched"].append(
                            {
                                "item_key": item_key,
                                "reason": "no_exact_catalog_match",
                            }
                        )
                elif len(exact) > 1:
                    summary["unmatched_items"] += 1
                    summary["ambiguous_items"] += 1
                    if len(summary["unmatched"]) < 20:
                        summary["unmatched"].append(
                            {
                                "item_key": item_key,
                                "reason": "ambiguous_catalog_identity",
                                "matches": len(exact),
                            }
                        )
            summary["matched_items"] = len(selected)
            if summary["unmatched_items"]:
                self._current_history_warning(
                    summary,
                    f"{summary['unmatched_items']} of "
                    f"{summary['requested_items']} ranked keys had no unique "
                    "exact poe.watch catalog match and were not backfilled.",
                )
            if not selected:
                summary.update(
                    {
                        "status": "partial",
                        "message": (
                            "No ranked item had one exact poe.watch catalog "
                            "identity; no histories were attached."
                        ),
                    }
                )
                return summary

            divine_assets = [
                asset
                for asset in assets
                if str(asset.get("name") or "").strip().casefold()
                == "divine orb"
                and str(asset.get("category") or "").strip().casefold()
                == "currency"
            ]
            if len(divine_assets) != 1:
                raise DataSourceError(
                    "poe.watch catalog must contain exactly one Currency / "
                    f"Divine Orb row; found {len(divine_assets)}"
                )
            divine_asset = divine_assets[0]
            summary["assets_written"] = int(
                self.storage.upsert_historical_assets(
                    [divine_asset, *selected]
                )
                or 0
            )
            divine_record = self._current_history_response(
                league,
                str(divine_asset["source_item_id"]),
                CURRENT_HISTORY_DIVINE_CATEGORY,
            )
            if not isinstance(divine_record["payload"], list):
                self._store_invalid_current_history(
                    divine_record,
                    league,
                    CURRENT_HISTORY_DIVINE_CATEGORY,
                    {
                        "kind": "current-divine-history",
                        "source_item_id": str(
                            divine_asset["source_item_id"]
                        ),
                    },
                    "response was not a history array",
                    summary,
                )
                raise DataSourceError(
                    "poe.watch current Divine Orb response was not a history "
                    "array"
                )
            divine_points = parse_daily_history_points(
                divine_record["payload"],
                league.start_at,
                league.end_at,
            )
            raw_divine_curve = {
                int(point["league_day"]): float(point["mean"])
                for point in divine_points
            }
            try:
                divine_quality = _validate_divine_curve(
                    raw_divine_curve,
                    minimum_points=1,
                )
            except Exception as error:
                self._store_invalid_current_history(
                    divine_record,
                    league,
                    CURRENT_HISTORY_DIVINE_CATEGORY,
                    {
                        "kind": "current-divine-history",
                        "source_item_id": str(
                            divine_asset["source_item_id"]
                        ),
                        "provider_observed_days": sorted(raw_divine_curve),
                    },
                    str(error),
                    summary,
                )
                raise
            divine_snapshot_id = self._store_current_history_record(
                divine_record,
                league,
                CURRENT_HISTORY_DIVINE_CATEGORY,
                {
                    "kind": "current-divine-history",
                    "source_item_id": str(divine_asset["source_item_id"]),
                    "provider_observed_days": sorted(raw_divine_curve),
                    "usable_days": sorted(divine_quality.prices),
                    "rejected_days": sorted(divine_quality.rejected_days),
                    "quality_issues": list(divine_quality.issues),
                    "normalization": "Chaos per Divine, exact league day",
                },
                summary,
            )
            divine_points_by_day = {
                int(point["league_day"]): point
                for point in divine_points
                if int(point["league_day"]) in divine_quality.prices
            }

            current_day = max(1, int(league.day or 1))
            for asset in selected:
                item_key = str(asset["item_key"])
                source_item_id = str(asset["source_item_id"])
                try:
                    record = self._current_history_response(
                        league,
                        source_item_id,
                        CURRENT_HISTORY_ITEM_CATEGORY,
                    )
                    if not isinstance(record["payload"], list):
                        self._store_invalid_current_history(
                            record,
                            league,
                            CURRENT_HISTORY_ITEM_CATEGORY,
                            {
                                "kind": "current-item-history",
                                "item_key": item_key,
                                "source_item_id": source_item_id,
                            },
                            "response was not a history array",
                            summary,
                        )
                        raise DataSourceError(
                            "response was not a history array"
                        )
                    item_points = [
                        point
                        for point in parse_daily_history_points(
                            record["payload"],
                            league.start_at,
                            league.end_at,
                        )
                        if 1 <= int(point["league_day"]) <= current_day
                    ]
                    provider_days = {
                        int(point["league_day"]) for point in item_points
                    }
                    normalized_days = sorted(
                        provider_days.intersection(divine_quality.prices)
                    )
                    missing_provider_days = [
                        day
                        for day in range(1, current_day + 1)
                        if day not in provider_days
                    ]
                    missing_divine_days = sorted(
                        provider_days.difference(divine_quality.prices)
                    )
                    metadata = {
                        "kind": "current-item-history",
                        "provider": PoeWatchClient.SOURCE,
                        "provider_league": league.name,
                        "item_key": item_key,
                        "name": str(asset["name"]),
                        "category": str(asset["category"]),
                        "source_item_id": source_item_id,
                        "divine_source_item_id": str(
                            divine_asset["source_item_id"]
                        ),
                        "provider_observed_days": sorted(provider_days),
                        "provider_first_observed_day": (
                            min(provider_days) if provider_days else None
                        ),
                        "provider_last_observed_day": (
                            max(provider_days) if provider_days else None
                        ),
                        "provider_missing_days": missing_provider_days,
                        "normalized_days": normalized_days,
                        "missing_divine_anchor_days": missing_divine_days,
                        "interpolation": "none",
                    }
                    snapshot_id = self._store_current_history_record(
                        record,
                        league,
                        CURRENT_HISTORY_ITEM_CATEGORY,
                        metadata,
                        summary,
                    )
                    points: list[PricePoint] = []
                    for point in item_points:
                        point_day = int(point["league_day"])
                        divine_point = divine_points_by_day.get(point_day)
                        if divine_point is None:
                            continue
                        divine_chaos = divine_quality.prices[point_day]
                        points.append(
                            PricePoint(
                                league_id=league.id,
                                item_key=item_key,
                                name=str(asset["name"]),
                                category=str(asset["category"]),
                                source=CURRENT_HISTORY_PRICE_SOURCE,
                                observed_at=str(point["observed_at"]),
                                chaos_value=float(point["mean"]),
                                divine_value=(
                                    float(point["mean"]) / divine_chaos
                                ),
                                volume=point.get("volume"),
                                confidence=min(
                                    float(point["confidence"]),
                                    float(divine_point["confidence"]),
                                    0.9,
                                ),
                                details={
                                    "history_backfill": True,
                                    "provider": PoeWatchClient.SOURCE,
                                    "source_item_id": source_item_id,
                                    "divine_source_item_id": str(
                                        divine_asset["source_item_id"]
                                    ),
                                    "league_day": point_day,
                                    "divine_chaos": divine_chaos,
                                    "normalization": (
                                        "exact same-league, same-day Divine "
                                        "Orb observation"
                                    ),
                                    "interpolated": False,
                                    "divine_snapshot_id": divine_snapshot_id,
                                },
                                snapshot_id=snapshot_id,
                            )
                        )
                    if item_points and not points:
                        raise DataSourceError(
                            "no item day had an exact trustworthy Divine Orb "
                            "anchor"
                        )
                    summary["rows_written"] += (
                        self.storage.insert_price_points(points)
                        if points
                        else 0
                    )
                    summary["coverage"][item_key] = {
                        "source": PoeWatchClient.SOURCE,
                        "source_item_id": source_item_id,
                        "first_observed_day": (
                            min(provider_days) if provider_days else None
                        ),
                        "last_observed_day": (
                            max(provider_days) if provider_days else None
                        ),
                        "observed_days": sorted(provider_days),
                        "missing_days": missing_provider_days,
                        "normalized_days": normalized_days,
                        "missing_divine_anchor_days": missing_divine_days,
                        "interpolation": "none",
                    }
                    if record["cached"]:
                        summary["cached_items"] += 1
                    else:
                        summary["fetched_items"] += 1
                except Exception as error:
                    summary["failed_items"] += 1
                    self._current_history_warning(
                        summary,
                        f"{asset['name']} ({source_item_id}): {error}",
                    )

            if divine_quality.partial:
                self._current_history_warning(
                    summary,
                    "Current Divine Orb history contained rejected days: "
                    + "; ".join(divine_quality.issues),
                )
            summary["status"] = (
                "partial"
                if (
                    summary["failed_items"]
                    or summary["unmatched_items"]
                    or divine_quality.partial
                )
                else "success"
            )
            summary["message"] = (
                "Current ranked-item archive updated from exact dated "
                "poe.watch histories; genuine missing days were preserved."
            )
            state_endpoint = f"{league.id}:ranked-current-history"
            self.storage.update_source_state(
                source=CURRENT_HISTORY_PRICE_SOURCE,
                endpoint=state_endpoint,
                league_id=league.id,
                category="ranked-current-history",
                status=summary["status"],
                detail=(
                    f"{summary['matched_items']} exact ranked identities; "
                    f"{summary['unmatched_items']} unmatched; "
                    f"{summary['rows_written']} normalized daily rows; "
                    f"{summary['failed_items']} failures."
                ),
                success=True,
            )
            return summary
        except Exception as error:
            summary["status"] = "failed"
            summary["message"] = (
                "Current ranked-item history stopped safely: "
                f"{error}"
            )
            self._current_history_warning(summary, str(error))
            self.storage.update_source_state(
                source=CURRENT_HISTORY_PRICE_SOURCE,
                endpoint=f"{league.id}:ranked-current-history",
                league_id=league.id,
                category="ranked-current-history",
                status="unavailable",
                detail=str(error),
            )
            return summary
        finally:
            self._current_history_lock.release()

    def _poe_ninja_current_history_response(
        self,
        league: League,
        *,
        category: str,
        source_item_id: str,
        history_kind: str,
        snapshot_category: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        if history_kind == "exchange":
            endpoint = self.poe_ninja.exchange_details_url(
                league.id,
                category,
                source_item_id,
            )
            fetcher = self.poe_ninja.fetch_exchange_details
        elif history_kind == "stash-item":
            endpoint = self.poe_ninja.stash_item_history_url(
                league.id,
                category,
                source_item_id,
            )
            fetcher = self.poe_ninja.fetch_stash_item_history
        else:
            raise DataSourceError(f"Unsupported poe.ninja history kind: {history_kind}")
        cached = self.storage.latest_snapshot(
            source=PoeNinjaClient.SOURCE,
            endpoint=endpoint,
            league_id=league.id,
            category=snapshot_category,
        )
        if cached is not None and self._history_snapshot_is_current_day(
            league,
            str(cached.get("fetched_at") or ""),
        ):
            return {
                "payload": self._decode_cached_json(cached["raw"], endpoint),
                "snapshot_id": int(cached["id"]),
                "cached": True,
                "endpoint": endpoint,
                "fetched_at": cached["fetched_at"],
            }
        result = fetcher(league.id, category, source_item_id)
        time.sleep(CURRENT_HISTORY_REQUEST_PAUSE_SECONDS)
        snapshot_id, created = self._store_snapshot(
            result,
            source=PoeNinjaClient.SOURCE,
            league_id=league.id,
            category=snapshot_category,
            metadata={
                "provider": PoeNinjaClient.SOURCE,
                "history_kind": history_kind,
                "source_item_id": source_item_id,
            },
        )
        summary["snapshots_written"] += int(created)
        return {
            "payload": result.payload,
            "snapshot_id": snapshot_id,
            "cached": False,
            "endpoint": endpoint,
            "fetched_at": result.fetched_at,
        }

    def _store_poe_ninja_current_history_record(
        self,
        record: dict[str, Any],
        league: League,
        category: str,
        metadata: dict[str, Any],
        summary: dict[str, Any],
    ) -> int:
        del league, category, summary
        snapshot_id = int(record["snapshot_id"])
        self.storage.update_snapshot_metadata(snapshot_id, metadata)
        return snapshot_id

    def _current_history_assets(
        self,
        league: League,
        summary: dict[str, Any],
    ) -> list[dict[str, Any]]:
        endpoint = self.poe_watch.compact_url(league.name, all_items=True)
        cached = self.storage.latest_snapshot(
            source=PoeWatchClient.SOURCE,
            endpoint=endpoint,
            league_id=league.id,
        )
        if cached is not None:
            payload = self._decode_cached_json(cached["raw"], endpoint)
        else:
            result = self.poe_watch.fetch_compact(
                league.name,
                all_items=True,
            )
            snapshot_id, created = self._store_snapshot(
                result,
                source=PoeWatchClient.SOURCE,
                league_id=league.id,
                category=CURRENT_HISTORY_CATALOG_CATEGORY,
                metadata={
                    "all": True,
                    "purpose": "ranked current-item history identity",
                    "provider_league": league.name,
                },
            )
            del snapshot_id
            summary["snapshots_written"] += int(created)
            payload = result.payload
        assets = _compact_assets(payload)
        if not assets:
            raise DataSourceError(
                "poe.watch compact catalog contained no exact item identities"
            )
        return assets

    def _current_history_response(
        self,
        league: League,
        source_item_id: str,
        category: str,
    ) -> dict[str, Any]:
        endpoint = self.poe_watch.history_url(league.name, source_item_id)
        cached = self.storage.latest_snapshot(
            source=PoeWatchClient.SOURCE,
            endpoint=endpoint,
            league_id=league.id,
            category=category,
        )
        if cached is not None and self._history_snapshot_is_current_day(
            league,
            str(cached.get("fetched_at") or ""),
        ):
            return {
                "payload": self._decode_cached_json(
                    cached["raw"],
                    endpoint,
                ),
                "snapshot_id": int(cached["id"]),
                "result": None,
                "cached": True,
                "endpoint": endpoint,
                "fetched_at": cached["fetched_at"],
            }
        result = self.poe_watch.fetch_history(league.name, source_item_id)
        time.sleep(CURRENT_HISTORY_REQUEST_PAUSE_SECONDS)
        return {
            "payload": result.payload,
            "snapshot_id": None,
            "result": result,
            "cached": False,
            "endpoint": endpoint,
            "fetched_at": result.fetched_at,
        }

    @staticmethod
    def _history_snapshot_is_current_day(
        league: League,
        fetched_at: str,
    ) -> bool:
        fetched = parse_datetime(fetched_at)
        start = parse_datetime(league.start_at)
        if fetched is None or start is None:
            return False
        return league_day(fetched, start) == max(1, int(league.day or 1))

    def _store_current_history_record(
        self,
        record: dict[str, Any],
        league: League,
        category: str,
        metadata: dict[str, Any],
        summary: dict[str, Any],
    ) -> int:
        if record["result"] is None:
            snapshot_id = int(record["snapshot_id"])
            self.storage.update_snapshot_metadata(snapshot_id, metadata)
            return snapshot_id
        snapshot_id, created = self._store_snapshot(
            record["result"],
            source=PoeWatchClient.SOURCE,
            league_id=league.id,
            category=category,
            metadata=metadata,
        )
        summary["snapshots_written"] += int(created)
        return snapshot_id

    def _store_invalid_current_history(
        self,
        record: dict[str, Any],
        league: League,
        category: str,
        metadata: dict[str, Any],
        error: str,
        summary: dict[str, Any],
    ) -> None:
        self._store_current_history_record(
            record,
            league,
            category,
            {
                **metadata,
                "valid": False,
                "validation_error": error,
                "interpolation": "none",
            },
            summary,
        )

    @staticmethod
    def _current_history_warning(
        summary: dict[str, Any],
        message: str,
    ) -> None:
        warnings = summary["warnings"]
        if len(warnings) < 20:
            warnings.append(message)
        else:
            summary["warnings_suppressed"] = int(
                summary.get("warnings_suppressed") or 0
            ) + 1

    def sync(self, *, backfill_hours: int = 0) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise SyncAlreadyRunning("A market sync is already running")
        run_id = self.storage.start_sync_run()
        warnings: list[str] = []
        stats = {
            "rows_written": 0,
            "snapshots_written": 0,
            "endpoints_checked": 0,
            "not_modified": 0,
            "failed_endpoints": 0,
            "official_hours": 0,
            "forbidden_variants": 0,
            "forbidden_variants_mapped": 0,
            "standard_rows_written": 0,
            "standard_endpoints_checked": 0,
            "standard_failed_endpoints": 0,
            "standard_anchor_available": False,
            "poe_ninja_parity_rows": 0,
            "standard_parity_rows": 0,
            "demo_seeded": False,
            "live": False,
        }
        try:
            league = self._discover_league(warnings, stats)
            if league is None:
                return self._finish_without_live_data(run_id, warnings, stats)
            self.storage.upsert_league(league, current=True)
            self.storage.set_sync_run_league(run_id, league.id)

            successful_core = self._sync_poe_ninja(league, warnings, stats)
            # Optional legacy enrichment is quarantined from the golden price
            # path and must never make a failed poe.ninja price sync look fresh.
            self._sync_forbidden_jewels(league, warnings, stats)
            stats["standard_anchor_available"] = (
                self._sync_standard_prices(warnings, stats)
            )
            requested_hours = max(0, min(int(backfill_hours), 336))
            if requested_hours:
                self._sync_official_exchange(
                    league, requested_hours, warnings, stats
                )
            else:
                self.storage.update_source_state(
                    source=GGGClient.SOURCE,
                    endpoint=self.ggg.currency_exchange_url(),
                    league_id=league.id,
                    status="idle",
                    detail="Hourly history backfill was not requested.",
                )

            existing = self.storage.status_counts(league.id)["price_points"]
            fresh_prices = successful_core
            if not fresh_prices:
                if existing == 0:
                    return self._finish_without_live_data(run_id, warnings, stats)
                message = (
                    "No current price endpoint could be verified. Existing "
                    "local history was retained, but its freshness timestamp "
                    "was not advanced."
                )
                self.storage.finish_sync_run(
                    run_id,
                    status="failed",
                    rows_written=stats["rows_written"],
                    snapshots_written=stats["snapshots_written"],
                    message=message,
                    warnings=warnings,
                )
                return {
                    "ok": False,
                    "message": message,
                    "stats": stats,
                    "warnings": warnings,
                }

            stats["live"] = True
            status = "partial" if stats["failed_endpoints"] else "success"
            message = (
                f"Synced {league.name}: {stats['rows_written']} normalized "
                f"price rows from {stats['endpoints_checked']} endpoint checks."
            )
            self.storage.finish_sync_run(
                run_id,
                status=status,
                rows_written=stats["rows_written"],
                snapshots_written=stats["snapshots_written"],
                message=message,
                warnings=warnings,
            )
            return {
                "ok": True,
                "message": message,
                "stats": stats,
                "warnings": warnings,
            }
        except Exception as error:
            warnings.append(str(error))
            self.storage.finish_sync_run(
                run_id,
                status="failed",
                rows_written=stats["rows_written"],
                snapshots_written=stats["snapshots_written"],
                message="Market sync failed; previously stored data was left intact.",
                warnings=warnings,
            )
            return {
                "ok": False,
                "message": (
                    "Market sync failed; previously stored data was left intact."
                ),
                "stats": stats,
                "warnings": warnings,
            }
        finally:
            self._lock.release()

    def _discover_league(
        self, warnings: list[str], stats: dict[str, Any]
    ) -> League | None:
        endpoint = self.poe_ninja.league_url()
        state = self.storage.get_source_state(
            PoeNinjaClient.SOURCE, endpoint
        ) or {}
        try:
            result = self.poe_ninja.list_leagues(
                etag=state.get("etag"),
                last_modified=state.get("last_modified"),
            )
            stats["endpoints_checked"] += 1
            if result.not_modified:
                stats["not_modified"] += 1
                current = self.storage.get_current_league()
                if current and not current.is_demo:
                    league = current
                else:
                    raise DataSourceError(
                        "League list returned 304 but no saved live league exists"
                    )
            else:
                snapshot_id, created = self._store_snapshot(
                    result,
                    source=PoeNinjaClient.SOURCE,
                    league_id=None,
                    category="leagues",
                )
                del snapshot_id
                stats["snapshots_written"] += int(created)
                league = choose_softcore_league(result.payload)
            self.storage.update_source_state(
                source=PoeNinjaClient.SOURCE,
                endpoint=endpoint,
                status="ok",
                detail=f"Current softcore trade league: {league.name}",
                etag=result.etag,
                last_modified=result.last_modified,
                success=True,
            )
        except Exception as error:
            stats["failed_endpoints"] += 1
            warning = f"poe.ninja league discovery failed: {error}"
            warnings.append(warning)
            self.storage.update_source_state(
                source=PoeNinjaClient.SOURCE,
                endpoint=endpoint,
                status="unavailable",
                detail=str(error),
            )
            current = self.storage.get_current_league()
            return current if current and not current.is_demo else None

        # poe.ninja is authoritative whenever it supplies the league start.
        # Only fall back to optional metadata when that field is absent.
        if league.start_at:
            return league

        # Optional enrichment: poe.watch can fill a missing league start.
        watch_endpoint = self.poe_watch.leagues_url()
        watch_state = self.storage.get_source_state(
            PoeWatchClient.SOURCE, watch_endpoint
        ) or {}
        try:
            watch_result = self.poe_watch.list_leagues(
                etag=watch_state.get("etag"),
                last_modified=watch_state.get("last_modified"),
            )
            stats["endpoints_checked"] += 1
            if watch_result.not_modified:
                stats["not_modified"] += 1
                prior = self.storage.get_league(league.id)
                if prior and prior.start_at:
                    league.start_at = prior.start_at
            else:
                _, created = self._store_snapshot(
                    watch_result,
                    source=PoeWatchClient.SOURCE,
                    # The discovered league is persisted immediately after this
                    # enrichment step, so this metadata snapshot is global to
                    # avoid violating the raw-snapshot foreign key first.
                    league_id=None,
                    category="league-metadata",
                )
                stats["snapshots_written"] += int(created)
                league = enrich_league(league, watch_result.payload)
            self.storage.update_source_state(
                source=PoeWatchClient.SOURCE,
                endpoint=watch_endpoint,
                league_id=league.id,
                status="ok",
                detail=(
                    f"League metadata enriched for {league.name}."
                    if league.start_at
                    else f"No start date found for {league.name}."
                ),
                etag=watch_result.etag,
                last_modified=watch_result.last_modified,
                success=True,
            )
        except Exception as error:
            warnings.append(f"Optional poe.watch metadata unavailable: {error}")
            self.storage.update_source_state(
                source=PoeWatchClient.SOURCE,
                endpoint=watch_endpoint,
                league_id=league.id,
                status="unavailable",
                detail=str(error),
            )

        # If the user already has a service token, official metadata can fill
        # the same field. It is never required for a successful sync.
        if self.ggg.leagues_configured and not league.start_at:
            try:
                official = self.ggg.list_leagues()
                stats["endpoints_checked"] += 1
                league = enrich_league(league, official.payload)
            except Exception as error:
                warnings.append(f"Optional official league metadata failed: {error}")
        return league

    def _sync_poe_ninja(
        self,
        league: League,
        warnings: list[str],
        stats: dict[str, Any],
        *,
        standard_anchor: bool = False,
    ) -> bool:
        exchange_categories = self.storage.get_setting(
            "exchange_categories", DEFAULT_EXCHANGE_CATEGORIES
        )
        item_categories = self.storage.get_setting(
            "item_categories", DEFAULT_ITEM_CATEGORIES
        )
        jobs: list[
            tuple[
                str,
                str,
                Callable[..., FetchResult],
            ]
        ] = []
        for category in exchange_categories:
            jobs.append(
                (
                    str(category),
                    self.poe_ninja.exchange_url(league.id, str(category)),
                    self.poe_ninja.fetch_exchange,
                )
            )
        for category in item_categories:
            jobs.append(
                (
                    str(category),
                    self.poe_ninja.stash_item_url(league.id, str(category)),
                    self.poe_ninja.fetch_stash_item,
                )
            )

        any_success = False
        for category, endpoint, fetcher in jobs:
            state_source = (
                STANDARD_ANCHOR_SOURCE
                if standard_anchor
                else PoeNinjaClient.SOURCE
            )
            state = self.storage.get_source_state(
                state_source, endpoint, league.id, category
            ) or {}
            try:
                result = fetcher(
                    league.id,
                    category,
                    etag=state.get("etag"),
                    last_modified=state.get("last_modified"),
                )
                stats["endpoints_checked"] += 1
                if standard_anchor:
                    stats["standard_endpoints_checked"] += 1
                if result.not_modified:
                    stats["not_modified"] += 1
                    cached = self.storage.latest_snapshot(
                        source=state_source,
                        endpoint=endpoint,
                        league_id=league.id,
                        category=category,
                    )
                    if cached is None:
                        raise DataSourceError(
                            "poe.ninja returned 304 without a cached source snapshot"
                        )
                    cached_payload = self._decode_cached_json(
                        cached["raw"],
                        endpoint,
                    )
                    points = normalize_poe_ninja(
                        cached_payload,
                        league_id=league.id,
                        category=category,
                        observed_at=result.fetched_at,
                        snapshot_id=int(cached["id"]),
                    )
                    parity_rows = assert_poe_ninja_chaos_parity(
                        cached_payload,
                        points,
                    )
                    stats[
                        "standard_parity_rows"
                        if standard_anchor
                        else "poe_ninja_parity_rows"
                    ] += parity_rows
                    written = self.storage.insert_price_points(points)
                    stats["rows_written"] += written
                    if standard_anchor:
                        stats["standard_rows_written"] += written
                    any_success = True
                    detail = (
                        f"Revalidated unchanged; stored {len(points)} "
                        "fresh daily observations from the cached payload."
                    )
                else:
                    snapshot_id, created = self._store_snapshot(
                        result,
                        source=state_source,
                        league_id=league.id,
                        category=category,
                    )
                    stats["snapshots_written"] += int(created)
                    points = normalize_poe_ninja(
                        result.payload,
                        league_id=league.id,
                        category=category,
                        observed_at=result.fetched_at,
                        snapshot_id=snapshot_id,
                    )
                    parity_rows = assert_poe_ninja_chaos_parity(
                        result.payload,
                        points,
                    )
                    stats[
                        "standard_parity_rows"
                        if standard_anchor
                        else "poe_ninja_parity_rows"
                    ] += parity_rows
                    written = self.storage.insert_price_points(points)
                    stats["rows_written"] += written
                    if standard_anchor:
                        stats["standard_rows_written"] += written
                    any_success = True
                    detail = f"Stored {len(points)} normalized rows."
                self.storage.update_source_state(
                    source=state_source,
                    endpoint=endpoint,
                    league_id=league.id,
                    category=category,
                    status="ok",
                    detail=detail,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    success=True,
                )
            except Exception as error:
                if standard_anchor:
                    stats["standard_failed_endpoints"] += 1
                    label = "poe.ninja Standard"
                else:
                    stats["failed_endpoints"] += 1
                    label = "poe.ninja"
                warnings.append(f"{label} {category} failed: {error}")
                self.storage.update_source_state(
                    source=state_source,
                    endpoint=endpoint,
                    league_id=league.id,
                    category=category,
                    status="unavailable",
                    detail=str(error),
                )
        return any_success

    def _sync_standard_prices(
        self,
        warnings: list[str],
        stats: dict[str, Any],
    ) -> bool:
        """Persist current Standard prices as an optional long-term anchor."""

        standard = League(
            id=STANDARD_LEAGUE_ID,
            name=STANDARD_LEAGUE_ID,
        )
        self.storage.upsert_league(standard, current=False)
        standard_warnings: list[str] = []
        available = self._sync_poe_ninja(
            standard,
            standard_warnings,
            stats,
            standard_anchor=True,
        )
        if standard_warnings:
            warnings.append(
                "Standard long-term anchor was incomplete across "
                f"{len(standard_warnings)} endpoint"
                f"{'s' if len(standard_warnings) != 1 else ''}. "
                f"First error: {standard_warnings[0]}"
            )
        return available

    @staticmethod
    def _decode_cached_json(raw: bytes, endpoint: str) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DataSourceError(
                f"Cached response for {endpoint} is not valid JSON"
            ) from error

    def _load_passive_map(
        self,
        warnings: list[str],
        stats: dict[str, Any],
    ) -> tuple[PassiveMap, int | None, str]:
        endpoint = self.skill_tree.export_url()
        state = self.storage.get_source_state(
            GGGSkillTreeClient.SOURCE,
            endpoint,
            category=SKILL_TREE_CATEGORY,
        ) or {}
        cached = self.storage.latest_snapshot(
            source=GGGSkillTreeClient.SOURCE,
            endpoint=endpoint,
            league_id=None,
            category=SKILL_TREE_CATEGORY,
        )
        try:
            result = self.skill_tree.fetch_export(
                etag=state.get("etag"),
                last_modified=state.get("last_modified"),
            )
            stats["endpoints_checked"] += 1
            if result.not_modified:
                stats["not_modified"] += 1
                if cached is None:
                    raise DataSourceError(
                        "Official skill-tree export returned 304 but no local "
                        "raw snapshot exists"
                    )
                payload = self._decode_cached_json(cached["raw"], endpoint)
                snapshot_id = int(cached["id"])
                detail = (
                    "Official passive map not modified; reused the local raw "
                    "snapshot."
                )
            else:
                snapshot_id, created = self._store_snapshot(
                    result,
                    source=GGGSkillTreeClient.SOURCE,
                    league_id=None,
                    category=SKILL_TREE_CATEGORY,
                    metadata={
                        "purpose": (
                            "Forbidden Jewel passive-to-ascendancy mapping"
                        )
                    },
                )
                stats["snapshots_written"] += int(created)
                payload = result.payload
                detail = "Stored the official current passive-tree export."
            passive_map = parse_passive_map(payload)
            if not passive_map.passives:
                raise DataSourceError(
                    "Official skill-tree export contained no ascendancy "
                    "notable mapping"
                )
            self.storage.update_source_state(
                source=GGGSkillTreeClient.SOURCE,
                endpoint=endpoint,
                category=SKILL_TREE_CATEGORY,
                status="ok",
                detail=(
                    f"{detail} Parsed {len(passive_map.passives)} unique "
                    "ascendancy notables."
                ),
                etag=result.etag,
                last_modified=result.last_modified,
                success=True,
            )
            return passive_map, snapshot_id, endpoint
        except Exception as error:
            # A previously stored official export is a valid identity cache.
            # Reusing it does not claim that a failed request was fresh.
            if cached is not None:
                try:
                    payload = self._decode_cached_json(cached["raw"], endpoint)
                    passive_map = parse_passive_map(payload)
                    if passive_map.passives:
                        warnings.append(
                            "Official passive-tree refresh failed; reused the "
                            f"local raw mapping snapshot: {error}"
                        )
                        self.storage.update_source_state(
                            source=GGGSkillTreeClient.SOURCE,
                            endpoint=endpoint,
                            category=SKILL_TREE_CATEGORY,
                            status="stale",
                            detail=str(error),
                        )
                        return passive_map, int(cached["id"]), endpoint
                except Exception:
                    pass
            warnings.append(
                "Forbidden Jewel class mapping unavailable; exact prices were "
                f"kept without a meta multiplier: {error}"
            )
            self.storage.update_source_state(
                source=GGGSkillTreeClient.SOURCE,
                endpoint=endpoint,
                category=SKILL_TREE_CATEGORY,
                status="unavailable",
                detail=str(error),
            )
            return (
                PassiveMap({}, frozenset(), 0),
                None,
                endpoint,
            )

    def _sync_forbidden_jewels(
        self,
        league: League,
        warnings: list[str],
        stats: dict[str, Any],
    ) -> bool:
        """Store exact Flesh/Flame observations from poe.watch's catalog."""

        endpoint = self.poe_watch.compact_url(league.name, all_items=True)
        state = self.storage.get_source_state(
            PoeWatchClient.SOURCE,
            endpoint,
            league.id,
            FORBIDDEN_CATALOG_CATEGORY,
        ) or {}
        cached = self.storage.latest_snapshot(
            source=PoeWatchClient.SOURCE,
            endpoint=endpoint,
            league_id=league.id,
            category=FORBIDDEN_CATALOG_CATEGORY,
        )
        try:
            result = self.poe_watch.fetch_compact(
                league.name,
                all_items=True,
                etag=state.get("etag"),
                last_modified=state.get("last_modified"),
            )
            stats["endpoints_checked"] += 1
            if result.not_modified:
                stats["not_modified"] += 1
                if cached is None:
                    raise DataSourceError(
                        "poe.watch catalog returned 304 but no local raw "
                        "snapshot exists"
                    )
                payload = self._decode_cached_json(cached["raw"], endpoint)
                snapshot_id = int(cached["id"])
                detail_prefix = (
                    "Catalog revalidated unchanged; reused its raw snapshot."
                )
            else:
                snapshot_id, created = self._store_snapshot(
                    result,
                    source=PoeWatchClient.SOURCE,
                    league_id=league.id,
                    category=FORBIDDEN_CATALOG_CATEGORY,
                    metadata={
                        "all": True,
                        "purpose": "exact Forbidden Jewel current prices",
                        "provider_league": league.name,
                    },
                )
                stats["snapshots_written"] += int(created)
                payload = result.payload
                detail_prefix = "Stored the current full compact catalog."

            assets = _compact_assets(payload)
            exact_assets = [
                asset
                for asset in assets
                if str(asset.get("category") or "").casefold()
                == "forbiddenjewel"
            ]
            if not exact_assets:
                raise DataSourceError(
                    "poe.watch compact catalog contained no exact Forbidden "
                    "Flesh or Forbidden Flame variants"
                )
            passive_map, mapping_snapshot_id, mapping_endpoint = (
                self._load_passive_map(warnings, stats)
            )
            coverage = enrich_forbidden_assets(
                exact_assets,
                passive_map,
                mapping_snapshot_id=mapping_snapshot_id,
                mapping_endpoint=mapping_endpoint,
            )
            points = forbidden_price_points(
                assets,
                league_id=league.id,
                observed_at=result.fetched_at,
                snapshot_id=snapshot_id,
            )
            if not points:
                raise DataSourceError(
                    "poe.watch exact Forbidden Jewel rows had no positive "
                    "current prices"
                )
            # Keep the exact catalog identity alongside the normalized current
            # observations so the historical crawler and recommendation model
            # share one stable item key.
            self.storage.upsert_historical_assets(exact_assets)
            written = self.storage.insert_price_points(points)
            stats["rows_written"] += written
            stats["forbidden_variants"] = len(points)
            stats["forbidden_variants_mapped"] = coverage["mapped"]
            if coverage["mapped"] < coverage["total"]:
                warnings.append(
                    "Stored exact Forbidden Jewel prices, but "
                    f"{coverage['total'] - coverage['mapped']} of "
                    f"{coverage['total']} passives were not uniquely present "
                    "in the current official tree and will not receive a meta "
                    "multiplier."
                )
            self.storage.update_source_state(
                source=PoeWatchClient.SOURCE,
                endpoint=endpoint,
                league_id=league.id,
                category=FORBIDDEN_CATALOG_CATEGORY,
                status="ok",
                detail=(
                    f"{detail_prefix} Stored {len(points)} exact current "
                    "Forbidden Jewel variants; "
                    f"{coverage['mapped']} have official ascendancy metadata."
                ),
                etag=result.etag,
                last_modified=result.last_modified,
                success=True,
            )
            return True
        except Exception as error:
            stats["failed_endpoints"] += 1
            warnings.append(f"Exact Forbidden Jewel sync failed: {error}")
            self.storage.update_source_state(
                source=PoeWatchClient.SOURCE,
                endpoint=endpoint,
                league_id=league.id,
                category=FORBIDDEN_CATALOG_CATEGORY,
                status="unavailable",
                detail=str(error),
            )
            return False

    def _sync_official_exchange(
        self,
        league: League,
        backfill_hours: int,
        warnings: list[str],
        stats: dict[str, Any],
    ) -> None:
        now_hour = int(datetime.now(timezone.utc).timestamp() // 3600 * 3600)
        saved_cursor = self.storage.get_setting(
            f"ggg_currency_cursor:{league.id}"
        )
        cursor = (
            int(saved_cursor)
            if isinstance(saved_cursor, (int, float))
            else now_hour - backfill_hours * 3600
        )
        endpoint_root = self.ggg.currency_exchange_url()
        for _ in range(backfill_hours):
            if cursor >= now_hour:
                self.storage.update_source_state(
                    source=GGGClient.SOURCE,
                    endpoint=endpoint_root,
                    league_id=league.id,
                    category="hourly-exchange",
                    status="ok",
                    detail="Official exchange history is current through the latest completed hour.",
                    success=True,
                )
                break
            try:
                result = self.ggg.fetch_currency_exchange(cursor)
                stats["endpoints_checked"] += 1
                snapshot_id, created = self._store_snapshot(
                    result,
                    source=GGGClient.SOURCE,
                    league_id=league.id,
                    category="hourly-exchange",
                    metadata={"cursor": cursor},
                )
                stats["snapshots_written"] += int(created)
                observed = datetime.fromtimestamp(
                    cursor, tz=timezone.utc
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
                points = normalize_ggg_markets(
                    result.payload,
                    league_id=league.id,
                    league_name=league.name,
                    observed_at=observed,
                    snapshot_id=snapshot_id,
                )
                stats["rows_written"] += self.storage.insert_price_points(points)
                stats["official_hours"] += 1
                next_cursor = (
                    result.payload.get("next_change_id")
                    if isinstance(result.payload, dict)
                    else None
                )
                self.storage.update_source_state(
                    source=GGGClient.SOURCE,
                    endpoint=endpoint_root,
                    league_id=league.id,
                    category="hourly-exchange",
                    status="ok",
                    detail=(
                        f"Stored hourly digest at {observed}; "
                        f"{len(points)} league markets normalized."
                    ),
                    success=True,
                )
                if not isinstance(next_cursor, (int, float)):
                    warnings.append(
                        "Official exchange response omitted next_change_id; "
                        "backfill stopped safely."
                    )
                    break
                next_cursor = int(next_cursor)
                self.storage.set_setting(
                    f"ggg_currency_cursor:{league.id}", next_cursor
                )
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
            except Exception as error:
                stats["failed_endpoints"] += 1
                warnings.append(f"Official hourly exchange backfill stopped: {error}")
                self.storage.update_source_state(
                    source=GGGClient.SOURCE,
                    endpoint=endpoint_root,
                    league_id=league.id,
                    category="hourly-exchange",
                    status="unavailable",
                    detail=str(error),
                )
                break

    def _store_snapshot(
        self,
        result: FetchResult,
        *,
        source: str,
        league_id: str | None,
        category: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        raw = result.raw
        if not raw:
            raw = json.dumps(result.payload, separators=(",", ":")).encode("utf-8")
        return self.storage.add_snapshot(
            source=source,
            endpoint=result.url,
            league_id=league_id,
            category=category,
            fetched_at=result.fetched_at,
            status_code=result.status,
            raw=raw,
            etag=result.etag,
            last_modified=result.last_modified,
            metadata=metadata,
        )

    def _finish_without_live_data(
        self,
        run_id: int,
        warnings: list[str],
        stats: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.storage.get_current_league()
        if self.allow_demo_seed and (
            current is None
            or current.is_demo
            or self.storage.status_counts(current.id)["price_points"] == 0
        ):
            demo_stats = seed_demo(self.storage, make_current=True)
            stats["rows_written"] += demo_stats["rows_written"]
            stats["snapshots_written"] += demo_stats["snapshots_written"]
            stats["demo_seeded"] = True
            message = (
                "Live sources were unavailable. Loaded the clearly labelled "
                "offline demo fixture; no demo value is presented as live."
            )
            self.storage.set_sync_run_league(run_id, "demo-softcore-fixture")
            self.storage.finish_sync_run(
                run_id,
                status="demo",
                rows_written=stats["rows_written"],
                snapshots_written=stats["snapshots_written"],
                message=message,
                warnings=warnings,
            )
            return {
                "ok": True,
                "message": message,
                "stats": stats,
                "warnings": warnings,
            }
        message = (
            "Live sources were unavailable; previously stored data was left intact."
        )
        self.storage.finish_sync_run(
            run_id,
            status="failed",
            rows_written=stats["rows_written"],
            snapshots_written=stats["snapshots_written"],
            message=message,
            warnings=warnings,
        )
        return {
            "ok": False,
            "message": message,
            "stats": stats,
            "warnings": warnings,
        }
