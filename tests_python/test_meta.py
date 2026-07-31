from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from poe_advisor.meta import (
    LADDER_SOURCE,
    POE_NINJA_META_SOURCE,
    MetaService,
    PoeNinjaBuildClient,
    PublicLadderClient,
    class_counts,
    ladder_entries,
    nearest_poe_ninja_time_machine,
    parse_embedded_ladder_json,
    parse_poe_ninja_build_meta,
)
from poe_advisor.models import DataSourceError, FetchResult, League
from poe_advisor.storage import Storage


def ladder_html(entries: list[dict[str, Any]]) -> bytes:
    payload = {
        "ladder": {
            "total": len(entries),
            "page": 1,
            "limit": 20,
            "entries": entries,
        }
    }
    encoded = json.dumps(payload, separators=(",", ":"))
    return (
        "<html><script>var before = {}; var json = "
        + encoded
        + ", ladder = new LadderEntries(json);</script></html>"
    ).encode("utf-8")


def entry(rank: int, name: str, class_name: str) -> dict[str, Any]:
    return {
        "rank": rank,
        "character": {
            "id": f"character-{rank}",
            "name": name,
            "class": class_name,
        },
    }


def poe_ninja_build_html(
    shares: dict[str, str],
    *,
    sample_size: int,
    time_options: list[tuple[str, str]] | None = None,
) -> bytes:
    options = "".join(
        f'<option value="{value}">{label}</option>'
        for value, label in [
            ("", "Latest snapshot"),
            *(time_options or []),
        ]
    )
    classes = "".join(
        (
            '<div role="option" class="filter-list-cell">'
            f'<div class="class-name">{name}</div>'
            f'<div class="class-percentage">{percentage}</div>'
            "</div>"
        )
        for name, percentage in shares.items()
    )
    return (
        "<html><body>"
        f'<select id="Time machine">{options}</select>'
        f'<div role="listbox">{classes}</div>'
        f"<p>Found {sample_size:,} characters.</p>"
        "</body></html>"
    ).encode("utf-8")


class FakeLadderClient:
    SOURCE = LADDER_SOURCE

    def __init__(self, pages: dict[tuple[str, int], bytes]):
        self.pages = pages
        self.calls: list[tuple[str, int]] = []

    def ladder_url(self, league: str, page: int = 1) -> str:
        return f"https://fixture.invalid/ladders/{league}?page={page}"

    def fetch_page(self, league: str, page: int = 1) -> FetchResult:
        self.calls.append((league, page))
        raw = self.pages.get((league, page), ladder_html([]))
        return FetchResult(
            url=self.ladder_url(league, page),
            status=200,
            payload=None,
            raw=raw,
            etag=f'"{league}-{page}"',
            last_modified=None,
            fetched_at="2026-07-30T12:00:00Z",
        )


class FakePoeNinjaBuildClient:
    SOURCE = POE_NINJA_META_SOURCE

    def __init__(self, pages: dict[tuple[str, str | None], bytes]):
        self.pages = pages
        self.calls: list[tuple[str, str | None]] = []

    def build_url(
        self,
        league: str,
        *,
        timemachine: str | None = None,
    ) -> str:
        suffix = f"?timemachine={timemachine}" if timemachine else ""
        return f"https://fixture.invalid/poe1/builds/{league.lower()}{suffix}"

    def fetch_page(
        self,
        league: str,
        *,
        timemachine: str | None = None,
    ) -> FetchResult:
        self.calls.append((league, timemachine))
        raw = self.pages.get((league, timemachine), b"<html></html>")
        return FetchResult(
            url=self.build_url(league, timemachine=timemachine),
            status=200,
            payload=None,
            raw=raw,
            etag=None,
            last_modified=None,
            fetched_at="2026-07-30T12:00:00Z",
        )


class MetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.storage = Storage(
            Path(self.temporary_directory.name) / "meta.sqlite3"
        )
        self.now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        self.current = League(
            id="Current Softcore",
            name="Current Softcore",
            start_at="2026-07-24T20:00:00Z",
            # GGG/third-party league discovery can use this as an
            # open-ended sentinel for the active league.
            end_at="0001-01-01T00:00:00Z",
        )
        self.storage.upsert_league(self.current)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_embedded_json_parser_and_unique_class_counts(self) -> None:
        raw = ladder_html(
            [
                entry(1, 'Curly {Hero} "One"', "Elementalist"),
                entry(2, "Second", "Necromancer"),
                entry(2, "Second", "Necromancer"),
            ]
        )
        payload = parse_embedded_ladder_json(raw)
        entries = ladder_entries(payload)
        counts, sample_size = class_counts(entries)

        self.assertEqual(sample_size, 2)
        self.assertEqual(counts, {"Elementalist": 1, "Necromancer": 1})
        with self.assertRaises(DataSourceError):
            parse_embedded_ladder_json("<html>no ladder state</html>")

    def test_sync_persists_pages_profile_and_uses_freshness_cache(self) -> None:
        client = FakeLadderClient(
            {
                (self.current.id, 1): ladder_html(
                    [
                        entry(1, "One", "Elementalist"),
                        entry(2, "Two", "Necromancer"),
                    ]
                ),
                (self.current.id, 2): ladder_html(
                    [entry(3, "Three", "Elementalist")]
                ),
            }
        )
        service = MetaService(
            self.storage,
            client,
            page_delay_seconds=0,
            now=lambda: self.now,
        )

        result = service.sync_league(self.current, pages=2)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["league_day"], 6)
        self.assertEqual(result["sample_size"], 3)
        self.assertEqual(result["class_counts"]["Elementalist"], 2)
        self.assertAlmostEqual(result["class_shares"]["Elementalist"], 2 / 3)
        self.assertEqual(result["snapshots_written"], 2)
        self.assertEqual(
            self.storage.status_counts(self.current.id)["snapshots"],
            2,
        )

        stored = self.storage.latest_meta_class_snapshot(self.current.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["snapshot_ids"], [1, 2])
        self.assertEqual(stored["league_day"], 6)

        cached = service.sync_league(self.current.id, pages=2)
        self.assertEqual(cached["status"], "cached")
        self.assertEqual(cached["snapshots_written"], 0)
        self.assertEqual(len(client.calls), 2)

    def test_ended_league_profile_uses_final_league_day(self) -> None:
        ended = League(
            id="Ended",
            name="Ended",
            start_at="2026-01-01T20:00:00Z",
            end_at="2026-01-10T20:00:00Z",
        )
        client = FakeLadderClient(
            {(ended.id, 1): ladder_html([entry(1, "One", "Slayer")])}
        )
        service = MetaService(
            self.storage,
            client,
            page_delay_seconds=0,
            now=lambda: self.now,
        )

        result = service.sync_league(ended, pages=1)
        self.assertEqual(result["league_day"], 10)
        self.assertEqual(self.storage.get_league(ended.id), ended)

    def test_multiplier_uses_nearest_same_day_profile_and_reports_fallback(
        self,
    ) -> None:
        past_exact = League(id="Past Exact", name="Past Exact")
        past_fallback = League(id="Past Fallback", name="Past Fallback")
        for league in (past_exact, past_fallback):
            self.storage.upsert_league(league, current=False)

        def save(
            league_id: str,
            observed_at: str,
            league_day: int,
            elementalist_count: int,
        ) -> None:
            self.storage.save_meta_class_snapshot(
                league_id=league_id,
                observed_at=observed_at,
                source=LADDER_SOURCE,
                league_day=league_day,
                class_counts={
                    "Elementalist": elementalist_count,
                    "Other": 500 - elementalist_count,
                },
                sample_size=500,
                page_count=25,
            )

        save(self.current.id, "2026-07-30T12:00:00Z", 6, 300)
        save(past_exact.id, "2025-01-06T12:00:00Z", 6, 100)
        # A much later snapshot must not replace the exact day-six baseline.
        save(past_exact.id, "2025-04-10T12:00:00Z", 100, 450)
        save(past_fallback.id, "2024-04-10T12:00:00Z", 40, 50)

        service = MetaService(
            self.storage,
            FakeLadderClient({}),
            page_delay_seconds=0,
            now=lambda: self.now,
        )
        signal = service.ascendancy_multiplier(
            self.current,
            [past_exact, past_fallback],
            "Elementalist",
        )

        self.assertEqual(signal["status"], "ok")
        self.assertAlmostEqual(signal["current_share"], 0.6)
        self.assertAlmostEqual(signal["historical_share"], 0.15)
        self.assertEqual(signal["historical_league_count"], 2)
        self.assertEqual(signal["target_league_day"], 6)
        self.assertEqual(signal["baseline_quality"], "fallback")
        by_league = {
            profile["league_id"]: profile
            for profile in signal["historical_leagues"]
        }
        self.assertEqual(by_league[past_exact.id]["league_day"], 6)
        self.assertEqual(by_league[past_exact.id]["alignment"], "exact")
        self.assertEqual(by_league[past_fallback.id]["alignment"], "fallback")
        self.assertGreater(signal["multiplier"], 1.0)
        self.assertLessEqual(signal["multiplier"], 1.35)

    def test_sync_leagues_returns_json_serializable_partial_summary(self) -> None:
        client = FakeLadderClient(
            {
                (self.current.id, 1): ladder_html(
                    [entry(1, "One", "Elementalist")]
                )
            }
        )
        service = MetaService(
            self.storage,
            client,
            page_delay_seconds=0,
            now=lambda: self.now,
        )
        summary = service.sync_leagues(
            [self.current, {"id": "Missing Fixture", "name": "Missing"}],
            pages=1,
        )

        # The fake returns an empty page for the second league, which is
        # reported without discarding the successful first profile.
        self.assertEqual(summary["requested_leagues"], 2)
        self.assertEqual(summary["synced_leagues"], 1)
        self.assertEqual(summary["failed_leagues"], 1)
        json.dumps(summary)

    def test_public_ladder_url_encodes_league_name(self) -> None:
        client = PublicLadderClient(base_url="https://example.invalid")
        self.assertEqual(
            client.ladder_url("Keepers of the Flame", 2),
            "https://example.invalid/ladders/league/"
            "Keepers%20of%20the%20Flame?page=2",
        )

    def test_poe_ninja_parser_reads_visible_share_and_time_machine(self) -> None:
        raw = poe_ninja_build_html(
            {
                "Elementalist": "27%",
                "Chieftain": "9%",
                "Not A Class": "64%",
            },
            sample_size=124_381,
            time_options=[("day-5", "Day 5"), ("hour-18", "Hour 18")],
        )
        parsed = parse_poe_ninja_build_meta(raw)

        self.assertEqual(parsed["sample_size"], 124_381)
        self.assertEqual(
            parsed["class_shares"],
            {"Chieftain": 0.09, "Elementalist": 0.27},
        )
        self.assertEqual(
            nearest_poe_ninja_time_machine(parsed["time_options"], 6),
            ("day-5", 5),
        )

    def test_poe_ninja_is_preferred_and_uses_nearest_historical_snapshot(
        self,
    ) -> None:
        past = League(
            id="Mirage",
            name="Mirage",
            start_at="2026-03-06T19:00:00Z",
            end_at="2026-07-24T20:00:00Z",
        )
        self.storage.upsert_league(past, current=False)
        current_page = poe_ninja_build_html(
            {"Elementalist": "27%", "Chieftain": "9%"},
            sample_size=124_381,
            time_options=[("day-5", "Day 5")],
        )
        historical_index = poe_ninja_build_html(
            {"Elementalist": "7%", "Chieftain": "12%"},
            sample_size=100_000,
            time_options=[("week-2", "Week 2"), ("week-1", "Week 1")],
        )
        historical_week = poe_ninja_build_html(
            {"Elementalist": "10%", "Chieftain": "10%"},
            sample_size=60_000,
        )
        client = FakePoeNinjaBuildClient(
            {
                (self.current.id, None): current_page,
                (past.id, None): historical_index,
                (past.id, "week-1"): historical_week,
            }
        )
        service = MetaService(
            self.storage,
            client,
            page_delay_seconds=0,
            now=lambda: self.now,
        )

        current_result = service.sync_league(self.current)
        historical_result = service.sync_league(
            past,
            target_league_day=6,
        )
        signal = service.ascendancy_multiplier(
            self.current,
            [past],
            "Elementalist",
        )

        self.assertEqual(current_result["source"], POE_NINJA_META_SOURCE)
        self.assertAlmostEqual(
            current_result["class_shares"]["Elementalist"],
            0.27,
        )
        self.assertEqual(historical_result["league_day"], 7)
        self.assertEqual(historical_result["timemachine"], "week-1")
        self.assertEqual(
            client.calls,
            [
                (self.current.id, None),
                (past.id, None),
                (past.id, "week-1"),
            ],
        )
        self.assertEqual(signal["source"], POE_NINJA_META_SOURCE)
        self.assertAlmostEqual(signal["current_share"], 0.27)
        self.assertAlmostEqual(signal["historical_share"], 0.10)
        self.assertEqual(signal["baseline_quality"], "near-day")

    def test_poe_ninja_build_url_uses_the_public_build_page(self) -> None:
        client = PoeNinjaBuildClient(base_url="https://example.invalid")
        self.assertEqual(
            client.build_url("Keepers", timemachine="week-1"),
            "https://example.invalid/poe1/builds/keepers"
            "?timemachine=week-1",
        )


if __name__ == "__main__":
    unittest.main()
