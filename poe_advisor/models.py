from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


STANDARD_LEAGUE_ID = "Standard"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(slots=True)
class League:
    id: str
    name: str
    start_at: str | None = None
    end_at: str | None = None
    realm: str = "pc"
    is_hardcore: bool = False
    is_ssf: bool = False
    is_demo: bool = False

    @property
    def day(self) -> int | None:
        start = parse_datetime(self.start_at)
        if start is None:
            return None
        # League day 1 is the first 24-hour window after launch.  poe.ninja's
        # first midnight economy bucket is only a few hours after a typical
        # evening launch, so it must remain on day 1 rather than becoming day
        # 2 merely because the UTC calendar date changed.
        elapsed_days = int((utc_now() - start).total_seconds() // 86_400)
        return max(1, elapsed_days + 1)


@dataclass(slots=True)
class PricePoint:
    league_id: str
    item_key: str
    name: str
    category: str
    source: str
    observed_at: str
    chaos_value: float | None
    divine_value: float
    listing_count: int | None = None
    volume: float | None = None
    confidence: float = 0.5
    details: dict[str, Any] = field(default_factory=dict)
    snapshot_id: int | None = None


@dataclass(slots=True)
class FetchResult:
    url: str
    status: int
    payload: Any | None
    raw: bytes
    etag: str | None
    last_modified: str | None
    fetched_at: str
    headers: dict[str, str] = field(default_factory=dict)
    not_modified: bool = False


class AdvisorError(RuntimeError):
    """Base error for expected advisor failures."""


class DataSourceError(AdvisorError):
    """A remote data source returned an unusable response."""


class ConfigurationError(AdvisorError):
    """A required local setting is missing or invalid."""
