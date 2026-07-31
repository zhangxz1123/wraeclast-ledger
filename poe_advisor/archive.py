from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Callable


_PUBLIC_CURSOR_PREFIX = "ggg_currency_cursor:"
_PROHIBITED_PUBLIC_TEXT = (
    "github_pat_",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "authorization:",
    "bearer ",
    "access_token",
    "refresh_token",
    "client_secret",
    "private_key",
    "c:\\users\\",
    "/users/",
    "\\.codex\\",
    "/.codex/",
)


def create_compressed_database_snapshot(
    database_path: str | Path,
    output_path: str | Path,
    *,
    compression_level: int = 6,
) -> dict[str, Any]:
    """Create a consistent gzip-compressed SQLite backup.

    SQLite's online backup API includes committed WAL pages, so this remains
    safe while the local dashboard is open. The destination is replaced only
    after the backup passes ``PRAGMA quick_check`` and compression succeeds.
    """

    return _create_compressed_database_snapshot(
        database_path,
        output_path,
        compression_level=compression_level,
    )


def create_public_market_snapshot(
    database_path: str | Path,
    output_path: str | Path,
    *,
    compression_level: int = 6,
) -> dict[str, Any]:
    """Create a sanitized, updater-compatible public market archive.

    Normalized market, historical, meta, and recommendation tables are kept.
    Raw provider responses and local diagnostics are removed, snapshot foreign
    keys are detached, meta snapshot IDs are cleared, and only settings needed
    by the automated updater are retained. The sanitized copy is vacuumed
    before compression so deleted payload pages cannot remain in the file.

    The source database is never modified.
    """

    return _create_compressed_database_snapshot(
        database_path,
        output_path,
        compression_level=compression_level,
        sanitizer=_sanitize_public_market_database,
    )


def _create_compressed_database_snapshot(
    database_path: str | Path,
    output_path: str | Path,
    *,
    compression_level: int,
    sanitizer: Callable[[sqlite3.Connection], dict[str, int]] | None = None,
) -> dict[str, Any]:
    source_path = Path(database_path).expanduser().resolve()
    destination_path = Path(output_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite archive does not exist: {source_path}")
    if source_path == destination_path:
        raise ValueError("Archive output must differ from the SQLite database.")
    if not 1 <= int(compression_level) <= 9:
        raise ValueError("Compression level must be between 1 and 9.")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    backup_handle = tempfile.NamedTemporaryFile(
        prefix="wraeclast-ledger-",
        suffix=".sqlite3",
        dir=destination_path.parent,
        delete=False,
    )
    backup_path = Path(backup_handle.name)
    backup_handle.close()
    compressed_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
        dir=destination_path.parent,
        delete=False,
    )
    compressed_path = Path(compressed_handle.name)
    compressed_handle.close()

    sanitization: dict[str, int] | None = None
    database_bytes_before_sanitization: int | None = None
    database_bytes_after_sanitization: int | None = None
    try:
        with (
            closing(sqlite3.connect(str(source_path))) as source,
            closing(sqlite3.connect(str(backup_path))) as destination,
        ):
            source.backup(destination)

        database_bytes_before_sanitization = backup_path.stat().st_size
        if sanitizer is not None:
            with closing(
                sqlite3.connect(str(backup_path), isolation_level=None)
            ) as backup:
                backup.row_factory = sqlite3.Row
                backup.execute("PRAGMA busy_timeout = 20000")
                # The source normally uses WAL. Moving this isolated copy back
                # to DELETE mode ensures every sanitized page is in the one
                # database file that will be compressed.
                backup.execute("PRAGMA journal_mode = DELETE")
                backup.execute("PRAGMA foreign_keys = ON")
                sanitization = sanitizer(backup)
                backup.execute("VACUUM")
            database_bytes_after_sanitization = backup_path.stat().st_size

        with closing(sqlite3.connect(str(backup_path))) as validation:
            validation.execute("PRAGMA foreign_keys = ON")
            foreign_key_failures = validation.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_failures:
                first = tuple(foreign_key_failures[0])
                raise RuntimeError(
                    "SQLite backup failed foreign-key validation: "
                    f"{first!r}"
                )
            result = validation.execute("PRAGMA quick_check").fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise RuntimeError(
                    "SQLite backup failed integrity validation: "
                    f"{result[0] if result else 'no result'}"
                )

        with (
            backup_path.open("rb") as source_file,
            compressed_path.open("wb") as raw_destination,
            gzip.GzipFile(
                filename="poe_advisor.sqlite3",
                mode="wb",
                compresslevel=int(compression_level),
                fileobj=raw_destination,
                mtime=0,
            ) as compressed_destination,
        ):
            shutil.copyfileobj(
                source_file,
                compressed_destination,
                length=1024 * 1024,
            )
        os.replace(compressed_path, destination_path)
        summary: dict[str, Any] = {
            "database": str(source_path),
            "output": str(destination_path),
            "database_bytes": backup_path.stat().st_size,
            "compressed_bytes": destination_path.stat().st_size,
            "compression": "gzip",
            "integrity": "ok",
        }
        if sanitization is not None:
            summary.update(
                {
                    "archive_kind": "public-market",
                    "sanitized": True,
                    "database_bytes_before_sanitization": (
                        database_bytes_before_sanitization
                    ),
                    "database_bytes_after_sanitization": (
                        database_bytes_after_sanitization
                    ),
                    "sanitization": sanitization,
                }
            )
        else:
            summary.update({"archive_kind": "full", "sanitized": False})
        return summary
    finally:
        backup_path.unlink(missing_ok=True)
        compressed_path.unlink(missing_ok=True)
        for suffix in ("-journal", "-shm", "-wal"):
            Path(f"{backup_path}{suffix}").unlink(missing_ok=True)


def _sanitize_public_market_database(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    """Strip non-public state from an already isolated SQLite backup."""

    tables = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }
    counts = {
        "snapshot_references_cleared": 0,
        "meta_snapshot_references_cleared": 0,
        "historical_errors_cleared": 0,
        "raw_snapshots_removed": 0,
        "source_state_rows_removed": 0,
        "sync_runs_removed": 0,
        "settings_removed": 0,
        "text_values_scanned": 0,
    }

    connection.execute("BEGIN IMMEDIATE")
    try:
        # Discover every current/future foreign key to raw_snapshots instead
        # of assuming only the two snapshot_id columns in today's schema.
        for table in sorted(tables):
            quoted_table = _quote_identifier(table)
            columns = {
                str(row[1]): row
                for row in connection.execute(
                    f"PRAGMA table_info({quoted_table})"
                )
            }
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list({quoted_table})"
            ).fetchall()
            raw_reference_columns = {
                str(row[3])
                for row in foreign_keys
                if str(row[2]).lower() == "raw_snapshots"
            }
            for column in sorted(raw_reference_columns):
                column_info = columns.get(column)
                if column_info is None:
                    raise RuntimeError(
                        f"Foreign-key column {table}.{column} was not found."
                    )
                if bool(column_info[3]):
                    raise RuntimeError(
                        "Cannot preserve normalized public data because "
                        f"{table}.{column} is a non-null raw snapshot reference."
                    )
                cursor = connection.execute(
                    f"""
                    UPDATE {quoted_table}
                    SET {_quote_identifier(column)} = NULL
                    WHERE {_quote_identifier(column)} IS NOT NULL
                    """
                )
                counts["snapshot_references_cleared"] += max(
                    int(cursor.rowcount), 0
                )

        if "meta_class_snapshots" in tables:
            cursor = connection.execute(
                """
                UPDATE meta_class_snapshots
                SET snapshot_ids_json = '[]'
                WHERE snapshot_ids_json <> '[]'
                """
            )
            counts["meta_snapshot_references_cleared"] = max(
                int(cursor.rowcount), 0
            )

        if "historical_fetch_state" in tables:
            cursor = connection.execute(
                """
                UPDATE historical_fetch_state
                SET last_error = NULL
                WHERE last_error IS NOT NULL
                """
            )
            counts["historical_errors_cleared"] = max(int(cursor.rowcount), 0)

        if "source_state" in tables:
            cursor = connection.execute("DELETE FROM source_state")
            counts["source_state_rows_removed"] = max(int(cursor.rowcount), 0)

        if "sync_runs" in tables:
            cursor = connection.execute("DELETE FROM sync_runs")
            counts["sync_runs_removed"] = max(int(cursor.rowcount), 0)

        if "settings" in tables:
            for row in connection.execute(
                "SELECT key, value_json FROM settings"
            ).fetchall():
                key = str(row[0])
                try:
                    value = json.loads(str(row[1]))
                except (TypeError, json.JSONDecodeError):
                    value = None
                keep = (
                    key.startswith(_PUBLIC_CURSOR_PREFIX)
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                )
                if not keep:
                    connection.execute(
                        "DELETE FROM settings WHERE key = ?",
                        (key,),
                    )
                    counts["settings_removed"] += 1

        if "raw_snapshots" in tables:
            cursor = connection.execute("DELETE FROM raw_snapshots")
            counts["raw_snapshots_removed"] = max(int(cursor.rowcount), 0)

        counts["text_values_scanned"] = _assert_no_private_text(
            connection,
            tables,
        )
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_failures:
            first = tuple(foreign_key_failures[0])
            raise RuntimeError(
                "Public archive sanitization broke a foreign key: "
                f"{first!r}"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return counts


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _assert_no_private_text(
    connection: sqlite3.Connection,
    tables: set[str],
) -> int:
    """Reject common credential and local-path markers before publication."""

    scanned = 0
    for table in sorted(tables):
        quoted_table = _quote_identifier(table)
        text_columns = [
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({quoted_table})"
            )
            if "TEXT" in str(row[2]).upper()
        ]
        if not text_columns:
            continue
        select_columns = ", ".join(
            _quote_identifier(column) for column in text_columns
        )
        for row in connection.execute(
            f"SELECT {select_columns} FROM {quoted_table}"
        ):
            for index, value in enumerate(row):
                if value is None:
                    continue
                scanned += 1
                lowered = str(value).lower()
                if any(marker in lowered for marker in _PROHIBITED_PUBLIC_TEXT):
                    raise RuntimeError(
                        "Public archive contains prohibited private text in "
                        f"{table}.{text_columns[index]}."
                    )
    return scanned
