from __future__ import annotations

import gzip
import json
import math
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .clients import ClientConfig, Transport
from .models import DataSourceError, FetchResult, League, iso_utc, parse_datetime
from .storage import Storage


LADDER_SOURCE = "ggg-public-ladder"
LADDER_SOURCE_LABEL = "Official Path of Exile top-ladder sample"
LADDER_SAMPLE_CAVEAT = (
    "A sample of the public experience ladder, not the full player population."
)
POE_NINJA_META_SOURCE = "poe.ninja-builds"
POE_NINJA_META_SOURCE_LABEL = "poe.ninja indexed-build composition"
POE_NINJA_META_CAVEAT = (
    "poe.ninja indexed public characters, not a census of every active player."
)
META_SOURCE_PREFERENCE = (POE_NINJA_META_SOURCE, LADDER_SOURCE)

POE1_CLASS_NAMES = {
    "Ascendant",
    "Assassin",
    "Berserker",
    "Champion",
    "Chieftain",
    "Deadeye",
    "Duelist",
    "Elementalist",
    "Gladiator",
    "Guardian",
    "Hierophant",
    "Inquisitor",
    "Juggernaut",
    "Luminary",
    "Marauder",
    "Necromancer",
    "Occultist",
    "Pathfinder",
    "Raider",
    "Ranger",
    "Reliquarian",
    "Saboteur",
    "Scion",
    "Shadow",
    "Slayer",
    "Templar",
    "Trickster",
    "Warden",
    "Witch",
}


class _PoeNinjaBuildParser(HTMLParser):
    """Extract the server-rendered class summary without scraping build rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.class_shares: dict[str, float] = {}
        self.time_options: list[dict[str, str]] = []
        self.all_text: list[str] = []
        self._capture_kind: str | None = None
        self._capture_depth = 0
        self._capture_text: list[str] = []
        self._pending_class: str | None = None
        self._in_time_select = False
        self._option_value: str | None = None
        self._option_text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {str(name): str(value or "") for name, value in attrs}
        classes = set(attributes.get("class", "").split())

        if tag == "select" and attributes.get("id") == "Time machine":
            self._in_time_select = True
        elif self._in_time_select and tag == "option":
            self._option_value = attributes.get("value", "")
            self._option_text = []

        if self._capture_kind is not None:
            self._capture_depth += 1
        elif tag == "div" and "class-name" in classes:
            self._capture_kind = "class"
            self._capture_depth = 1
            self._capture_text = []
        elif tag == "div" and "class-percentage" in classes:
            self._capture_kind = "percentage"
            self._capture_depth = 1
            self._capture_text = []

    def handle_data(self, data: str) -> None:
        text = unescape(str(data))
        if text.strip():
            self.all_text.append(text)
        if self._capture_kind is not None:
            self._capture_text.append(text)
        if self._option_value is not None:
            self._option_text.append(text)

    def handle_endtag(self, tag: str) -> None:
        if self._capture_kind is not None:
            self._capture_depth -= 1
            if self._capture_depth == 0:
                value = " ".join(self._capture_text).strip()
                if self._capture_kind == "class":
                    self._pending_class = value
                elif (
                    self._capture_kind == "percentage"
                    and self._pending_class in POE1_CLASS_NAMES
                ):
                    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*%", value)
                    if match:
                        self.class_shares[self._pending_class] = (
                            float(match.group(1)) / 100.0
                        )
                    self._pending_class = None
                self._capture_kind = None
                self._capture_text = []

        if self._in_time_select and tag == "option":
            if self._option_value is not None:
                self.time_options.append(
                    {
                        "value": self._option_value,
                        "label": " ".join(self._option_text).strip(),
                    }
                )
            self._option_value = None
            self._option_text = []
        elif self._in_time_select and tag == "select":
            self._in_time_select = False


def parse_poe_ninja_build_meta(html: bytes | str) -> dict[str, Any]:
    """Parse poe.ninja's visible build-composition summary."""

    if isinstance(html, bytes):
        text = html.decode("utf-8", errors="replace")
    else:
        text = str(html)
    parser = _PoeNinjaBuildParser()
    parser.feed(text)
    parser.close()
    joined_text = " ".join(parser.all_text)
    sample_match = re.search(
        r"\bFound\s+([0-9][0-9,]*)\s+characters?\b",
        joined_text,
        flags=re.IGNORECASE,
    )
    if sample_match is None:
        raise DataSourceError(
            "poe.ninja's build page did not expose its indexed character count"
        )
    sample_size = int(sample_match.group(1).replace(",", ""))
    if sample_size <= 0 or not parser.class_shares:
        raise DataSourceError(
            "poe.ninja's build page did not expose a usable class distribution"
        )
    return {
        "sample_size": sample_size,
        "class_shares": dict(
            sorted(
                parser.class_shares.items(),
                key=lambda pair: pair[0].casefold(),
            )
        ),
        "time_options": parser.time_options,
    }


def poe_ninja_time_machine_day(value: str) -> int | None:
    """Translate poe.ninja's day/week/hour snapshot labels to league day."""

    normalized = str(value or "").strip().lower()
    match = re.fullmatch(r"day-(\d+)", normalized)
    if match:
        return max(1, int(match.group(1)))
    match = re.fullmatch(r"week-(\d+)", normalized)
    if match:
        return max(1, int(match.group(1)) * 7)
    match = re.fullmatch(r"hour-(\d+)", normalized)
    if match:
        return 1
    return None


def nearest_poe_ninja_time_machine(
    options: Iterable[Mapping[str, Any]],
    target_league_day: int,
) -> tuple[str | None, int | None]:
    """Return poe.ninja's closest available historical snapshot."""

    target = max(1, int(target_league_day))
    candidates: list[tuple[int, bool, int, str]] = []
    for option in options:
        value = str(option.get("value") or "").strip()
        day = poe_ninja_time_machine_day(value)
        if not value or day is None:
            continue
        candidates.append((abs(day - target), day > target, day, value))
    if not candidates:
        return None, None
    _, _, day, value = min(candidates)
    return value, day


def parse_embedded_ladder_json(html: bytes | str) -> dict[str, Any]:
    """Extract the JSON assigned to ``var json`` on a public ladder page.

    A small brace scanner is used instead of a greedy regular expression so
    braces and escaped quotes inside character names cannot truncate the JSON.
    """

    if isinstance(html, bytes):
        text = html.decode("utf-8", errors="replace")
    else:
        text = str(html)
    assignment = re.search(r"\bvar\s+json\s*=", text)
    if assignment is None:
        raise DataSourceError(
            "The public ladder page did not contain its embedded ladder JSON"
        )
    start = text.find("{", assignment.end())
    if start < 0:
        raise DataSourceError(
            "The public ladder page contained an empty ladder JSON assignment"
        )

    depth = 0
    in_string = False
    escaped = False
    end: int | None = None
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break

    if end is None:
        raise DataSourceError(
            "The public ladder page contained incomplete embedded ladder JSON"
        )
    try:
        payload = json.loads(text[start:end])
    except json.JSONDecodeError as error:
        raise DataSourceError(
            "The public ladder page contained invalid embedded ladder JSON"
        ) from error
    if not isinstance(payload, dict):
        raise DataSourceError("The public ladder JSON was not an object")
    return payload


def ladder_entries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    ladder = payload.get("ladder", payload)
    if not isinstance(ladder, Mapping):
        raise DataSourceError("The public ladder JSON did not contain a ladder")
    entries = ladder.get("entries")
    if not isinstance(entries, list):
        raise DataSourceError(
            "The public ladder JSON did not contain an entries list"
        )
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


def class_counts(
    entries: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, int], int]:
    """Count the ascendancy/class label among unique ladder characters."""

    counts: Counter[str] = Counter()
    seen: set[tuple[str, ...]] = set()
    for entry in entries:
        character = entry.get("character")
        if not isinstance(character, Mapping):
            continue
        class_name = str(character.get("class") or "").strip()
        if not class_name:
            continue
        marker_values = (
            str(entry.get("rank") or ""),
            str(character.get("id") or ""),
            str(character.get("name") or ""),
        )
        marker = tuple(marker_values)
        # Some fixtures/sources omit identifiers. Keep those observations
        # instead of folding every anonymous row into one.
        if any(marker_values):
            if marker in seen:
                continue
            seen.add(marker)
        counts[class_name] += 1
    normalized = dict(sorted(counts.items(), key=lambda pair: pair[0].casefold()))
    return normalized, sum(normalized.values())


class PublicLadderClient:
    """Conservative HTML client for GGG's public ladder pages."""

    SOURCE = LADDER_SOURCE

    def __init__(
        self,
        config: ClientConfig | None = None,
        *,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        base_url: str = "https://www.pathofexile.com",
    ):
        self.config = config or ClientConfig.from_environment()
        self.transport = transport or self._default_transport
        self.sleeper = sleeper
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _default_transport(request: Request, timeout: float) -> Any:
        return urlopen(request, timeout=timeout)

    def ladder_url(self, league: str, page: int = 1) -> str:
        league_path = quote(str(league).strip(), safe="")
        query = urlencode({"page": max(1, int(page))})
        return f"{self.base_url}/ladders/league/{league_path}?{query}"

    def fetch_page(self, league: str, page: int = 1) -> FetchResult:
        url = self.ladder_url(league, page)
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip",
            "User-Agent": self.config.user_agent,
        }
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            request = Request(url, headers=headers, method="GET")
            try:
                response = self.transport(request, self.config.timeout_seconds)
                try:
                    raw_status = getattr(response, "status", None)
                    status = int(
                        raw_status
                        if raw_status is not None
                        else response.getcode()
                    )
                    raw = response.read()
                    response_headers = {
                        str(key).lower(): str(value)
                        for key, value in response.headers.items()
                    }
                finally:
                    close = getattr(response, "close", None)
                    if close:
                        close()
                if response_headers.get("content-encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return FetchResult(
                    url=url,
                    status=status,
                    payload=None,
                    raw=raw,
                    etag=response_headers.get("etag"),
                    last_modified=response_headers.get("last-modified"),
                    fetched_at=iso_utc(),
                    headers=response_headers,
                )
            except HTTPError as error:
                last_error = error
                retryable = error.code == 429 or 500 <= error.code < 600
                if retryable and attempt < self.config.max_retries:
                    self.sleeper(self._retry_delay(error, attempt))
                    continue
                try:
                    body = error.read(300).decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                raise DataSourceError(
                    f"GET {url} returned HTTP {error.code}"
                    + (f": {body}" if body else "")
                ) from error
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt < self.config.max_retries:
                    self.sleeper(
                        min(
                            float(2**attempt),
                            self.config.max_retry_after_seconds,
                        )
                    )
                    continue
                raise DataSourceError(f"GET {url} failed: {error}") from error
        raise DataSourceError(f"GET {url} failed: {last_error}")

    def _retry_delay(self, error: HTTPError, attempt: int) -> float:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = float(2**attempt)
        return min(max(0.0, delay), self.config.max_retry_after_seconds)


class PoeNinjaBuildClient:
    """Fetch poe.ninja's server-rendered PoE 1 build summaries."""

    SOURCE = POE_NINJA_META_SOURCE

    def __init__(
        self,
        config: ClientConfig | None = None,
        *,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        base_url: str = "https://poe.ninja",
    ):
        self.config = config or ClientConfig.from_environment()
        self.transport = transport or self._default_transport
        self.sleeper = sleeper
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _default_transport(request: Request, timeout: float) -> Any:
        return urlopen(request, timeout=timeout)

    @staticmethod
    def league_slug(league: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "", str(league).strip().lower())
        if not slug:
            raise ValueError("A poe.ninja league slug is required")
        return slug

    def build_url(
        self,
        league: str,
        *,
        timemachine: str | None = None,
    ) -> str:
        slug = quote(self.league_slug(league), safe="")
        url = f"{self.base_url}/poe1/builds/{slug}"
        if timemachine:
            url += "?" + urlencode({"timemachine": str(timemachine)})
        return url

    def fetch_page(
        self,
        league: str,
        *,
        timemachine: str | None = None,
    ) -> FetchResult:
        url = self.build_url(league, timemachine=timemachine)
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": self.config.user_agent,
        }
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            request = Request(url, headers=headers, method="GET")
            try:
                response = self.transport(request, self.config.timeout_seconds)
                try:
                    raw_status = getattr(response, "status", None)
                    status = int(
                        raw_status
                        if raw_status is not None
                        else response.getcode()
                    )
                    raw = response.read()
                    response_headers = {
                        str(key).lower(): str(value)
                        for key, value in response.headers.items()
                    }
                finally:
                    close = getattr(response, "close", None)
                    if close:
                        close()
                if response_headers.get("content-encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return FetchResult(
                    url=url,
                    status=status,
                    payload=None,
                    raw=raw,
                    etag=response_headers.get("etag"),
                    last_modified=response_headers.get("last-modified"),
                    fetched_at=iso_utc(),
                    headers=response_headers,
                )
            except HTTPError as error:
                last_error = error
                retryable = error.code == 429 or 500 <= error.code < 600
                if retryable and attempt < self.config.max_retries:
                    self.sleeper(self._retry_delay(error, attempt))
                    continue
                try:
                    body = error.read(300).decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                raise DataSourceError(
                    f"GET {url} returned HTTP {error.code}"
                    + (f": {body}" if body else "")
                ) from error
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt < self.config.max_retries:
                    self.sleeper(
                        min(
                            float(2**attempt),
                            self.config.max_retry_after_seconds,
                        )
                    )
                    continue
                raise DataSourceError(f"GET {url} failed: {error}") from error
        raise DataSourceError(f"GET {url} failed: {last_error}")

    def _retry_delay(self, error: HTTPError, attempt: int) -> float:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = float(2**attempt)
        return min(max(0.0, delay), self.config.max_retry_after_seconds)


_DEFAULT_FALLBACK_CLIENT = object()


class MetaService:
    """Build and query a local class-popularity archive."""

    def __init__(
        self,
        storage: Storage,
        client: Any | None = None,
        *,
        fallback_client: Any = _DEFAULT_FALLBACK_CLIENT,
        sleeper: Callable[[float], None] = time.sleep,
        page_delay_seconds: float = 0.1,
        now: Callable[[], datetime] | None = None,
    ):
        self.storage = storage
        if client is None:
            self.client = PoeNinjaBuildClient()
            self.fallback_client = (
                PublicLadderClient()
                if fallback_client is _DEFAULT_FALLBACK_CLIENT
                else fallback_client
            )
        else:
            self.client = client
            self.fallback_client = (
                None
                if fallback_client is _DEFAULT_FALLBACK_CLIENT
                else fallback_client
            )
        self.sleeper = sleeper
        self.page_delay_seconds = max(0.0, float(page_delay_seconds))
        self.now = now or (lambda: datetime.now(timezone.utc))

    def sync_league(
        self,
        league: Any,
        *,
        pages: int = 10,
        force: bool = False,
        freshness_hours: float = 12.0,
        target_league_day: int | None = None,
    ) -> dict[str, Any]:
        normalized_league = self._coerce_league(league)
        normalized_league = self._ensure_league(normalized_league)
        pages = max(1, min(100, int(pages)))
        clients = [self.client]
        if self.fallback_client is not None:
            clients.append(self.fallback_client)
        errors: list[dict[str, str]] = []

        for candidate in clients:
            source = str(getattr(candidate, "SOURCE", "")).strip()
            if not source:
                errors.append(
                    {
                        "source": "unknown",
                        "error": "Meta client did not declare a source",
                    }
                )
                continue
            latest = self.storage.latest_meta_class_snapshot(
                normalized_league.id,
                source=source,
            )
            if (
                not force
                and latest is not None
                and self._is_fresh(latest.get("observed_at"), freshness_hours)
            ):
                result = self._cached_summary(
                    latest,
                    pages_requested=pages,
                )
                if errors:
                    result["fallback_errors"] = errors
                return result

            try:
                if source == POE_NINJA_META_SOURCE:
                    result = self._sync_poe_ninja_league(
                        normalized_league,
                        candidate,
                        target_league_day=target_league_day,
                        pages_requested=pages,
                    )
                else:
                    result = self._sync_ladder_league(
                        normalized_league,
                        candidate,
                        pages=pages,
                    )
            except Exception as error:
                errors.append({"source": source, "error": str(error)})
                continue
            if errors:
                result["fallback_errors"] = errors
            return result

        detail = "; ".join(
            f"{item['source']}: {item['error']}" for item in errors
        )
        raise DataSourceError(
            f"No class-composition source succeeded for "
            f"{normalized_league.id}"
            + (f" ({detail})" if detail else "")
        )

    def _sync_poe_ninja_league(
        self,
        league: League,
        client: Any,
        *,
        target_league_day: int | None,
        pages_requested: int,
    ) -> dict[str, Any]:
        sampled_league_day = (
            max(1, int(target_league_day))
            if target_league_day is not None
            else self._sampled_league_day(league)
        )
        ended = self._league_has_ended(league)
        base = client.fetch_page(league.id, timemachine=None)
        base_profile = parse_poe_ninja_build_meta(base.raw)
        snapshot_ids: list[int] = []
        snapshots_written = 0

        base_snapshot_id, created = self.storage.add_snapshot(
            source=client.SOURCE,
            endpoint=base.url,
            league_id=league.id,
            category="class-popularity",
            fetched_at=base.fetched_at,
            status_code=base.status,
            raw=base.raw,
            etag=base.etag,
            last_modified=base.last_modified,
            metadata={
                "league_day": sampled_league_day,
                "sample_kind": "catalog",
                "method": "poe-ninja-build-overview-html",
                "population_bias": "poe.ninja-indexed-public-builds",
            },
        )
        snapshot_ids.append(base_snapshot_id)
        snapshots_written += int(created)

        selected_value: str | None = None
        selected_day: int | None = None
        profile = base_profile
        profile_result = base
        if ended and target_league_day is not None:
            selected_value, selected_day = nearest_poe_ninja_time_machine(
                base_profile.get("time_options", []),
                sampled_league_day,
            )
            if selected_value:
                profile_result = client.fetch_page(
                    league.id,
                    timemachine=selected_value,
                )
                profile = parse_poe_ninja_build_meta(profile_result.raw)
                profile_snapshot_id, created = self.storage.add_snapshot(
                    source=client.SOURCE,
                    endpoint=profile_result.url,
                    league_id=league.id,
                    category="class-popularity",
                    fetched_at=profile_result.fetched_at,
                    status_code=profile_result.status,
                    raw=profile_result.raw,
                    etag=profile_result.etag,
                    last_modified=profile_result.last_modified,
                    metadata={
                        "league_day": selected_day,
                        "requested_league_day": sampled_league_day,
                        "sample_kind": "historical",
                        "timemachine": selected_value,
                        "method": "poe-ninja-build-overview-html",
                        "population_bias": "poe.ninja-indexed-public-builds",
                    },
                )
                snapshot_ids.append(profile_snapshot_id)
                snapshots_written += int(created)

        stored = self.storage.save_meta_class_snapshot(
            league_id=league.id,
            observed_at=iso_utc(self._aware_now()),
            source=client.SOURCE,
            league_day=selected_day or sampled_league_day,
            class_counts={},
            class_shares=dict(profile["class_shares"]),
            sample_size=int(profile["sample_size"]),
            page_count=1,
            snapshot_ids=snapshot_ids,
        )
        summary = self._stored_summary(
            stored,
            status="ok",
            pages_requested=pages_requested,
            snapshots_written=snapshots_written,
        )
        summary["timemachine"] = selected_value
        summary["requested_league_day"] = sampled_league_day
        summary["endpoint"] = profile_result.url
        return summary

    def _sync_ladder_league(
        self,
        league: League,
        client: Any,
        *,
        pages: int,
    ) -> dict[str, Any]:
        observed_at = iso_utc(self._aware_now())
        sampled_league_day = self._sampled_league_day(league)
        final_ladder = self._league_has_ended(league)
        all_entries: list[dict[str, Any]] = []
        snapshot_ids: list[int] = []
        snapshots_written = 0
        pages_sampled = 0
        for page in range(1, pages + 1):
            result = client.fetch_page(league.id, page)
            snapshot_id, created = self.storage.add_snapshot(
                source=client.SOURCE,
                endpoint=result.url,
                league_id=league.id,
                category="class-popularity",
                fetched_at=result.fetched_at,
                status_code=result.status,
                raw=result.raw,
                etag=result.etag,
                last_modified=result.last_modified,
                metadata={
                    "page": page,
                    "league_day": sampled_league_day,
                    "sample_kind": "final" if final_ladder else "current",
                    "method": "public-top-ladder-html",
                    "population_bias": "top-experience-ladder",
                },
            )
            snapshot_ids.append(snapshot_id)
            snapshots_written += int(created)

            payload = parse_embedded_ladder_json(result.raw)
            page_entries = ladder_entries(payload)
            pages_sampled += 1
            if not page_entries:
                break
            all_entries.extend(page_entries)
            if page < pages and self.page_delay_seconds:
                self.sleeper(self.page_delay_seconds)

        counts, sample_size = class_counts(all_entries)
        if sample_size <= 0:
            raise DataSourceError(
                f"The public ladder returned no class observations for "
                f"{league.id}"
            )
        stored = self.storage.save_meta_class_snapshot(
            league_id=league.id,
            observed_at=observed_at,
            source=client.SOURCE,
            league_day=sampled_league_day,
            class_counts=counts,
            sample_size=sample_size,
            page_count=pages_sampled,
            snapshot_ids=snapshot_ids,
        )
        return self._stored_summary(
            stored,
            status="ok",
            pages_requested=pages,
            snapshots_written=snapshots_written,
        )

    def sync_leagues(
        self,
        leagues: Iterable[Any],
        *,
        pages: int = 10,
        force: bool = False,
        freshness_hours: float = 12.0,
        target_league_day: int | None = None,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for league in leagues:
            try:
                result = self.sync_league(
                    league,
                    pages=pages,
                    force=force,
                    freshness_hours=freshness_hours,
                    target_league_day=target_league_day,
                )
            except Exception as error:
                try:
                    league_id = self._coerce_league(league).id
                except (TypeError, ValueError):
                    league_id = str(league)
                result = {
                    "status": "failed",
                    "league_id": league_id,
                    "source": self.client.SOURCE,
                    "source_label": self._source_details(
                        self.client.SOURCE
                    )[0],
                    "error": str(error),
                }
            league_id = str(result.get("league_id") or "")
            result_source = str(
                result.get("source") or self.client.SOURCE
            )
            try:
                endpoint = str(
                    result.get("endpoint")
                    or self._source_endpoint(result_source, league_id)
                )
                if result.get("status") == "failed":
                    self.storage.update_source_state(
                        source=result_source,
                        endpoint=endpoint,
                        league_id=league_id,
                        category="class-popularity",
                        status="error",
                        detail=str(
                            result.get("error") or "Meta sample failed"
                        ),
                        success=False,
                    )
                else:
                    noun = (
                        "poe.ninja indexed-build profile"
                        if result_source == POE_NINJA_META_SOURCE
                        else "Top-ladder sample"
                    )
                    self.storage.update_source_state(
                        source=result_source,
                        endpoint=endpoint,
                        league_id=league_id,
                        category="class-popularity",
                        status="ok",
                        detail=(
                            f"{noun}: "
                            f"{int(result.get('sample_size') or 0)} "
                            f"characters on league day "
                            f"{int(result.get('league_day') or 1)}."
                        ),
                        success=True,
                    )
            except Exception:
                # Source-state reporting is diagnostic only; a successfully
                # stored meta profile must remain usable if this row cannot be
                # updated.
                pass
            for fallback_error in result.get("fallback_errors", []):
                error_source = str(fallback_error.get("source") or "")
                if not error_source:
                    continue
                try:
                    self.storage.update_source_state(
                        source=error_source,
                        endpoint=self._source_endpoint(
                            error_source,
                            league_id,
                        ),
                        league_id=league_id,
                        category="class-popularity",
                        status="error",
                        detail=str(
                            fallback_error.get("error")
                            or "Preferred meta source failed"
                        ),
                        success=False,
                    )
                except Exception:
                    pass
            results.append(result)

        summary_sources = sorted(
            {
                str(result.get("source"))
                for result in results
                if result.get("source")
            }
        )
        primary_label, primary_caveat = self._source_details(
            self.client.SOURCE
        )
        return {
            "source": self.client.SOURCE,
            "source_label": primary_label,
            "caveat": primary_caveat,
            "sources_used": summary_sources,
            "requested_leagues": len(results),
            "synced_leagues": sum(
                result.get("status") == "ok" for result in results
            ),
            "cached_leagues": sum(
                result.get("status") == "cached" for result in results
            ),
            "failed_leagues": sum(
                result.get("status") == "failed" for result in results
            ),
            "total_sample_size": sum(
                int(result.get("sample_size") or 0) for result in results
            ),
            "snapshots_written": sum(
                int(result.get("snapshots_written") or 0) for result in results
            ),
            "leagues": results,
        }

    def ascendancy_multiplier(
        self,
        current_league: Any,
        historical_leagues: Iterable[Any],
        ascendancy: str,
        *,
        elasticity: float = 0.35,
        smoothing_share: float = 0.005,
        minimum: float = 0.75,
        maximum: float = 1.35,
    ) -> dict[str, Any]:
        """Return a conservative demand multiplier for a meta-linked item."""

        current_id = self._coerce_league(current_league).id
        historical_ids = list(
            dict.fromkeys(
                self._coerce_league(league).id for league in historical_leagues
            )
        )
        historical_ids = [
            league_id for league_id in historical_ids if league_id != current_id
        ]
        ascendancy = str(ascendancy).strip()
        signal_source = self._select_comparable_source(
            current_id,
            historical_ids,
        )
        source_label, source_caveat = self._source_details(signal_source)
        base_result: dict[str, Any] = {
            "status": "unavailable",
            "ascendancy": ascendancy,
            "current_league_id": current_id,
            "current_share": None,
            "current_sample_size": 0,
            "historical_share": None,
            "historical_sample_size": 0,
            "historical_league_count": 0,
            "historical_leagues": [],
            "target_league_day": None,
            "baseline_quality": "unavailable",
            "baseline_mean_day_distance": None,
            "alignment_confidence": 0.0,
            "raw_relative_popularity": None,
            "multiplier": 1.0,
            "confidence": 0.0,
            "source": signal_source,
            "source_label": source_label,
            "caveat": source_caveat,
        }
        if not ascendancy:
            return base_result

        current = self.storage.latest_meta_class_snapshot(
            current_id, source=signal_source
        )
        if current is None or int(current.get("sample_size") or 0) <= 0:
            return base_result

        target_league_day = max(1, int(current.get("league_day") or 1))
        profiles: list[dict[str, Any]] = []
        for league_id in historical_ids:
            snapshot = self.storage.nearest_meta_class_snapshot(
                league_id,
                target_league_day,
                source=signal_source,
            )
            if snapshot is None or int(snapshot.get("sample_size") or 0) <= 0:
                continue
            sampled_day = max(1, int(snapshot.get("league_day") or 1))
            distance = abs(sampled_day - target_league_day)
            profiles.append(
                {
                    "league_id": league_id,
                    "share": self._share(snapshot, ascendancy),
                    "sample_size": int(snapshot["sample_size"]),
                    "observed_at": snapshot["observed_at"],
                    "league_day": sampled_day,
                    "target_league_day": target_league_day,
                    "day_distance": distance,
                    "alignment": (
                        "exact"
                        if distance == 0
                        else "near"
                        if distance <= 3
                        else "fallback"
                    ),
                }
            )
        if not profiles:
            return base_result

        current_share = self._share(current, ascendancy)
        historical_share = sum(
            float(profile["share"]) for profile in profiles
        ) / len(profiles)
        historical_sample_size = sum(
            int(profile["sample_size"]) for profile in profiles
        )
        smoothing_share = max(0.0001, float(smoothing_share))
        raw_relative = (current_share + smoothing_share) / (
            historical_share + smoothing_share
        )
        elasticity = max(0.0, min(1.0, float(elasticity)))
        minimum = max(0.01, float(minimum))
        maximum = max(minimum, float(maximum))
        bounded = max(minimum, min(maximum, raw_relative**elasticity))

        current_sample_size = int(current["sample_size"])
        average_historical_sample = historical_sample_size / len(profiles)
        distances = [int(profile["day_distance"]) for profile in profiles]
        mean_day_distance = sum(distances) / len(distances)
        if all(distance == 0 for distance in distances):
            baseline_quality = "same-day"
            alignment_confidence = 1.0
        elif max(distances) <= 3:
            baseline_quality = "near-day"
            alignment_confidence = 0.9
        else:
            baseline_quality = "fallback"
            # End-of-league profiles are useful directional evidence, but
            # they are not equivalent to a same-day baseline. Shrink their
            # impact while keeping a modest signal until daily local profiles
            # accumulate across future leagues.
            alignment_confidence = max(
                0.35,
                1.0 / (1.0 + mean_day_distance / 60.0),
            )
        confidence = min(
            1.0,
            math.sqrt(current_sample_size / 500.0),
            math.sqrt(average_historical_sample / 500.0),
            len(profiles) / 3.0,
        ) * alignment_confidence
        # Small samples still indicate direction, but their adjustment is
        # shrunk toward neutral rather than treated as population truth.
        multiplier = 1.0 + ((bounded - 1.0) * confidence)
        multiplier = max(minimum, min(maximum, multiplier))

        return {
            **base_result,
            "status": "ok",
            "current_share": current_share,
            "current_sample_size": current_sample_size,
            "historical_share": historical_share,
            "historical_sample_size": historical_sample_size,
            "historical_league_count": len(profiles),
            "historical_leagues": profiles,
            "target_league_day": target_league_day,
            "baseline_quality": baseline_quality,
            "baseline_mean_day_distance": mean_day_distance,
            "alignment_confidence": alignment_confidence,
            "raw_relative_popularity": raw_relative,
            "multiplier": multiplier,
            "confidence": confidence,
        }

    def latest_profile(self, league_id: str) -> dict[str, Any] | None:
        """Return the newest profile from the highest-priority local source."""

        for source in self._candidate_sources():
            profile = self.storage.latest_meta_class_snapshot(
                str(league_id),
                source=source,
            )
            if profile is not None and int(profile.get("sample_size") or 0) > 0:
                return profile
        return None

    def _candidate_sources(self) -> list[str]:
        sources: list[str] = []
        for candidate in (self.client, self.fallback_client):
            source = str(getattr(candidate, "SOURCE", "") or "").strip()
            if source and source not in sources:
                sources.append(source)
        return sources or [POE_NINJA_META_SOURCE, LADDER_SOURCE]

    def _select_comparable_source(
        self,
        current_league_id: str,
        historical_league_ids: Iterable[str],
    ) -> str:
        historical_ids = [str(value) for value in historical_league_ids]
        for source in self._candidate_sources():
            current = self.storage.latest_meta_class_snapshot(
                current_league_id,
                source=source,
            )
            if current is None or int(current.get("sample_size") or 0) <= 0:
                continue
            if any(
                self.storage.latest_meta_class_snapshot(
                    league_id,
                    source=source,
                )
                is not None
                for league_id in historical_ids
            ):
                return source
        for source in self._candidate_sources():
            current = self.storage.latest_meta_class_snapshot(
                current_league_id,
                source=source,
            )
            if current is not None and int(current.get("sample_size") or 0) > 0:
                return source
        return self._candidate_sources()[0]

    def _source_endpoint(self, source: str, league_id: str) -> str:
        for candidate in (self.client, self.fallback_client):
            if str(getattr(candidate, "SOURCE", "")) != str(source):
                continue
            if source == POE_NINJA_META_SOURCE:
                return str(candidate.build_url(league_id))
            return str(candidate.ladder_url(league_id, 1))
        return str(source)

    @staticmethod
    def _source_details(source: str) -> tuple[str, str]:
        if str(source) == POE_NINJA_META_SOURCE:
            return POE_NINJA_META_SOURCE_LABEL, POE_NINJA_META_CAVEAT
        if str(source) == LADDER_SOURCE:
            return LADDER_SOURCE_LABEL, LADDER_SAMPLE_CAVEAT
        label = str(source or "Class-composition source")
        return label, "A source-specific player sample, not a full census."

    def _ensure_league(self, league: League) -> League:
        stored = self.storage.get_league(league.id)
        if stored is None:
            self.storage.upsert_league(league, current=False)
            return league
        return League(
            id=stored.id,
            name=stored.name or league.name,
            start_at=stored.start_at or league.start_at,
            end_at=stored.end_at or league.end_at,
            realm=stored.realm,
            is_hardcore=stored.is_hardcore,
            is_ssf=stored.is_ssf,
            is_demo=stored.is_demo,
        )

    def _is_fresh(self, observed_at: Any, freshness_hours: float) -> bool:
        parsed = parse_datetime(str(observed_at or ""))
        if parsed is None:
            return False
        age_seconds = (self._aware_now() - parsed).total_seconds()
        return age_seconds <= max(0.0, float(freshness_hours)) * 3600.0

    def _aware_now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _sampled_league_day(self, league: League) -> int:
        start = parse_datetime(league.start_at)
        if start is None:
            return 1
        reference = self._aware_now()
        end = parse_datetime(league.end_at)
        if end is not None and end <= start:
            # Some upstream league payloads use year 0001 as an open-ended
            # sentinel. It is not a real league end.
            end = None
        # A ladder crawled after a league ended reflects its final standings,
        # not the calendar day on which the HTML happened to be downloaded.
        if end is not None and end < reference:
            reference = end
        elapsed = (reference - start).total_seconds()
        return max(1, math.floor(elapsed / 86400.0) + 1)

    def _league_has_ended(self, league: League) -> bool:
        start = parse_datetime(league.start_at)
        end = parse_datetime(league.end_at)
        return (
            end is not None
            and (start is None or end > start)
            and end < self._aware_now()
        )

    @staticmethod
    def _coerce_league(value: Any) -> League:
        if isinstance(value, League):
            return value
        if isinstance(value, str):
            league_id = value.strip()
            if not league_id:
                raise ValueError("League ID cannot be empty")
            return League(id=league_id, name=league_id)
        if isinstance(value, Mapping):
            league_id = str(value.get("id") or value.get("league_id") or "").strip()
            name = str(value.get("name") or league_id).strip()
            start_at = value.get("start_at")
            end_at = value.get("end_at")
        else:
            league_id = str(
                getattr(value, "id", None)
                or getattr(value, "league_id", None)
                or ""
            ).strip()
            name = str(getattr(value, "name", None) or league_id).strip()
            start_at = getattr(value, "start_at", None)
            end_at = getattr(value, "end_at", None)
        if not league_id:
            raise ValueError("League ID is required")
        return League(
            id=league_id,
            name=name or league_id,
            start_at=str(start_at) if start_at else None,
            end_at=str(end_at) if end_at else None,
        )

    @staticmethod
    def _share(snapshot: Mapping[str, Any], class_name: str) -> float:
        shares = snapshot.get("class_shares")
        if not isinstance(shares, Mapping):
            return 0.0
        target = class_name.casefold()
        for name, share in shares.items():
            if str(name).casefold() != target:
                continue
            try:
                return max(0.0, float(share))
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _stored_summary(
        self,
        stored: Mapping[str, Any],
        *,
        status: str,
        pages_requested: int,
        snapshots_written: int,
    ) -> dict[str, Any]:
        source_label, caveat = self._source_details(str(stored["source"]))
        return {
            "status": status,
            "league_id": stored["league_id"],
            "observed_at": stored["observed_at"],
            "league_day": int(stored["league_day"]),
            "sample_size": int(stored["sample_size"]),
            "page_count": int(stored["page_count"]),
            "pages_requested": int(pages_requested),
            "class_counts": dict(stored["class_counts"]),
            "class_shares": dict(stored["class_shares"]),
            "snapshots_written": int(snapshots_written),
            "source": stored["source"],
            "source_label": source_label,
            "caveat": caveat,
        }

    def _cached_summary(
        self,
        stored: Mapping[str, Any],
        *,
        pages_requested: int,
    ) -> dict[str, Any]:
        return self._stored_summary(
            stored,
            status="cached",
            pages_requested=pages_requested,
            snapshots_written=0,
        )
