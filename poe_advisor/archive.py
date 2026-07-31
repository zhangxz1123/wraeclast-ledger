from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any


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

    try:
        with (
            closing(sqlite3.connect(str(source_path))) as source,
            closing(sqlite3.connect(str(backup_path))) as destination,
        ):
            source.backup(destination)
            result = destination.execute("PRAGMA quick_check").fetchone()
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
        return {
            "database": str(source_path),
            "output": str(destination_path),
            "database_bytes": backup_path.stat().st_size,
            "compressed_bytes": destination_path.stat().st_size,
            "compression": "gzip",
            "integrity": "ok",
        }
    finally:
        backup_path.unlink(missing_ok=True)
        compressed_path.unlink(missing_ok=True)
