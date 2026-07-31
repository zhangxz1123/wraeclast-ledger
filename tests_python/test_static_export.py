from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from poe_advisor.archive import create_compressed_database_snapshot
from poe_advisor.demo import seed_demo
from poe_advisor.static_export import export_github_pages
from poe_advisor.storage import Storage


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class StaticExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "source.sqlite3"
        seed_demo(Storage(self.database), make_current=True)

    def test_export_is_hashed_complete_and_excludes_database(self) -> None:
        output = self.root / "pages"
        result = export_github_pages(
            database_path=self.database,
            web_dir=PROJECT_ROOT / "web",
            output_path=output,
            repository="example/wraeclast-ledger",
        )

        self.assertTrue(result["ok"])
        self.assertGreater(result["ranked_items"], 0)
        self.assertTrue((output / ".nojekyll").is_file())
        self.assertEqual(
            (output / "index.html").read_text(encoding="utf-8").count(
                'href="./styles.css"'
            ),
            1,
        )
        manifest = json.loads(
            (output / "data" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["mode"], "github-pages")
        self.assertEqual(
            manifest["workflow_url"],
            (
                "https://github.com/example/wraeclast-ledger/actions/"
                "workflows/daily-pages.yml"
            ),
        )
        self.assertRegex(
            manifest["catalog"],
            r"^data/recommendations\.[0-9a-f]{16}\.json$",
        )
        self.assertRegex(
            manifest["status"],
            r"^data/status\.[0-9a-f]{16}\.json$",
        )

        catalog = json.loads(
            (output / manifest["catalog"]).read_text(encoding="utf-8")
        )
        rankings = catalog["rankings"]
        keys = [row["key"] for row in rankings]
        self.assertEqual(len(keys), len(set(keys)))
        for horizon in ("3", "7", "14"):
            ranks = sorted(row["static_ranks"][horizon] for row in rankings)
            self.assertEqual(ranks, list(range(1, len(rankings) + 1)))

        for shard, relative_path in manifest["history_shards"].items():
            self.assertRegex(shard, r"^[0-9a-f]{2}$")
            shard_payload = json.loads(
                (output / relative_path).read_text(encoding="utf-8")
            )
            for item_key in shard_payload["items"]:
                row = next(row for row in rankings if row["key"] == item_key)
                self.assertEqual(row["history_shard"], shard)

        published_files = [
            path.relative_to(output).as_posix() for path in output.rglob("*")
            if path.is_file()
        ]
        self.assertFalse(
            any(
                name.endswith((".sqlite", ".sqlite3", ".sqlite3.gz"))
                for name in published_files
            )
        )

    def test_compressed_snapshot_round_trips_with_integrity(self) -> None:
        compressed = self.root / "archive" / "poe_advisor.sqlite3.gz"
        summary = create_compressed_database_snapshot(
            self.database,
            compressed,
            compression_level=1,
        )
        self.assertEqual(summary["integrity"], "ok")
        self.assertGreater(summary["compressed_bytes"], 0)

        restored = self.root / "restored.sqlite3"
        with gzip.open(compressed, "rb") as source:
            restored.write_bytes(source.read())
        restored_storage = Storage(restored)
        self.assertTrue(restored_storage.healthcheck())
        self.assertEqual(
            restored_storage.status_counts()["price_points"],
            Storage(self.database).status_counts()["price_points"],
        )


if __name__ == "__main__":
    unittest.main()
