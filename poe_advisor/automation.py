from __future__ import annotations

from pathlib import Path
from typing import Any

from .server import AdvisorApplication


def run_daily_update(
    *,
    database_path: str | Path,
    web_dir: str | Path,
    history_hours: int = 0,
    seasonal_items: int = 20,
    current_history_items: int = 2_000,
) -> dict[str, Any]:
    """Run the same durable refresh stages used by the interactive dashboard."""

    if not 0 <= int(history_hours) <= 336:
        raise ValueError("history_hours must be between 0 and 336")
    if not 0 <= int(seasonal_items) <= 2000:
        raise ValueError("seasonal_items must be between 0 and 2000")
    if not 0 <= int(current_history_items) <= 2000:
        raise ValueError("current_history_items must be between 0 and 2000")

    application = AdvisorApplication.create(
        database_path=database_path,
        web_dir=web_dir,
        allow_demo_seed=False,
    )
    result = application.sync_service.sync(
        backfill_hours=int(history_hours),
    )
    if not result.get("ok"):
        return result

    league = application.storage.get_current_league()
    if league is None or league.is_demo:
        return {
            **result,
            "ok": False,
            "status": "failed",
            "message": "The daily refresh did not produce a live trade league.",
        }

    warnings = result.setdefault("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
        result["warnings"] = warnings

    try:
        meta_summary = application.sync_meta(league)
        result["meta_sync"] = meta_summary
        if meta_summary.get("failed_leagues"):
            warnings.append(
                "One or more optional build-composition samples failed; "
                "existing profiles remain available."
            )
    except Exception as error:
        result["meta_sync"] = {
            "status": "failed",
            "message": str(error),
        }
        warnings.append(
            "Optional build-composition refresh failed; existing profiles "
            "remain available."
        )

    preliminary = application.recommendation_engine.generate(
        league,
        budget=100.0,
        horizon=7,
        persist=False,
    )
    rankings = preliminary.get("rankings")
    if not isinstance(rankings, list):
        rankings = preliminary.get("recommendations", [])
    ranked_keys = [
        str(item.get("curve_key") or item.get("key") or "").strip()
        for item in rankings
        if isinstance(item, dict)
        and (item.get("curve_key") or item.get("key"))
    ]
    if ranked_keys:
        try:
            current_history = (
                application.sync_service.sync_current_item_histories(
                    league,
                    ranked_keys,
                    max_items=int(current_history_items),
                )
            )
            result["current_history_sync"] = current_history
            if current_history.get("status") in {"failed", "partial"}:
                warnings.append(
                    "Some ranked current-league price curves remain "
                    "incomplete."
                )
        except Exception as error:
            result["current_history_sync"] = {
                "status": "failed",
                "message": str(error),
            }
            warnings.append(
                "Ranked current-league curve refresh stopped safely; stored "
                "curves remain available."
            )

    if seasonal_items and application.history_service is not None:
        try:
            seasonal = application.history_service.backfill(
                league,
                max_items=int(seasonal_items),
            )
            result["seasonal_sync"] = seasonal
            if seasonal.get("status") not in {"success", "partial"}:
                warnings.append(
                    "The optional completed-league backfill did not advance."
                )
        except Exception as error:
            result["seasonal_sync"] = {
                "status": "failed",
                "message": str(error),
            }
            warnings.append(
                "Completed-league backfill stopped safely; previous history "
                "remains available."
            )

    recommendation = application.recommendation_engine.generate(
        league,
        budget=100.0,
        horizon=7,
        persist=True,
    )
    result["recommendation_summary"] = {
        "generated_at": recommendation["generated_at"],
        "rankings": len(recommendation.get("rankings", [])),
        "horizons": [3, 7, 14],
    }
    result["message"] = (
        "Daily market archive, price curves, and recommendation model updated."
    )
    return result
