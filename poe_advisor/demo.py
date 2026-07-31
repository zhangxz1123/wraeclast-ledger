from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import League, PricePoint, iso_utc
from .normalization import canonical_key
from .storage import Storage


DEMO_LEAGUE_ID = "demo-softcore-fixture"
DEMO_LEAGUE_NAME = "Offline Demo Softcore"

# Prices are expressed in Divine Orbs and shaped after common liquid PoE 1
# markets. They are deliberately illustrative, never labelled as live.
DEMO_ASSETS: tuple[dict[str, Any], ...] = (
    {
        "name": "Veiled Orb",
        "category": "Currency",
        "start": 0.19,
        "growth": 0.031,
        "dip": 0.38,
        "listings": 640,
    },
    {
        "name": "Orb of Conflict",
        "category": "Currency",
        "start": 0.075,
        "growth": 0.023,
        "dip": 0.32,
        "listings": 980,
    },
    {
        "name": "Sacred Crystallised Lifeforce",
        "category": "Currency",
        "start": 0.0028,
        "growth": 0.018,
        "dip": 0.28,
        "listings": 5200,
    },
    {
        "name": "Deafening Essence of Horror",
        "category": "Essence",
        "start": 0.026,
        "growth": 0.019,
        "dip": 0.34,
        "listings": 1900,
    },
    {
        "name": "Maven's Writ",
        "category": "Fragment",
        "start": 0.63,
        "growth": 0.017,
        "dip": 0.30,
        "listings": 530,
    },
    {
        "name": "The Doctor",
        "category": "DivinationCard",
        "start": 4.4,
        "growth": 0.014,
        "dip": 0.35,
        "listings": 260,
    },
    {
        "name": "The Apothecary",
        "category": "DivinationCard",
        "start": 7.3,
        "growth": 0.012,
        "dip": 0.11,
        "listings": 180,
    },
    {
        "name": "Unnatural Instinct",
        "category": "UniqueJewel",
        "start": 1.45,
        "growth": 0.016,
        "dip": 0.32,
        "listings": 420,
    },
    {
        "name": "Awakened Multistrike Support",
        "category": "SkillGem",
        "start": 9.8,
        "growth": 0.013,
        "dip": 0.08,
        "listings": 120,
    },
    {
        "name": "Prime Chaotic Resonator",
        "category": "Resonator",
        "start": 0.082,
        "growth": 0.008,
        "dip": 0.09,
        "listings": 1600,
    },
    {
        "name": "Reliquary Scarab of Vision",
        "category": "Scarab",
        "start": 0.035,
        "growth": 0.011,
        "dip": 0.36,
        "listings": 2400,
    },
    {
        "name": "Mirror of Kalandra",
        "category": "Currency",
        "start": 530.0,
        "growth": 0.007,
        "dip": 0.0,
        "listings": 25,
    },
)


def seed_demo(
    storage: Storage,
    *,
    make_current: bool = True,
    days: int = 45,
    now: datetime | None = None,
) -> dict[str, int]:
    """Seed deterministic, clearly-labelled market history for offline use."""

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    end_day = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = end_day - timedelta(days=max(14, days) - 1)
    league = League(
        id=DEMO_LEAGUE_ID,
        name=DEMO_LEAGUE_NAME,
        start_at=iso_utc(start - timedelta(days=5)),
        realm="pc",
        is_demo=True,
    )
    storage.upsert_league(league, current=make_current)
    fixture_payload = {
        "kind": "offline-demo-fixture",
        "disclaimer": "Synthetic illustrative data; not live market prices.",
        "generated_for_date": end_day.date().isoformat(),
        "assets": DEMO_ASSETS,
    }
    raw = json.dumps(fixture_payload, separators=(",", ":")).encode("utf-8")
    snapshot_id, snapshot_created = storage.add_snapshot(
        source="demo-fixture",
        endpoint="builtin://demo-market-v1",
        league_id=league.id,
        category="mixed",
        fetched_at=iso_utc(now),
        status_code=200,
        raw=raw,
        metadata={"synthetic": True, "version": 1},
    )

    points: list[PricePoint] = []
    count_days = max(14, days)
    for asset_index, asset in enumerate(DEMO_ASSETS):
        for day_index in range(count_days):
            trend = math.exp(float(asset["growth"]) * day_index)
            cycle = 1.0 + 0.035 * math.sin(day_index * 0.72 + asset_index)
            # A short, partially recovered drawdown creates a few plausible
            # value opportunities without modelling a still-falling price.
            days_from_end = count_days - 1 - day_index
            if days_from_end > 4:
                dip_progress = 0.0
            else:
                dip_progress = 0.65 + 0.0875 * days_from_end
            dip = 1.0 - float(asset["dip"]) * dip_progress
            divine = max(0.00001, float(asset["start"]) * trend * cycle * dip)
            chaos_per_divine = 132.0 + 1.05 * day_index + 3.5 * math.sin(
                day_index / 4
            )
            listings = max(
                5,
                int(
                    float(asset["listings"])
                    * (1.0 + 0.08 * math.sin(day_index / 3 + asset_index))
                ),
            )
            observed = start + timedelta(days=day_index)
            points.append(
                PricePoint(
                    league_id=league.id,
                    item_key=canonical_key(
                        str(asset["name"]),
                        str(asset["category"]),
                        str(asset["name"]),
                    ),
                    name=str(asset["name"]),
                    category=str(asset["category"]),
                    source="demo-fixture",
                    observed_at=iso_utc(observed),
                    chaos_value=divine * chaos_per_divine,
                    divine_value=divine,
                    listing_count=listings,
                    volume=float(listings) * (0.6 + 0.3 * math.sin(day_index / 5) ** 2),
                    confidence=0.88,
                    details={
                        "synthetic": True,
                        "fixture_version": 1,
                        "do_not_treat_as_live": True,
                    },
                    snapshot_id=snapshot_id,
                )
            )
    rows = storage.insert_price_points(points)
    storage.update_source_state(
        source="demo-fixture",
        endpoint="builtin://demo-market-v1",
        league_id=league.id,
        status="demo",
        detail="Illustrative offline fixture; not live data.",
        success=True,
    )
    return {
        "rows_written": rows,
        "snapshots_written": int(snapshot_created),
        "assets": len(DEMO_ASSETS),
    }
