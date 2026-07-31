from __future__ import annotations

from collections.abc import Iterable
from typing import Any


POE_NINJA_CURRENT_SOURCE = "poe.ninja"
POE_NINJA_HISTORY_SOURCE = "poe.ninja-history"
POE_NINJA_STANDARD_STATE_SOURCE = "poe.ninja-standard"

# Recommendation prices are deliberately fail-closed. Other providers may be
# archived for diagnostics, but they cannot enter a production price curve or
# forecast unless this policy is changed explicitly.
GOLDEN_PRICE_PROVIDER = "poe.ninja"
PRICE_PROVENANCE_POLICY = "poe-ninja-golden-v1"
CURRENT_PRICE_SOURCES = (POE_NINJA_CURRENT_SOURCE,)
HISTORICAL_PRICE_SOURCES = (POE_NINJA_HISTORY_SOURCE,)
STANDARD_PRICE_SOURCES = (POE_NINJA_CURRENT_SOURCE,)


def normalize_source_filter(
    sources: Iterable[str] | str | None,
) -> tuple[str, ...] | None:
    """Return a deterministic source allowlist while preserving ``None``.

    ``None`` means that a low-level storage caller deliberately wants every
    archived provider. An empty iterable means no provider is allowed and must
    therefore produce an empty result, rather than silently disabling the
    filter.
    """

    if sources is None:
        return None
    values = (sources,) if isinstance(sources, str) else sources
    return tuple(
        dict.fromkeys(
            source
            for value in values
            if (source := str(value).strip())
        )
    )


def production_price_provenance() -> dict[str, Any]:
    """Describe the enforced provider boundary in public payloads."""

    return {
        "policy": PRICE_PROVENANCE_POLICY,
        "golden_provider": GOLDEN_PRICE_PROVIDER,
        "fail_closed": True,
        "current_price_sources": list(CURRENT_PRICE_SOURCES),
        "historical_price_sources": list(HISTORICAL_PRICE_SOURCES),
        "standard_price_sources": list(STANDARD_PRICE_SOURCES),
        "source_labels": {
            POE_NINJA_CURRENT_SOURCE: "poe.ninja current economy API",
            POE_NINJA_HISTORY_SOURCE: (
                "poe.ninja official completed-league archive"
            ),
            POE_NINJA_STANDARD_STATE_SOURCE: (
                "Standard sync state label (prices use poe.ninja)"
            ),
        },
    }


def has_production_price_provenance(payload: dict[str, Any]) -> bool:
    """Return whether a recommendation payload declares the exact policy."""

    return payload.get("price_provenance") == production_price_provenance()
