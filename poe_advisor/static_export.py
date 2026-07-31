from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from .recommendation import FORECAST_HORIZONS
from .server import AdvisorApplication


SCHEMA_VERSION = 1
DEFAULT_CANONICAL_HORIZON = 7
STATIC_ASSETS = ("index.html", "styles.css", "app.js", "og.png")


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


def _has_chart_data(comparison: dict[str, Any]) -> bool:
    current = comparison.get("current_league", {})
    weighted = comparison.get("weighted_historical", {})
    return bool(
        current.get("points")
        or weighted.get("points")
        or comparison.get("past_leagues")
    )


def export_github_pages(
    *,
    database_path: str | Path,
    web_dir: str | Path,
    output_path: str | Path,
    repository: str | None = None,
    include_histories: bool = True,
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

    canonical_payload = application.recommendations(
        budget=100.0,
        horizon=DEFAULT_CANONICAL_HORIZON,
    )
    rankings = canonical_payload.get("rankings")
    if not isinstance(rankings, list):
        rankings = canonical_payload.get("recommendations", [])
    if not isinstance(rankings, list) or not rankings:
        raise RuntimeError("The current archive produced no ranked items.")

    rank_maps: dict[str, dict[str, int]] = {
        str(DEFAULT_CANONICAL_HORIZON): _rank_map(canonical_payload)
    }
    for horizon in FORECAST_HORIZONS:
        if horizon == DEFAULT_CANONICAL_HORIZON:
            continue
        horizon_payload = application.recommendations(
            budget=100.0,
            horizon=horizon,
        )
        rank_maps[str(horizon)] = _rank_map(horizon_payload)

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

    canonical_payload["rankings"] = rankings
    canonical_payload.pop("recommendations", None)
    canonical_payload["horizon"] = DEFAULT_CANONICAL_HORIZON
    canonical_payload["static_export"] = {
        "schema_version": SCHEMA_VERSION,
        "rank_horizons": list(FORECAST_HORIZONS),
        "history_shards": len(history_shards),
        "history_items": history_items,
    }
    ranking_summary = canonical_payload.setdefault("ranking_summary", {})
    ranking_summary["returned"] = len(rankings)
    ranking_summary["limit"] = None
    ranking_summary["pagination"] = {
        "mode": "client",
        "default_page_size": 50,
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
        "status": status_path,
        "catalog": catalog_path,
        "history_shards": history_shards,
        "archive": {
            "storage": "github-release-asset",
            "release_tag": "market-archive",
            "asset": "poe_advisor.sqlite3.gz",
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
        "status_bytes": status_bytes,
        "history_bytes": history_bytes,
        "total_bytes": total_bytes,
        "manifest": str(manifest_destination),
    }
