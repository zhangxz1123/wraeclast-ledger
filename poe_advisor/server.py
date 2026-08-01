from __future__ import annotations

import json
import math
import mimetypes
import threading
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .historical import (
    BROADLY_COVERED_LEAGUE_IDS,
    BROADLY_COVERED_LEAGUES,
    COMPLETED_LEAGUES,
)
from .meta import (
    LADDER_SOURCE,
    POE_NINJA_META_CAVEAT,
    POE_NINJA_META_SOURCE,
    MetaService,
)
from .ninja_history import PoeNinjaHistoryService
from .recommendation import (
    CURRENT_CURVE_FORECAST_WEIGHT,
    FORECAST_HORIZONS,
    HISTORICAL_FORECAST_WEIGHT,
    HISTORICAL_MODEL_CONFIDENCE_FLOOR,
    RecommendationEngine,
    _current_curve_projection,
)
from .provenance import (
    CURRENT_PRICE_SOURCES,
    HISTORICAL_PRICE_SOURCES,
    production_price_provenance,
)
from .seasonality import SeasonalModel
from .storage import Storage
from .sync import (
    CURRENT_HISTORY_MAX_ITEMS,
    STANDARD_ANCHOR_SOURCE,
    SyncAlreadyRunning,
    SyncService,
)


MAX_REQUEST_BYTES = 64 * 1024
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/styles.css": "styles.css",
    "/app.js": "app.js",
    "/og.png": "og.png",
}


@dataclass(slots=True)
class AdvisorApplication:
    storage: Storage
    sync_service: SyncService
    recommendation_engine: RecommendationEngine
    web_dir: Path
    history_service: PoeNinjaHistoryService | None = None
    meta_service: MetaService | None = None
    _recommendation_cache: dict[
        tuple[str, int | None, int], dict[str, Any]
    ] = field(default_factory=dict, init=False, repr=False)
    _recommendation_cache_lock: Any = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    @classmethod
    def create(
        cls,
        *,
        database_path: str | Path,
        web_dir: str | Path,
        allow_demo_seed: bool = True,
    ) -> "AdvisorApplication":
        storage = Storage(database_path)
        meta_service = MetaService(storage)
        return cls(
            storage=storage,
            sync_service=SyncService(
                storage,
                allow_demo_seed=allow_demo_seed,
            ),
            recommendation_engine=RecommendationEngine(
                storage,
                meta_service=meta_service,
            ),
            web_dir=Path(web_dir).expanduser().resolve(),
            history_service=PoeNinjaHistoryService(storage),
            meta_service=meta_service,
        )

    def status(self) -> dict[str, Any]:
        league = self.storage.get_current_league()
        counts = self.storage.status_counts(league.id if league else None)
        meta_profile = (
            self.meta_service.latest_profile(league.id)
            if league
            and self.meta_service is not None
            else None
        )
        meta_source = (
            str(meta_profile.get("source"))
            if meta_profile
            else POE_NINJA_META_SOURCE
        )
        sources = []
        friendly_names = {
            "poe.ninja": "poe.ninja economy",
            STANDARD_ANCHOR_SOURCE: "poe.ninja Standard anchor",
            "poe.ninja-history": "poe.ninja completed-league archive",
            "poe.ninja-current-history": "poe.ninja current-league curves",
            "poe.watch-history": "Legacy poe.watch history (quarantined)",
            POE_NINJA_META_SOURCE: "poe.ninja build composition",
            "ggg-currency-exchange": "Official Currency Exchange",
            "poe.watch": "poe.watch market catalog",
            "ggg-skilltree-export": "Official passive-tree export",
            LADDER_SOURCE: "Official top-ladder meta sample",
        }
        for source in self.storage.list_source_summaries():
            is_meta = source["source"] in {
                POE_NINJA_META_SOURCE,
                LADDER_SOURCE,
            }
            sources.append(
                {
                    "name": friendly_names.get(source["source"], source["source"]),
                    "source": source["source"],
                    "status": source["status"],
                    "detail": (
                        (
                            f"Current-league class sample: "
                            f"{int(meta_profile['sample_size'])} characters "
                            f"on league day "
                            f"{int(meta_profile['league_day'])}."
                        )
                        if (
                            is_meta
                            and meta_profile
                            and source["source"] == meta_source
                        )
                        else source.get("detail")
                    ),
                    "last_checked_at": source.get("last_checked_at"),
                    "last_success_at": source.get("last_success_at"),
                    "required": source["source"]
                    not in {
                        "poe.watch",
                        "poe.watch-history",
                        "ggg-skilltree-export",
                        "poe.ninja-current-history",
                        STANDARD_ANCHOR_SOURCE,
                        POE_NINJA_META_SOURCE,
                        LADDER_SOURCE,
                    },
                }
            )
        meta_profiles = self.storage.list_meta_class_snapshots(
            source=meta_source,
            latest_only=True,
        )
        return {
            "league": (
                {
                    "id": league.id,
                    "name": league.name,
                    "start_at": league.start_at,
                    "end_at": league.end_at,
                    "day": league.day,
                }
                if league
                else None
            ),
            "last_sync_at": self.storage.last_sync_at(league.id if league else None),
            "database": counts,
            "sources": sources,
            "demo_mode": bool(league and league.is_demo),
            "syncing": self.sync_service.is_syncing,
            "history_syncing": bool(
                self.history_service and self.history_service.is_syncing
            ),
            "history_progress": (
                self.history_service.progress()
                if self.history_service
                else {"status": "unavailable"}
            ),
            "meta": {
                "available": meta_profile is not None,
                "sample_size": (
                    int(meta_profile["sample_size"])
                    if meta_profile
                    else 0
                ),
                "league_day": (
                    int(meta_profile["league_day"])
                    if meta_profile
                    else None
                ),
                "observed_at": (
                    meta_profile["observed_at"]
                    if meta_profile
                    else None
                ),
                "profiles": len(meta_profiles),
                "source": meta_source,
                "caveat": (
                    POE_NINJA_META_CAVEAT
                    if meta_source == POE_NINJA_META_SOURCE
                    else "Top-experience-ladder sample, not the full player "
                    "population."
                ),
            },
        }

    def sync_meta(self, league: Any) -> dict[str, Any]:
        if self.meta_service is None or getattr(league, "is_demo", False):
            return {
                "status": "skipped",
                "message": "Meta sampling is unavailable for this dataset.",
            }
        current = self.meta_service.sync_leagues(
            [league],
            pages=10,
            freshness_hours=12,
        )
        current_profile = self.meta_service.latest_profile(league.id)
        current_source = (
            str(current_profile.get("source"))
            if current_profile
            else POE_NINJA_META_SOURCE
        )
        target_league_day = (
            max(1, int(current_profile.get("league_day") or 1))
            if current_profile
            else max(1, int(getattr(league, "day", 1) or 1))
        )
        missing_historical = []
        for spec in COMPLETED_LEAGUES:
            nearest = self.storage.nearest_meta_class_snapshot(
                spec.league_id,
                target_league_day,
                source=current_source,
            )
            if nearest is None:
                missing_historical.append(spec)
                continue
            if (
                current_source == POE_NINJA_META_SOURCE
                and abs(
                    int(nearest.get("league_day") or 1)
                    - target_league_day
                )
                > 3
            ):
                missing_historical.append(spec)
        historical = (
            self.meta_service.sync_leagues(
                missing_historical,
                pages=10,
                force=current_source == POE_NINJA_META_SOURCE,
                freshness_hours=24 * 365 * 100,
                target_league_day=target_league_day,
            )
            if missing_historical
            else {
                "requested_leagues": 0,
                "synced_leagues": 0,
                "cached_leagues": 0,
                "failed_leagues": 0,
                "snapshots_written": 0,
                "leagues": [],
            }
        )
        failed = int(current.get("failed_leagues") or 0) + int(
            historical.get("failed_leagues") or 0
        )
        return {
            "status": "partial" if failed else "ok",
            "failed_leagues": failed,
            "current": current,
            "historical": historical,
        }

    def recommendations(self, *, budget: float, horizon: int) -> dict[str, Any]:
        league = self.storage.get_current_league()
        if league is None:
            return {
                "mode": "forecast_ranking",
                "allocation_mode": "none",
                "generated_at": None,
                "league": None,
                "budget": round(budget, 2),
                "budget_affects_ranking": False,
                "horizon": horizon,
                "reserve": None,
                "invested": None,
                "confidence_note": (
                    "No local snapshot exists yet. Sync the market to build the "
                    "first Divine-relative price archive."
                ),
                "recommendations": [],
                "watchlist": [],
            }
        cache_key = (league.id, league.day, int(horizon))
        # Collapse simultaneous requests from multiple open tabs into one
        # calculation. The result is stable until a sync/backfill explicitly
        # replaces the cache, while the legacy budget field remains
        # request-specific and cannot affect rank.
        with self._recommendation_cache_lock:
            payload = self._recommendation_cache.get(cache_key)
            if payload is None:
                payload = self.recommendation_engine.generate(
                    league,
                    budget=budget,
                    horizon=horizon,
                    persist=False,
                )
                self._recommendation_cache[cache_key] = payload
            result = dict(payload)
        result["budget"] = round(budget, 2)
        return result

    def remember_recommendations(self, payload: dict[str, Any]) -> None:
        """Replace stale cached horizons after a data-changing operation."""

        league = payload.get("league")
        if not isinstance(league, dict):
            return
        league_id = str(league.get("id") or "").strip()
        if not league_id:
            return
        league_day = league.get("day")
        horizon = int(payload.get("horizon") or 7)
        cache_key = (league_id, league_day, horizon)
        with self._recommendation_cache_lock:
            for key in [
                key
                for key in self._recommendation_cache
                if key[0] == league_id
            ]:
                self._recommendation_cache.pop(key, None)
            self._recommendation_cache[cache_key] = payload

    def history(self, item_key: str) -> dict[str, Any]:
        league = self.storage.get_current_league()
        if league is None:
            return {
                "league": None,
                "item": None,
                "history": [],
                "seasonal_comparison": None,
            }
        return {
            "league": {"id": league.id, "name": league.name},
            "item": self.storage.item_metadata(
                league.id,
                item_key,
                sources=None if league.is_demo else CURRENT_PRICE_SOURCES,
            ),
            "history": self.storage.all_time_item_history(
                league.id,
                item_key,
                sources=None if league.is_demo else CURRENT_PRICE_SOURCES,
            ),
            "seasonal_comparison": self._seasonal_comparison(
                league=league,
                item_key=item_key,
            ),
        }

    def _seasonal_comparison(
        self,
        *,
        league: Any,
        item_key: str,
    ) -> dict[str, Any]:
        # The chart is an audit view of poe.ninja's exact daily archive, so it
        # includes positive Low observations. Forecast targets are calculated
        # from a separate model-grade curve; a display-only point must never
        # become a target merely because it can be plotted.
        confidence_floor = HISTORICAL_MODEL_CONFIDENCE_FLOOR
        display_grade_floor = 0.5
        decay = SeasonalModel.RECENCY_DECAY_PER_LEAGUE
        current_rows = self.storage.daily_item_history(
            league.id,
            item_key,
            league.start_at,
            minimum_confidence=0.0,
            sources=None if league.is_demo else CURRENT_PRICE_SOURCES,
        )
        calendar = list(reversed(BROADLY_COVERED_LEAGUES))
        display_historical_rows = self.storage.seasonal_price_curve_rows(
            item_key,
            [spec.league_id for spec in calendar],
            minimum_confidence=0.0,
            sources=None if league.is_demo else HISTORICAL_PRICE_SOURCES,
        )
        forecast_historical_rows = self.storage.seasonal_price_curve_rows(
            item_key,
            [spec.league_id for spec in calendar],
            minimum_confidence=confidence_floor,
            sources=None if league.is_demo else HISTORICAL_PRICE_SOURCES,
        )
        display_rows_by_league: dict[str, list[dict[str, Any]]] = {}
        for row in display_historical_rows:
            display_rows_by_league.setdefault(
                str(row["league_id"]), []
            ).append(row)
        forecast_rows_by_league: dict[str, list[dict[str, Any]]] = {}
        for row in forecast_historical_rows:
            forecast_rows_by_league.setdefault(
                str(row["league_id"]), []
            ).append(row)

        past_leagues: list[dict[str, Any]] = []
        weighted_by_day: dict[int, list[tuple[float, float]]] = {}
        league_calendar: list[dict[str, Any]] = []
        for age_rank, spec in enumerate(calendar):
            raw_weight = decay**age_rank
            league_calendar.append(
                {
                    "league_id": spec.league_id,
                    "league_name": spec.name,
                    "start_at": spec.start_at,
                    "age_rank": age_rank,
                    "raw_weight": raw_weight,
                }
            )
            rows = display_rows_by_league.get(spec.league_id, [])
            if not rows:
                continue
            points = []
            for row in rows:
                league_day = int(row["league_day"])
                divine_value = float(row["divine_value"])
                weighted_by_day.setdefault(league_day, []).append(
                    (divine_value, raw_weight)
                )
                points.append(
                    {
                        "league_day": league_day,
                        "divine_value": divine_value,
                        "observed_at": row["observed_at"],
                        "confidence": float(row["confidence"]),
                        "source": row["source"],
                        "model_grade": (
                            float(row["confidence"]) >= confidence_floor
                        ),
                    }
                )
            past_leagues.append(
                {
                    "league_id": spec.league_id,
                    "league_name": spec.name,
                    "start_at": spec.start_at,
                    "age_rank": age_rank,
                    "raw_weight": raw_weight,
                    "points": points,
                }
            )

        weighted_points = []
        for league_day, values_and_weights in sorted(weighted_by_day.items()):
            weight_total = sum(weight for _, weight in values_and_weights)
            if weight_total <= 0:
                continue
            weighted_points.append(
                {
                    "league_day": league_day,
                    "divine_value": sum(
                        value * weight
                        for value, weight in values_and_weights
                    )
                    / weight_total,
                    "contributing_leagues": len(values_and_weights),
                }
            )

        forecast_weighted_by_day: dict[
            int, list[tuple[float, float]]
        ] = {}
        for age_rank, spec in enumerate(calendar):
            raw_weight = decay**age_rank
            for row in forecast_rows_by_league.get(spec.league_id, []):
                forecast_weighted_by_day.setdefault(
                    int(row["league_day"]), []
                ).append((float(row["divine_value"]), raw_weight))
        forecast_weighted_points = []
        for league_day, values_and_weights in sorted(
            forecast_weighted_by_day.items()
        ):
            weight_total = sum(weight for _, weight in values_and_weights)
            if weight_total <= 0:
                continue
            forecast_weighted_points.append(
                {
                    "league_day": league_day,
                    "divine_value": sum(
                        value * weight
                        for value, weight in values_and_weights
                    )
                    / weight_total,
                    "contributing_leagues": len(values_and_weights),
                }
            )

        forecast_contributors_by_day = {
            int(point["league_day"]): int(
                point["contributing_leagues"]
            )
            for point in forecast_weighted_points
        }
        for point in weighted_points:
            point["forecast_grade_contributing_leagues"] = (
                forecast_contributors_by_day.get(
                    int(point["league_day"]),
                    0,
                )
            )

        current_points = [
            {
                "league_day": int(row["league_day"]),
                "divine_value": float(row["divine_value"]),
                "observed_at": row["observed_at"],
                "confidence": float(row["confidence"]),
                "source": row["source"],
                "model_grade": (
                    float(row["confidence"]) >= display_grade_floor
                ),
            }
            for row in current_rows
        ]
        current_day = max(1, int(league.day or 1))
        forecast_weighted_price_by_day = {
            int(point["league_day"]): float(point["divine_value"])
            for point in forecast_weighted_points
        }
        current_price = (
            float(current_points[-1]["divine_value"])
            if current_points
            else None
        )
        forecast_horizons: dict[str, dict[str, Any]] = {}
        for horizon in FORECAST_HORIZONS:
            historical_target = forecast_weighted_price_by_day.get(
                current_day + horizon
            )
            historical_gain = (
                historical_target / current_price - 1.0
                if historical_target is not None
                and current_price is not None
                and current_price > 0
                else None
            )
            projection = _current_curve_projection(
                current_points,
                horizon,
            )
            projection_gain = projection["capped_gain"]
            use_projection = (
                historical_gain is not None
                and projection_gain is not None
                and int(projection["point_count"]) >= 2
            )
            if historical_gain is None:
                expected_gain = None
            elif use_projection:
                expected_gain = math.exp(
                    HISTORICAL_FORECAST_WEIGHT
                    * math.log1p(historical_gain)
                    + CURRENT_CURVE_FORECAST_WEIGHT
                    * math.log1p(float(projection_gain))
                ) - 1.0
            else:
                expected_gain = historical_gain
            contributing = next(
                (
                    int(point["contributing_leagues"])
                    for point in forecast_weighted_points
                    if int(point["league_day"])
                    == current_day + horizon
                ),
                0,
            )
            forecast_horizons[str(horizon)] = {
                "days": horizon,
                "historical_target_price_divine": historical_target,
                "historical_target_divine": historical_target,
                "historical_target_gain": historical_gain,
                "historical_target_gain_pct": (
                    historical_gain * 100.0
                    if historical_gain is not None
                    else None
                ),
                "historical_sample_leagues": contributing,
                "sample_leagues": [
                    league["league_id"]
                    for league in league_calendar
                    if any(
                        int(point["league_day"]) == current_day + horizon
                        for point in forecast_rows_by_league.get(
                            str(league["league_id"]),
                            [],
                        )
                    )
                ],
                "sample_league_names": [
                    league["league_name"]
                    for league in league_calendar
                    if any(
                        int(point["league_day"]) == current_day + horizon
                        for point in forecast_rows_by_league.get(
                            str(league["league_id"]),
                            [],
                        )
                    )
                ],
                "current_curve_projection": projection,
                "current_curve_gain_pct": (
                    projection_gain * 100.0
                    if projection_gain is not None
                    else None
                ),
                "current_curve_used": use_projection,
                "historical_weight": (
                    HISTORICAL_FORECAST_WEIGHT
                    if use_projection
                    else (1.0 if historical_gain is not None else None)
                ),
                "current_curve_weight": (
                    CURRENT_CURVE_FORECAST_WEIGHT
                    if use_projection
                    else (0.0 if historical_gain is not None else None)
                ),
                "expected_gain": expected_gain,
                "expected_gain_pct": (
                    expected_gain * 100.0
                    if expected_gain is not None
                    else None
                ),
                "expected_price_divine": (
                    current_price * (1.0 + expected_gain)
                    if current_price is not None
                    and expected_gain is not None
                    else None
                ),
            }
        observed_days = sorted(
            {
                int(point["league_day"])
                for point in current_points
                if 1 <= int(point["league_day"]) <= current_day
            }
        )
        archive = self.storage.current_item_history_archive(
            league.id,
            item_key,
        )
        provider_days = []
        normalized_days = []
        provider_missing_days = []
        missing_divine_days = []
        if archive:
            provider_days = sorted(
                {
                    int(day)
                    for day in archive.get("provider_observed_days", [])
                    if isinstance(day, (int, float))
                    and 1 <= int(day) <= current_day
                }
            )
            normalized_days = sorted(
                {
                    int(day)
                    for day in archive.get("normalized_days", [])
                    if isinstance(day, (int, float))
                    and 1 <= int(day) <= current_day
                }
            )
            provider_missing_days = sorted(
                {
                    int(day)
                    for day in archive.get("provider_missing_days", [])
                    if isinstance(day, (int, float))
                    and 1 <= int(day) <= current_day
                }
            )
            missing_divine_days = sorted(
                {
                    int(day)
                    for day in archive.get(
                        "missing_divine_anchor_days",
                        [],
                    )
                    if isinstance(day, (int, float))
                    and 1 <= int(day) <= current_day
                }
            )
        missing_days = [
            day
            for day in range(1, current_day + 1)
            if day not in observed_days
        ]
        if archive and provider_days:
            source_limitation = (
                (
                    "The dated provider's first exact item observation is "
                    f"league day {provider_days[0]}; earlier days remain blank."
                )
                if provider_days[0] > 1
                else (
                    "Only exact provider observations are plotted; any later "
                    "missing days remain blank."
                )
            )
        elif archive:
            source_limitation = (
                "The dated provider returned no usable exact item "
                "observations; the gap remains blank."
            )
        else:
            source_limitation = (
                "No dated upstream item history has been archived for this "
                "item yet; only local observations are plotted."
            )

        return {
            "price_provenance": (
                None if league.is_demo else production_price_provenance()
            ),
            "current_league": {
                "league_id": league.id,
                "league_name": league.name,
                "start_at": league.start_at,
                "points": current_points,
                "coverage": {
                    "through_league_day": current_day,
                    "first_observed_day": (
                        observed_days[0] if observed_days else None
                    ),
                    "last_observed_day": (
                        observed_days[-1] if observed_days else None
                    ),
                    "observed_days": observed_days,
                    "missing_days": missing_days,
                    "sources": sorted(
                        {str(point["source"]) for point in current_points}
                    ),
                    "dated_archive_attempted": archive is not None,
                    "dated_archive_source": (
                        archive.get("provider", "poe.watch")
                        if archive
                        else None
                    ),
                    "dated_archive_fetched_at": (
                        archive.get("fetched_at") if archive else None
                    ),
                    "normalization_version": (
                        archive.get("normalization_version")
                        if archive
                        else None
                    ),
                    "provider_observed_days": provider_days,
                    "provider_first_observed_day": (
                        provider_days[0] if provider_days else None
                    ),
                    "provider_missing_days": provider_missing_days,
                    "normalized_days": normalized_days,
                    "missing_divine_anchor_days": missing_divine_days,
                    "source_limitation": source_limitation,
                    "interpolation": "none",
                },
            },
            "weighted_historical": {
                "points": weighted_points,
                "series_role": "display_only",
                "confidence_floor": 0.0,
                "forecast_targets_use_this_series": False,
            },
            "past_leagues": past_leagues,
            "forecast_horizons": forecast_horizons,
            "calculation": {
                "currency": "Divine Orb",
                "current_confidence_floor": 0.0,
                "historical_confidence_floor": confidence_floor,
                "confidence_floor": confidence_floor,
                "display_grade_floor": display_grade_floor,
                "historical_display_confidence_floor": 0.0,
                "historical_forecast_confidence_floor": confidence_floor,
                "weighted_historical_series_role": "display_only",
                "forecast_target_series": (
                    "separate_model_grade_weighted_historical"
                ),
                "recency_decay_per_league": decay,
                "age_rank_basis": (
                    "Four broadly covered completed leagues only; Mirage has "
                    "age rank 0, followed by Keepers, Mercenaries, and Settlers."
                ),
                "historical_aggregation": (
                    "Per-day recency-weighted arithmetic mean, normalized over "
                    "completed leagues with any positive exact poe.ninja "
                    "observation. This is the display curve and includes Low "
                    "observations."
                ),
                "forecast_historical_aggregation": (
                    "Forecast targets use a separate per-day recency-weighted "
                    "mean containing only exact poe.ninja observations at or "
                    "above the historical forecast confidence floor."
                ),
                "current_point_selection": (
                    "Newest positive-price observation within each league day; "
                    "confidence never removes a point. Values below the "
                    "separate display-grade threshold are marked "
                    "model_grade=false for audit."
                ),
                "interpolation": "none",
                "league_calendar": league_calendar,
                "included_league_ids": list(
                    BROADLY_COVERED_LEAGUE_IDS
                ),
            },
        }


class AdvisorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        application: AdvisorApplication,
    ):
        self.application = application
        super().__init__(server_address, AdvisorRequestHandler)


class AdvisorRequestHandler(BaseHTTPRequestHandler):
    server: AdvisorHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in STATIC_FILES:
            self._serve_static(STATIC_FILES[parsed.path])
            return
        if parsed.path == "/api/health":
            healthy = self.server.application.storage.healthcheck()
            self._send_json(
                {"ok": healthy, "service": "wraeclast-ledger"},
                HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path == "/api/status":
            self._send_json(self.server.application.status())
            return
        if parsed.path == "/api/recommendations":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                budget = self._bounded_float(query.get("budget", ["100"])[0], 1, 100000)
                horizon = self._bounded_int(query.get("horizon", ["7"])[0], 1, 30)
                payload = self.server.application.recommendations(
                    budget=budget,
                    horizon=horizon,
                )
                response_payload = dict(payload)
                # Ranking payloads expose the same full list under both
                # `rankings` and the legacy `recommendations` alias.
                # Omitting the duplicate alias on the wire halves the local
                # response while preserving the engine's Python API.
                if isinstance(response_payload.get("rankings"), list):
                    response_payload.pop("recommendations", None)
                self._send_json(response_payload)
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            return
        if parsed.path == "/api/history":
            query = urllib.parse.parse_qs(parsed.query)
            item_key = str(query.get("key", [""])[0]).strip()
            if not item_key:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "A non-empty item history key is required.",
                )
                return
            self._send_json(self.server.application.history(item_key))
            return
        if parsed.path == "/api/settings":
            self._send_json(self.server.application.storage.public_settings())
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Route not found.")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/sync":
            try:
                body = self._read_json_body()
                backfill_hours = self._bounded_int(
                    body.get("backfill_hours", 0),
                    0,
                    336,
                )
                budget = self._bounded_float(body.get("budget", 100), 1, 100000)
                horizon = self._bounded_int(body.get("horizon", 7), 1, 30)
                result = self.server.application.sync_service.sync(
                    backfill_hours=backfill_hours
                )
                if result.get("ok"):
                    application = self.server.application
                    league = application.storage.get_current_league()
                    if league is not None:
                        meta_summary = application.sync_meta(league)
                        result["meta_sync"] = meta_summary
                        if meta_summary.get("failed_leagues"):
                            warnings = result.setdefault("warnings", [])
                            if isinstance(warnings, list):
                                warnings.append(
                                    "Market prices synced, but one or more "
                                    "optional class-composition samples failed. "
                                    "Existing local profiles remain usable."
                                )
                        preliminary = (
                            application.recommendation_engine.generate(
                                league,
                                budget=budget,
                                horizon=horizon,
                                persist=False,
                            )
                        )
                        rankings = preliminary.get("rankings")
                        if not isinstance(rankings, list):
                            rankings = preliminary.get("recommendations", [])
                        ranked_keys = [
                            str(
                                item.get("curve_key")
                                or item.get("key")
                                or ""
                            ).strip()
                            for item in rankings
                            if isinstance(item, dict)
                            and (
                                item.get("curve_key")
                                or item.get("key")
                            )
                        ]
                        current_history_sync = getattr(
                            application.sync_service,
                            "sync_current_item_histories",
                            None,
                        )
                        if callable(current_history_sync):
                            try:
                                current_history = current_history_sync(
                                    league,
                                    ranked_keys,
                                    max_items=CURRENT_HISTORY_MAX_ITEMS,
                                )
                            except Exception as error:
                                current_history = {
                                    "status": "failed",
                                    "message": (
                                        "Current ranked-item history stopped "
                                        f"safely: {error}"
                                    ),
                                    "warnings": [str(error)],
                                }
                            result["current_history_sync"] = current_history
                            if current_history.get("status") in {
                                "failed",
                                "partial",
                            }:
                                warnings = result.setdefault("warnings", [])
                                history_warnings = current_history.get(
                                    "warnings",
                                    [],
                                )
                                first_warning = (
                                    history_warnings[0]
                                    if isinstance(history_warnings, list)
                                    and history_warnings
                                    else current_history.get("message")
                                )
                                if (
                                    isinstance(warnings, list)
                                    and first_warning
                                ):
                                    warnings.append(
                                        "Some ranked current curves remain "
                                        f"incomplete: {first_warning}"
                                    )
                        recommendation = (
                            application.recommendation_engine.generate(
                                league,
                                budget=budget,
                                horizon=horizon,
                                persist=True,
                            )
                        )
                        application.remember_recommendations(recommendation)
                        result["recommendation_summary"] = {
                            "generated_at": recommendation["generated_at"],
                            "ideas": len(recommendation["recommendations"]),
                            "rankings": len(
                                recommendation.get("rankings", [])
                            ),
                            "invested": recommendation["invested"],
                            "reserve": recommendation["reserve"],
                        }
                status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY
                self._send_json(result, status)
            except SyncAlreadyRunning as error:
                self._send_error_json(HTTPStatus.CONFLICT, str(error))
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            except Exception:
                self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "The sync stopped unexpectedly; previously stored data is intact.",
                )
            return
        if parsed.path == "/api/seasonal/backfill":
            try:
                body = self._read_json_body()
                max_items = self._bounded_int(
                    body.get("max_items", 80),
                    1,
                    2000,
                )
                budget = self._bounded_float(
                    body.get("budget", 100),
                    1,
                    100000,
                )
                horizon = self._bounded_int(
                    body.get("horizon", 7),
                    1,
                    30,
                )
                application = self.server.application
                league = application.storage.get_current_league()
                if league is None or league.is_demo:
                    raise ValueError(
                        "Run a successful live market sync before building "
                        "the completed-league archive."
                    )
                if application.history_service is None:
                    self._send_error_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "Historical backfill is not configured.",
                    )
                    return
                result = application.history_service.backfill(
                    league,
                    max_items=max_items,
                )
                if result.get("status") in {"success", "partial"}:
                    meta_summary = application.sync_meta(league)
                    result["meta_sync"] = meta_summary
                    if meta_summary.get("failed_leagues"):
                        warnings = result.setdefault("warnings", [])
                        if isinstance(warnings, list):
                            warnings.append(
                                "Historical prices were retained, but one or "
                                "more optional class-composition samples failed."
                            )
                    recommendation = application.recommendation_engine.generate(
                        league,
                        budget=budget,
                        horizon=horizon,
                        persist=True,
                    )
                    application.remember_recommendations(recommendation)
                    result["recommendation_summary"] = {
                        "generated_at": recommendation["generated_at"],
                        "ideas": len(recommendation["recommendations"]),
                        "invested": recommendation["invested"],
                        "reserve": recommendation["reserve"],
                    }
                status = (
                    HTTPStatus.CONFLICT
                    if result.get("status") == "busy"
                    else (
                        HTTPStatus.BAD_GATEWAY
                        if result.get("status") == "failed"
                        else HTTPStatus.OK
                    )
                )
                self._send_json(result, status)
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            except Exception:
                self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "Historical backfill stopped unexpectedly; previously "
                    "stored data is intact and the next run can resume.",
                )
            return
        if parsed.path == "/api/settings":
            try:
                body = self._read_json_body()
                allowed = {"exchange_categories", "item_categories"}
                rejected = set(body) - allowed
                if rejected:
                    raise ValueError(
                        "Unsupported setting keys: " + ", ".join(sorted(rejected))
                    )
                for key, value in body.items():
                    if (
                        not isinstance(value, list)
                        or not value
                        or len(value) > 32
                        or any(not isinstance(entry, str) or not entry for entry in value)
                    ):
                        raise ValueError(
                            f"{key} must be a non-empty list of category names."
                        )
                    self.server.application.storage.set_setting(key, value)
                self._send_json(
                    self.server.application.storage.public_settings()
                )
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Route not found.")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in STATIC_FILES:
            self._serve_static(STATIC_FILES[parsed.path], body=False)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Route not found.", body=False)

    def _serve_static(self, filename: str, *, body: bool = True) -> None:
        root = self.server.application.web_dir
        path = (root / filename).resolve()
        if root not in path.parents or not path.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "Static asset not found.")
            return
        try:
            payload = path.read_bytes()
        except OSError:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Static asset could not be read.",
            )
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            (
                f"{content_type}; charset=utf-8"
                if path.suffix != ".png"
                else content_type
            ),
        )
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Cache-Control",
            "public, max-age=86400" if path.suffix == ".png" else "no-cache",
        )
        self._send_security_headers()
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def _read_json_body(self) -> dict[str, Any]:
        content_length_raw = self.headers.get("Content-Length", "0")
        try:
            content_length = int(content_length_raw)
        except ValueError as error:
            raise ValueError("Invalid Content-Length header.") from error
        if content_length < 0 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("JSON request body is too large.")
        raw = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Request body must be valid UTF-8 JSON.") from error
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    @staticmethod
    def _bounded_float(value: Any, lower: float, upper: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Budget must be a number.") from error
        if not lower <= parsed <= upper:
            raise ValueError(f"Budget must be between {lower:g} and {upper:g}.")
        return parsed

    @staticmethod
    def _bounded_int(value: Any, lower: int, upper: int) -> int:
        try:
            parsed_float = float(value)
            parsed = int(parsed_float)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("Expected a whole number.") from error
        if parsed != parsed_float or not lower <= parsed <= upper:
            raise ValueError(
                f"Expected a whole number between {lower} and {upper}."
            )
        return parsed

    def _send_json(
        self,
        payload: Any,
        status: HTTPStatus = HTTPStatus.OK,
        *,
        body: bool = True,
    ) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        if body:
            self.wfile.write(encoded)

    def _send_error_json(
        self,
        status: HTTPStatus,
        message: str,
        *,
        body: bool = True,
    ) -> None:
        self._send_json(
            {"ok": False, "detail": message},
            status,
            body=body,
        )

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        )

    def log_message(self, format: str, *args: Any) -> None:
        # Keep routine browser polling quiet while preserving errors.
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(format, *args)


def create_server(
    *,
    host: str,
    port: int,
    database_path: str | Path,
    web_dir: str | Path,
    allow_demo_seed: bool = True,
) -> AdvisorHTTPServer:
    application = AdvisorApplication.create(
        database_path=database_path,
        web_dir=web_dir,
        allow_demo_seed=allow_demo_seed,
    )
    return AdvisorHTTPServer((host, port), application)


def serve_in_thread(server: AdvisorHTTPServer) -> threading.Thread:
    thread = threading.Thread(
        target=server.serve_forever,
        name="wraeclast-ledger-http",
        daemon=True,
    )
    thread.start()
    return thread
