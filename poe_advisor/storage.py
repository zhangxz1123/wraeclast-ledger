from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import League, PricePoint, iso_utc
from .provenance import normalize_source_filter


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS leagues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    start_at TEXT,
    end_at TEXT,
    realm TEXT NOT NULL DEFAULT 'pc',
    is_hardcore INTEGER NOT NULL DEFAULT 0,
    is_ssf INTEGER NOT NULL DEFAULT 0,
    is_demo INTEGER NOT NULL DEFAULT 0,
    is_current INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    league_id TEXT,
    rows_written INTEGER NOT NULL DEFAULT 0,
    snapshots_written INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (league_id) REFERENCES leagues(id)
);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    league_id TEXT,
    category TEXT,
    fetched_at TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    etag TEXT,
    last_modified TEXT,
    sha256 TEXT NOT NULL,
    payload_gzip BLOB NOT NULL,
    payload_bytes INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (league_id) REFERENCES leagues(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_snapshot
ON raw_snapshots(source, endpoint, COALESCE(league_id, ''), sha256);

CREATE TABLE IF NOT EXISTS price_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    chaos_value REAL,
    divine_value REAL NOT NULL CHECK(divine_value >= 0),
    listing_count INTEGER,
    volume REAL,
    confidence REAL NOT NULL DEFAULT 0.5,
    details_json TEXT NOT NULL DEFAULT '{}',
    snapshot_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (league_id) REFERENCES leagues(id),
    FOREIGN KEY (snapshot_id) REFERENCES raw_snapshots(id),
    UNIQUE(league_id, item_key, source, observed_at)
);

CREATE INDEX IF NOT EXISTS ix_price_history
ON price_points(league_id, item_key, observed_at);

CREATE INDEX IF NOT EXISTS ix_price_observed
ON price_points(league_id, observed_at);

CREATE TABLE IF NOT EXISTS source_state (
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    league_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    etag TEXT,
    last_modified TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    detail TEXT,
    PRIMARY KEY(source, endpoint, league_id, category)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    budget REAL NOT NULL,
    horizon_days INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (league_id) REFERENCES leagues(id)
);

CREATE TABLE IF NOT EXISTS historical_assets (
    source TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    source_category TEXT NOT NULL DEFAULT '',
    source_group TEXT NOT NULL DEFAULT '',
    variant_json TEXT NOT NULL DEFAULT '{}',
    current_daily REAL,
    current_chaos REAL,
    current_divine REAL,
    low_confidence INTEGER NOT NULL DEFAULT 0,
    eligible INTEGER NOT NULL DEFAULT 0,
    seen_at TEXT NOT NULL,
    PRIMARY KEY(source, source_item_id)
);

CREATE INDEX IF NOT EXISTS ix_historical_assets_eligible_daily
ON historical_assets(eligible, current_daily DESC, item_key);

CREATE INDEX IF NOT EXISTS ix_historical_assets_item
ON historical_assets(item_key, source, source_item_id);

CREATE TABLE IF NOT EXISTS historical_fetch_state (
    source TEXT NOT NULL,
    league_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    status TEXT NOT NULL,
    points_written INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source, league_id, source_item_id),
    FOREIGN KEY (league_id) REFERENCES leagues(id),
    FOREIGN KEY (source, source_item_id)
        REFERENCES historical_assets(source, source_item_id)
);

CREATE INDEX IF NOT EXISTS ix_historical_fetch_status
ON historical_fetch_state(status, league_id, source);

CREATE TABLE IF NOT EXISTS seasonal_prices (
    league_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    source TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    league_day INTEGER NOT NULL CHECK(league_day >= 1),
    observed_at TEXT NOT NULL,
    chaos_value REAL,
    divine_value REAL NOT NULL CHECK(divine_value > 0),
    volume REAL,
    confidence REAL NOT NULL DEFAULT 0.5,
    snapshot_id INTEGER,
    details_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(league_id, item_key, source, league_day),
    FOREIGN KEY (league_id) REFERENCES leagues(id),
    FOREIGN KEY (source, source_item_id)
        REFERENCES historical_assets(source, source_item_id),
    FOREIGN KEY (snapshot_id) REFERENCES raw_snapshots(id)
);

CREATE INDEX IF NOT EXISTS ix_seasonal_item_day_league
ON seasonal_prices(item_key, league_day, league_id);

CREATE INDEX IF NOT EXISTS ix_seasonal_league_day_item
ON seasonal_prices(league_id, league_day, item_key);

CREATE INDEX IF NOT EXISTS ix_seasonal_source_item_day
ON seasonal_prices(source, source_item_id, league_day);

-- GitHub-hosted updates use this integer-keyed representation for immutable
-- poe.ninja completed-league rows.  The normal local importer continues to
-- populate seasonal_prices, which intentionally remains the richer research
-- archive.  Keeping strings in three small dictionaries makes the hosted
-- price table practical on GitHub's standard 14 GB runner.
CREATE TABLE IF NOT EXISTS compact_seasonal_leagues (
    id INTEGER PRIMARY KEY,
    league_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS compact_seasonal_items (
    id INTEGER PRIMARY KEY,
    item_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS compact_seasonal_source_items (
    id INTEGER PRIMARY KEY,
    source_item_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS compact_seasonal_prices (
    league_key INTEGER NOT NULL,
    item_key INTEGER NOT NULL,
    source_item_key INTEGER NOT NULL,
    league_day INTEGER NOT NULL CHECK(league_day >= 1),
    observed_epoch INTEGER NOT NULL CHECK(observed_epoch >= 0),
    chaos_value REAL,
    divine_value REAL NOT NULL CHECK(divine_value > 0),
    confidence REAL NOT NULL DEFAULT 0.5,
    PRIMARY KEY(league_key, item_key, league_day),
    FOREIGN KEY (league_key) REFERENCES compact_seasonal_leagues(id),
    FOREIGN KEY (item_key) REFERENCES compact_seasonal_items(id),
    FOREIGN KEY (source_item_key)
        REFERENCES compact_seasonal_source_items(id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_compact_seasonal_item_day_league
ON compact_seasonal_prices(item_key, league_day, league_key);

-- One league is staged at a time and promoted atomically.  A failed or
-- cancelled dump import therefore cannot replace the last good compact curve.
CREATE TABLE IF NOT EXISTS compact_seasonal_prices_staging (
    league_key INTEGER NOT NULL,
    item_key INTEGER NOT NULL,
    source_item_key INTEGER NOT NULL,
    league_day INTEGER NOT NULL CHECK(league_day >= 1),
    observed_epoch INTEGER NOT NULL CHECK(observed_epoch >= 0),
    chaos_value REAL,
    divine_value REAL NOT NULL CHECK(divine_value > 0),
    confidence REAL NOT NULL DEFAULT 0.5,
    PRIMARY KEY(league_key, item_key, league_day),
    FOREIGN KEY (league_key) REFERENCES compact_seasonal_leagues(id),
    FOREIGN KEY (item_key) REFERENCES compact_seasonal_items(id),
    FOREIGN KEY (source_item_key)
        REFERENCES compact_seasonal_source_items(id)
) WITHOUT ROWID;

-- Query code reads one relation in either storage mode.  If compact rows are
-- present for an official-history league they are authoritative for that
-- league; unrelated legacy providers and full local leagues remain visible.
CREATE VIEW IF NOT EXISTS seasonal_price_rows AS
SELECT league_id, item_key, source, source_item_id, league_day, observed_at,
       chaos_value, divine_value, volume, confidence, snapshot_id,
       details_json, updated_at
FROM seasonal_prices AS full_price
WHERE full_price.source <> 'poe.ninja-history'
   OR NOT EXISTS (
       SELECT 1
       FROM compact_seasonal_prices AS compact_price
       JOIN compact_seasonal_leagues AS compact_league
         ON compact_league.id = compact_price.league_key
       WHERE compact_league.league_id = full_price.league_id
       LIMIT 1
   )
UNION ALL
SELECT compact_league.league_id,
       compact_item.item_key,
       'poe.ninja-history' AS source,
       compact_source.source_item_id,
       compact_price.league_day,
       strftime(
           '%Y-%m-%dT%H:%M:%SZ', compact_price.observed_epoch, 'unixepoch'
       ) AS observed_at,
       compact_price.chaos_value,
       compact_price.divine_value,
       NULL AS volume,
       compact_price.confidence,
       NULL AS snapshot_id,
       '{}' AS details_json,
       strftime(
           '%Y-%m-%dT%H:%M:%SZ', compact_price.observed_epoch, 'unixepoch'
       ) AS updated_at
FROM compact_seasonal_prices AS compact_price
JOIN compact_seasonal_leagues AS compact_league
  ON compact_league.id = compact_price.league_key
JOIN compact_seasonal_items AS compact_item
  ON compact_item.id = compact_price.item_key
JOIN compact_seasonal_source_items AS compact_source
  ON compact_source.id = compact_price.source_item_key;

CREATE TABLE IF NOT EXISTS meta_class_snapshots (
    league_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    league_day INTEGER NOT NULL CHECK(league_day >= 1),
    sample_size INTEGER NOT NULL CHECK(sample_size >= 0),
    page_count INTEGER NOT NULL DEFAULT 0 CHECK(page_count >= 0),
    counts_json TEXT NOT NULL DEFAULT '{}',
    shares_json TEXT NOT NULL DEFAULT '{}',
    snapshot_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY(league_id, observed_at, source),
    FOREIGN KEY (league_id) REFERENCES leagues(id)
);

CREATE INDEX IF NOT EXISTS ix_meta_class_latest
ON meta_class_snapshots(league_id, source, observed_at DESC);

PRAGMA user_version = 4;
"""


class Storage:
    """Thin SQLite repository.

    Connections are short-lived so the class is safe to use from the threaded
    HTTP server. SQLite WAL mode keeps reads responsive while a sync writes.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        compact_history: bool | None = None,
    ):
        self.path = Path(path).expanduser().resolve()
        if compact_history is None:
            compact_history = str(
                os.environ.get("POE_ADVISOR_COMPACT_HISTORY", "")
            ).strip().casefold() in {"1", "true", "yes", "on"}
        self.compact_history_mode = bool(compact_history)
        self._compact_dimension_cache: dict[str, dict[str, int]] = {
            "league": {},
            "item": {},
            "source_item": {},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=20.0, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    def upsert_league(self, league: League, *, current: bool = True) -> None:
        now = iso_utc()
        with self.transaction() as connection:
            if current:
                connection.execute("UPDATE leagues SET is_current = 0")
            connection.execute(
                """
                INSERT INTO leagues (
                    id, name, start_at, end_at, realm, is_hardcore, is_ssf,
                    is_demo, is_current, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    start_at = COALESCE(excluded.start_at, leagues.start_at),
                    end_at = COALESCE(excluded.end_at, leagues.end_at),
                    realm = excluded.realm,
                    is_hardcore = excluded.is_hardcore,
                    is_ssf = excluded.is_ssf,
                    is_demo = excluded.is_demo,
                    is_current = excluded.is_current,
                    updated_at = excluded.updated_at
                """,
                (
                    league.id,
                    league.name,
                    league.start_at,
                    league.end_at,
                    league.realm,
                    int(league.is_hardcore),
                    int(league.is_ssf),
                    int(league.is_demo),
                    int(current),
                    now,
                    now,
                ),
            )

    def get_current_league(self) -> League | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM leagues
                WHERE is_current = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._league_from_row(row) if row else None

    def get_league(self, league_id: str) -> League | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM leagues WHERE id = ?", (league_id,)
            ).fetchone()
        return self._league_from_row(row) if row else None

    @staticmethod
    def _league_from_row(row: sqlite3.Row) -> League:
        return League(
            id=row["id"],
            name=row["name"],
            start_at=row["start_at"],
            end_at=row["end_at"],
            realm=row["realm"],
            is_hardcore=bool(row["is_hardcore"]),
            is_ssf=bool(row["is_ssf"]),
            is_demo=bool(row["is_demo"]),
        )

    def start_sync_run(self, league_id: str | None = None) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sync_runs(started_at, status, league_id)
                VALUES (?, 'running', ?)
                """,
                (iso_utc(), league_id),
            )
            return int(cursor.lastrowid)

    def set_sync_run_league(self, run_id: int, league_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE sync_runs SET league_id = ? WHERE id = ?",
                (league_id, run_id),
            )

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        rows_written: int,
        snapshots_written: int,
        message: str,
        warnings: list[str],
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE sync_runs SET
                    finished_at = ?, status = ?, rows_written = ?,
                    snapshots_written = ?, message = ?, warnings_json = ?
                WHERE id = ?
                """,
                (
                    iso_utc(),
                    status,
                    rows_written,
                    snapshots_written,
                    message,
                    json.dumps(warnings, separators=(",", ":")),
                    run_id,
                ),
            )

    def add_snapshot(
        self,
        *,
        source: str,
        endpoint: str,
        league_id: str | None,
        category: str | None,
        fetched_at: str,
        status_code: int,
        raw: bytes,
        etag: str | None = None,
        last_modified: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        digest = hashlib.sha256(raw).hexdigest()
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO raw_snapshots(
                    source, endpoint, league_id, category, fetched_at,
                    status_code, etag, last_modified, sha256, payload_gzip,
                    payload_bytes, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    endpoint,
                    league_id,
                    category,
                    fetched_at,
                    status_code,
                    etag,
                    last_modified,
                    digest,
                    compressed,
                    len(raw),
                    json.dumps(metadata or {}, separators=(",", ":")),
                ),
            )
            created = cursor.rowcount == 1
            if created:
                return int(cursor.lastrowid), True
            row = connection.execute(
                """
                SELECT id FROM raw_snapshots
                WHERE source = ? AND endpoint = ?
                  AND COALESCE(league_id, '') = COALESCE(?, '')
                  AND sha256 = ?
                """,
                (source, endpoint, league_id, digest),
            ).fetchone()
            return int(row["id"]), False

    def read_snapshot(self, snapshot_id: int) -> bytes | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT payload_gzip FROM raw_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
        return gzip.decompress(row["payload_gzip"]) if row else None

    def update_snapshot_metadata(
        self,
        snapshot_id: int,
        metadata: dict[str, Any],
    ) -> bool:
        """Refresh derived audit metadata without changing preserved raw data."""

        encoded = json.dumps(metadata, separators=(",", ":"))
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE raw_snapshots
                SET metadata_json = ?
                WHERE id = ?
                """,
                (encoded, int(snapshot_id)),
            )
        return cursor.rowcount == 1

    def latest_snapshot(
        self,
        *,
        source: str,
        endpoint: str,
        league_id: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest matching raw snapshot and its decoded bytes."""

        clauses = [
            "source = ?",
            "endpoint = ?",
            "COALESCE(league_id, '') = COALESCE(?, '')",
        ]
        parameters: list[Any] = [source, endpoint, league_id]
        if category is not None:
            clauses.append("category = ?")
            parameters.append(category)
        with closing(self.connect()) as connection:
            row = connection.execute(
                f"""
                SELECT id, source, endpoint, league_id, category, fetched_at,
                       status_code, etag, last_modified, payload_gzip,
                       payload_bytes, metadata_json
                FROM raw_snapshots
                WHERE {' AND '.join(clauses)}
                ORDER BY fetched_at DESC, id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["raw"] = gzip.decompress(result.pop("payload_gzip"))
        try:
            result["metadata"] = json.loads(result.pop("metadata_json"))
        except json.JSONDecodeError:
            result["metadata"] = {}
        return result

    def insert_price_points(self, points: Iterable[PricePoint]) -> int:
        rows = [
            (
                point.league_id,
                point.item_key,
                point.name,
                point.category,
                point.source,
                point.observed_at,
                point.chaos_value,
                point.divine_value,
                point.listing_count,
                point.volume,
                max(0.0, min(1.0, point.confidence)),
                json.dumps(point.details, separators=(",", ":")),
                point.snapshot_id,
                iso_utc(),
            )
            for point in points
            if point.divine_value is not None and point.divine_value >= 0
        ]
        if not rows:
            return 0
        with self.transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO price_points(
                    league_id, item_key, name, category, source, observed_at,
                    chaos_value, divine_value, listing_count, volume,
                    confidence, details_json, snapshot_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(league_id, item_key, source, observed_at) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    chaos_value = excluded.chaos_value,
                    divine_value = excluded.divine_value,
                    listing_count = excluded.listing_count,
                    volume = excluded.volume,
                    confidence = excluded.confidence,
                    details_json = excluded.details_json,
                    snapshot_id = excluded.snapshot_id
                """,
                rows,
            )
            return connection.total_changes - before

    def update_source_state(
        self,
        *,
        source: str,
        endpoint: str,
        league_id: str = "",
        category: str = "",
        status: str,
        detail: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        success: bool = False,
    ) -> None:
        now = iso_utc()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO source_state(
                    source, endpoint, league_id, category, etag, last_modified,
                    last_checked_at, last_success_at, status, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, endpoint, league_id, category) DO UPDATE SET
                    etag = COALESCE(excluded.etag, source_state.etag),
                    last_modified = COALESCE(
                        excluded.last_modified, source_state.last_modified
                    ),
                    last_checked_at = excluded.last_checked_at,
                    last_success_at = COALESCE(
                        excluded.last_success_at, source_state.last_success_at
                    ),
                    status = excluded.status,
                    detail = excluded.detail
                """,
                (
                    source,
                    endpoint,
                    league_id,
                    category,
                    etag,
                    last_modified,
                    now,
                    now if success else None,
                    status,
                    detail,
                ),
            )

    def get_source_state(
        self, source: str, endpoint: str, league_id: str = "", category: str = ""
    ) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM source_state
                WHERE source = ? AND endpoint = ? AND league_id = ? AND category = ?
                """,
                (source, endpoint, league_id, category),
            ).fetchone()
        return dict(row) if row else None

    def latest_source_success_at(
        self,
        source: str,
        league_id: str,
    ) -> str | None:
        """Return the newest successful endpoint verification for a source."""

        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT MAX(last_success_at) AS last_success_at
                FROM source_state
                WHERE source = ? AND league_id = ?
                  AND last_success_at IS NOT NULL
                """,
                (str(source), str(league_id)),
            ).fetchone()
        return str(row["last_success_at"]) if row and row["last_success_at"] else None

    def list_source_summaries(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT source, status, detail, last_checked_at, last_success_at
                FROM source_state
                ORDER BY source, last_checked_at DESC
                """
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            value = dict(row)
            grouped.setdefault(str(value["source"]), []).append(value)

        unavailable_states = {"error", "failed", "offline", "unavailable"}
        warning_states = {"limited", "stale", "warning"}
        summaries: list[dict[str, Any]] = []
        for source, states in grouped.items():
            latest = states[0]
            unavailable = [
                state
                for state in states
                if str(state["status"]).lower() in unavailable_states
            ]
            warnings = [
                state
                for state in states
                if str(state["status"]).lower() in warning_states
            ]
            if unavailable:
                status = "unavailable"
                detail = (
                    f"{len(unavailable)} configured endpoint"
                    f"{'s are' if len(unavailable) != 1 else ' is'} unavailable. "
                    f"Latest error: {unavailable[0].get('detail') or 'no detail'}"
                )
            elif warnings:
                status = "warning"
                detail = warnings[0].get("detail")
            else:
                status = latest["status"]
                detail = latest.get("detail")
            summaries.append(
                {
                    "source": source,
                    "status": status,
                    "detail": detail,
                    "last_checked_at": latest.get("last_checked_at"),
                    "last_success_at": max(
                        (
                            str(state["last_success_at"])
                            for state in states
                            if state.get("last_success_at")
                        ),
                        default=None,
                    ),
                }
            )
        return summaries

    def item_histories(
        self,
        league_id: str,
        *,
        days: int = 90,
        item_key: str | None = None,
        sources: Iterable[str] | str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        parameters: list[Any] = [league_id, f"-{max(1, days)} days"]
        key_clause = ""
        if item_key:
            key_clause = "AND item_key = ?"
            parameters.append(item_key)
        allowed_sources = normalize_source_filter(sources)
        if allowed_sources == ():
            return {}
        source_clause = ""
        if allowed_sources is not None:
            placeholders = ",".join("?" for _ in allowed_sources)
            source_clause = f"AND source IN ({placeholders})"
            parameters.extend(allowed_sources)
        query = f"""
            SELECT item_key, name, category, observed_at, chaos_value,
                   divine_value, listing_count, volume, confidence, source,
                   details_json
            FROM price_points
            WHERE league_id = ?
              AND observed_at >= datetime('now', ?)
              {key_clause}
              {source_clause}
            ORDER BY item_key, observed_at ASC,
                     CASE source
                         WHEN 'poe.ninja' THEN 0
                         WHEN 'ggg-currency-exchange' THEN 1
                         ELSE 2
                     END
        """
        with closing(self.connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        seen: dict[str, set[str]] = {}
        for row in rows:
            key = row["item_key"]
            # At most one observation per source timestamp in the analysis view.
            marker = row["observed_at"]
            if marker in seen.setdefault(key, set()):
                continue
            seen[key].add(marker)
            value = dict(row)
            try:
                value["details"] = json.loads(value.pop("details_json"))
            except json.JSONDecodeError:
                value["details"] = {}
            grouped.setdefault(key, []).append(value)
        return grouped

    def all_time_item_history(
        self,
        league_id: str,
        item_key: str,
        limit: int = 1000,
        *,
        sources: Iterable[str] | str | None = None,
    ) -> list[dict[str, Any]]:
        allowed_sources = normalize_source_filter(sources)
        if allowed_sources == ():
            return []
        source_clause = ""
        parameters: list[Any] = [league_id, item_key]
        if allowed_sources is not None:
            placeholders = ",".join("?" for _ in allowed_sources)
            source_clause = f"AND source IN ({placeholders})"
            parameters.extend(allowed_sources)
        parameters.append(max(1, min(limit, 10000)))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM (
                    SELECT observed_at, divine_value, chaos_value,
                           listing_count, volume, source, confidence
                    FROM price_points
                    WHERE league_id = ? AND item_key = ?
                      {source_clause}
                    ORDER BY observed_at DESC
                    LIMIT ?
                )
                ORDER BY observed_at ASC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def daily_item_history(
        self,
        league_id: str,
        item_key: str,
        league_start_at: str | None,
        *,
        minimum_confidence: float = 0.5,
        sources: Iterable[str] | str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one exact observed current-league price per league day.

        The newest qualifying observation in each UTC league-day bucket is
        selected. This is a daily close-like series, not an interpolated one.
        A source preference only breaks ties when two sources share the exact
        same observation timestamp.
        """

        if not league_start_at:
            return []
        confidence_floor = max(0.0, min(1.0, float(minimum_confidence)))
        allowed_sources = normalize_source_filter(sources)
        if allowed_sources == ():
            return []
        source_clause = ""
        source_parameters: list[Any] = []
        if allowed_sources is not None:
            placeholders = ",".join("?" for _ in allowed_sources)
            source_clause = f"AND source IN ({placeholders})"
            source_parameters.extend(allowed_sources)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                WITH eligible AS (
                    SELECT observed_at, divine_value, chaos_value,
                           listing_count, volume, source, confidence,
                           CAST(
                               julianday(date(observed_at))
                               - julianday(date(?)) AS INTEGER
                           ) + 1 AS league_day,
                           ROW_NUMBER() OVER (
                               PARTITION BY CAST(
                                   julianday(date(observed_at))
                                   - julianday(date(?)) AS INTEGER
                               )
                               ORDER BY observed_at DESC,
                                        CASE source
                                            WHEN 'poe.ninja' THEN 0
                                            WHEN 'ggg-currency-exchange' THEN 1
                                            ELSE 2
                                        END,
                                        id DESC
                           ) AS preference_rank
                    FROM price_points
                    WHERE league_id = ?
                      AND item_key = ?
                      AND confidence >= ?
                      AND divine_value > 0
                      AND date(observed_at) >= date(?)
                      {source_clause}
                )
                SELECT league_day, observed_at, divine_value, chaos_value,
                       listing_count, volume, source, confidence
                FROM eligible
                WHERE preference_rank = 1
                  AND league_day >= 1
                ORDER BY league_day
                """,
                (
                    league_start_at,
                    league_start_at,
                    league_id,
                    item_key,
                    confidence_floor,
                    league_start_at,
                    *source_parameters,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def seasonal_price_curve_rows(
        self,
        item_key: str,
        league_ids: Iterable[str],
        *,
        minimum_confidence: float = 0.5,
        sources: Iterable[str] | str | None = None,
    ) -> list[dict[str, Any]]:
        """Return exact daily completed-league prices for one item.

        At most one row is returned for each league/day. If multiple providers
        exist, the highest-confidence and most recently updated observation is
        selected deterministically. Missing days remain missing.
        """

        leagues = list(
            dict.fromkeys(
                str(league_id).strip()
                for league_id in league_ids
                if str(league_id).strip()
            )
        )
        if not leagues:
            return []
        allowed_sources = normalize_source_filter(sources)
        if allowed_sources == ():
            return []
        confidence_floor = max(0.0, min(1.0, float(minimum_confidence)))
        placeholders = ",".join("?" for _ in leagues)
        parameters: list[Any] = [
            item_key,
            confidence_floor,
            *leagues,
        ]
        source_clause = ""
        if allowed_sources is not None:
            source_placeholders = ",".join("?" for _ in allowed_sources)
            source_clause = f"AND price.source IN ({source_placeholders})"
            parameters.extend(allowed_sources)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT price.league_id,
                           COALESCE(leagues.name, price.league_id)
                               AS league_name,
                           leagues.start_at AS league_start_at,
                           price.league_day,
                           price.observed_at,
                           price.divine_value,
                           price.chaos_value,
                           price.volume,
                           price.source,
                           price.source_item_id,
                           price.confidence,
                           ROW_NUMBER() OVER (
                               PARTITION BY price.league_id, price.league_day
                               ORDER BY price.confidence DESC,
                                        price.updated_at DESC,
                                        price.source,
                                        price.source_item_id
                           ) AS preference_rank
                    FROM seasonal_price_rows AS price
                    LEFT JOIN leagues
                      ON leagues.id = price.league_id
                    WHERE price.item_key = ?
                      AND price.confidence >= ?
                      AND price.divine_value > 0
                      AND price.league_id IN ({placeholders})
                      {source_clause}
                )
                SELECT league_id, league_name, league_start_at, league_day,
                       observed_at, divine_value, chaos_value, volume, source,
                       source_item_id, confidence
                FROM ranked
                WHERE preference_rank = 1
                ORDER BY league_day, league_start_at, league_id
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def seasonal_lifecycle_rows(
        self,
        item_keys: Iterable[str],
        league_ids: Iterable[str],
        *,
        minimum_league_day: int = 1,
        maximum_league_day: int = 120,
        minimum_confidence: float = 0.0,
        sources: Iterable[str] | str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one preferred daily bar per item and completed league.

        Lifecycle classification needs the whole curve for many current items.
        Item keys are pushed into SQLite in bounded batches so archive-only
        rows never enter Python memory and a large live catalog cannot exceed
        SQLite's parameter limit.
        """

        keys = sorted({
            str(item_key).strip()
            for item_key in item_keys
            if str(item_key).strip()
        })
        leagues = list(
            dict.fromkeys(
                str(league_id).strip()
                for league_id in league_ids
                if str(league_id).strip()
            )
        )
        if not keys or not leagues:
            return []
        allowed_sources = normalize_source_filter(sources)
        if allowed_sources == ():
            return []
        first_day = max(1, int(minimum_league_day))
        last_day = max(first_day, int(maximum_league_day))
        confidence_floor = max(0.0, min(1.0, float(minimum_confidence)))
        league_placeholders = ",".join("?" for _ in leagues)
        source_clause = ""
        if allowed_sources is not None:
            source_placeholders = ",".join("?" for _ in allowed_sources)
            source_clause = f"AND price.source IN ({source_placeholders})"
        rows: list[sqlite3.Row] = []
        with closing(self.connect()) as connection:
            for offset in range(0, len(keys), 400):
                key_batch = keys[offset : offset + 400]
                key_placeholders = ",".join("?" for _ in key_batch)
                parameters: list[Any] = [
                    first_day,
                    last_day,
                    confidence_floor,
                    *key_batch,
                    *leagues,
                ]
                if allowed_sources is not None:
                    parameters.extend(allowed_sources)
                rows.extend(
                    connection.execute(
                        f"""
                        WITH ranked AS (
                            SELECT price.item_key,
                                   price.league_id,
                                   COALESCE(leagues.name, price.league_id)
                                       AS league_name,
                                   leagues.start_at AS league_start_at,
                                   price.league_day,
                                   price.observed_at,
                                   price.divine_value,
                                   price.chaos_value,
                                   price.volume,
                                   price.source,
                                   price.source_item_id,
                                   price.confidence,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY price.item_key,
                                                    price.league_id,
                                                    price.league_day
                                       ORDER BY price.confidence DESC,
                                                price.updated_at DESC,
                                                price.source,
                                                price.source_item_id
                                   ) AS preference_rank
                            FROM seasonal_price_rows AS price
                            LEFT JOIN leagues
                              ON leagues.id = price.league_id
                            WHERE price.league_day BETWEEN ? AND ?
                              AND price.confidence >= ?
                              AND price.divine_value > 0
                              AND price.item_key IN ({key_placeholders})
                              AND price.league_id IN ({league_placeholders})
                              {source_clause}
                        )
                        SELECT item_key, league_id, league_name,
                               league_start_at, league_day, observed_at,
                               divine_value, chaos_value, volume, source,
                               source_item_id, confidence
                        FROM ranked
                        WHERE preference_rank = 1
                        ORDER BY item_key, league_start_at, league_id,
                                 league_day
                        """,
                        parameters,
                    ).fetchall()
                )
        return [dict(row) for row in rows]

    def current_item_history_archive(
        self,
        league_id: str,
        item_key: str,
    ) -> dict[str, Any] | None:
        """Return coverage metadata from the newest exact history response.

        Raw history responses are stored independently of normalized price
        rows, including valid empty responses. Reading their metadata lets the
        chart distinguish "the provider had no day-1 observation" from "this
        item has not been backfilled yet."
        """

        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, fetched_at, endpoint, metadata_json
                FROM raw_snapshots
                WHERE source = 'poe.ninja'
                  AND league_id = ?
                  AND category = 'current-item-history'
                ORDER BY fetched_at DESC, id DESC
                LIMIT 5000
                """,
                (league_id,),
            ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if str(metadata.get("item_key") or "") != item_key:
                continue
            return {
                "snapshot_id": int(row["id"]),
                "fetched_at": row["fetched_at"],
                "endpoint": row["endpoint"],
                **metadata,
            }
        return None

    def item_metadata(
        self,
        league_id: str,
        item_key: str,
        *,
        sources: Iterable[str] | str | None = None,
    ) -> dict[str, Any] | None:
        allowed_sources = normalize_source_filter(sources)
        if allowed_sources == ():
            return None
        source_clause = ""
        parameters: list[Any] = [league_id, item_key]
        if allowed_sources is not None:
            placeholders = ",".join("?" for _ in allowed_sources)
            source_clause = f"AND source IN ({placeholders})"
            parameters.extend(allowed_sources)
        with closing(self.connect()) as connection:
            row = connection.execute(
                f"""
                SELECT item_key, name, category
                FROM price_points
                WHERE league_id = ? AND item_key = ?
                  {source_clause}
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return dict(row) if row else None

    def latest_item_prices(
        self,
        league_id: str,
        *,
        sources: Iterable[str] | str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return the newest exact-key price for every item in one league.

        The lookup is intentionally keyed only by the normalized ``item_key``.
        That makes it suitable for matching temporary-league observations to
        Standard without fuzzy-name joins that could mix variants.
        """

        allowed_sources = normalize_source_filter(sources)
        if allowed_sources == ():
            return {}
        newest_source_clause = ""
        point_source_clause = ""
        newest_source_parameters: list[Any] = []
        point_source_parameters: list[Any] = []
        if allowed_sources is not None:
            placeholders = ",".join("?" for _ in allowed_sources)
            newest_source_clause = f"AND source IN ({placeholders})"
            point_source_clause = f"AND point.source IN ({placeholders})"
            newest_source_parameters.extend(allowed_sources)
            point_source_parameters.extend(allowed_sources)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                WITH newest AS (
                    SELECT item_key, MAX(observed_at) AS observed_at
                    FROM price_points
                    WHERE league_id = ?
                      {newest_source_clause}
                    GROUP BY item_key
                )
                SELECT point.item_key, point.name, point.category,
                       point.observed_at, point.chaos_value,
                       point.divine_value, point.listing_count, point.volume,
                       point.confidence, point.source
                FROM price_points AS point
                JOIN newest
                  ON newest.item_key = point.item_key
                 AND newest.observed_at = point.observed_at
                WHERE point.league_id = ?
                  {point_source_clause}
                ORDER BY point.item_key,
                         CASE point.source
                             WHEN 'poe.ninja' THEN 0
                             WHEN 'ggg-currency-exchange' THEN 1
                             ELSE 2
                         END,
                         point.id DESC
                """,
                (
                    league_id,
                    *newest_source_parameters,
                    league_id,
                    *point_source_parameters,
                ),
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["item_key"])
            if key not in latest:
                latest[key] = dict(row)
        return latest

    def upsert_historical_assets(
        self, assets: Iterable[dict[str, Any]]
    ) -> int:
        """Insert or refresh the source catalog used for historical crawling."""

        now = iso_utc()

        def required_text(asset: dict[str, Any], key: str) -> str:
            value = asset.get(key)
            text = str(value).strip() if value is not None else ""
            if not text:
                raise ValueError(f"Historical asset {key} must be non-empty")
            return text

        def json_text(value: Any) -> str:
            if value is None:
                value = {}
            if isinstance(value, str):
                try:
                    json.loads(value)
                except json.JSONDecodeError:
                    return json.dumps(value, separators=(",", ":"))
                return value
            return json.dumps(value, separators=(",", ":"))

        rows: list[tuple[Any, ...]] = []
        for asset in assets:
            source = required_text(asset, "source")
            source_item_id = required_text(asset, "source_item_id")
            category = required_text(asset, "category")
            rows.append(
                (
                    source,
                    source_item_id,
                    required_text(asset, "item_key"),
                    required_text(asset, "name"),
                    category,
                    str(asset.get("source_category") or category),
                    str(asset.get("source_group") or asset.get("group") or ""),
                    json_text(asset.get("variant_json", asset.get("variant"))),
                    asset.get("current_daily", asset.get("daily")),
                    asset.get("current_chaos", asset.get("chaos")),
                    asset.get("current_divine", asset.get("divine")),
                    int(bool(asset.get("low_confidence", False))),
                    int(bool(asset.get("eligible", False))),
                    str(asset.get("seen_at") or now),
                )
            )
        if not rows:
            return 0

        with self.transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO historical_assets(
                    source, source_item_id, item_key, name, category,
                    source_category, source_group, variant_json,
                    current_daily, current_chaos, current_divine,
                    low_confidence, eligible, seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                rows,
            )
            return connection.total_changes - before

    def list_historical_assets(
        self, eligible_only: bool = True
    ) -> list[dict[str, Any]]:
        clause = "WHERE eligible = 1" if eligible_only else ""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT source, source_item_id, item_key, name, category,
                       source_category, source_group, variant_json,
                       current_daily, current_chaos, current_divine,
                       low_confidence, eligible, seen_at
                FROM historical_assets
                {clause}
                ORDER BY current_daily DESC, name COLLATE NOCASE,
                         source, source_item_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def history_fetch_succeeded(
        self, source: str, league_id: str, source_item_id: str
    ) -> bool:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM historical_fetch_state
                WHERE source = ? AND league_id = ? AND source_item_id = ?
                  AND LOWER(status) IN (
                      'success', 'succeeded', 'complete', 'completed'
                  )
                LIMIT 1
                """,
                (source, league_id, str(source_item_id)),
            ).fetchone()
        return row is not None

    def set_history_fetch_state(
        self,
        *,
        source: str,
        league_id: str,
        source_item_id: str,
        status: str,
        points_written: int = 0,
        last_error: str | None = None,
    ) -> None:
        source = str(source).strip()
        league_id = str(league_id).strip()
        source_item_id = str(source_item_id).strip()
        status = str(status).strip()
        if not source or not league_id or not source_item_id or not status:
            raise ValueError(
                "Historical fetch source, league, item, and status are required"
            )
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO historical_fetch_state(
                    source, league_id, source_item_id, status,
                    points_written, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, league_id, source_item_id) DO UPDATE SET
                    status = excluded.status,
                    points_written = excluded.points_written,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    source,
                    league_id,
                    source_item_id,
                    status,
                    max(0, int(points_written)),
                    last_error,
                    iso_utc(),
                ),
            )

    def upsert_seasonal_prices(
        self, prices: Iterable[dict[str, Any]]
    ) -> int:
        """Store one consolidated source price per item and league day."""

        now = iso_utc()

        def required_text(price: dict[str, Any], key: str) -> str:
            value = price.get(key)
            text = str(value).strip() if value is not None else ""
            if not text:
                raise ValueError(f"Seasonal price {key} must be non-empty")
            return text

        def json_text(value: Any) -> str:
            if value is None:
                value = {}
            if isinstance(value, str):
                try:
                    json.loads(value)
                except json.JSONDecodeError:
                    return json.dumps(value, separators=(",", ":"))
                return value
            return json.dumps(value, separators=(",", ":"))

        rows: list[tuple[Any, ...]] = []
        for price in prices:
            league_day = int(price.get("league_day", 0))
            if league_day < 1:
                raise ValueError("Seasonal price league_day must be at least 1")
            divine_value = float(price["divine_value"])
            if divine_value <= 0:
                raise ValueError("Seasonal price divine_value must be positive")
            confidence = max(
                0.0, min(1.0, float(price.get("confidence", 0.5)))
            )
            rows.append(
                (
                    required_text(price, "league_id"),
                    required_text(price, "item_key"),
                    required_text(price, "source"),
                    required_text(price, "source_item_id"),
                    league_day,
                    required_text(price, "observed_at"),
                    price.get("chaos_value"),
                    divine_value,
                    price.get("volume"),
                    confidence,
                    price.get("snapshot_id"),
                    json_text(price.get("details_json", price.get("details"))),
                    str(price.get("updated_at") or now),
                )
            )
        if not rows:
            return 0

        with self.transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO seasonal_prices(
                    league_id, item_key, source, source_item_id, league_day,
                    observed_at, chaos_value, divine_value, volume, confidence,
                    snapshot_id, details_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(league_id, item_key, source, league_day) DO UPDATE SET
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
                rows,
            )
            return connection.total_changes - before

    @staticmethod
    def _compact_epoch(value: Any) -> int:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Compact seasonal price observed_at must be non-empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                f"Invalid compact seasonal observed_at {text!r}"
            ) from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.astimezone(timezone.utc).timestamp())

    def _compact_dimension_ids(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        column: str,
        cache_name: str,
        values: Iterable[str],
    ) -> dict[str, int]:
        """Resolve compact dictionary IDs without one query per price row."""

        cache = self._compact_dimension_cache[cache_name]
        requested = list(dict.fromkeys(str(value) for value in values))
        missing = [value for value in requested if value not in cache]
        for offset in range(0, len(missing), 400):
            batch = missing[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            for row in connection.execute(
                f"SELECT id, {column} FROM {table} "
                f"WHERE {column} IN ({placeholders})",
                batch,
            ):
                cache[str(row[column])] = int(row["id"])
        missing = [value for value in requested if value not in cache]
        if missing:
            connection.executemany(
                f"INSERT OR IGNORE INTO {table}({column}) VALUES (?)",
                ((value,) for value in missing),
            )
            for offset in range(0, len(missing), 400):
                batch = missing[offset : offset + 400]
                placeholders = ",".join("?" for _ in batch)
                for row in connection.execute(
                    f"SELECT id, {column} FROM {table} "
                    f"WHERE {column} IN ({placeholders})",
                    batch,
                ):
                    cache[str(row[column])] = int(row["id"])
        unresolved = [value for value in requested if value not in cache]
        if unresolved:
            raise RuntimeError(
                f"Could not resolve compact {cache_name} IDs: {unresolved[:3]}"
            )
        return {value: cache[value] for value in requested}

    def upsert_compact_seasonal_prices(
        self,
        prices: Iterable[dict[str, Any]],
        *,
        staging: bool = True,
    ) -> int:
        """Store golden completed-league rows in the hosted compact format.

        Only official poe.ninja history (or its private staging source) is
        accepted because the physical row deliberately omits a repeated source
        string.  Callers must omit archive-only identities; the full local
        importer remains responsible for retaining those research rows.
        """

        normalized: list[dict[str, Any]] = []
        for raw in prices:
            source = str(raw.get("source") or "").strip()
            if source != "poe.ninja-history" and not source.startswith(
                "poe.ninja-history-staging-"
            ):
                raise ValueError(
                    "Compact seasonal storage accepts only poe.ninja-history"
                )
            league_id = str(raw.get("league_id") or "").strip()
            item_key = str(raw.get("item_key") or "").strip()
            source_item_id = str(raw.get("source_item_id") or "").strip()
            if not league_id or not item_key or not source_item_id:
                raise ValueError(
                    "Compact seasonal league/item/source-item IDs are required"
                )
            league_day = int(raw.get("league_day") or 0)
            divine_value = float(raw["divine_value"])
            if league_day < 1 or divine_value <= 0:
                raise ValueError(
                    "Compact seasonal day and Divine value must be positive"
                )
            chaos = raw.get("chaos_value")
            normalized.append(
                {
                    "league_id": league_id,
                    "item_key": item_key,
                    "source_item_id": source_item_id,
                    "league_day": league_day,
                    "observed_epoch": self._compact_epoch(raw.get("observed_at")),
                    "chaos_value": float(chaos) if chaos is not None else None,
                    "divine_value": divine_value,
                    "confidence": max(
                        0.0, min(1.0, float(raw.get("confidence", 0.5)))
                    ),
                }
            )
        if not normalized:
            return 0

        target = (
            "compact_seasonal_prices_staging"
            if staging
            else "compact_seasonal_prices"
        )
        with self.transaction() as connection:
            leagues = self._compact_dimension_ids(
                connection,
                table="compact_seasonal_leagues",
                column="league_id",
                cache_name="league",
                values=(row["league_id"] for row in normalized),
            )
            items = self._compact_dimension_ids(
                connection,
                table="compact_seasonal_items",
                column="item_key",
                cache_name="item",
                values=(row["item_key"] for row in normalized),
            )
            source_items = self._compact_dimension_ids(
                connection,
                table="compact_seasonal_source_items",
                column="source_item_id",
                cache_name="source_item",
                values=(row["source_item_id"] for row in normalized),
            )
            before = connection.total_changes
            connection.executemany(
                f"""
                INSERT INTO {target}(
                    league_key, item_key, source_item_key, league_day,
                    observed_epoch, chaos_value, divine_value, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(league_key, item_key, league_day) DO UPDATE SET
                    source_item_key = excluded.source_item_key,
                    observed_epoch = excluded.observed_epoch,
                    chaos_value = excluded.chaos_value,
                    divine_value = excluded.divine_value,
                    confidence = excluded.confidence
                WHERE excluded.confidence >= {target}.confidence
                """,
                (
                    (
                        leagues[row["league_id"]],
                        items[row["item_key"]],
                        source_items[row["source_item_id"]],
                        row["league_day"],
                        row["observed_epoch"],
                        row["chaos_value"],
                        row["divine_value"],
                        row["confidence"],
                    )
                    for row in normalized
                ),
            )
            return connection.total_changes - before

    def clear_compact_seasonal_staging(self, league_id: str) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM compact_seasonal_prices_staging
                WHERE league_key = (
                    SELECT id FROM compact_seasonal_leagues
                    WHERE league_id = ?
                )
                """,
                (str(league_id),),
            )
            return max(0, int(cursor.rowcount))

    def promote_compact_seasonal_prices(self, league_id: str) -> int:
        """Atomically replace one compact production league from staging."""

        with self.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM compact_seasonal_leagues WHERE league_id = ?",
                (str(league_id),),
            ).fetchone()
            if row is None:
                return 0
            league_key = int(row[0])
            staged = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM compact_seasonal_prices_staging
                    WHERE league_key = ?
                    """,
                    (league_key,),
                ).fetchone()[0]
            )
            if staged <= 0:
                return 0
            connection.execute(
                "DELETE FROM compact_seasonal_prices WHERE league_key = ?",
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
            return staged

    def seasonal_price_storage_counts(
        self,
        league_id: str,
        *,
        source: str = "poe.ninja-history",
    ) -> dict[str, Any]:
        """Return exact full, compact, and effective row counts for a league."""

        with closing(self.connect()) as connection:
            full = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM seasonal_prices
                    WHERE league_id = ? AND source = ?
                    """,
                    (str(league_id), str(source)),
                ).fetchone()[0]
            )
            compact = 0
            if source == "poe.ninja-history":
                compact = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM compact_seasonal_prices AS price
                        JOIN compact_seasonal_leagues AS league
                          ON league.id = price.league_key
                        WHERE league.league_id = ?
                        """,
                        (str(league_id),),
                    ).fetchone()[0]
                )
        mode = "compact" if compact > 0 else "full"
        return {
            "full": full,
            "compact": compact,
            "effective": compact if compact > 0 else full,
            "storage_mode": mode,
        }

    def compact_official_history_from_full(
        self,
        league_ids: Iterable[str] | None = None,
        *,
        force: bool = False,
        batch_size: int = 50_000,
    ) -> dict[str, Any]:
        """Additively convert eligible full rows to compact hosted storage.

        Conversion is streaming and commits one league atomically.  Completed
        leagues are skipped on rerun unless ``force`` is requested, making a
        multi-league conversion resumable without modifying the full archive.
        """

        requested = None
        if league_ids is not None:
            requested = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in league_ids
                    if str(value).strip()
                )
            )
        with closing(self.connect()) as connection:
            if requested is None:
                leagues = [
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT league_id FROM seasonal_prices
                        WHERE source = 'poe.ninja-history'
                        ORDER BY league_id
                        """
                    )
                ]
            else:
                leagues = requested

        summary: dict[str, Any] = {
            "status": "success",
            "storage_mode": "compact",
            "leagues_converted": 0,
            "leagues_skipped": 0,
            "source_rows_read": 0,
            "stored_rows": 0,
            "leagues": [],
        }
        fetch_size = max(100, int(batch_size))
        for league_id in leagues:
            counts = self.seasonal_price_storage_counts(league_id)
            if counts["compact"] > 0 and not force:
                summary["leagues_skipped"] += 1
                summary["leagues"].append(
                    {
                        "league_id": league_id,
                        "status": "skipped",
                        "stored_rows": counts["compact"],
                    }
                )
                continue
            self.clear_compact_seasonal_staging(league_id)
            source_rows = 0
            with closing(self.connect()) as reader:
                cursor = reader.execute(
                    """
                    SELECT price.league_id, price.item_key, price.source,
                           price.source_item_id, price.league_day,
                           price.observed_at, price.chaos_value,
                           price.divine_value, price.confidence
                    FROM seasonal_prices AS price
                    JOIN historical_assets AS asset
                      ON asset.source = price.source
                     AND asset.source_item_id = price.source_item_id
                    WHERE price.league_id = ?
                      AND price.source = 'poe.ninja-history'
                      AND asset.eligible = 1
                    """,
                    (league_id,),
                )
                while True:
                    batch = cursor.fetchmany(fetch_size)
                    if not batch:
                        break
                    payload = [dict(row) for row in batch]
                    source_rows += len(payload)
                    self.upsert_compact_seasonal_prices(
                        payload,
                        staging=True,
                    )
            stored = self.promote_compact_seasonal_prices(league_id)
            if source_rows > 0 and stored <= 0:
                raise RuntimeError(
                    f"Compact conversion produced no rows for {league_id}"
                )
            summary["leagues_converted"] += 1
            summary["source_rows_read"] += source_rows
            summary["stored_rows"] += stored
            summary["leagues"].append(
                {
                    "league_id": league_id,
                    "status": "success",
                    "source_rows_read": source_rows,
                    "stored_rows": stored,
                }
            )
        return summary

    def seasonal_return_rows(
        self,
        league_day: int,
        horizon: int,
        item_keys: Iterable[str] | None = None,
        *,
        sources: Iterable[str] | str | None = None,
    ) -> list[dict[str, Any]]:
        league_day = int(league_day)
        horizon = int(horizon)
        if league_day < 1 or horizon < 1:
            raise ValueError("League day and horizon must both be positive")

        key_clause = ""
        parameters: list[Any] = [horizon, league_day]
        allowed_sources = normalize_source_filter(sources)
        if allowed_sources == ():
            return []
        source_clause = ""
        if allowed_sources is not None:
            placeholders = ",".join("?" for _ in allowed_sources)
            source_clause = f"AND entry.source IN ({placeholders})"
            parameters.extend(allowed_sources)
        if item_keys is not None:
            if isinstance(item_keys, str):
                keys = [item_keys]
            else:
                keys = list(dict.fromkeys(str(key) for key in item_keys))
            if not keys:
                return []
            placeholders = ",".join("?" for _ in keys)
            key_clause = f"AND entry.item_key IN ({placeholders})"
            parameters.extend(keys)

        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT entry.league_id,
                       leagues.name AS league_name,
                       leagues.start_at AS league_start_at,
                       entry.item_key,
                       assets.name,
                       assets.category,
                       entry.source,
                       entry.source_item_id,
                       entry.league_day AS entry_day,
                       exit.league_day AS exit_day,
                       entry.observed_at AS entry_observed_at,
                       exit.observed_at AS exit_observed_at,
                       entry.chaos_value AS entry_chaos,
                       exit.chaos_value AS exit_chaos,
                       entry.divine_value AS entry_divine,
                       exit.divine_value AS exit_divine,
                       entry.volume AS entry_volume,
                       exit.volume AS exit_volume,
                       entry.confidence AS entry_confidence,
                       exit.confidence AS exit_confidence,
                       MIN(entry.confidence, exit.confidence) AS confidence,
                       (exit.divine_value / entry.divine_value) - 1.0
                           AS forward_return
                FROM seasonal_price_rows AS entry
                JOIN seasonal_price_rows AS exit
                  ON exit.league_id = entry.league_id
                 AND exit.item_key = entry.item_key
                 AND exit.source = entry.source
                 AND exit.source_item_id = entry.source_item_id
                 AND exit.league_day = entry.league_day + ?
                JOIN leagues
                  ON leagues.id = entry.league_id
                JOIN historical_assets AS assets
                  ON assets.source = entry.source
                 AND assets.source_item_id = entry.source_item_id
                WHERE entry.league_day = ?
                  {source_clause}
                  {key_clause}
                ORDER BY entry.item_key, leagues.start_at, entry.league_id,
                         entry.source, entry.source_item_id
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def seasonal_entry_rows(
        self,
        league_day: int,
        item_keys: Iterable[str] | None = None,
        *,
        minimum_confidence: float = 0.5,
        sources: Iterable[str] | str | None = None,
    ) -> list[dict[str, Any]]:
        """Return exact same-day price levels without requiring a future row.

        At most one qualifying provider row is returned per item and league.
        This keeps cross-league fair-value estimates independent of whether
        the same source also has an observation at a requested exit horizon.
        """

        league_day = int(league_day)
        if league_day < 1:
            raise ValueError("League day must be positive")
        confidence_floor = max(0.0, min(1.0, float(minimum_confidence)))

        key_clause = ""
        parameters: list[Any] = [league_day, confidence_floor]
        allowed_sources = normalize_source_filter(sources)
        if allowed_sources == ():
            return []
        source_clause = ""
        if allowed_sources is not None:
            placeholders = ",".join("?" for _ in allowed_sources)
            source_clause = f"AND price.source IN ({placeholders})"
            parameters.extend(allowed_sources)
        if item_keys is not None:
            if isinstance(item_keys, str):
                keys = [item_keys]
            else:
                keys = list(dict.fromkeys(str(key) for key in item_keys))
            if not keys:
                return []
            placeholders = ",".join("?" for _ in keys)
            key_clause = f"AND price.item_key IN ({placeholders})"
            parameters.extend(keys)

        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT price.league_id,
                           leagues.name AS league_name,
                           leagues.start_at AS league_start_at,
                           price.item_key,
                           price.source,
                           price.source_item_id,
                           price.league_day AS entry_day,
                           price.observed_at AS entry_observed_at,
                           price.chaos_value AS entry_chaos,
                           price.divine_value AS entry_divine,
                           price.volume AS entry_volume,
                           price.confidence AS entry_confidence,
                           ROW_NUMBER() OVER (
                               PARTITION BY price.item_key, price.league_id
                               ORDER BY price.confidence DESC,
                                        price.updated_at DESC,
                                        price.source,
                                        price.source_item_id
                           ) AS preference_rank
                    FROM seasonal_price_rows AS price
                    JOIN leagues
                      ON leagues.id = price.league_id
                    WHERE price.league_day = ?
                      AND price.confidence >= ?
                      AND price.divine_value > 0
                      {source_clause}
                      {key_clause}
                )
                SELECT league_id, league_name, league_start_at, item_key,
                       source, source_item_id, entry_day, entry_observed_at,
                       entry_chaos, entry_divine, entry_volume,
                       entry_confidence
                FROM ranked
                WHERE preference_rank = 1
                ORDER BY item_key, league_start_at, league_id
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def seasonal_status_counts(self) -> dict[str, int]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM historical_assets)
                        AS catalog_assets,
                    (SELECT COUNT(*) FROM historical_assets
                     WHERE eligible = 1)
                        AS eligible_assets,
                    (SELECT COUNT(*) FROM seasonal_price_rows)
                        AS seasonal_prices,
                    (SELECT COUNT(DISTINCT league_id)
                     FROM seasonal_price_rows)
                        AS historical_leagues,
                    (SELECT COUNT(*) FROM compact_seasonal_prices)
                        AS compact_seasonal_prices,
                    (SELECT COUNT(*) FROM historical_fetch_state
                     WHERE LOWER(status) IN (
                         'success', 'succeeded', 'complete', 'completed'
                     ))
                        AS completed_fetches,
                    (SELECT COUNT(*) FROM historical_fetch_state
                     WHERE LOWER(status) IN (
                         'success', 'succeeded', 'complete', 'completed',
                         'partial'
                     ))
                        AS usable_fetches
                """
            ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def save_meta_class_snapshot(
        self,
        *,
        league_id: str,
        observed_at: str,
        source: str,
        league_day: int,
        class_counts: dict[str, int],
        class_shares: dict[str, float] | None = None,
        sample_size: int | None = None,
        page_count: int = 0,
        snapshot_ids: Iterable[int] = (),
    ) -> dict[str, Any]:
        """Persist a derived class-popularity sample.

        Raw source pages remain in ``raw_snapshots``. This compact table keeps
        the aggregate needed by recommendation queries, so reading the latest
        meta profile never has to decompress or parse those HTML pages again.
        """

        league_id = str(league_id).strip()
        observed_at = str(observed_at).strip()
        source = str(source).strip()
        if not league_id or not observed_at or not source:
            raise ValueError(
                "Meta snapshot league, observation time, and source are required"
            )
        league_day = int(league_day)
        if league_day < 1:
            raise ValueError("Meta snapshot league_day must be at least 1")

        normalized_counts: dict[str, int] = {}
        for raw_name, raw_count in class_counts.items():
            name = str(raw_name).strip()
            count = int(raw_count)
            if not name or count <= 0:
                continue
            normalized_counts[name] = normalized_counts.get(name, 0) + count

        counted_size = sum(normalized_counts.values())
        if sample_size is None:
            sample_size = counted_size
        sample_size = int(sample_size)
        if sample_size < 0:
            raise ValueError("Meta snapshot sample_size cannot be negative")
        if counted_size > sample_size:
            raise ValueError(
                "Meta snapshot class counts cannot exceed sample_size"
            )

        denominator = sample_size or counted_size
        if class_shares is None:
            shares = {
                name: count / denominator
                for name, count in normalized_counts.items()
                if denominator > 0
            }
        else:
            shares: dict[str, float] = {}
            for raw_name, raw_share in class_shares.items():
                name = str(raw_name).strip()
                share = float(raw_share)
                if not name or not math.isfinite(share) or share < 0 or share > 1:
                    continue
                shares[name] = share
        normalized_snapshot_ids = list(
            dict.fromkeys(
                int(snapshot_id)
                for snapshot_id in snapshot_ids
                if int(snapshot_id) > 0
            )
        )
        now = iso_utc()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO meta_class_snapshots(
                    league_id, observed_at, source, league_day, sample_size,
                    page_count, counts_json, shares_json, snapshot_ids_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(league_id, observed_at, source) DO UPDATE SET
                    league_day = excluded.league_day,
                    sample_size = excluded.sample_size,
                    page_count = excluded.page_count,
                    counts_json = excluded.counts_json,
                    shares_json = excluded.shares_json,
                    snapshot_ids_json = excluded.snapshot_ids_json
                """,
                (
                    league_id,
                    observed_at,
                    source,
                    league_day,
                    sample_size,
                    max(0, int(page_count)),
                    json.dumps(
                        normalized_counts, sort_keys=True, separators=(",", ":")
                    ),
                    json.dumps(shares, sort_keys=True, separators=(",", ":")),
                    json.dumps(normalized_snapshot_ids, separators=(",", ":")),
                    now,
                ),
            )
        result = self.get_meta_class_snapshot(
            league_id,
            observed_at,
            source=source,
        )
        if result is None:  # pragma: no cover - guarded by the successful insert
            raise RuntimeError("Meta snapshot could not be read after writing")
        return result

    def get_meta_class_snapshot(
        self,
        league_id: str,
        observed_at: str,
        *,
        source: str = "ggg-public-ladder",
    ) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT league_id, observed_at, source, league_day, sample_size,
                       page_count, counts_json, shares_json, snapshot_ids_json,
                       created_at
                FROM meta_class_snapshots
                WHERE league_id = ? AND observed_at = ? AND source = ?
                LIMIT 1
                """,
                (str(league_id), str(observed_at), str(source)),
            ).fetchone()
        return self._meta_snapshot_from_row(row) if row else None

    def latest_meta_class_snapshot(
        self,
        league_id: str,
        *,
        source: str = "ggg-public-ladder",
    ) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT league_id, observed_at, source, league_day, sample_size,
                       page_count, counts_json, shares_json, snapshot_ids_json,
                       created_at
                FROM meta_class_snapshots
                WHERE league_id = ? AND source = ?
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (str(league_id), str(source)),
            ).fetchone()
        return self._meta_snapshot_from_row(row) if row else None

    def nearest_meta_class_snapshot(
        self,
        league_id: str,
        league_day: int,
        *,
        source: str = "ggg-public-ladder",
    ) -> dict[str, Any] | None:
        """Return the archived profile closest to the requested league day."""

        league_day = max(1, int(league_day))
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT league_id, observed_at, source, league_day, sample_size,
                       page_count, counts_json, shares_json, snapshot_ids_json,
                       created_at
                FROM meta_class_snapshots
                WHERE league_id = ? AND source = ?
                ORDER BY ABS(league_day - ?) ASC,
                         CASE WHEN league_day <= ? THEN 0 ELSE 1 END ASC,
                         observed_at DESC
                LIMIT 1
                """,
                (str(league_id), str(source), league_day, league_day),
            ).fetchone()
        return self._meta_snapshot_from_row(row) if row else None

    def list_meta_class_snapshots(
        self,
        *,
        league_ids: Iterable[str] | None = None,
        source: str = "ggg-public-ladder",
        latest_only: bool = False,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [str(source)]
        league_clause = ""
        if league_ids is not None:
            ids = list(
                dict.fromkeys(
                    str(league_id).strip()
                    for league_id in league_ids
                    if str(league_id).strip()
                )
            )
            if not ids:
                return []
            league_clause = (
                "AND snapshots.league_id IN ("
                + ",".join("?" for _ in ids)
                + ")"
            )
            parameters.extend(ids)

        latest_clause = ""
        if latest_only:
            latest_clause = """
                AND snapshots.observed_at = (
                    SELECT MAX(newer.observed_at)
                    FROM meta_class_snapshots AS newer
                    WHERE newer.league_id = snapshots.league_id
                      AND newer.source = snapshots.source
                )
            """

        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT snapshots.league_id, snapshots.observed_at,
                       snapshots.source, snapshots.league_day,
                       snapshots.sample_size, snapshots.page_count,
                       snapshots.counts_json,
                       snapshots.shares_json, snapshots.snapshot_ids_json,
                       snapshots.created_at
                FROM meta_class_snapshots AS snapshots
                WHERE snapshots.source = ?
                  {league_clause}
                  {latest_clause}
                ORDER BY snapshots.league_id, snapshots.observed_at DESC
                """,
                parameters,
            ).fetchall()
        return [self._meta_snapshot_from_row(row) for row in rows]

    @staticmethod
    def _meta_snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for output_key, storage_key, fallback in (
            ("class_counts", "counts_json", {}),
            ("class_shares", "shares_json", {}),
            ("snapshot_ids", "snapshot_ids_json", []),
        ):
            raw = result.pop(storage_key)
            try:
                result[output_key] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                result[output_key] = fallback
        return result

    def set_setting(self, key: str, value: Any) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, separators=(",", ":")), iso_utc()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default

    def public_settings(self) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT key, value_json FROM settings ORDER BY key"
            ).fetchall()
        hidden = {"ggg_oauth_token", "oauth_token", "api_key"}
        result: dict[str, Any] = {}
        for row in rows:
            if row["key"].lower() in hidden:
                continue
            try:
                result[row["key"]] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                continue
        return result

    def save_recommendations(
        self, league_id: str, budget: float, horizon: int, payload: dict[str, Any]
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO recommendation_runs(
                    league_id, generated_at, budget, horizon_days, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    league_id,
                    payload["generated_at"],
                    budget,
                    horizon,
                    json.dumps(payload, separators=(",", ":")),
                ),
            )

    def status_counts(self, league_id: str | None = None) -> dict[str, int]:
        with closing(self.connect()) as connection:
            if league_id:
                snapshots = connection.execute(
                    "SELECT COUNT(*) FROM raw_snapshots WHERE league_id = ?",
                    (league_id,),
                ).fetchone()[0]
                price_points = connection.execute(
                    "SELECT COUNT(*) FROM price_points WHERE league_id = ?",
                    (league_id,),
                ).fetchone()[0]
                exchange_hours = connection.execute(
                    """
                    SELECT COUNT(DISTINCT observed_at)
                    FROM price_points
                    WHERE league_id = ? AND source = 'ggg-currency-exchange'
                    """,
                    (league_id,),
                ).fetchone()[0]
            else:
                snapshots = connection.execute(
                    "SELECT COUNT(*) FROM raw_snapshots"
                ).fetchone()[0]
                price_points = connection.execute(
                    "SELECT COUNT(*) FROM price_points"
                ).fetchone()[0]
                exchange_hours = connection.execute(
                    """
                    SELECT COUNT(DISTINCT league_id || ':' || observed_at)
                    FROM price_points
                    WHERE source = 'ggg-currency-exchange'
                    """
                ).fetchone()[0]
        size_bytes = 0
        for database_file in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                size_bytes += database_file.stat().st_size
            except OSError:
                continue
        counts = {
            "snapshots": int(snapshots),
            "price_points": int(price_points),
            "exchange_hours": int(exchange_hours),
            "size_bytes": int(size_bytes),
        }
        with closing(self.connect()) as connection:
            counts["total_snapshots"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM raw_snapshots"
                ).fetchone()[0]
            )
        counts.update(self.seasonal_status_counts())
        return counts

    def last_sync_at(self, league_id: str | None = None) -> str | None:
        parameters: tuple[Any, ...] = ()
        clause = ""
        if league_id:
            clause = "AND league_id = ?"
            parameters = (league_id,)
        with closing(self.connect()) as connection:
            row = connection.execute(
                f"""
                SELECT finished_at FROM sync_runs
                WHERE status IN ('success', 'partial', 'demo')
                  AND finished_at IS NOT NULL
                  {clause}
                ORDER BY finished_at DESC LIMIT 1
                """,
                parameters,
            ).fetchone()
        return row["finished_at"] if row else None

    def latest_successful_sync_window(
        self,
        league_id: str,
    ) -> dict[str, str] | None:
        """Return the exact window of the newest usable live sync.

        Current overview rows absent from that window are no longer members
        of poe.ninja's current catalog. Keeping the start timestamp lets the
        recommendation layer reject a variant that disappeared upstream even
        when its previous observation is from the same UTC league day.

        A partial run is deliberately usable here: at least one current
        poe.ninja endpoint succeeded, and rows from failed endpoints were not
        refreshed. Using the partial run's start as the membership cutoff
        therefore keeps refreshed categories while failing closed for every
        stale category and variant. Falling back to an older all-success run
        would make those stale rows appear live again.
        """

        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT started_at, finished_at, status
                FROM sync_runs
                WHERE league_id = ?
                  AND status IN ('success', 'partial')
                  AND finished_at IS NOT NULL
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """,
                (str(league_id),),
            ).fetchone()
        return dict(row) if row else None

    def healthcheck(self) -> bool:
        try:
            with closing(self.connect()) as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False
