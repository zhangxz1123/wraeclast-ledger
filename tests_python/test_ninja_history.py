from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from poe_advisor.models import FetchResult, League
from poe_advisor.ninja_history import (
    DUMP_SETTING_PREFIX,
    DownloadReceipt,
    PoeNinjaHistoryImporter,
    PoeNinjaHistoryService,
    PoeNinjaDumpClient,
    _confidence_value,
    parse_dump_catalog,
)
from poe_advisor.normalization import normalize_poe_ninja
from poe_advisor.storage import Storage


LEAGUE = "Fixture"
MIN_DATE = "2024-01-01T00:00:00Z"
MAX_DATE = "2024-01-02T00:00:00Z"


def fixture_zip(
    *,
    include_second_anchor: bool = True,
    second_anchor: float = 120,
) -> bytes:
    currencies = [
        "League;Date;Get;Pay;Value;Confidence",
        f"{LEAGUE};2024-01-01;Divine Orb;Chaos Orb;100;0.99",
        f"{LEAGUE};2024-01-01;Chromatic Orb;Chaos Orb;0.5;0.8",
        # Reciprocal/non-Chaos quotes must never replace direct Chaos quotes.
        f"{LEAGUE};2024-01-01;Chromatic Orb;Divine Orb;999;0.1",
        f"{LEAGUE};2024-01-01;Chaos Orb;Chromatic Orb;2;0.1",
    ]
    if include_second_anchor:
        currencies.extend(
            [
                f"{LEAGUE};2024-01-02;Divine Orb;Chaos Orb;{second_anchor};0.95",
                f"{LEAGUE};2024-01-02;Chromatic Orb;Chaos Orb;0.6;0.75",
            ]
        )
    items = [
        "League;Date;Id;Type;Name;BaseType;Variant;Links;Value;Confidence",
        (
            f"{LEAGUE};2024-01-01;95714;SkillGem;"
            "Awakened Enlighten Support;;Level 4, 0% Quality, Corrupted;;250;0.9"
        ),
        (
            f"{LEAGUE};2024-01-02;95714;SkillGem;"
            "Awakened Enlighten Support;;Level 4, 0% Quality, Corrupted;;240;0.85"
        ),
        (
            f"{LEAGUE};2024-01-01;99999;SkillGem;Unknown Support;"
            ";Level 1;;50;0.7"
        ),
        (
            f"{LEAGUE};2024-01-01;95714;SkillGem;Wrong Reused Name;"
            ";Level 4, 0% Quality, Corrupted;;500;1"
        ),
    ]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{LEAGUE}.currency.csv", "\n".join(currencies))
        archive.writestr(f"{LEAGUE}.items.csv", "\n".join(items))
    return output.getvalue()


class FakeDumpClient:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.downloads = 0

    @staticmethod
    def dump_url(league_name: str) -> str:
        return f"fixture://dump?name={league_name}"

    def fetch_catalog(self) -> FetchResult:
        payload = [
            {
                "leagueName": LEAGUE,
                "minDate": MIN_DATE,
                "maxDate": MAX_DATE,
                "zipName": f"{LEAGUE}.zip",
            }
        ]
        return FetchResult(
            url="fixture://catalog",
            status=200,
            payload=payload,
            raw=b"[]",
            etag=None,
            last_modified=None,
            fetched_at="2026-07-31T00:00:00Z",
        )

    def download_dump(
        self, league_name: str, destination: str | Path
    ) -> DownloadReceipt:
        self.downloads += 1
        Path(destination).write_bytes(self.raw)
        return DownloadReceipt(
            url=self.dump_url(league_name),
            status=200,
            bytes_written=len(self.raw),
            sha256=hashlib.sha256(self.raw).hexdigest(),
        )


class PoeNinjaHistoryImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = Storage(Path(self.directory.name) / "ledger.sqlite3")
        self.storage.upsert_league(
            League(
                id="Current",
                name="Current",
                start_at="2026-07-25T00:00:00Z",
            ),
            current=True,
        )
        points = normalize_poe_ninja(
            {
                "lines": [
                    {
                        "id": 95714,
                        "detailsId": "awakened-enlighten-support-4c",
                        "name": "Awakened Enlighten Support",
                        "category": "SkillGem",
                        "variant": "Level 4, 0% Quality, Corrupted",
                        "chaosValue": 300,
                        "divineValue": 2,
                        "listingCount": 20,
                    },
                    {
                        "id": "chromatic",
                        "detailsId": "chromatic-orb",
                        "name": "Chromatic Orb",
                        "category": "Currency",
                        "chaosValue": 0.75,
                        "divineValue": 0.005,
                        "listingCount": 100,
                    },
                ]
            },
            league_id="Current",
            category="SkillGem",
            observed_at="2026-07-31T00:00:00Z",
            snapshot_id=1,
        )
        for point in points:
            point.snapshot_id = None
        self.storage.insert_price_points(points)

    def test_imports_direct_prices_with_exact_item_id_and_divine_anchor(
        self,
    ) -> None:
        client = FakeDumpClient(fixture_zip())
        importer = PoeNinjaHistoryImporter(
            self.storage,
            client,
            batch_size=100,
            temporary_directory=self.directory.name,
        )

        first = importer.sync()
        second = importer.sync()

        self.assertEqual(first["status"], "success")
        self.assertEqual(first["dumps_imported"], 1)
        self.assertEqual(first["unmatched_item_rows"], 1)
        self.assertEqual(first["identity_mismatch_rows"], 1)
        self.assertEqual(second["dumps_skipped"], 1)
        self.assertEqual(client.downloads, 1)

        with closing(self.storage.connect()) as connection:
            item_rows = connection.execute(
                """
                SELECT item_key, source_item_id, league_day, chaos_value,
                       divine_value, source, details_json
                FROM seasonal_prices
                WHERE league_id = ? AND source_item_id = '95714'
                ORDER BY league_day
                """,
                (LEAGUE,),
            ).fetchall()
            chromatic = connection.execute(
                """
                SELECT league_day, chaos_value, divine_value
                FROM seasonal_prices
                WHERE league_id = ?
                  AND item_key = 'currency:chromatic-orb'
                ORDER BY league_day
                """,
                (LEAGUE,),
            ).fetchall()
            source_count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM seasonal_prices
                WHERE league_id = ? AND source = 'poe.ninja-history'
                """,
                (LEAGUE,),
            ).fetchone()["count"]

        self.assertEqual(len(item_rows), 2)
        self.assertIn("awakened-enlighten-support-4c", item_rows[0]["item_key"])
        self.assertAlmostEqual(item_rows[0]["chaos_value"], 250.0)
        self.assertAlmostEqual(item_rows[0]["divine_value"], 2.5)
        self.assertAlmostEqual(item_rows[1]["divine_value"], 2.0)
        self.assertEqual(item_rows[0]["source"], "poe.ninja-history")
        self.assertEqual(len(chromatic), 2)
        self.assertAlmostEqual(chromatic[0]["chaos_value"], 0.5)
        self.assertAlmostEqual(chromatic[0]["divine_value"], 0.005)
        # Two unmatched/reused-ID rows are retained under archive-only keys;
        # they remain queryable but can never join the current ranking.
        self.assertEqual(source_count, 8)

        marker = self.storage.get_setting(f"{DUMP_SETTING_PREFIX}{LEAGUE}")
        self.assertEqual(marker["status"], "success")
        self.assertEqual(marker["zip_name"], f"{LEAGUE}.zip")
        state = self.storage.get_source_state(
            "poe.ninja-history",
            client.dump_url(LEAGUE),
            LEAGUE,
            "archive",
        )
        self.assertEqual(state["status"], "success")

        # A marker cannot hide missing production rows.
        with self.storage.transaction() as connection:
            connection.execute(
                """
                DELETE FROM seasonal_prices
                WHERE league_id = ? AND source = 'poe.ninja-history'
                  AND rowid = (
                      SELECT MIN(rowid) FROM seasonal_prices
                      WHERE league_id = ? AND source = 'poe.ninja-history'
                  )
                """,
                (LEAGUE, LEAGUE),
            )
        repaired = importer.sync()
        self.assertEqual(repaired["dumps_imported"], 1)
        self.assertEqual(client.downloads, 2)

    def test_never_borrows_a_divine_anchor_from_another_day(self) -> None:
        client = FakeDumpClient(fixture_zip(include_second_anchor=False))
        importer = PoeNinjaHistoryImporter(
            self.storage,
            client,
            temporary_directory=self.directory.name,
        )

        summary = importer.sync()

        self.assertGreaterEqual(summary["missing_anchor_rows"], 1)
        with closing(self.storage.connect()) as connection:
            item_days = connection.execute(
                """
                SELECT league_day FROM seasonal_prices
                WHERE league_id = ? AND source_item_id = '95714'
                ORDER BY league_day
                """,
                (LEAGUE,),
            ).fetchall()
        self.assertEqual([row["league_day"] for row in item_days], [1])

    def test_normalization_preserves_both_poe_ninja_identities(self) -> None:
        with closing(self.storage.connect()) as connection:
            row = connection.execute(
                """
                SELECT details_json FROM price_points
                WHERE league_id = 'Current' AND name =
                    'Awakened Enlighten Support'
                """
            ).fetchone()
        import json

        details = json.loads(row["details_json"])
        self.assertEqual(details["poe_ninja_id"], "95714")
        self.assertEqual(details["detailsId"], "awakened-enlighten-support-4c")

    def test_service_matches_legacy_backfill_status_interface(self) -> None:
        client = FakeDumpClient(fixture_zip())
        service = PoeNinjaHistoryService(
            self.storage,
            client,
            league_names=(LEAGUE,),
            temporary_directory=self.directory.name,
        )

        summary = service.backfill("Current", max_items=1)

        self.assertEqual(summary["status"], "success")
        self.assertTrue(summary["max_items_ignored"])
        self.assertEqual(summary["histories_fetched"], 1)
        self.assertGreater(summary["assets_written"], 0)
        self.assertFalse(service.is_syncing)
        self.assertEqual(service.progress()["status"], "success")
        self.assertEqual(service.last_summary["status"], "success")

    def test_archive_only_row_remaps_when_current_identity_appears(self) -> None:
        client = FakeDumpClient(fixture_zip())
        importer = PoeNinjaHistoryImporter(
            self.storage,
            client,
            temporary_directory=self.directory.name,
        )
        importer.sync()
        points = normalize_poe_ninja(
            {
                "lines": [
                    {
                        "id": 99999,
                        "detailsId": "unknown-support-current",
                        "name": "Unknown Support",
                        "category": "SkillGem",
                        "variant": "Level 1",
                        "chaosValue": 50,
                        "divineValue": 0.5,
                    }
                ]
            },
            league_id="Current",
            category="SkillGem",
            observed_at="2026-07-31T01:00:00Z",
            snapshot_id=2,
        )
        points[0].snapshot_id = None
        self.storage.insert_price_points(points)

        summary = importer.sync()

        self.assertEqual(client.downloads, 1)
        self.assertGreaterEqual(summary["archive_assets_remapped"], 1)
        with closing(self.storage.connect()) as connection:
            row = connection.execute(
                """
                SELECT asset.item_key, asset.eligible, COUNT(price.league_day)
                FROM historical_assets AS asset
                JOIN seasonal_prices AS price
                  ON price.source = asset.source
                 AND price.source_item_id = asset.source_item_id
                WHERE asset.source = 'poe.ninja-history'
                  AND asset.name = 'Unknown Support'
                GROUP BY asset.item_key, asset.eligible
                """
            ).fetchone()
        self.assertEqual(row["eligible"], 1)
        self.assertIn("unknown-support-current", row["item_key"])
        self.assertEqual(row[2], 1)

    def test_implausible_direct_divine_anchor_fails_without_production_rows(self) -> None:
        client = FakeDumpClient(fixture_zip(second_anchor=10_000))
        importer = PoeNinjaHistoryImporter(
            self.storage,
            client,
            temporary_directory=self.directory.name,
        )

        summary = importer.sync()

        self.assertEqual(summary["status"], "partial")
        with closing(self.storage.connect()) as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM seasonal_prices
                WHERE source LIKE 'poe.ninja-history%'
                """
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_high_confidence_direct_anchor_jump_remains_golden(self) -> None:
        client = FakeDumpClient(fixture_zip(second_anchor=500))
        importer = PoeNinjaHistoryImporter(
            self.storage,
            client,
            temporary_directory=self.directory.name,
        )

        summary = importer.sync()

        self.assertEqual(summary["status"], "success")
        with closing(self.storage.connect()) as connection:
            second_day = connection.execute(
                """
                SELECT divine_value FROM seasonal_prices
                WHERE league_id = ? AND source_item_id = '95714'
                  AND league_day = 2
                """,
                (LEAGUE,),
            ).fetchone()[0]
        self.assertAlmostEqual(second_day, 240.0 / 500.0)

    def test_hosted_compact_import_keeps_exact_eligible_rows_and_markers(
        self,
    ) -> None:
        compact_storage = Storage(
            self.storage.path,
            compact_history=True,
        )
        client = FakeDumpClient(fixture_zip())
        importer = PoeNinjaHistoryImporter(
            compact_storage,
            client,
            batch_size=100,
            temporary_directory=self.directory.name,
        )

        first = importer.sync()
        second = importer.sync()

        self.assertEqual(first["status"], "success")
        self.assertEqual(first["storage_mode"], "compact")
        self.assertEqual(first["raw_source_rows_seen"], 10)
        self.assertEqual(first["normalized_source_rows"], 8)
        self.assertEqual(first["eligible_source_rows"], 6)
        self.assertEqual(first["seasonal_rows_written"], 6)
        self.assertEqual(second["dumps_skipped"], 1)
        self.assertEqual(client.downloads, 1)

        counts = compact_storage.seasonal_price_storage_counts(LEAGUE)
        self.assertEqual(counts["full"], 0)
        self.assertEqual(counts["compact"], 6)
        self.assertEqual(counts["effective"], 6)
        marker = compact_storage.get_setting(f"{DUMP_SETTING_PREFIX}{LEAGUE}")
        self.assertEqual(marker["storage_mode"], "compact")
        self.assertEqual(marker["raw_source_rows_seen"], 10)
        self.assertEqual(marker["normalized_source_rows"], 8)
        self.assertEqual(marker["eligible_source_rows"], 6)
        self.assertEqual(marker["stored_seasonal_rows"], 6)
        self.assertEqual(marker["seasonal_rows_written"], 6)

        curves = compact_storage.seasonal_price_curve_rows(
            next(
                row["item_key"]
                for row in compact_storage.latest_item_prices("Current").values()
                if row["name"] == "Awakened Enlighten Support"
            ),
            [LEAGUE],
            minimum_confidence=0.0,
            sources=("poe.ninja-history",),
        )
        self.assertEqual(len(curves), 2)
        self.assertEqual([row["league_day"] for row in curves], [1, 2])
        self.assertEqual(
            [row["observed_at"] for row in curves],
            ["2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z"],
        )
        self.assertAlmostEqual(curves[0]["chaos_value"], 250.0)
        self.assertAlmostEqual(curves[0]["divine_value"], 2.5)
        self.assertEqual(curves[0]["source"], "poe.ninja-history")
        self.assertEqual(curves[0]["source_item_id"], "95714")
        self.assertAlmostEqual(curves[0]["confidence"], 0.9)

        with compact_storage.transaction() as connection:
            connection.execute(
                """
                DELETE FROM compact_seasonal_prices
                WHERE league_key = (
                    SELECT id FROM compact_seasonal_leagues
                    WHERE league_id = ?
                )
                  AND item_key = (
                    SELECT MIN(item_key) FROM compact_seasonal_prices
                    WHERE league_key = (
                        SELECT id FROM compact_seasonal_leagues
                        WHERE league_id = ?
                    )
                  )
                  AND league_day = 1
                """,
                (LEAGUE, LEAGUE),
            )
        repaired = importer.sync()
        self.assertEqual(repaired["dumps_imported"], 1)
        self.assertEqual(client.downloads, 2)
        self.assertEqual(
            compact_storage.seasonal_price_storage_counts(LEAGUE)["compact"],
            6,
        )


class DumpCatalogTests(unittest.TestCase):
    def test_categorical_confidence_is_preserved(self) -> None:
        self.assertEqual(_confidence_value("High"), 0.9)
        self.assertEqual(_confidence_value("Medium"), 0.65)
        self.assertEqual(_confidence_value("Low"), 0.35)

    def test_downloader_passes_timeout_as_keyword_not_request_body(self) -> None:
        raw = fixture_zip()
        observed: dict[str, object] = {}

        class Response(io.BytesIO):
            status = 200
            headers: dict[str, str] = {}

            @staticmethod
            def getcode() -> int:
                return 200

        def opener(request: object, *, timeout: float) -> Response:
            observed["request"] = request
            observed["timeout"] = timeout
            return Response(raw)

        client = PoeNinjaDumpClient(opener=opener)
        with tempfile.TemporaryDirectory() as directory:
            receipt = client.download_dump(
                LEAGUE,
                Path(directory) / "fixture.zip",
            )
        self.assertEqual(receipt.bytes_written, len(raw))
        self.assertEqual(observed["timeout"], client.config.timeout_seconds)

    def test_catalog_uses_exact_public_field_names(self) -> None:
        descriptors = parse_dump_catalog(
            [
                {
                    "leagueName": "Settlers",
                    "minDate": "2024-07-26T00:00:00Z",
                    "maxDate": "2025-06-09T00:00:00Z",
                    "zipName": "Settlers.zip",
                }
            ]
        )
        self.assertEqual(descriptors[0].league_name, "Settlers")
        self.assertEqual(descriptors[0].start_date.isoformat(), "2024-07-26")

    def test_catalog_rejects_missing_identity_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "zipName"):
            parse_dump_catalog(
                [
                    {
                        "leagueName": "Settlers",
                        "minDate": "2024-07-26T00:00:00Z",
                        "maxDate": "2025-06-09T00:00:00Z",
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
