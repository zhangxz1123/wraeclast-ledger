from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

from poe_advisor.__main__ import _validate_host
from poe_advisor.demo import seed_demo
from poe_advisor.historical import COMPLETED_LEAGUES
from poe_advisor.models import PricePoint, iso_utc, parse_datetime
from poe_advisor.recommendation import RecommendationEngine
from poe_advisor.server import (
    AdvisorApplication,
    AdvisorHTTPServer,
    serve_in_thread,
)
from poe_advisor.storage import Storage


class FakeSyncService:
    def __init__(self) -> None:
        self.is_syncing = False
        self.backfills: list[int] = []
        self.current_history_requests: list[list[str]] = []

    def sync(self, *, backfill_hours: int = 0) -> dict[str, Any]:
        self.backfills.append(backfill_hours)
        return {
            "ok": True,
            "mode": "offline-test",
            "backfill_hours": backfill_hours,
        }

    def sync_current_item_histories(
        self,
        league: Any,
        item_keys: list[str],
        *,
        max_items: int = 100,
    ) -> dict[str, Any]:
        del league
        selected = list(item_keys[:max_items])
        self.current_history_requests.append(selected)
        return {
            "status": "success",
            "requested_items": len(selected),
            "matched_items": len(selected),
            "rows_written": 0,
            "warnings": [],
        }


class CLIValidationTests(unittest.TestCase):
    def test_server_binding_is_loopback_only(self) -> None:
        self.assertEqual(_validate_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(_validate_host("localhost"), "localhost")
        with self.assertRaisesRegex(ValueError, "local-only"):
            _validate_host("0.0.0.0")

    def test_recommendations_are_cached_per_league_day_and_horizon(
        self,
    ) -> None:
        class CountingEngine:
            def __init__(self) -> None:
                self.calls = 0

            def generate(
                self,
                league: Any,
                *,
                budget: float,
                horizon: int,
                persist: bool,
            ) -> dict[str, Any]:
                del persist
                self.calls += 1
                return {
                    "mode": "forecast_ranking",
                    "generated_at": "2026-07-31T00:00:00Z",
                    "league": {
                        "id": league.id,
                        "day": league.day,
                    },
                    "budget": budget,
                    "horizon": horizon,
                    "rankings": [],
                    "recommendations": [],
                }

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "cache.sqlite3")
            seed_demo(storage, days=3)
            engine = CountingEngine()
            application = AdvisorApplication(
                storage=storage,
                sync_service=FakeSyncService(),
                recommendation_engine=engine,  # type: ignore[arg-type]
                web_dir=Path(directory),
            )

            first = application.recommendations(budget=100, horizon=7)
            second = application.recommendations(budget=25, horizon=7)
            third = application.recommendations(budget=25, horizon=3)

        self.assertEqual(engine.calls, 2)
        self.assertEqual(first["budget"], 100)
        self.assertEqual(second["budget"], 25)
        self.assertEqual(third["horizon"], 3)


class HTTPServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(cls.temporary_directory.name)
        web_dir = root / "web"
        web_dir.mkdir()
        (web_dir / "index.html").write_text(
            "<!doctype html><title>Fixture Ledger</title>",
            encoding="utf-8",
        )
        (web_dir / "styles.css").write_text(
            "body { color: #eee; }",
            encoding="utf-8",
        )
        (web_dir / "app.js").write_text(
            "document.body.dataset.ready = 'true';",
            encoding="utf-8",
        )
        (web_dir / "og.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")

        storage = Storage(root / "server.sqlite3")
        seed_demo(storage, days=20)
        cls.fake_sync = FakeSyncService()
        application = AdvisorApplication(
            storage=storage,
            sync_service=cls.fake_sync,
            recommendation_engine=RecommendationEngine(storage),
            web_dir=web_dir.resolve(),
        )
        cls.server = AdvisorHTTPServer(("127.0.0.1", 0), application)
        cls.thread = serve_in_thread(cls.server)
        cls.base_url = (
            f"http://127.0.0.1:{cls.server.server_address[1]}"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary_directory.cleanup()

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Any | None = None,
    ) -> tuple[int, dict[str, Any], Any]:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            body = error.read()
            return error.code, json.loads(body), error.headers
        with response:
            body = response.read()
            return response.status, json.loads(body), response.headers

    def test_health_status_and_static_assets(self) -> None:
        status, health, headers = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            health,
            {"ok": True, "service": "wraeclast-ledger"},
        )
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        status, payload, _ = self.request_json("/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["demo_mode"])
        self.assertEqual(payload["league"]["name"], "Offline Demo Softcore")
        self.assertGreater(payload["database"]["price_points"], 0)
        self.assertFalse(payload["syncing"])
        self.assertFalse(payload["history_syncing"])
        self.assertIn("seasonal_prices", payload["database"])
        self.assertIn("usable_fetches", payload["database"])
        self.assertIn("meta", payload)
        self.assertFalse(payload["meta"]["available"])

        with urllib.request.urlopen(self.base_url + "/", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertIn(b"Fixture Ledger", response.read())
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
        with urllib.request.urlopen(
            self.base_url + "/app.js",
            timeout=5,
        ) as response:
            self.assertEqual(
                response.headers["Content-Type"],
                "text/javascript; charset=utf-8",
            )

        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=5,
        )
        connection.request("HEAD", "/index.html")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"")
        self.assertGreater(int(response.getheader("Content-Length")), 0)
        connection.close()

    def test_recommendations_and_history_endpoints(self) -> None:
        connection = self.server.application.storage.connect()
        before_runs = connection.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0]
        connection.close()
        status, payload, _ = self.request_json(
            "/api/recommendations?budget=100&horizon=7"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["budget"], 100.0)
        self.assertEqual(payload["horizon"], 7)
        self.assertIsInstance(payload["rankings"], list)
        self.assertNotIn("recommendations", payload)
        self.assertLessEqual(len(payload["rankings"]), 100)
        self.assertEqual(payload["ranking_summary"]["limit"], 100)
        self.request_json("/api/recommendations?budget=90&horizon=3")
        connection = self.server.application.storage.connect()
        after_runs = connection.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(after_runs, before_runs)

        item_key = "currency:veiled-orb"
        storage = self.server.application.storage
        existing_history = storage.all_time_item_history(
            "demo-softcore-fixture",
            item_key,
        )
        latest_observed = parse_datetime(existing_history[-1]["observed_at"])
        assert latest_observed is not None
        storage.insert_price_points(
            [
                PricePoint(
                    league_id="demo-softcore-fixture",
                    item_key=item_key,
                    name="Veiled Orb",
                    category="Currency",
                    source="low-confidence-fixture",
                    observed_at=iso_utc(
                        latest_observed + timedelta(seconds=1)
                    ),
                    chaos_value=16.0,
                    divine_value=0.123,
                    confidence=0.2,
                )
            ]
        )
        for spec in COMPLETED_LEAGUES:
            storage.upsert_league(spec.as_league(), current=False)
        storage.upsert_historical_assets(
            [
                {
                    "source": "poe.ninja-history",
                    "source_item_id": "veiled-orb-history",
                    "item_key": item_key,
                    "name": "Veiled Orb",
                    "category": "Currency",
                }
            ]
        )
        seasonal_rows = []
        fixture_values = {
            "Mercenaries": {1: 10.0},
            "Keepers": {1: 20.0, 2: 25.0},
            "Mirage": {1: 40.0, 2: 45.0},
        }
        for spec in COMPLETED_LEAGUES:
            for league_day, divine_value in fixture_values.get(
                spec.league_id, {}
            ).items():
                seasonal_rows.append(
                    {
                        "league_id": spec.league_id,
                        "item_key": item_key,
                        "source": "poe.ninja-history",
                        "source_item_id": "veiled-orb-history",
                        "league_day": league_day,
                        "observed_at": spec.start_at,
                        "divine_value": divine_value,
                        "confidence": 0.8,
                    }
                )
        seasonal_rows.append(
            {
                "league_id": "Settlers",
                "item_key": item_key,
                "source": "poe.ninja-history",
                "source_item_id": "veiled-orb-history",
                "league_day": 1,
                "observed_at": "2024-07-26T20:00:00Z",
                "divine_value": 1000.0,
                "confidence": 0.49,
            }
        )
        storage.upsert_seasonal_prices(seasonal_rows)
        status, history, _ = self.request_json(
            "/api/history?key=" + urllib.parse.quote(item_key)
        )
        self.assertEqual(status, 200)
        self.assertEqual(history["item"]["item_key"], item_key)
        self.assertGreater(len(history["history"]), 0)
        comparison = history["seasonal_comparison"]
        self.assertGreater(
            len(comparison["current_league"]["points"]),
            0,
        )
        self.assertEqual(
            comparison["current_league"]["points"][-1]["divine_value"],
            0.123,
        )
        self.assertFalse(
            comparison["current_league"]["points"][-1]["model_grade"],
        )
        current_coverage = comparison["current_league"]["coverage"]
        self.assertGreaterEqual(
            current_coverage["through_league_day"],
            current_coverage["last_observed_day"],
        )
        self.assertEqual(current_coverage["interpolation"], "none")
        self.assertFalse(current_coverage["dated_archive_attempted"])
        self.assertIn(
            "No dated upstream item history",
            current_coverage["source_limitation"],
        )
        self.assertEqual(
            [
                point["league_day"]
                for point in comparison["weighted_historical"]["points"]
            ],
            [1, 2],
        )
        expected_day_one = (
            40.0
            + 20.0 * 0.72
            + 10.0 * 0.72**2
        ) / (1.0 + 0.72 + 0.72**2)
        self.assertAlmostEqual(
            comparison["weighted_historical"]["points"][0]["divine_value"],
            expected_day_one,
        )
        self.assertEqual(
            comparison["weighted_historical"]["points"][0][
                "contributing_leagues"
            ],
            3,
        )
        self.assertEqual(
            [
                curve["league_id"]
                for curve in comparison["past_leagues"]
            ],
            ["Mirage", "Keepers", "Mercenaries"],
        )
        self.assertEqual(
            comparison["calculation"]["recency_decay_per_league"],
            0.72,
        )
        self.assertEqual(
            comparison["calculation"]["confidence_floor"],
            0.5,
        )
        self.assertEqual(
            comparison["calculation"]["current_confidence_floor"],
            0.0,
        )
        self.assertEqual(
            comparison["calculation"]["historical_confidence_floor"],
            0.5,
        )
        self.assertEqual(
            comparison["calculation"]["display_grade_floor"],
            0.5,
        )
        self.assertEqual(
            comparison["calculation"]["interpolation"],
            "none",
        )

        status, error, _ = self.request_json("/api/history")
        self.assertEqual(status, 400)
        self.assertFalse(error["ok"])
        status, error, _ = self.request_json(
            "/api/recommendations?budget=not-a-number"
        )
        self.assertEqual(status, 400)
        self.assertIn("Budget", error["detail"])

    def test_sync_and_settings_post_validation(self) -> None:
        connection = self.server.application.storage.connect()
        before_runs = connection.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0]
        connection.close()
        status, payload, _ = self.request_json(
            "/api/sync",
            method="POST",
            payload={"backfill_hours": 12, "budget": 80, "horizon": 3},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.fake_sync.backfills[-1], 12)
        self.assertIn("current_history_sync", payload)
        self.assertGreater(
            payload["current_history_sync"]["requested_items"],
            0,
        )
        self.assertLessEqual(
            payload["current_history_sync"]["requested_items"],
            100,
        )
        self.assertEqual(
            len(self.fake_sync.current_history_requests[-1]),
            payload["current_history_sync"]["requested_items"],
        )
        self.assertEqual(payload["meta_sync"]["status"], "skipped")
        self.assertIsNone(payload["recommendation_summary"]["reserve"])
        self.assertIsNone(payload["recommendation_summary"]["invested"])
        connection = self.server.application.storage.connect()
        after_runs = connection.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(after_runs, before_runs + 1)

        status, error, _ = self.request_json(
            "/api/sync",
            method="POST",
            payload={"backfill_hours": 1.5},
        )
        self.assertEqual(status, 400)
        self.assertIn("whole number", error["detail"])

        status, error, _ = self.request_json(
            "/api/seasonal/backfill",
            method="POST",
            payload={"max_items": 80},
        )
        self.assertEqual(status, 400)
        self.assertIn("live market sync", error["detail"])

        settings = {
            "exchange_categories": ["Currency", "Fragment"],
            "item_categories": ["Scarab"],
        }
        status, returned, _ = self.request_json(
            "/api/settings",
            method="POST",
            payload=settings,
        )
        self.assertEqual(status, 200)
        self.assertEqual(returned, settings)
        status, returned, _ = self.request_json("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(returned, settings)

        status, error, _ = self.request_json(
            "/api/settings",
            method="POST",
            payload={"api_key": "secret"},
        )
        self.assertEqual(status, 400)
        self.assertIn("Unsupported setting", error["detail"])

    def test_unknown_route_and_non_object_json_are_rejected(self) -> None:
        status, error, _ = self.request_json("/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(error["detail"], "Route not found.")

        request = urllib.request.Request(
            self.base_url + "/api/settings",
            data=b"[]",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 400)
        self.assertIn(
            "JSON object",
            json.loads(raised.exception.read())["detail"],
        )


if __name__ == "__main__":
    unittest.main()
