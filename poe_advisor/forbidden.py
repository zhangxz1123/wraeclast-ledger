from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from .clients import GGGSkillTreeClient, PoeWatchClient
from .models import PricePoint


FORBIDDEN_CATEGORY = "ForbiddenJewel"
FORBIDDEN_CATALOG_CATEGORY = "exact-forbidden-jewels"
SKILL_TREE_CATEGORY = "forbidden-passive-map"


@dataclass(frozen=True, slots=True)
class PassiveMap:
    """Exact current ascendancy-notable metadata from the official tree."""

    passives: dict[str, dict[str, str]]
    ambiguous_names: frozenset[str]
    node_count: int


def _object_rows(value: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        return [
            (str(key), row)
            for key, row in value.items()
            if isinstance(row, dict)
        ]
    if isinstance(value, list):
        return [
            (str(index), row)
            for index, row in enumerate(value)
            if isinstance(row, dict)
        ]
    return []


def _tree_root(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict) and (
        isinstance(data.get("nodes"), (dict, list))
        or isinstance(data.get("classes"), (dict, list))
    ):
        return data
    return payload


def parse_passive_map(payload: Any) -> PassiveMap:
    """Build an auditable passive-name mapping from GGG's tree export.

    Only nodes explicitly marked as ascendancy notables or ascendancy
    multiple-choice options are accepted. The latter covers Ascendant choices
    and a small number of mutually exclusive ascendancy passives that can
    appear on Forbidden Jewels. Duplicate names that point at different
    ascendancies are excluded rather than guessed.
    """

    root = _tree_root(payload)
    ascendancy_classes: dict[str, str] = {}
    ascendancy_names: dict[str, str] = {}
    for _, character_class in _object_rows(root.get("classes")):
        base_class = str(character_class.get("name") or "").strip()
        if not base_class:
            continue
        for _, ascendancy in _object_rows(character_class.get("ascendancies")):
            identifier = str(ascendancy.get("id") or "").strip()
            name = str(ascendancy.get("name") or identifier).strip()
            if identifier:
                ascendancy_classes[identifier.casefold()] = base_class
                ascendancy_names[identifier.casefold()] = name or identifier
            if name:
                ascendancy_classes[name.casefold()] = base_class
                ascendancy_names[name.casefold()] = name

    candidates: dict[str, dict[str, str]] = {}
    ambiguous: set[str] = set()
    node_count = 0
    for node_id, node in _object_rows(root.get("nodes")):
        if node.get("isBloodline") is True or not (
            node.get("isNotable") is True
            or node.get("isMultipleChoiceOption") is True
        ):
            continue
        passive_name = str(node.get("name") or "").strip()
        ascendancy = str(
            node.get("ascendancyName")
            or node.get("ascendancy")
            or ""
        ).strip()
        if not passive_name or not ascendancy:
            continue
        node_count += 1
        key = passive_name.casefold()
        canonical_ascendancy = ascendancy_names.get(
            ascendancy.casefold(),
            ascendancy,
        )
        metadata = {
            "passiveName": passive_name,
            "ascendancy": canonical_ascendancy,
            "ascendancyId": ascendancy,
            "baseClass": ascendancy_classes.get(ascendancy.casefold(), ""),
            "sourceNodeId": str(node.get("skill") or node_id),
            "mappingSource": GGGSkillTreeClient.SOURCE,
        }
        prior = candidates.get(key)
        if prior is None:
            candidates[key] = metadata
            continue
        if (
            prior["ascendancy"].casefold()
            != canonical_ascendancy.casefold()
            or prior["baseClass"].casefold()
            != metadata["baseClass"].casefold()
        ):
            ambiguous.add(key)

    for key in ambiguous:
        candidates.pop(key, None)
    return PassiveMap(
        passives=candidates,
        ambiguous_names=frozenset(ambiguous),
        node_count=node_count,
    )


def cached_passive_map(
    storage: Any,
    *,
    client: GGGSkillTreeClient | None = None,
) -> tuple[PassiveMap, int, str] | None:
    """Load the latest locally archived official tree without networking."""

    tree_client = client or GGGSkillTreeClient()
    endpoint = tree_client.export_url()
    snapshot = storage.latest_snapshot(
        source=GGGSkillTreeClient.SOURCE,
        endpoint=endpoint,
        league_id=None,
        category=SKILL_TREE_CATEGORY,
    )
    if snapshot is None:
        return None
    try:
        payload = json.loads(snapshot["raw"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    mapping = parse_passive_map(payload)
    if not mapping.passives:
        return None
    return mapping, int(snapshot["id"]), endpoint


def enrich_forbidden_assets(
    assets: Iterable[dict[str, Any]],
    passive_map: PassiveMap,
    *,
    mapping_snapshot_id: int | None,
    mapping_endpoint: str,
) -> dict[str, int]:
    """Attach GGG-owned class metadata to exact poe.watch variants."""

    total = 0
    mapped = 0
    ambiguous = 0
    for asset in assets:
        if str(asset.get("category") or "").casefold() != "forbiddenjewel":
            continue
        total += 1
        variant = asset.get("variant")
        if not isinstance(variant, dict):
            variant = {}
            asset["variant"] = variant
        passive_name = str(
            variant.get("passiveName") or asset.get("name") or ""
        ).strip()
        key = passive_name.casefold()
        metadata = passive_map.passives.get(key)
        if metadata is not None:
            enriched = dict(metadata)
            enriched["mappingEndpoint"] = mapping_endpoint
            if mapping_snapshot_id is not None:
                enriched["mappingSnapshotId"] = str(mapping_snapshot_id)
            variant["metadata"] = enriched
            variant["metadata_status"] = "mapped"
            mapped += 1
        elif key in passive_map.ambiguous_names:
            variant.pop("metadata", None)
            variant["metadata_status"] = "ambiguous"
            ambiguous += 1
        else:
            variant.pop("metadata", None)
            variant["metadata_status"] = "unmapped"
    return {
        "total": total,
        "mapped": mapped,
        "unmapped": total - mapped - ambiguous,
        "ambiguous": ambiguous,
    }


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _catalog_divine_chaos(assets: Iterable[dict[str, Any]]) -> float | None:
    matches = [
        asset
        for asset in assets
        if str(asset.get("name") or "").strip().casefold() == "divine orb"
        and str(asset.get("category") or "").strip().casefold() == "currency"
    ]
    if len(matches) != 1:
        return None
    return _positive_float(matches[0].get("current_chaos"))


def _price_confidence(asset: dict[str, Any]) -> float:
    if bool(asset.get("low_confidence")):
        return 0.4
    volume = _positive_float(
        asset.get("current_daily", asset.get("daily_volume"))
    )
    if volume is None:
        return 0.6
    return max(0.6, min(0.92, math.log10(volume + 1.0) / 4.0))


def forbidden_price_points(
    assets: Iterable[dict[str, Any]],
    *,
    league_id: str,
    observed_at: str,
    snapshot_id: int,
) -> list[PricePoint]:
    """Normalize exact compact-catalog variants into current observations."""

    asset_rows = list(assets)
    divine_chaos = _catalog_divine_chaos(asset_rows)
    points: list[PricePoint] = []
    for asset in asset_rows:
        if str(asset.get("category") or "").casefold() != "forbiddenjewel":
            continue
        divine_value = _positive_float(asset.get("current_divine"))
        chaos_value = _positive_float(asset.get("current_chaos"))
        if divine_value is None and chaos_value is not None and divine_chaos:
            divine_value = chaos_value / divine_chaos
        if divine_value is None:
            continue
        variant = asset.get("variant")
        details = dict(variant) if isinstance(variant, dict) else {}
        source_name = str(
            details.get("source_name")
            or asset.get("name")
            or ""
        ).strip()
        if not source_name:
            continue
        details.update(
            {
                "source_item_id": str(asset.get("source_item_id") or ""),
                "priceSource": PoeWatchClient.SOURCE,
                "exactVariant": True,
            }
        )
        points.append(
            PricePoint(
                league_id=league_id,
                item_key=str(asset["item_key"]),
                name=source_name,
                category=FORBIDDEN_CATEGORY,
                source=PoeWatchClient.SOURCE,
                observed_at=observed_at,
                chaos_value=chaos_value,
                divine_value=divine_value,
                listing_count=None,
                volume=_positive_float(asset.get("current_daily")),
                confidence=_price_confidence(asset),
                details=details,
                snapshot_id=snapshot_id,
            )
        )
    return points
