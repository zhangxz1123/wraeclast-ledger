from __future__ import annotations

import csv
import hashlib
import json
import math
import tempfile
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from .clients import ClientConfig, HttpJsonClient
from .historical import BROADLY_COVERED_LEAGUE_IDS
from .models import DataSourceError, FetchResult, League, iso_utc
from .normalization import canonical_key, slugify
from .provenance import POE_NINJA_CURRENT_SOURCE, POE_NINJA_HISTORY_SOURCE


DUMP_IMPORT_VERSION = 4
DUMP_SETTING_PREFIX = "poe_ninja_dump:"
CHAOS_ORB_NAME = "Chaos Orb"
DIVINE_ORB_NAME = "Divine Orb"
MIN_DIVINE_CHAOS = 5.0
MAX_DIVINE_CHAOS = 5_000.0


@dataclass(frozen=True, slots=True)
class DumpDescriptor:
    league_name: str
    min_date: str
    max_date: str
    zip_name: str

    @property
    def start_date(self) -> date:
        return _parse_iso_date(self.min_date)

    @property
    def end_date(self) -> date:
        return _parse_iso_date(self.max_date)

    @property
    def fingerprint(self) -> dict[str, Any]:
        return {
            "import_version": DUMP_IMPORT_VERSION,
            "league_name": self.league_name,
            "min_date": self.min_date,
            "max_date": self.max_date,
            "zip_name": self.zip_name,
        }


@dataclass(frozen=True, slots=True)
class DownloadReceipt:
    url: str
    status: int
    bytes_written: int
    sha256: str
    etag: str | None = None
    last_modified: str | None = None


def parse_dump_catalog(payload: Any) -> list[DumpDescriptor]:
    """Parse poe.ninja's completed-league dump catalog.

    The production endpoint currently returns a bare JSON array.  Accepting a
    ``dumps`` wrapper as well makes the parser tolerant of an innocuous API
    envelope change without weakening field validation.
    """

    rows = payload.get("dumps") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("poe.ninja dump catalog must be an array")
    descriptors: list[DumpDescriptor] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("poe.ninja dump catalog rows must be objects")
        values = {
            key: str(row.get(key) or "").strip()
            for key in ("leagueName", "minDate", "maxDate", "zipName")
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValueError(
                "poe.ninja dump catalog row is missing " + ", ".join(missing)
            )
        descriptor = DumpDescriptor(
            league_name=values["leagueName"],
            min_date=values["minDate"],
            max_date=values["maxDate"],
            zip_name=values["zipName"],
        )
        if descriptor.start_date > descriptor.end_date:
            raise ValueError(
                f"{descriptor.league_name} dump has an inverted date range"
            )
        if descriptor.league_name in seen:
            raise ValueError(
                f"duplicate poe.ninja dump league {descriptor.league_name!r}"
            )
        seen.add(descriptor.league_name)
        descriptors.append(descriptor)
    return descriptors


class PoeNinjaDumpClient:
    """Streaming client for poe.ninja's completed-league dump API."""

    SOURCE = POE_NINJA_HISTORY_SOURCE

    def __init__(
        self,
        http: HttpJsonClient | None = None,
        *,
        base_url: str = "https://poe.ninja",
        opener: Callable[..., Any] | None = None,
        config: ClientConfig | None = None,
        chunk_size: int = 1024 * 1024,
    ):
        self.config = config or ClientConfig.from_environment()
        self.http = http or HttpJsonClient(self.config)
        self.base_url = base_url.rstrip("/")
        self.opener = opener or urlopen
        self.chunk_size = max(64 * 1024, int(chunk_size))

    def catalog_url(self) -> str:
        return f"{self.base_url}/poe1/api/data/dumps"

    def dump_url(self, league_name: str) -> str:
        query = urlencode({"name": str(league_name).strip()})
        return f"{self.base_url}/poe1/api/data/dumps/dump?{query}"

    def fetch_catalog(self) -> FetchResult:
        return self.http.get_json(
            self.catalog_url(),
            extra_headers={"Referer": f"{self.base_url}/data"},
        )

    def download_dump(
        self, league_name: str, destination: str | Path
    ) -> DownloadReceipt:
        """Download a dump incrementally to a seekable local ZIP file."""

        url = self.dump_url(league_name)
        request = Request(
            url,
            headers={
                "Accept": "application/zip, application/octet-stream",
                "Accept-Encoding": "identity",
                "User-Agent": self.config.user_agent,
                "Referer": f"{self.base_url}/data",
            },
            method="GET",
        )
        try:
            response = self.opener(
                request,
                timeout=self.config.timeout_seconds,
            )
        except HTTPError as error:
            raise DataSourceError(
                f"GET {url} returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise DataSourceError(f"GET {url} failed: {error}") from error

        target = Path(destination)
        digest = hashlib.sha256()
        size = 0
        try:
            status = int(getattr(response, "status", response.getcode()))
            headers = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
            }
            with target.open("wb") as output:
                while True:
                    chunk = response.read(self.chunk_size)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as error:
            raise DataSourceError(
                f"Could not persist poe.ninja dump {league_name}: {error}"
            ) from error
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        if status < 200 or status >= 300 or size == 0:
            raise DataSourceError(
                f"GET {url} returned unusable dump status={status}, bytes={size}"
            )
        return DownloadReceipt(
            url=url,
            status=status,
            bytes_written=size,
            sha256=digest.hexdigest(),
            etag=headers.get("etag"),
            last_modified=headers.get("last-modified"),
        )


class PoeNinjaHistoryImporter:
    """Import exact-identity completed-league prices from poe.ninja dumps."""

    SOURCE = POE_NINJA_HISTORY_SOURCE

    def __init__(
        self,
        storage: Any,
        client: PoeNinjaDumpClient | None = None,
        *,
        batch_size: int = 50_000,
        temporary_directory: str | Path | None = None,
    ):
        self.storage = storage
        self.client = client or PoeNinjaDumpClient()
        self.batch_size = max(100, int(batch_size))
        self.temporary_directory = (
            Path(temporary_directory) if temporary_directory else None
        )

    def sync(
        self,
        league_names: Iterable[str] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        catalog = self.client.fetch_catalog()
        descriptors = parse_dump_catalog(catalog.payload)
        selected = None
        if league_names is not None:
            selected = {
                str(name).strip().casefold()
                for name in league_names
                if str(name).strip()
            }
            descriptors = [
                descriptor
                for descriptor in descriptors
                if descriptor.league_name.casefold() in selected
            ]
            found = {descriptor.league_name.casefold() for descriptor in descriptors}
            missing = sorted(selected - found)
            if missing:
                raise ValueError(
                    "poe.ninja dump catalog is missing requested leagues: "
                    + ", ".join(missing)
                )

        summary: dict[str, Any] = {
            "status": "success",
            "catalog_url": catalog.url,
            "catalog_count": len(descriptors),
            "dumps_imported": 0,
            "dumps_skipped": 0,
            "historical_assets_written": 0,
            "seasonal_rows_written": 0,
            "currency_rows_seen": 0,
            "currency_rows_matched": 0,
            "item_rows_seen": 0,
            "item_rows_matched": 0,
            "item_rows_archived_only": 0,
            "matched_item_categories": {},
            "unmatched_item_rows": 0,
            "identity_mismatch_rows": 0,
            "missing_anchor_rows": 0,
            "raw_source_rows_seen": 0,
            "normalized_source_rows": 0,
            "eligible_source_rows": 0,
            "storage_mode": (
                "compact"
                if bool(getattr(self.storage, "compact_history_mode", False))
                else "full"
            ),
            "errors": [],
            "dumps": [],
        }
        for descriptor in descriptors:
            try:
                result = self.sync_dump(descriptor, force=force)
            except Exception as error:
                summary["status"] = "partial"
                summary["errors"].append(
                    f"{descriptor.league_name}: {error}"
                )
                continue
            summary["dumps"].append(result)
            if result["status"] == "skipped":
                summary["dumps_skipped"] += 1
                continue
            summary["dumps_imported"] += 1
            for key in (
                "historical_assets_written",
                "seasonal_rows_written",
                "currency_rows_seen",
                "currency_rows_matched",
                "item_rows_seen",
                "item_rows_matched",
                "item_rows_archived_only",
                "unmatched_item_rows",
                "identity_mismatch_rows",
                "missing_anchor_rows",
                "raw_source_rows_seen",
                "normalized_source_rows",
                "eligible_source_rows",
            ):
                summary[key] += int(result.get(key, 0))
            categories = result.get("matched_item_categories", {})
            if isinstance(categories, dict):
                for category, count in categories.items():
                    summary["matched_item_categories"][str(category)] = (
                        int(summary["matched_item_categories"].get(category, 0))
                        + int(count)
                    )
        summary["archive_assets_remapped"] = (
            self.remap_archive_only_current_identities()
        )
        self._refresh_marker_row_counts(descriptors)
        return summary

    def _refresh_marker_row_counts(
        self,
        descriptors: Iterable[DumpDescriptor],
    ) -> None:
        for descriptor in descriptors:
            key = f"{DUMP_SETTING_PREFIX}{descriptor.league_name}"
            marker = self.storage.get_setting(key, {})
            if not _marker_matches(marker, descriptor):
                continue
            counts = self.storage.seasonal_price_storage_counts(
                descriptor.league_name,
                source=self.SOURCE,
            )
            marker = dict(marker)
            marker["seasonal_rows_written"] = int(counts["effective"])
            marker["stored_seasonal_rows"] = int(counts["effective"])
            marker["storage_mode"] = str(counts["storage_mode"])
            self.storage.set_setting(key, marker)

    def remap_archive_only_current_identities(self) -> int:
        """Promote archived rows when their exact current identity appears.

        Completed ZIPs are immutable, but a new league's current catalog fills
        in over time. Keeping every unmatched row lets us repair its local
        crosswalk without downloading the ZIP again.
        """

        identities, current_by_key = self._current_identities()
        visible: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for current in current_by_key.values():
            visible.setdefault(
                (
                    _identity_text(current.get("category")),
                    _identity_text(current.get("name")),
                ),
                [],
            ).append(current)
        with closing(self.storage.connect()) as connection:
            raw_assets = connection.execute(
                """
                SELECT source_item_id, item_key, name, category,
                       source_category, source_group, variant_json,
                       current_daily, current_chaos, current_divine,
                       low_confidence
                FROM historical_assets
                WHERE source = ? AND eligible = 0
                ORDER BY source_item_id
                """,
                (self.SOURCE,),
            ).fetchall()
        remapped = 0
        for raw_asset in raw_assets:
            asset = dict(raw_asset)
            try:
                variant = json.loads(str(asset.get("variant_json") or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(variant, dict) or not variant.get("archive_only"):
                continue
            dump_row = {
                "Id": variant.get("dump_id"),
                "Type": asset.get("source_category") or asset.get("category"),
                "Name": asset.get("name"),
                "BaseType": variant.get("baseType"),
                "Variant": variant.get("variant"),
                "Links": variant.get("links"),
            }
            dump_id = str(variant.get("dump_id") or "").strip()
            current = identities.get(dump_id)
            if current is not None and not _identity_matches(dump_row, current):
                current = None
            if current is None:
                candidates = visible.get(
                    (
                        _identity_text(dump_row["Type"]),
                        _identity_text(dump_row["Name"]),
                    ),
                    [],
                )
                candidates = [
                    candidate
                    for candidate in candidates
                    if _identity_matches(
                        dump_row,
                        candidate,
                        strict_visible=True,
                    )
                ]
                if len(candidates) != 1:
                    continue
                current = candidates[0]
            source_item_id = str(asset["source_item_id"])
            old_item_key = str(asset["item_key"])
            new_item_key = str(current["item_key"])
            with closing(self.storage.connect()) as connection:
                price_rows = connection.execute(
                    """
                    SELECT league_id, league_day, observed_at, chaos_value,
                           divine_value, volume, confidence, snapshot_id,
                           details_json, updated_at
                    FROM seasonal_prices
                    WHERE source = ? AND source_item_id = ?
                    ORDER BY league_id, league_day
                    """,
                    (self.SOURCE, source_item_id),
                ).fetchall()
            if not price_rows:
                # Hosted compact mode intentionally omits archive-only rows.
                # Do not mark an identity eligible when there is no retained
                # curve to promote.
                continue
            normalized_prices: list[dict[str, Any]] = []
            for raw_price in price_rows:
                price = dict(raw_price)
                try:
                    details = json.loads(str(price.pop("details_json") or "{}"))
                except json.JSONDecodeError:
                    details = {}
                if not isinstance(details, dict):
                    details = {}
                details.update(
                    {
                        "archive_only": False,
                        "identity_match": "remapped-from-retained-dump-row",
                        "current_item_key": new_item_key,
                    }
                )
                normalized_prices.append(
                    {
                        **price,
                        "item_key": new_item_key,
                        "source": self.SOURCE,
                        "source_item_id": source_item_id,
                        "details": details,
                    }
                )
            with self.storage.transaction() as connection:
                if normalized_prices:
                    connection.executemany(
                        """
                        INSERT INTO seasonal_prices(
                            league_id, item_key, source, source_item_id,
                            league_day, observed_at, chaos_value, divine_value,
                            volume, confidence, snapshot_id, details_json,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(league_id, item_key, source, league_day)
                        DO UPDATE SET
                            source_item_id = excluded.source_item_id,
                            observed_at = excluded.observed_at,
                            chaos_value = excluded.chaos_value,
                            divine_value = excluded.divine_value,
                            volume = excluded.volume,
                            confidence = excluded.confidence,
                            snapshot_id = excluded.snapshot_id,
                            details_json = excluded.details_json,
                            updated_at = excluded.updated_at
                        WHERE excluded.confidence >= seasonal_prices.confidence
                        """,
                        [
                            (
                                price["league_id"],
                                price["item_key"],
                                price["source"],
                                price["source_item_id"],
                                int(price["league_day"]),
                                price["observed_at"],
                                price.get("chaos_value"),
                                float(price["divine_value"]),
                                price.get("volume"),
                                float(price.get("confidence", 0.5)),
                                price.get("snapshot_id"),
                                json.dumps(
                                    price.get("details") or {},
                                    separators=(",", ":"),
                                ),
                                price.get("updated_at") or iso_utc(),
                            )
                            for price in normalized_prices
                        ],
                    )
                if old_item_key != new_item_key:
                    connection.execute(
                        """
                        DELETE FROM seasonal_prices
                        WHERE source = ? AND source_item_id = ? AND item_key = ?
                        """,
                        (self.SOURCE, source_item_id, old_item_key),
                    )
                connection.execute(
                    """
                    UPDATE historical_assets
                    SET item_key = ?, name = ?, category = ?,
                        variant_json = ?, current_daily = ?,
                        current_chaos = ?, current_divine = ?,
                        eligible = 1, seen_at = ?
                    WHERE source = ? AND source_item_id = ?
                    """,
                    (
                        new_item_key,
                        str(current["name"]),
                        str(current["category"]),
                        json.dumps(
                            dict(current.get("details") or {}),
                            separators=(",", ":"),
                        ),
                        current.get("volume"),
                        current.get("chaos_value"),
                        current.get("divine_value"),
                        iso_utc(),
                        self.SOURCE,
                        source_item_id,
                    ),
                )
            remapped += 1
        return remapped

    def sync_dump(
        self, descriptor: DumpDescriptor, *, force: bool = False
    ) -> dict[str, Any]:
        endpoint = self.client.dump_url(descriptor.league_name)
        if not force and self._already_imported(descriptor, endpoint):
            return {
                "status": "skipped",
                "league_id": descriptor.league_name,
                "zip_name": descriptor.zip_name,
            }

        staging_source = (
            f"{self.SOURCE}-staging-v{DUMP_IMPORT_VERSION}:"
            f"{slugify(descriptor.league_name)}"
        )
        # A killed process can leave only quarantined staging rows. Clear them
        # before retrying; production rows are replaced only after the entire
        # ZIP has parsed and passed its coverage checks.
        self._clear_staging(descriptor.league_name, staging_source)
        self.storage.update_source_state(
            source=self.SOURCE,
            endpoint=endpoint,
            league_id=descriptor.league_name,
            category="archive",
            status="syncing",
            detail=json.dumps(descriptor.fingerprint, separators=(",", ":")),
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="poe-ninja-dump-",
                dir=self.temporary_directory,
            ) as directory:
                archive = Path(directory) / descriptor.zip_name
                receipt = self.client.download_dump(
                    descriptor.league_name, archive
                )
                result = self.import_archive(
                    descriptor,
                    archive,
                    source=staging_source,
                )
                self._validate_complete_import(descriptor, result)
                production_rows = self._promote_staging(
                    descriptor.league_name,
                    staging_source,
                )
                result["production_rows"] = production_rows
        except Exception as error:
            self._clear_staging(descriptor.league_name, staging_source)
            self.storage.update_source_state(
                source=self.SOURCE,
                endpoint=endpoint,
                league_id=descriptor.league_name,
                category="archive",
                status="error",
                detail=str(error),
            )
            raise

        marker = {
            **descriptor.fingerprint,
            "status": "success",
            "sha256": receipt.sha256,
            "download_bytes": receipt.bytes_written,
            "seasonal_rows_written": result["production_rows"],
            "stored_seasonal_rows": result["production_rows"],
            "raw_source_rows_seen": int(result["raw_source_rows_seen"]),
            "normalized_source_rows": int(result["normalized_source_rows"]),
            "eligible_source_rows": int(result["eligible_source_rows"]),
            "storage_mode": str(result["storage_mode"]),
            "imported_at": iso_utc(),
        }
        self.storage.set_setting(
            f"{DUMP_SETTING_PREFIX}{descriptor.league_name}", marker
        )
        self.storage.update_source_state(
            source=self.SOURCE,
            endpoint=endpoint,
            league_id=descriptor.league_name,
            category="archive",
            status="success",
            detail=json.dumps(marker, separators=(",", ":")),
            etag=receipt.etag,
            last_modified=receipt.last_modified,
            success=True,
        )
        return {
            "status": "success",
            "league_id": descriptor.league_name,
            "zip_name": descriptor.zip_name,
            "sha256": receipt.sha256,
            "download_bytes": receipt.bytes_written,
            **result,
        }

    def import_archive(
        self,
        descriptor: DumpDescriptor,
        archive_path: str | Path,
        *,
        source: str | None = None,
    ) -> dict[str, Any]:
        write_source = str(source or self.SOURCE)
        current_identities, current_by_key = self._current_identities()
        self._ensure_league(descriptor)
        summary = {
            "historical_assets_written": 0,
            "seasonal_rows_written": 0,
            "currency_rows_seen": 0,
            "currency_rows_matched": 0,
            "item_rows_seen": 0,
            "item_rows_matched": 0,
            "item_rows_archived_only": 0,
            "matched_item_categories": {},
            "unmatched_item_rows": 0,
            "identity_mismatch_rows": 0,
            "missing_anchor_rows": 0,
            "raw_source_rows_seen": 0,
            "normalized_source_rows": 0,
            "eligible_source_rows": 0,
            "storage_mode": (
                "compact"
                if bool(getattr(self.storage, "compact_history_mode", False))
                else "full"
            ),
        }
        try:
            with ZipFile(archive_path) as archive:
                currency_name = f"{descriptor.league_name}.currency.csv"
                item_name = f"{descriptor.league_name}.items.csv"
                names = set(archive.namelist())
                missing = [
                    name for name in (currency_name, item_name) if name not in names
                ]
                if missing:
                    raise ValueError(
                        "poe.ninja dump is missing " + ", ".join(missing)
                    )
                anchors = self._divine_anchors(
                    archive, currency_name, descriptor
                )
                if not anchors:
                    raise ValueError(
                        "poe.ninja currency dump has no direct Divine Orb/Chaos "
                        "Orb anchors"
                    )
                self._import_currencies(
                    archive,
                    currency_name,
                    descriptor,
                    anchors,
                    current_by_key,
                    summary,
                    write_source,
                )
                self._import_items(
                    archive,
                    item_name,
                    descriptor,
                    anchors,
                    current_identities,
                    current_by_key,
                    summary,
                    write_source,
                )
        except BadZipFile as error:
            raise ValueError("poe.ninja dump is not a valid ZIP archive") from error
        summary["raw_source_rows_seen"] = int(
            summary["currency_rows_seen"] + summary["item_rows_seen"]
        )
        summary["normalized_source_rows"] = int(
            summary["currency_rows_matched"]
            + summary["item_rows_matched"]
            + summary["item_rows_archived_only"]
        )
        summary["eligible_source_rows"] = int(
            summary["currency_rows_matched"] + summary["item_rows_matched"]
        )
        return summary

    @staticmethod
    def _validate_complete_import(
        descriptor: DumpDescriptor,
        summary: dict[str, Any],
    ) -> None:
        if int(summary.get("seasonal_rows_written", 0)) <= 0:
            raise ValueError(
                f"{descriptor.league_name} dump produced no seasonal prices"
            )
        if int(summary.get("currency_rows_matched", 0)) <= 0:
            raise ValueError(
                f"{descriptor.league_name} dump matched no direct currencies"
            )
        if int(summary.get("item_rows_matched", 0)) <= 0:
            raise ValueError(
                f"{descriptor.league_name} dump matched no current item identities"
            )
        matched = int(summary.get("item_rows_matched", 0))
        archived_only = int(summary.get("item_rows_archived_only", 0))
        comparable = matched + archived_only
        match_ratio = matched / comparable if comparable else 0.0
        summary["item_match_ratio"] = match_ratio
        if match_ratio < 0.05:
            raise ValueError(
                f"{descriptor.league_name} current-identity coverage is only "
                f"{match_ratio:.1%}; refusing to replace the broad archive"
            )
        categories = summary.get("matched_item_categories")
        if (
            descriptor.league_name in BROADLY_COVERED_LEAGUE_IDS
            and (not isinstance(categories, dict) or len(categories) < 3)
        ):
            raise ValueError(
                f"{descriptor.league_name} matched fewer than three current "
                "item modalities"
            )

    def _clear_staging(self, league_id: str, staging_source: str) -> None:
        if bool(getattr(self.storage, "compact_history_mode", False)):
            self.storage.clear_compact_seasonal_staging(league_id)
            with self.storage.transaction() as connection:
                connection.execute(
                    "DELETE FROM historical_assets WHERE source = ?",
                    (staging_source,),
                )
            return
        with self.storage.transaction() as connection:
            connection.execute(
                "DELETE FROM seasonal_prices WHERE league_id = ? AND source = ?",
                (league_id, staging_source),
            )
            connection.execute(
                "DELETE FROM historical_assets WHERE source = ?",
                (staging_source,),
            )

    def _promote_staging(self, league_id: str, staging_source: str) -> int:
        """Atomically replace one production league after a complete import."""

        compact = bool(getattr(self.storage, "compact_history_mode", False))
        with self.storage.transaction() as connection:
            connection.execute(
                """
                INSERT INTO historical_assets(
                    source, source_item_id, item_key, name, category,
                    source_category, source_group, variant_json,
                    current_daily, current_chaos, current_divine,
                    low_confidence, eligible, seen_at
                )
                SELECT ?, source_item_id, item_key, name, category,
                       source_category, source_group, variant_json,
                       current_daily, current_chaos, current_divine,
                       low_confidence, eligible, seen_at
                FROM historical_assets
                WHERE source = ?
                ON CONFLICT(source, source_item_id) DO UPDATE SET
                    item_key = excluded.item_key,
                    name = excluded.name,
                    category = excluded.category,
                    source_category = excluded.source_category,
                    source_group = excluded.source_group,
                    variant_json = excluded.variant_json,
                    current_daily = excluded.current_daily,
                    current_chaos = excluded.current_chaos,
                    current_divine = excluded.current_divine,
                    low_confidence = excluded.low_confidence,
                    eligible = excluded.eligible,
                    seen_at = excluded.seen_at
                """,
                (self.SOURCE, staging_source),
            )
            if compact:
                league_row = connection.execute(
                    """
                    SELECT id FROM compact_seasonal_leagues
                    WHERE league_id = ?
                    """,
                    (league_id,),
                ).fetchone()
                if league_row is None:
                    raise RuntimeError(
                        f"Compact staging has no league dictionary for {league_id}"
                    )
                league_key = int(league_row[0])
                production_rows = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM compact_seasonal_prices_staging
                        WHERE league_key = ?
                        """,
                        (league_key,),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    DELETE FROM compact_seasonal_prices
                    WHERE league_key = ?
                    """,
                    (league_key,),
                )
                connection.execute(
                    """
                    INSERT INTO compact_seasonal_prices(
                        league_key, item_key, source_item_key, league_day,
                        observed_epoch, chaos_value, divine_value, confidence
                    )
                    SELECT league_key, item_key, source_item_key, league_day,
                           observed_epoch, chaos_value, divine_value, confidence
                    FROM compact_seasonal_prices_staging
                    WHERE league_key = ?
                    """,
                    (league_key,),
                )
                connection.execute(
                    """
                    DELETE FROM compact_seasonal_prices_staging
                    WHERE league_key = ?
                    """,
                    (league_key,),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM seasonal_prices
                    WHERE league_id = ? AND source = ?
                    """,
                    (league_id, self.SOURCE),
                )
                promoted = connection.execute(
                    """
                    UPDATE seasonal_prices
                    SET source = ?
                    WHERE league_id = ? AND source = ?
                    """,
                    (self.SOURCE, league_id, staging_source),
                )
                production_rows = max(0, int(promoted.rowcount))
        # The source update above proves there are no staging price children.
        # Clean up their temporary parent identities separately, with foreign
        # key scans disabled, so tens of thousands of parent deletes do not
        # hold the multi-million-row promotion transaction open.
        with closing(self.storage.connect()) as connection:
            if compact:
                remaining = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM compact_seasonal_prices_staging AS price
                        JOIN compact_seasonal_leagues AS league
                          ON league.id = price.league_key
                        WHERE league.league_id = ?
                        """,
                        (league_id,),
                    ).fetchone()[0]
                )
            else:
                remaining = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM seasonal_prices
                        WHERE league_id = ? AND source = ?
                        """,
                        (league_id, staging_source),
                    ).fetchone()[0]
                )
            if remaining:
                raise RuntimeError(
                    f"Refusing to remove {staging_source}: {remaining} "
                    "staging price rows remain"
                )
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM historical_assets WHERE source = ?",
                    (staging_source,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
        return production_rows

    def _already_imported(
        self, descriptor: DumpDescriptor, endpoint: str
    ) -> bool:
        marker = self.storage.get_setting(
            f"{DUMP_SETTING_PREFIX}{descriptor.league_name}", {}
        )
        if _marker_matches(marker, descriptor) and self._marker_row_count_matches(
            descriptor.league_name,
            marker,
        ):
            return True
        state = self.storage.get_source_state(
            self.SOURCE,
            endpoint,
            descriptor.league_name,
            "archive",
        )
        if not state or str(state.get("status", "")).casefold() != "success":
            return False
        try:
            detail = json.loads(str(state.get("detail") or "{}"))
        except json.JSONDecodeError:
            return False
        return _marker_matches(detail, descriptor) and self._marker_row_count_matches(
            descriptor.league_name,
            detail,
        )

    def _marker_row_count_matches(
        self,
        league_id: str,
        marker: dict[str, Any],
    ) -> bool:
        expected = marker.get(
            "stored_seasonal_rows",
            marker.get("seasonal_rows_written"),
        )
        if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
            return False
        marker_mode = str(marker.get("storage_mode") or "full")
        requested_mode = (
            "compact"
            if bool(getattr(self.storage, "compact_history_mode", False))
            else "full"
        )
        if marker_mode != requested_mode:
            return False
        counts = self.storage.seasonal_price_storage_counts(
            league_id,
            source=self.SOURCE,
        )
        actual = int(counts[marker_mode])
        return actual == expected

    def _ensure_league(self, descriptor: DumpDescriptor) -> None:
        existing = self.storage.get_league(descriptor.league_name)
        if existing is not None:
            return
        self.storage.upsert_league(
            League(
                id=descriptor.league_name,
                name=descriptor.league_name,
                start_at=descriptor.min_date,
                end_at=descriptor.max_date,
            ),
            current=False,
        )

    def _current_identities(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        current = self.storage.get_current_league()
        if current is None:
            raise ValueError(
                "A current league poe.ninja catalog is required before dump import"
            )
        with closing(self.storage.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, item_key, name, category, observed_at, chaos_value,
                       divine_value, volume, confidence, details_json
                FROM price_points
                WHERE league_id = ? AND source = ?
                ORDER BY observed_at DESC, id DESC
                """,
                (current.id, POE_NINJA_CURRENT_SOURCE),
            ).fetchall()
        identities: dict[str, dict[str, Any]] = {}
        by_key: dict[str, dict[str, Any]] = {}
        for row in rows:
            point = dict(row)
            try:
                details = json.loads(str(point.pop("details_json") or "{}"))
            except json.JSONDecodeError:
                details = {}
            point["details"] = details if isinstance(details, dict) else {}
            key = str(point["item_key"])
            by_key.setdefault(key, point)
            source_id = str(
                point["details"].get("poe_ninja_id")
                or point["details"].get("id")
                or ""
            ).strip()
            if not source_id:
                continue
            previous = identities.get(source_id)
            if previous is None:
                identities[source_id] = point
            elif (
                previous.get("observed_at") == point.get("observed_at")
                and str(previous["item_key"]) != key
            ):
                raise ValueError(
                    "Current poe.ninja id maps to multiple item identities: "
                    f"{source_id}"
                )
        if not identities:
            raise ValueError(
                "Current poe.ninja rows do not preserve any source item IDs; "
                "run a fresh current sync first"
            )
        return identities, by_key

    def _divine_anchors(
        self,
        archive: ZipFile,
        member_name: str,
        descriptor: DumpDescriptor,
    ) -> dict[str, float]:
        anchors: dict[str, tuple[float, float]] = {}
        reader = self._csv_reader(
            archive,
            member_name,
            {"League", "Date", "Get", "Pay", "Value", "Confidence"},
        )
        with reader as rows:
            for row in rows:
                self._validate_row_league(row, descriptor, rows.line_num)
                if not _direct_chaos_pair(row, DIVINE_ORB_NAME):
                    continue
                value = _positive_float(row.get("Value"))
                if value is None:
                    continue
                date_text = self._row_day(row, descriptor)[0]
                confidence = _confidence_value(row.get("Confidence"))
                previous = anchors.get(date_text)
                if previous is None or confidence > previous[1]:
                    anchors[date_text] = (value, confidence)
        values = {key: value[0] for key, value in anchors.items()}
        for day, value in values.items():
            if not MIN_DIVINE_CHAOS <= value <= MAX_DIVINE_CHAOS:
                raise ValueError(
                    f"Implausible direct Divine/Chaos anchor on {day}: {value}"
                )
        return values

    def _import_currencies(
        self,
        archive: ZipFile,
        member_name: str,
        descriptor: DumpDescriptor,
        anchors: dict[str, float],
        current_by_key: dict[str, dict[str, Any]],
        summary: dict[str, Any],
        source: str,
    ) -> None:
        assets: dict[str, dict[str, Any]] = {}
        prices: list[dict[str, Any]] = []
        registered: set[str] = set()
        reader = self._csv_reader(
            archive,
            member_name,
            {"League", "Date", "Get", "Pay", "Value", "Confidence"},
        )
        with reader as rows:
            for row in rows:
                summary["currency_rows_seen"] += 1
                self._validate_row_league(row, descriptor, rows.line_num)
                name = str(row.get("Get") or "").strip()
                if not name or not _direct_chaos_pair(row, name):
                    continue
                chaos_value = _positive_float(row.get("Value"))
                if chaos_value is None:
                    continue
                date_text, league_day, observed_at = self._row_day(
                    row, descriptor
                )
                divine_chaos = anchors.get(date_text)
                if divine_chaos is None:
                    summary["missing_anchor_rows"] += 1
                    continue
                item_key = canonical_key(name, "Currency")
                current = current_by_key.get(item_key, {})
                source_item_id = f"currency:{slugify(name)}"
                if source_item_id not in registered:
                    assets[source_item_id] = self._asset(
                        source_item_id=source_item_id,
                        item_key=item_key,
                        name=str(current.get("name") or name),
                        category=str(current.get("category") or "Currency"),
                        source_category="Currency",
                        source_group="currency.csv",
                        current=current,
                        variant={"currency_name": name},
                        source=source,
                    )
                    registered.add(source_item_id)
                prices.append(
                    {
                        "league_id": descriptor.league_name,
                        "item_key": item_key,
                        "source": source,
                        "source_item_id": source_item_id,
                        "league_day": league_day,
                        "observed_at": observed_at,
                        "chaos_value": chaos_value,
                        "divine_value": chaos_value / divine_chaos,
                        "volume": None,
                        "confidence": _confidence_value(row.get("Confidence")),
                        "details": {
                            "provider": "poe.ninja",
                            "dump": descriptor.zip_name,
                            "get": name,
                            "pay": CHAOS_ORB_NAME,
                            "divine_chaos": divine_chaos,
                        },
                    }
                )
                summary["currency_rows_matched"] += 1
                if len(prices) >= self.batch_size:
                    self._flush(assets, prices, summary)
        self._flush(assets, prices, summary)

    def _import_items(
        self,
        archive: ZipFile,
        member_name: str,
        descriptor: DumpDescriptor,
        anchors: dict[str, float],
        identities: dict[str, dict[str, Any]],
        current_by_key: dict[str, dict[str, Any]],
        summary: dict[str, Any],
        source: str,
    ) -> None:
        assets: dict[str, dict[str, Any]] = {}
        prices: list[dict[str, Any]] = []
        registered: set[str] = set()
        visible_identities: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for current in current_by_key.values():
            key = (
                _identity_text(current.get("category")),
                _identity_text(current.get("name")),
            )
            visible_identities.setdefault(key, []).append(current)
        reader = self._csv_reader(
            archive,
            member_name,
            {
                "League",
                "Date",
                "Id",
                "Type",
                "Name",
                "BaseType",
                "Variant",
                "Links",
                "Value",
                "Confidence",
            },
        )
        with reader as rows:
            for row in rows:
                summary["item_rows_seen"] += 1
                self._validate_row_league(row, descriptor, rows.line_num)
                source_item_id = str(row.get("Id") or "").strip()
                identity = identities.get(source_item_id)
                identity_match = "exact-current-poe-ninja-id"
                archive_only = False
                if identity is None:
                    candidates = visible_identities.get(
                        (
                            _identity_text(row.get("Type")),
                            _identity_text(row.get("Name")),
                        ),
                        [],
                    )
                    candidates = [
                        candidate
                        for candidate in candidates
                        if _identity_matches(row, candidate, strict_visible=True)
                    ]
                    if len(candidates) != 1:
                        summary["unmatched_item_rows"] += 1
                        archive_only = True
                    else:
                        identity = candidates[0]
                        identity_match = "exact-category-name-visible-variant"
                elif not _identity_matches(row, identity):
                    summary["identity_mismatch_rows"] += 1
                    archive_only = True
                if archive_only:
                    dump_name = str(row.get("Name") or "").strip()
                    dump_type = str(row.get("Type") or "Unknown").strip()
                    if not dump_name:
                        continue
                    archive_fingerprint = hashlib.sha256(
                        "|".join(
                            str(row.get(key) or "")
                            for key in (
                                "Id",
                                "Type",
                                "Name",
                                "BaseType",
                                "Variant",
                                "Links",
                            )
                        ).encode("utf-8")
                    ).hexdigest()[:16]
                    source_item_id = (
                        f"archive:{source_item_id or 'missing'}:"
                        f"{archive_fingerprint}"
                    )
                    identity = {
                        "item_key": canonical_key(
                            dump_name,
                            dump_type,
                            f"archive-{archive_fingerprint}",
                        ),
                        "name": dump_name,
                        "category": dump_type,
                        "details": {
                            "archive_only": True,
                            "dump_id": row.get("Id"),
                            "baseType": row.get("BaseType"),
                            "variant": row.get("Variant"),
                            "links": row.get("Links"),
                        },
                    }
                    identity_match = "archive-only-unmatched-current-identity"
                chaos_value = _positive_float(row.get("Value"))
                if chaos_value is None:
                    continue
                date_text, league_day, observed_at = self._row_day(
                    row, descriptor
                )
                divine_chaos = anchors.get(date_text)
                if divine_chaos is None:
                    summary["missing_anchor_rows"] += 1
                    continue
                if source_item_id not in registered:
                    assets[source_item_id] = self._asset(
                        source_item_id=source_item_id,
                        item_key=str(identity["item_key"]),
                        name=str(identity["name"]),
                        category=str(identity["category"]),
                        source_category=str(row.get("Type") or identity["category"]),
                        source_group="items.csv",
                        current=identity,
                        variant=dict(identity.get("details") or {}),
                        source=source,
                        eligible=not archive_only,
                    )
                    registered.add(source_item_id)
                prices.append(
                    {
                        "league_id": descriptor.league_name,
                        "item_key": str(identity["item_key"]),
                        "source": source,
                        "source_item_id": source_item_id,
                        "league_day": league_day,
                        "observed_at": observed_at,
                        "chaos_value": chaos_value,
                        "divine_value": chaos_value / divine_chaos,
                        "volume": None,
                        "confidence": _confidence_value(row.get("Confidence")),
                        "details": {
                            "provider": "poe.ninja",
                            "dump": descriptor.zip_name,
                            "poe_ninja_id": source_item_id,
                            "identity_match": identity_match,
                            "archive_only": archive_only,
                            "dump_type": row.get("Type"),
                            "dump_name": row.get("Name"),
                            "dump_base_type": row.get("BaseType"),
                            "dump_variant": row.get("Variant"),
                            "dump_links": row.get("Links"),
                            "divine_chaos": divine_chaos,
                        },
                    }
                )
                if archive_only:
                    summary["item_rows_archived_only"] += 1
                else:
                    summary["item_rows_matched"] += 1
                    category_name = str(identity["category"])
                    categories = summary["matched_item_categories"]
                    categories[category_name] = int(
                        categories.get(category_name, 0)
                    ) + 1
                if len(prices) >= self.batch_size:
                    self._flush(assets, prices, summary)
        self._flush(assets, prices, summary)

    def _asset(
        self,
        *,
        source_item_id: str,
        item_key: str,
        name: str,
        category: str,
        source_category: str,
        source_group: str,
        current: dict[str, Any],
        variant: dict[str, Any],
        source: str,
        eligible: bool = True,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "source_item_id": source_item_id,
            "item_key": item_key,
            "name": name,
            "category": category,
            "source_category": source_category,
            "source_group": source_group,
            "variant": variant,
            "current_daily": current.get("volume"),
            "current_chaos": current.get("chaos_value"),
            "current_divine": current.get("divine_value"),
            "low_confidence": False,
            "eligible": eligible,
            "seen_at": iso_utc(),
        }

    def _flush(
        self,
        assets: dict[str, dict[str, Any]],
        prices: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> None:
        if assets:
            summary["historical_assets_written"] += int(
                self.storage.upsert_historical_assets(assets.values()) or 0
            )
            assets.clear()
        if prices:
            if bool(getattr(self.storage, "compact_history_mode", False)):
                eligible_prices = [
                    price
                    for price in prices
                    if not bool(
                        (price.get("details") or {}).get("archive_only")
                    )
                ]
                summary["seasonal_rows_written"] += int(
                    self.storage.upsert_compact_seasonal_prices(
                        eligible_prices,
                        staging=True,
                    )
                    or 0
                )
            else:
                summary["seasonal_rows_written"] += int(
                    self.storage.upsert_seasonal_prices(prices) or 0
                )
            prices.clear()

    @staticmethod
    def _csv_reader(
        archive: ZipFile,
        member_name: str,
        required_fields: set[str],
    ) -> "_CsvRows":
        return _CsvRows(archive, member_name, required_fields)

    @staticmethod
    def _validate_row_league(
        row: dict[str, str], descriptor: DumpDescriptor, line_number: int
    ) -> None:
        league = str(row.get("League") or "").strip()
        if league != descriptor.league_name:
            raise ValueError(
                f"{descriptor.zip_name} row {line_number} has league "
                f"{league!r}, expected {descriptor.league_name!r}"
            )

    @staticmethod
    def _row_day(
        row: dict[str, str], descriptor: DumpDescriptor
    ) -> tuple[str, int, str]:
        raw = str(row.get("Date") or "").strip()
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as error:
            raise ValueError(f"Invalid poe.ninja dump date {raw!r}") from error
        if parsed < descriptor.start_date or parsed > descriptor.end_date:
            raise ValueError(
                f"poe.ninja dump date {raw!r} falls outside catalog range"
            )
        return raw, (parsed - descriptor.start_date).days + 1, f"{raw}T00:00:00Z"


class PoeNinjaHistoryService(PoeNinjaHistoryImporter):
    """Drop-in replacement for the legacy per-item history backfill service.

    A poe.ninja dump is already a complete league export, so ``max_items`` is
    retained only for compatibility with the HTTP/automation call sites and is
    deliberately not used to truncate the authoritative archive.
    """

    def __init__(
        self,
        *args: Any,
        league_names: Iterable[str] | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.league_names = tuple(
            league_names
            if league_names is not None
            else BROADLY_COVERED_LEAGUE_IDS
        )
        self._run_lock = threading.Lock()
        self._is_syncing = False
        self._progress: dict[str, Any] = {
            "status": "idle",
            "completed": 0,
            "total": 0,
        }
        self._last_summary: dict[str, Any] | None = None

    @property
    def is_syncing(self) -> bool:
        return self._is_syncing

    @property
    def last_summary(self) -> dict[str, Any] | None:
        return dict(self._last_summary) if self._last_summary else None

    def progress(self) -> dict[str, Any]:
        return dict(self._progress)

    def backfill(
        self,
        current_league: League | dict[str, Any] | str,
        max_items: int = 80,
    ) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {
                "status": "busy",
                "message": "A poe.ninja historical dump import is running.",
                "progress": self.progress(),
            }
        self._is_syncing = True
        started_at = iso_utc()
        league_id = _league_id(current_league)
        self._progress = {
            "status": "catalog",
            "completed": 0,
            "total": 1,
            "message": "Checking poe.ninja completed-league dumps.",
        }
        try:
            imported = self.sync(self.league_names)
            status = str(imported.get("status") or "failed")
            summary = {
                **imported,
                "status": status,
                "started_at": started_at,
                "finished_at": iso_utc(),
                "current_league_id": league_id,
                "requested_max_items": int(max_items),
                "max_items_ignored": True,
                # Compatibility names consumed by existing status/reporting.
                "assets_written": int(
                    imported.get("historical_assets_written", 0)
                ),
                "histories_fetched": int(imported.get("dumps_imported", 0)),
                "histories_skipped": int(imported.get("dumps_skipped", 0)),
                "histories_failed": len(imported.get("errors", [])),
                "warnings": list(imported.get("errors", [])),
                "warnings_suppressed": 0,
                "seasonal_rows_quarantined": 0,
                "snapshots_written": 0,
                "leagues": [
                    str(dump.get("league_id"))
                    for dump in imported.get("dumps", [])
                    if isinstance(dump, dict) and dump.get("league_id")
                ],
            }
            summary["message"] = (
                "poe.ninja completed-league dumps imported."
                if status == "success"
                else "Some poe.ninja completed-league dumps failed to import."
            )
            self._last_summary = summary
            self._progress = {
                "status": status,
                "completed": 1,
                "total": 1,
                "message": summary["message"],
            }
            return summary
        except Exception as error:
            summary = {
                "status": "failed",
                "started_at": started_at,
                "finished_at": iso_utc(),
                "current_league_id": league_id,
                "message": str(error),
                "warnings": [str(error)],
            }
            self._last_summary = summary
            self._progress = {
                "status": "failed",
                "completed": 1,
                "total": 1,
                "message": str(error),
            }
            return summary
        finally:
            self._is_syncing = False
            self._run_lock.release()


class _CsvRows:
    """Context-managed, streaming semicolon-delimited ZIP member reader."""

    def __init__(
        self, archive: ZipFile, member_name: str, required_fields: set[str]
    ):
        self.archive = archive
        self.member_name = member_name
        self.required_fields = required_fields
        self.raw: Any | None = None
        self.text: TextIOWrapper | None = None
        self.reader: csv.DictReader[str] | None = None

    def __enter__(self) -> csv.DictReader[str]:
        self.raw = self.archive.open(self.member_name, "r")
        self.text = TextIOWrapper(self.raw, encoding="utf-8-sig", newline="")
        self.reader = csv.DictReader(self.text, delimiter=";")
        fields = set(self.reader.fieldnames or [])
        missing = sorted(self.required_fields - fields)
        if missing:
            self.__exit__(None, None, None)
            raise ValueError(
                f"{self.member_name} is missing CSV columns: {', '.join(missing)}"
            )
        return self.reader

    def __exit__(self, *_: Any) -> None:
        if self.text is not None:
            self.text.close()
        elif self.raw is not None:
            self.raw.close()


def _parse_iso_date(value: str) -> date:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid poe.ninja catalog date {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _confidence_value(value: Any) -> float:
    label = str(value or "").strip().casefold()
    categorical = {
        "high": 0.9,
        "medium": 0.65,
        "low": 0.35,
    }
    if label in categorical:
        return categorical[label]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(number):
        return 0.5
    return max(0.0, min(1.0, number))


def _direct_chaos_pair(row: dict[str, str], item_name: str) -> bool:
    return (
        str(row.get("Get") or "").strip() == item_name
        and str(row.get("Pay") or "").strip() == CHAOS_ORB_NAME
    )


def _identity_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _identity_matches(
    dump_row: dict[str, str],
    current: dict[str, Any],
    *,
    strict_visible: bool = False,
) -> bool:
    if _identity_text(dump_row.get("Name")) != _identity_text(current.get("name")):
        return False
    details = current.get("details")
    if not isinstance(details, dict):
        return True
    for dump_key, current_key in (
        ("BaseType", "baseType"),
        ("Variant", "variant"),
        ("Links", "links"),
    ):
        historical = _identity_text(dump_row.get(dump_key))
        present = _identity_text(details.get(current_key))
        # Several completed-league categories (notably divination cards and
        # tattoos) repeat the display name in BaseType even though the current
        # poe.ninja row has no baseType field.  That value carries no identity
        # information, so it must not prevent the strict visible-identity
        # fallback used when dump IDs have changed between leagues.  Real base
        # types, variants, and link counts remain exact discriminators.
        if (
            strict_visible
            and dump_key == "BaseType"
            and historical
            and not present
            and historical == _identity_text(dump_row.get("Name"))
        ):
            continue
        if strict_visible and historical and not present:
            return False
        if historical and present and historical != present:
            return False
    return True


def _marker_matches(marker: Any, descriptor: DumpDescriptor) -> bool:
    if not isinstance(marker, dict):
        return False
    if str(marker.get("status") or "success").casefold() != "success":
        return False
    return all(marker.get(key) == value for key, value in descriptor.fingerprint.items())


def _league_id(value: League | dict[str, Any] | str) -> str:
    if isinstance(value, League):
        return value.id
    if isinstance(value, dict):
        return str(value.get("id") or value.get("name") or "").strip()
    return str(value).strip()
