from __future__ import annotations

import math
import re
from typing import Any

from .models import PricePoint


_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    # Trade API details IDs normally omit apostrophes rather than replacing
    # them with separators (for example, "Valdo's" -> "valdos").
    value = value.lower().replace("'", "").replace("’", "")
    return _NON_SLUG.sub("-", value).strip("-")


def canonical_key(name: str, category: str, details_id: str | None = None) -> str:
    category_slug = slugify(category)
    if category_slug in {
        "currency",
        "currency-exchange",
        "fragment",
        "fragments",
    }:
        family = "currency"
    else:
        family = category_slug
    return f"{family}:{slugify(details_id or name)}"


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _metadata_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    candidates: list[Any] = []
    core = payload.get("core")
    if isinstance(core, dict):
        candidates.append(core.get("items"))
    candidates.extend([payload.get("items"), payload.get("currencyDetails")])
    result: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key, value in candidate.items():
                if isinstance(value, dict):
                    result[str(key)] = value
        elif isinstance(candidate, list):
            for value in candidate:
                if not isinstance(value, dict):
                    continue
                for key in ("id", "detailsId", "tradeId"):
                    if value.get(key) is not None:
                        result[str(value[key])] = value
    return result


def _reference_id(payload: dict[str, Any]) -> str | None:
    core = payload.get("core")
    if not isinstance(core, dict):
        return None
    primary = core.get("primary")
    if isinstance(primary, str):
        return primary
    if isinstance(primary, dict):
        for key in ("id", "detailsId", "tradeId"):
            if primary.get(key):
                return str(primary[key])
    return None


def _find_reference_value(
    lines: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    target_name: str,
) -> float | None:
    target = slugify(target_name)
    for line in lines:
        line_id = str(line.get("id", line.get("detailsId", "")))
        meta = metadata.get(line_id, {})
        name = str(
            line.get("currencyTypeName")
            or line.get("name")
            or meta.get("name")
            or line_id
        )
        if slugify(name) == target or slugify(line_id) in {
            target,
            target.removesuffix("-orb"),
        }:
            return _finite_positive(
                line.get("primaryValue")
                or line.get("chaosEquivalent")
                or line.get("chaosValue")
            )
    return None


def _core_rate(payload: dict[str, Any], target: str) -> float | None:
    core = payload.get("core")
    if not isinstance(core, dict) or not isinstance(core.get("rates"), dict):
        return None
    target_slug = slugify(target)
    for identifier, value in core["rates"].items():
        if slugify(str(identifier)) in {
            target_slug,
            target_slug.removesuffix("-orb"),
        }:
            return _finite_positive(value)
    return None


def _confidence(listings: int | None, volume: float | None) -> float:
    if listings is not None:
        return max(0.2, min(0.98, math.log10(max(1, listings) + 1) / 3.0))
    if volume is not None:
        return max(0.25, min(0.95, math.log10(max(1.0, volume) + 1) / 4.0))
    return 0.45


def normalize_poe_ninja(
    payload: Any,
    *,
    league_id: str,
    category: str,
    observed_at: str,
    snapshot_id: int,
) -> list[PricePoint]:
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
        raise ValueError("poe.ninja response must contain a lines array")
    raw_lines = [line for line in payload["lines"] if isinstance(line, dict)]
    metadata = _metadata_map(payload)
    reference = slugify(_reference_id(payload) or "")
    divine_in_primary = (
        1.0
        if reference in {"divine", "divine-orb"}
        else None
    )
    if divine_in_primary is None:
        divine_rate = _core_rate(payload, "Divine Orb")
        if divine_rate:
            divine_in_primary = 1.0 / divine_rate
    if divine_in_primary is None:
        divine_in_primary = _find_reference_value(
            raw_lines, metadata, "Divine Orb"
        )
    chaos_in_primary = (
        1.0
        if reference in {"chaos", "chaos-orb"}
        else None
    )
    if chaos_in_primary is None:
        chaos_rate = _core_rate(payload, "Chaos Orb")
        if chaos_rate:
            chaos_in_primary = 1.0 / chaos_rate
    if chaos_in_primary is None:
        chaos_in_primary = _find_reference_value(raw_lines, metadata, "Chaos Orb")

    points: list[PricePoint] = []
    for line in raw_lines:
        line_id = str(line.get("id", line.get("detailsId", "")))
        meta = metadata.get(line_id, {})
        name = str(
            line.get("currencyTypeName")
            or line.get("name")
            or meta.get("name")
            or line_id.replace("-", " ").title()
        ).strip()
        if not name:
            continue
        details_id = str(
            line.get("detailsId") or meta.get("detailsId") or line_id or name
        )
        direct_divine = _finite_positive(line.get("divineValue"))
        direct_chaos = _finite_positive(
            line.get("chaosValue") or line.get("chaosEquivalent")
        )
        primary_value = _finite_positive(line.get("primaryValue"))
        divine_value = direct_divine
        if divine_value is None and primary_value and divine_in_primary:
            divine_value = primary_value / divine_in_primary
        if divine_value is None and direct_chaos and divine_in_primary:
            divine_value = direct_chaos / divine_in_primary
        if divine_value is None:
            continue
        chaos_value = direct_chaos
        if chaos_value is None and primary_value and chaos_in_primary:
            chaos_value = primary_value / chaos_in_primary
        listings_raw = line.get("listingCount")
        if listings_raw is None:
            pay = line.get("pay")
            receive = line.get("receive")
            listing_candidates = [
                side.get("listing_count")
                for side in (pay, receive)
                if isinstance(side, dict) and side.get("listing_count") is not None
            ]
            listings_raw = max(listing_candidates) if listing_candidates else None
        try:
            listings = int(listings_raw) if listings_raw is not None else None
        except (TypeError, ValueError):
            listings = None
        volume_raw = line.get("volumePrimaryValue") or line.get("count")
        if volume_raw is None and isinstance(line.get("receive"), dict):
            volume_raw = line["receive"].get("count")
        volume = _finite_positive(volume_raw)
        item_category = str(line.get("category") or category)
        details = {
            key: line[key]
            for key in (
                "variant",
                "corrupted",
                "links",
                "gemLevel",
                "gemQuality",
                "mapTier",
                "baseType",
                "icon",
            )
            if line.get(key) is not None
        }
        line_metadata = line.get("metadata")
        if not isinstance(line_metadata, dict):
            line_metadata = meta.get("metadata")
        if isinstance(line_metadata, dict):
            # Forbidden Jewel class/ascendancy/passive information is used by
            # the local meta-demand model. Keep the source object intact so
            # future fields remain available without a schema migration.
            details["metadata"] = dict(line_metadata)
        sparkline = line.get("sparkline") or line.get("sparkLine")
        if isinstance(sparkline, dict):
            # These are source-provided relative trend samples, not timestamped
            # observations. Preserve them as metadata but never manufacture
            # historical price rows from them.
            details["relative_trend_samples"] = sparkline.get("data")
            details["relative_total_change_pct"] = sparkline.get("totalChange")
        variant_parts = [
            f"{key}={line[key]}"
            for key in (
                "variant",
                "corrupted",
                "links",
                "gemLevel",
                "gemQuality",
                "mapTier",
            )
            if line.get(key) is not None
        ]
        identity = details_id
        if variant_parts:
            identity += "-" + "-".join(variant_parts)
        point = PricePoint(
            league_id=league_id,
            item_key=canonical_key(name, item_category, identity),
            name=name,
            category=item_category,
            source="poe.ninja",
            observed_at=observed_at,
            chaos_value=chaos_value,
            divine_value=divine_value,
            listing_count=listings,
            volume=volume,
            confidence=_confidence(listings, volume),
            details=details,
            snapshot_id=snapshot_id,
        )
        points.append(point)
    return points


_CURRENCY_NAMES = {
    "chaos": "Chaos Orb",
    "divine": "Divine Orb",
    "exalted": "Exalted Orb",
    "annul": "Orb of Annulment",
    "annulment": "Orb of Annulment",
    "vaal": "Vaal Orb",
    "alch": "Orb of Alchemy",
    "alchemy": "Orb of Alchemy",
    "fusing": "Orb of Fusing",
    "chromatic": "Chromatic Orb",
    "jeweller": "Jeweller's Orb",
    "regret": "Orb of Regret",
    "scouring": "Orb of Scouring",
    "alteration": "Orb of Alteration",
    "augmentation": "Orb of Augmentation",
    "transmutation": "Orb of Transmutation",
    "chance": "Orb of Chance",
    "mirror": "Mirror of Kalandra",
}


def _humanize_identifier(identifier: str) -> str:
    basename = identifier.rsplit("/", 1)[-1]
    basename = re.sub(
        r"^(Currency|DivinationCard|Scarab|Essence|Fossil|Oil|Fragment)",
        "",
        basename,
    )
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", basename)
    spaced = re.sub(r"[_-]+", " ", spaced).strip()
    return spaced or identifier.rsplit("/", 1)[-1]


def _identifier_info(identifier: str) -> tuple[str, str, str, bool]:
    value = identifier.rsplit("/", 1)[-1].lower()
    exact_items = {
        "currencygemquality": (
            "gemcutters-prism",
            "Gemcutter's Prism",
            "Currency",
        ),
        "currencyupgradetorare": (
            "orb-of-alchemy",
            "Orb of Alchemy",
            "Currency",
        ),
        "currencyvaldopuzzlebox": (
            "valdos-puzzle-box",
            "Valdo's Puzzle Box",
            "Fragment",
        ),
        "currencyeldritchrerollrare": (
            "eldritch-chaos-orb",
            "Eldritch Chaos Orb",
            "Currency",
        ),
        "currencyeldritchaddmodtorare": (
            "eldritch-exalted-orb",
            "Eldritch Exalted Orb",
            "Currency",
        ),
        "currencyeldritchannul": (
            "eldritch-orb-of-annulment",
            "Eldritch Orb of Annulment",
            "Currency",
        ),
        "currencydelvecraftingcold": (
            "frigid-fossil",
            "Frigid Fossil",
            "Fossil",
        ),
        "currencydelvecraftingfire": (
            "scorched-fossil",
            "Scorched Fossil",
            "Fossil",
        ),
        "currencydelvecraftingphysical": (
            "jagged-fossil",
            "Jagged Fossil",
            "Fossil",
        ),
        "currencydelvecraftinglightning": (
            "metallic-fossil",
            "Metallic Fossil",
            "Fossil",
        ),
        "currencydelvecraftingchaos": (
            "aberrant-fossil",
            "Aberrant Fossil",
            "Fossil",
        ),
        "currencydelvecraftingcaster": (
            "aetheric-fossil",
            "Aetheric Fossil",
            "Fossil",
        ),
        "currencydelvecraftingspeed": (
            "shuddering-fossil",
            "Shuddering Fossil",
            "Fossil",
        ),
        "currencydelvecraftinglife": (
            "pristine-fossil",
            "Pristine Fossil",
            "Fossil",
        ),
        "currencydelvecraftingdefences": (
            "dense-fossil",
            "Dense Fossil",
            "Fossil",
        ),
        "currencydelvecraftingattack": (
            "serrated-fossil",
            "Serrated Fossil",
            "Fossil",
        ),
        "currencydelvecraftingelemental": (
            "prismatic-fossil",
            "Prismatic Fossil",
            "Fossil",
        ),
        "currencydelvecraftingminion": (
            "bound-fossil",
            "Bound Fossil",
            "Fossil",
        ),
        "currencydelvecraftingmana": (
            "lucent-fossil",
            "Lucent Fossil",
            "Fossil",
        ),
        "currencydelvecraftingabyss": (
            "hollow-fossil",
            "Hollow Fossil",
            "Fossil",
        ),
        "currencydelvecraftingbleedpoison": (
            "corroded-fossil",
            "Corroded Fossil",
            "Fossil",
        ),
        "currencydelvecraftinggemlevel": (
            "faceted-fossil",
            "Faceted Fossil",
            "Fossil",
        ),
        "currencydelvecraftingminionsauras": (
            "bound-fossil",
            "Bound Fossil",
            "Fossil",
        ),
        "currencydelvecraftingquality": (
            "perfect-fossil",
            "Perfect Fossil",
            "Fossil",
        ),
        "currencydelvecraftingluckymodrolls": (
            "sanctified-fossil",
            "Sanctified Fossil",
            "Fossil",
        ),
        "currencydelvecraftingsockets": (
            "encrusted-fossil",
            "Encrusted Fossil",
            "Fossil",
        ),
        "currencydelvecraftingattackmods": (
            "serrated-fossil",
            "Serrated Fossil",
            "Fossil",
        ),
        "currencydelvecraftingenchant": (
            "enchanted-fossil",
            "Enchanted Fossil",
            "Fossil",
        ),
        "currencydelvecraftingmirror": (
            "fractured-fossil",
            "Fractured Fossil",
            "Fossil",
        ),
        "currencydelvecraftingcorruptessence": (
            "glyphic-fossil",
            "Glyphic Fossil",
            "Fossil",
        ),
        "currencydelvecraftingcastermods": (
            "aetheric-fossil",
            "Aetheric Fossil",
            "Fossil",
        ),
        "currencydelvecraftingsellprice": (
            "gilded-fossil",
            "Gilded Fossil",
            "Fossil",
        ),
        "currencydelvecraftingvaal": (
            "bloodstained-fossil",
            "Bloodstained Fossil",
            "Fossil",
        ),
        "currencydelvecraftingrandom": (
            "tangled-fossil",
            "Tangled Fossil",
            "Fossil",
        ),
        "currencylegionfragmenteternal": (
            "timeless-eternal-emblem",
            "Timeless Eternal Emblem",
            "Fragment",
        ),
        "currencylegionfragmentvaal": (
            "timeless-vaal-emblem",
            "Timeless Vaal Emblem",
            "Fragment",
        ),
        "currencylegionfragmentkarui": (
            "timeless-karui-emblem",
            "Timeless Karui Emblem",
            "Fragment",
        ),
        "currencylegionfragmentmaraketh": (
            "timeless-maraketh-emblem",
            "Timeless Maraketh Emblem",
            "Fragment",
        ),
        "currencylegionfragmenttemplar": (
            "timeless-templar-emblem",
            "Timeless Templar Emblem",
            "Fragment",
        ),
    }
    if value in exact_items:
        code, name, category = exact_items[value]
        return code, name, category, True

    essence_match = re.fullmatch(r"currencyessence([a-z]+)([1-7])", value)
    if essence_match:
        family = essence_match.group(1)
        if family in {"delirium", "horror", "hysteria", "insanity"}:
            name = f"Essence of {family.title()}"
        else:
            tier = {
                "1": "Whispering",
                "2": "Muttering",
                "3": "Weeping",
                "4": "Wailing",
                "5": "Screaming",
                "6": "Shrieking",
                "7": "Deafening",
            }[essence_match.group(2)]
            name = f"{tier} Essence of {family.title()}"
        return slugify(name), name, "Essence", True

    eldritch_match = re.fullmatch(r"currencyeldritch(ichor|ember)([1234])", value)
    if eldritch_match:
        tier = {
            "1": "Lesser",
            "2": "Greater",
            "3": "Grand",
            "4": "Exceptional",
        }[eldritch_match.group(2)]
        kind = eldritch_match.group(1).title()
        name = f"{tier} Eldritch {kind}"
        return slugify(name), name, "Currency", True

    aliases = {
        "currencyrerollrare": "chaos",
        "currencychaos": "chaos",
        "currencymodvalues": "divine",
        "currencydivine": "divine",
        "currencyaddmodtorare": "exalted",
        "currencyexalted": "exalted",
        "currencyremovemod": "annulment",
        "currencyannulment": "annulment",
        "currencyvaal": "vaal",
        "currencyupgradelevel2": "alchemy",
        "currencyalchemy": "alchemy",
        "currencyrerollsocketlinks": "fusing",
        "currencyfusing": "fusing",
        "currencyrerollsocketcolours": "chromatic",
        "currencychromatic": "chromatic",
        "currencyrerollsocketnumbers": "jeweller",
        "currencyjeweller": "jeweller",
        "currencypassiveskillrefund": "regret",
        "currencyregret": "regret",
        "currencydestroyrare": "scouring",
        "currencyscouring": "scouring",
        "currencyrerollmagic": "alteration",
        "currencyalteration": "alteration",
        "currencyaddmodtomagic": "augmentation",
        "currencyupgradelevel1": "transmutation",
        "currencyupgradelevel3": "chance",
        "currencymirror": "mirror",
    }
    code = aliases.get(value, slugify(value.removeprefix("currency")))
    if code in _CURRENCY_NAMES:
        return code, _CURRENCY_NAMES[code], "Currency", True
    lower_path = identifier.lower()
    category_paths = (
        ("divinationcard", "DivinationCard"),
        ("scarab", "Scarab"),
        ("essence", "Essence"),
        ("fossil", "Fossil"),
        ("resonator", "Resonator"),
        ("oil", "Oil"),
        ("fragment", "Fragment"),
        ("delirium", "DeliriumOrb"),
        ("tattoo", "Tattoo"),
        ("omen", "Omen"),
    )
    category = "Currency"
    for needle, label in category_paths:
        if needle in lower_path:
            category = label
            break
    name = _humanize_identifier(identifier)
    # Humanising identifiers is reliable for self-describing families such as
    # divination cards and scarabs. Unknown generic currency identifiers can
    # be internal implementation names, however, so flag them as unresolved
    # and keep them out of allocations until an exact mapping is known.
    return code, name, category, category != "Currency"


def _currency_name(code: str) -> str:
    return _CURRENCY_NAMES.get(code, code.replace("-", " ").title())


def _ratio_value(
    ratio: Any, item_identifier: str, divine_identifier: str
) -> float | None:
    if not isinstance(ratio, dict):
        return None
    divine_amount = _finite_positive(ratio.get(divine_identifier))
    item_amount = _finite_positive(ratio.get(item_identifier))
    if divine_amount and item_amount:
        return divine_amount / item_amount
    return None


def normalize_ggg_markets(
    payload: Any,
    *,
    league_id: str,
    league_name: str,
    observed_at: str,
    snapshot_id: int,
) -> list[PricePoint]:
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise ValueError("GGG currency exchange response must contain markets")
    markets = [market for market in payload["markets"] if isinstance(market, dict)]
    filtered: list[
        tuple[dict[str, Any], list[str], list[tuple[str, str, str, bool]]]
    ] = []
    for market in markets:
        market_league = str(market.get("league", ""))
        if market_league and market_league not in {league_id, league_name}:
            continue
        identifiers = market.get("market_pair")
        if not isinstance(identifiers, list) or len(identifiers) != 2:
            identifiers = str(market.get("market_id", "")).split("|")
        if len(identifiers) != 2:
            continue
        identifiers = [str(identifier) for identifier in identifiers]
        filtered.append(
            (
                market,
                identifiers,
                [_identifier_info(identifier) for identifier in identifiers],
            )
        )

    divine_per_chaos: float | None = None
    for market, identifiers, info in filtered:
        codes = [part[0] for part in info]
        if set(codes) != {"chaos", "divine"}:
            continue
        chaos_index = codes.index("chaos")
        divine_index = codes.index("divine")
        values = [
            value
            for value in (
                _ratio_value(
                    market.get("lowest_ratio"),
                    identifiers[chaos_index],
                    identifiers[divine_index],
                ),
                _ratio_value(
                    market.get("highest_ratio"),
                    identifiers[chaos_index],
                    identifiers[divine_index],
                ),
            )
            if value is not None
        ]
        if values:
            divine_per_chaos = sum(values) / len(values)
            break

    points: list[PricePoint] = []
    for market, identifiers, info in filtered:
        codes = [part[0] for part in info]
        reference_indexes = [
            index for index, code in enumerate(codes) if code in {"chaos", "divine"}
        ]
        if not reference_indexes:
            continue
        # Prefer Divine as the quote when present, otherwise use Chaos.
        quote_index = (
            codes.index("divine") if "divine" in codes else codes.index("chaos")
        )
        item_index = 1 - quote_index
        item = codes[item_index]
        item_identifier = identifiers[item_index]
        quote_identifier = identifiers[quote_index]
        values = [
            value
            for value in (
                _ratio_value(
                    market.get("lowest_ratio"), item_identifier, quote_identifier
                ),
                _ratio_value(
                    market.get("highest_ratio"), item_identifier, quote_identifier
                ),
            )
            if value is not None
        ]
        if not values:
            continue
        quote_per_item = sum(values) / len(values)
        if codes[quote_index] == "divine":
            divine_value = quote_per_item
            chaos_value = (
                divine_value / divine_per_chaos if divine_per_chaos else None
            )
        else:
            if divine_per_chaos is None:
                continue
            chaos_value = quote_per_item
            divine_value = quote_per_item * divine_per_chaos
        volume_map = market.get("volume_traded")
        volume = (
            _finite_positive(volume_map.get(item_identifier))
            if isinstance(volume_map, dict)
            else None
        )
        name = info[item_index][1] or _currency_name(item)
        category = info[item_index][2]
        points.append(
            PricePoint(
                league_id=league_id,
                # Name-based identities align official hourly markets with
                # poe.ninja details IDs, allowing both sources to contribute
                # to one local history while retaining the raw identifier
                # below for auditability.
                item_key=canonical_key(name, category, name),
                name=name,
                category=category,
                source="ggg-currency-exchange",
                observed_at=observed_at,
                chaos_value=chaos_value,
                divine_value=divine_value,
                listing_count=None,
                volume=volume,
                confidence=_confidence(None, volume),
                details={
                    "market_id": market.get("market_id"),
                    "item_identifier": item_identifier,
                    "lowest_ratio": market.get("lowest_ratio"),
                    "highest_ratio": market.get("highest_ratio"),
                    "official_hourly_digest": True,
                    "identifier_resolved": info[item_index][3],
                },
                snapshot_id=snapshot_id,
            )
        )
    return points
