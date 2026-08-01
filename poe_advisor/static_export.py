from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from .provenance import (
    CURRENT_PRICE_SOURCES,
    HISTORICAL_PRICE_SOURCES,
    STANDARD_PRICE_SOURCES,
    has_production_price_provenance,
)
from .historical import BROADLY_COVERED_LEAGUE_IDS
from .ninja_history import DUMP_IMPORT_VERSION, DUMP_SETTING_PREFIX
from .recommendation import FORECAST_HORIZONS
from .server import AdvisorApplication


SCHEMA_VERSION = 2
DEFAULT_CANONICAL_HORIZON = 7
RANKING_PAGE_SIZE = 100
STATIC_ASSETS = ("index.html", "styles.css", "app.js", "og.png")

RANKING_INDEX_FIELDS = (
    "key",
    "name",
    "category",
    "search_text",
    "price_divine",
    "price_chaos",
    "rank_3d",
    "rank_7d",
    "rank_14d",
)


def _compact_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")


def _write_hashed_json(
    output_root: Path,
    logical_stem: str,
    value: Any,
) -> tuple[str, int]:
    content = _compact_json_bytes(value)
    digest = hashlib.sha256(content).hexdigest()[:16]
    relative_path = f"{logical_stem}.{digest}.json"
    destination = output_root / Path(relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return relative_path.replace("\\", "/"), len(content)


def _prepare_output(output_path: str | Path, project_root: Path) -> Path:
    output = Path(output_path).expanduser().resolve()
    forbidden = {
        Path(output.anchor).resolve(),
        project_root.resolve(),
        (project_root / "web").resolve(),
        (project_root / "data").resolve(),
    }
    if output in forbidden:
        raise ValueError(
            "Static export output must be a dedicated build directory."
        )
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    return output


def _rank_map(payload: dict[str, Any]) -> dict[str, int]:
    rankings = payload.get("rankings")
    if not isinstance(rankings, list):
        rankings = payload.get("recommendations", [])
    return {
        str(row.get("key") or row.get("curve_key")): int(row["rank"])
        for row in rankings
        if isinstance(row, dict)
        and (row.get("key") or row.get("curve_key"))
        and row.get("rank") is not None
    }


def _rank_map_for_horizon(
    payload: dict[str, Any],
    horizon: int,
) -> dict[str, int]:
    """Re-rank one canonical all-horizon payload without recomputing prices."""

    rankings = payload.get("rankings")
    if not isinstance(rankings, list):
        rankings = payload.get("recommendations", [])
    rows = [
        row
        for row in rankings
        if isinstance(row, dict) and (row.get("key") or row.get("curve_key"))
    ]
    forecast_key = f"forecast_{int(horizon)}d"

    def expected_gain(row: dict[str, Any]) -> float | None:
        forecast = row.get(forecast_key)
        if not isinstance(forecast, dict):
            return None
        value = forecast.get("expected_gain")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    ordered = sorted(
        rows,
        key=lambda row: (
            expected_gain(row) is None,
            -expected_gain(row) if expected_gain(row) is not None else 0.0,
            str(row.get("name") or "").casefold(),
            str(row.get("key") or row.get("curve_key") or ""),
        ),
    )
    return {
        str(row.get("key") or row.get("curve_key")): rank
        for rank, row in enumerate(ordered, start=1)
    }


def _compact_forecast(value: Any, horizon: int) -> dict[str, Any]:
    """Retain only forecast fields rendered by the static dashboard."""

    if not isinstance(value, dict):
        return {"days": int(horizon)}
    blend = value.get("blend")
    compact: dict[str, Any] = {
        "days": int(value.get("days") or horizon),
    }
    retained = (
        "expected_gain_pct",
        "expected_price_divine",
        "raw_historical_target_divine",
        "historical_target_price_divine",
        "historical_target_divine",
        "meta_multiplier",
        "historical_target_gain_pct",
        "historical_sample_leagues",
        "historical_leagues",
        "sample_leagues",
        "sample_league_names",
        "current_curve_gain_pct",
        "missing_reason",
    )
    for key in retained:
        if key in value and value[key] is not None:
            compact[key] = value[key]
    if isinstance(blend, dict) and "current_curve_used" in blend:
        compact["blend"] = {
            "current_curve_used": bool(blend["current_curve_used"]),
        }
    return compact


def _compact_ranking_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build the complete-but-small row loaded for one visible page."""

    retained = (
        "key",
        "curve_key",
        "name",
        "category",
        "market_scope_code",
        "market_scope_label",
        "market_scope_caveat",
        "trade_identity",
        "price_divine",
        "current_price_divine",
        "price_chaos",
        "current_observed_at",
        "current_source",
        "historical_average_divine",
        "historical_recency_weighted_divine",
        "historical_level_sample_leagues",
        "seasonal_league_weights",
        "standard_anchor_divine",
        "standard_anchor_gap",
        "standard_anchor_gap_pct",
        "standard_anchor_ratio",
        "standard_anchor_observed_at",
        "standard_anchor_source",
        "meta_multiplier",
        "history_shard",
        "static_ranks",
    )
    compact = {
        key: row[key]
        for key in retained
        if key in row and row[key] is not None
    }
    compact["forecast_3d"] = _compact_forecast(row.get("forecast_3d"), 3)
    compact["forecast_7d"] = _compact_forecast(row.get("forecast_7d"), 7)
    compact["forecast_14d"] = _compact_forecast(
        row.get("forecast_14d"),
        14,
    )
    return compact


def _static_search_text(row: dict[str, Any]) -> str:
    """Generate the complete lowercase search corpus without full row data."""

    parts = [
        str(row.get("name") or ""),
        str(row.get("category") or ""),
        str(row.get("key") or row.get("curve_key") or ""),
    ]
    identity = row.get("trade_identity")
    if isinstance(identity, dict):
        for key, value in identity.items():
            if value is None:
                continue
            parts.extend((str(key).replace("_", " "), str(value)))
        if identity.get("corrupted") is True:
            parts.append("corrupted")
        elif identity.get("corrupted") is False:
            parts.append("uncorrupted")
    return " ".join(part.strip() for part in parts if part.strip()).casefold()


def _ranking_index_entry(row: dict[str, Any]) -> list[Any]:
    ranks = row.get("static_ranks") or {}
    return [
        str(row.get("key") or row.get("curve_key") or ""),
        str(row.get("name") or "Unknown item"),
        str(row.get("category") or "Other"),
        _static_search_text(row),
        row.get("price_divine", row.get("current_price_divine")),
        row.get("price_chaos"),
        ranks.get("3"),
        ranks.get("7"),
        ranks.get("14"),
    ]


def _has_chart_data(comparison: dict[str, Any]) -> bool:
    current = comparison.get("current_league", {})
    weighted = comparison.get("weighted_historical", {})
    return bool(
        current.get("points")
        or weighted.get("points")
        or comparison.get("past_leagues")
    )


def _compact_static_comparison(
    comparison: dict[str, Any],
    *,
    maximum_league_day: int,
) -> dict[str, Any]:
    """Keep only the curve window rendered by the static dashboard.

    Full per-league observations remain in the durable local SQLite archive.
    The browser plots the current curve against the weighted historical curve,
    so serializing every raw league curve and every late-league day adds
    gigabytes without changing the displayed result.
    """

    compact = dict(comparison)
    weighted = compact.get("weighted_historical")
    if isinstance(weighted, dict):
        weighted = dict(weighted)
        raw_points = weighted.get("points")
        if isinstance(raw_points, list):
            retained = [
                point
                for point in raw_points
                if isinstance(point, dict)
                and int(point.get("league_day") or 0)
                <= int(maximum_league_day)
            ]
            weighted["points"] = retained
            weighted["omitted_points"] = len(raw_points) - len(retained)
            weighted["static_maximum_league_day"] = int(maximum_league_day)
        compact["weighted_historical"] = weighted
    compact.pop("past_leagues", None)
    return compact


def _assert_export_provenance(
    payload: dict[str, Any],
    *,
    is_demo: bool,
) -> None:
    if is_demo:
        return
    if not has_production_price_provenance(payload):
        raise RuntimeError(
            "Refusing to publish recommendations without the exact "
            "poe.ninja source-of-truth policy."
        )
    rankings = payload.get("rankings", payload.get("recommendations", []))
    if not isinstance(rankings, list):
        raise RuntimeError("Recommendation rankings are not a list.")
    for row in rankings:
        if not isinstance(row, dict):
            raise RuntimeError("Recommendation ranking row is not an object.")
        if row.get("current_source") not in CURRENT_PRICE_SOURCES:
            raise RuntimeError(
                "Refusing to publish a current price outside the poe.ninja "
                "allowlist."
            )
        if (
            row.get("standard_anchor_divine") is not None
            and row.get("standard_anchor_source") not in STANDARD_PRICE_SOURCES
        ):
            raise RuntimeError(
                "Refusing to publish a Standard anchor outside the "
                "poe.ninja allowlist."
            )
        for horizon in ("forecast_3d", "forecast_7d", "forecast_14d"):
            forecast = row.get(horizon)
            if not isinstance(forecast, dict):
                continue
            observations = forecast.get("historical_observations", [])
            if not isinstance(observations, list):
                raise RuntimeError("Historical observations are not a list.")
            if any(
                not isinstance(observation, dict)
                or observation.get("source") not in HISTORICAL_PRICE_SOURCES
                for observation in observations
            ):
                raise RuntimeError(
                    "Refusing to publish historical prices outside the "
                    "poe.ninja archive allowlist."
                )


def _assert_curve_provenance(
    comparison: dict[str, Any],
    *,
    is_demo: bool,
) -> None:
    """Fail closed if a static chart contains a non-golden price row."""

    if is_demo:
        return
    current_points = comparison.get("current_league", {}).get("points", [])
    if any(
        not isinstance(point, dict)
        or point.get("source") not in CURRENT_PRICE_SOURCES
        for point in current_points
    ):
        raise RuntimeError(
            "Refusing to publish a current curve outside the poe.ninja "
            "allowlist."
        )
    past_leagues = comparison.get("past_leagues", [])
    for past_league in past_leagues:
        if not isinstance(past_league, dict):
            raise RuntimeError("Historical curve league is not an object.")
        points = past_league.get("points", [])
        if any(
            not isinstance(point, dict)
            or point.get("source") not in HISTORICAL_PRICE_SOURCES
            for point in points
        ):
            raise RuntimeError(
                "Refusing to publish a historical curve outside the "
                "poe.ninja archive allowlist."
            )


def _assert_completed_history_ready(storage: Any) -> None:
    """Refuse a production build unless all golden league dumps committed."""

    missing_markers: list[str] = []
    expected_counts: dict[str, tuple[str, int]] = {}
    for league_id in BROADLY_COVERED_LEAGUE_IDS:
        marker = storage.get_setting(f"{DUMP_SETTING_PREFIX}{league_id}", {})
        if (
            not isinstance(marker, dict)
            or marker.get("status") != "success"
            or marker.get("import_version") != DUMP_IMPORT_VERSION
            or marker.get("league_name") != league_id
            or not isinstance(
                marker.get(
                    "stored_seasonal_rows",
                    marker.get("seasonal_rows_written"),
                ),
                int,
            )
            or isinstance(
                marker.get(
                    "stored_seasonal_rows",
                    marker.get("seasonal_rows_written"),
                ),
                bool,
            )
            or int(
                marker.get(
                    "stored_seasonal_rows",
                    marker.get("seasonal_rows_written", 0),
                )
            )
            <= 0
        ):
            missing_markers.append(league_id)
        else:
            mode = str(marker.get("storage_mode") or "full")
            stored = int(
                marker.get(
                    "stored_seasonal_rows",
                    marker["seasonal_rows_written"],
                )
            )
            if mode not in {"full", "compact"}:
                missing_markers.append(league_id)
                continue
            if mode == "compact":
                raw_counts = [
                    marker.get("raw_source_rows_seen"),
                    marker.get("normalized_source_rows"),
                    marker.get("eligible_source_rows"),
                ]
                valid_raw_counts = all(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                    for value in raw_counts
                )
                if (
                    not valid_raw_counts
                    or not (
                        int(raw_counts[0]) >= int(raw_counts[1])
                        >= int(raw_counts[2]) >= stored
                    )
                ):
                    missing_markers.append(league_id)
                    continue
            expected_counts[league_id] = (mode, stored)

    actual_counts: dict[str, tuple[str, int]] = {}
    if hasattr(storage, "seasonal_price_storage_counts"):
        for league_id in BROADLY_COVERED_LEAGUE_IDS:
            mode, _ = expected_counts.get(league_id, ("full", 0))
            counts = storage.seasonal_price_storage_counts(
                league_id,
                source=HISTORICAL_PRICE_SOURCES[0],
            )
            actual_counts[league_id] = (mode, int(counts[mode]))
    else:
        # Compatibility for isolated test doubles and pre-v4 local archives.
        with closing(storage.connect()) as connection:
            actual_counts = {
                str(row[0]): ("full", int(row[1]))
                for row in connection.execute(
                    """
                    SELECT league_id, COUNT(*)
                    FROM seasonal_prices
                    WHERE source IN ({})
                    GROUP BY league_id
                    """.format(
                        ",".join("?" for _ in HISTORICAL_PRICE_SOURCES)
                    ),
                    HISTORICAL_PRICE_SOURCES,
                )
            }
    missing_rows = [
        league_id
        for league_id in BROADLY_COVERED_LEAGUE_IDS
        if actual_counts.get(league_id) != expected_counts.get(league_id)
    ]
    if missing_markers or missing_rows:
        missing = sorted(set(missing_markers + missing_rows))
        raise RuntimeError(
            "Refusing to publish without complete poe.ninja dump imports for: "
            + ", ".join(missing)
        )


def export_github_pages(
    *,
    database_path: str | Path,
    web_dir: str | Path,
    output_path: str | Path,
    repository: str | None = None,
    include_histories: bool = True,
    allow_demo_export: bool = False,
) -> dict[str, Any]:
    """Export the local application as a read-only GitHub Pages artifact."""

    web_root = Path(web_dir).expanduser().resolve()
    project_root = web_root.parent
    output = _prepare_output(output_path, project_root)
    application = AdvisorApplication.create(
        database_path=database_path,
        web_dir=web_root,
        allow_demo_seed=False,
    )
    league = application.storage.get_current_league()
    if league is None:
        raise RuntimeError(
            "No current league is stored. Run a successful market sync first."
        )
    if league.is_demo and not allow_demo_export:
        raise RuntimeError(
            "Refusing to publish an offline demo fixture as market data."
        )
    if not league.is_demo:
        _assert_completed_history_ready(application.storage)

    canonical_payload = application.recommendations(
        budget=100.0,
        horizon=DEFAULT_CANONICAL_HORIZON,
    )
    _assert_export_provenance(canonical_payload, is_demo=league.is_demo)
    rankings = canonical_payload.get("rankings")
    if not isinstance(rankings, list):
        rankings = canonical_payload.get("recommendations", [])
    if not isinstance(rankings, list) or not rankings:
        raise RuntimeError("The current archive produced no ranked items.")

    rank_maps: dict[str, dict[str, int]] = {
        str(horizon): _rank_map_for_horizon(canonical_payload, horizon)
        for horizon in FORECAST_HORIZONS
    }
    canonical_rank_map = _rank_map(canonical_payload)
    if (
        not league.is_demo
        and rank_maps[str(DEFAULT_CANONICAL_HORIZON)]
        != canonical_rank_map
    ):
        raise RuntimeError(
            "Static horizon ranking disagrees with the canonical model rank."
        )

    expected_keys = set(rank_maps[str(DEFAULT_CANONICAL_HORIZON)])
    for horizon, ranks in rank_maps.items():
        if set(ranks) != expected_keys:
            raise RuntimeError(
                f"Static rank universe differs for the {horizon}-day horizon."
            )

    row_by_key: dict[str, dict[str, Any]] = {}
    for row in rankings:
        if not isinstance(row, dict):
            continue
        item_key = str(row.get("key") or row.get("curve_key") or "")
        if not item_key:
            continue
        row.pop("history", None)
        row["static_ranks"] = {
            horizon: ranks[item_key]
            for horizon, ranks in rank_maps.items()
        }
        row_by_key[item_key] = row

    history_shards: dict[str, str] = {}
    history_items = 0
    history_bytes = 0
    if include_histories:
        keys_by_shard: dict[str, list[str]] = defaultdict(list)
        for item_key in sorted(row_by_key):
            shard = hashlib.sha256(item_key.encode("utf-8")).hexdigest()[:2]
            keys_by_shard[shard].append(item_key)

        for shard, item_keys in sorted(keys_by_shard.items()):
            shard_items: dict[str, dict[str, Any]] = {}
            for item_key in item_keys:
                comparison = application._seasonal_comparison(
                    league=league,
                    item_key=item_key,
                )
                _assert_curve_provenance(
                    comparison,
                    is_demo=league.is_demo,
                )
                current_points = comparison.get("current_league", {}).get(
                    "points", []
                )
                current_last_day = max(
                    (
                        int(point.get("league_day") or 0)
                        for point in current_points
                        if isinstance(point, dict)
                    ),
                    default=max(1, int(league.day or 1)),
                )
                comparison = _compact_static_comparison(
                    comparison,
                    maximum_league_day=(
                        current_last_day + max(FORECAST_HORIZONS)
                    ),
                )
                if not _has_chart_data(comparison):
                    continue
                shard_items[item_key] = comparison
                row_by_key[item_key]["history_shard"] = shard
            if not shard_items:
                continue
            relative_path, byte_count = _write_hashed_json(
                output,
                f"data/history/{shard}",
                {"items": shard_items},
            )
            history_shards[shard] = relative_path
            history_items += len(shard_items)
            history_bytes += byte_count

    ranking_pages: dict[str, list[str]] = {}
    ranking_page_count = 0
    ranking_page_bytes = 0
    for horizon in FORECAST_HORIZONS:
        horizon_key = str(horizon)
        ordered_keys = sorted(
            row_by_key,
            key=lambda item_key: rank_maps[horizon_key][item_key],
        )
        horizon_pages: list[str] = []
        for start in range(0, len(ordered_keys), RANKING_PAGE_SIZE):
            page_keys = ordered_keys[start : start + RANKING_PAGE_SIZE]
            page_number = start // RANKING_PAGE_SIZE + 1
            page_rows: list[dict[str, Any]] = []
            for item_key in page_keys:
                page_row = _compact_ranking_row(row_by_key[item_key])
                page_row["rank"] = rank_maps[horizon_key][item_key]
                page_rows.append(page_row)
            relative_path, byte_count = _write_hashed_json(
                output,
                f"data/rankings/{horizon_key}/{page_number:04d}",
                {
                    "horizon": horizon,
                    "page": page_number,
                    "page_size": RANKING_PAGE_SIZE,
                    "items": page_rows,
                },
            )
            horizon_pages.append(relative_path)
            ranking_page_count += 1
            ranking_page_bytes += byte_count
        ranking_pages[horizon_key] = horizon_pages

    canonical_order = sorted(
        row_by_key,
        key=lambda item_key: rank_maps[str(DEFAULT_CANONICAL_HORIZON)][
            item_key
        ],
    )
    ranking_index_path, ranking_index_bytes = _write_hashed_json(
        output,
        "data/ranking-index",
        {
            "fields": list(RANKING_INDEX_FIELDS),
            "items": [
                _ranking_index_entry(row_by_key[item_key])
                for item_key in canonical_order
            ],
        },
    )

    # The static catalog is model/status metadata only. Full ranking rows live
    # in bounded shards, while the compact index supports exact client-side
    # search, filters, hidden items, and horizon ordering.
    canonical_payload.pop("rankings", None)
    canonical_payload.pop("recommendations", None)
    canonical_payload.pop("watchlist", None)
    canonical_payload["horizon"] = DEFAULT_CANONICAL_HORIZON
    canonical_payload["static_export"] = {
        "schema_version": SCHEMA_VERSION,
        "rank_horizons": list(FORECAST_HORIZONS),
        "ranking_page_size": RANKING_PAGE_SIZE,
        "ranking_pages": ranking_page_count,
        "history_shards": len(history_shards),
        "history_items": history_items,
    }
    ranking_summary = canonical_payload.setdefault("ranking_summary", {})
    ranking_summary["returned"] = len(rankings)
    ranking_summary["limit"] = None
    ranking_summary["pagination"] = {
        "mode": "static-page-shards",
        "default_page_size": 50,
        "shard_size": RANKING_PAGE_SIZE,
    }

    status_payload = application.status()
    status_payload["hosting"] = {
        "mode": "github-pages",
        "generated_at": canonical_payload.get("generated_at"),
        "repository": repository or os.environ.get("GITHUB_REPOSITORY"),
    }
    status_path, status_bytes = _write_hashed_json(
        output,
        "data/status",
        status_payload,
    )
    catalog_path, catalog_bytes = _write_hashed_json(
        output,
        "data/recommendations",
        canonical_payload,
    )

    repository_name = (
        str(repository or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    )
    repository_url = (
        f"https://github.com/{repository_name}" if repository_name else None
    )
    workflow_url = (
        f"{repository_url}/actions/workflows/daily-pages.yml"
        if repository_url
        else None
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "mode": "github-pages",
        "generated_at": canonical_payload.get("generated_at"),
        "data_updated_at": status_payload.get("last_sync_at"),
        "repository": repository_name or None,
        "repository_url": repository_url,
        "workflow_url": workflow_url,
        "commit_sha": os.environ.get("GITHUB_SHA"),
        "league": {
            "id": league.id,
            "name": league.name,
            "day": league.day,
        },
        "ranked_items": len(rankings),
        "history_items": history_items,
        "price_provenance": canonical_payload.get("price_provenance"),
        "status": status_path,
        "catalog": catalog_path,
        "ranking_index": ranking_index_path,
        "ranking_pages": {
            "page_size": RANKING_PAGE_SIZE,
            "horizons": ranking_pages,
        },
        "ranking_facets": {
            "categories": sorted(
                {
                    str(row.get("category") or "Other")
                    for row in row_by_key.values()
                }
            ),
        },
        "history_shards": history_shards,
        "archive": {
            "storage": "github-release-asset",
            "release_tag": "market-archive",
            "asset": "poe_market_compact_history.sqlite3.gz",
            "public_market_only": True,
            "included_in_pages": False,
        },
    }
    manifest_destination = output / "data" / "manifest.json"
    manifest_destination.parent.mkdir(parents=True, exist_ok=True)
    manifest_destination.write_bytes(_compact_json_bytes(manifest))

    for asset in STATIC_ASSETS:
        source = web_root / asset
        if not source.is_file():
            raise FileNotFoundError(f"Dashboard asset is missing: {source}")
        shutil.copy2(source, output / asset)
    (output / ".nojekyll").write_text("", encoding="utf-8")

    total_bytes = sum(
        path.stat().st_size for path in output.rglob("*") if path.is_file()
    )
    return {
        "ok": True,
        "output": str(output),
        "generated_at": canonical_payload.get("generated_at"),
        "league": league.id,
        "ranked_items": len(rankings),
        "history_items": history_items,
        "history_shards": len(history_shards),
        "catalog_bytes": catalog_bytes,
        "ranking_index_bytes": ranking_index_bytes,
        "ranking_page_bytes": ranking_page_bytes,
        "ranking_pages": ranking_page_count,
        "status_bytes": status_bytes,
        "history_bytes": history_bytes,
        "total_bytes": total_bytes,
        "manifest": str(manifest_destination),
    }
