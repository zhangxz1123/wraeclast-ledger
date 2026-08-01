from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from poe_advisor.__main__ import build_parser
from poe_advisor.automation import run_daily_update


class DailyAutomationTests(unittest.TestCase):
    def test_cli_market_syncs_default_to_no_hourly_audit_backfill(self) -> None:
        parser = build_parser()

        self.assertEqual(parser.parse_args(["sync"]).history_hours, 0)
        self.assertEqual(
            parser.parse_args(["daily-update"]).history_hours,
            0,
        )
        self.assertEqual(
            parser.parse_args(["daily-update"]).current_history_items,
            2000,
        )

    def test_scheduled_workflow_defaults_hourly_audit_backfill_off(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "daily-pages.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('default: "0"', workflow)
        self.assertIn('- "0"', workflow)
        self.assertIn("inputs.history_hours || '0'", workflow)
        self.assertNotIn("inputs.history_hours || '168'", workflow)
        self.assertIn("inputs.current_history_items || '2000'", workflow)

    def test_complete_refresh_runs_curves_history_and_final_model(self) -> None:
        league = SimpleNamespace(id="Live", is_demo=False)
        application = SimpleNamespace()
        application.storage = Mock()
        application.storage.get_current_league.return_value = league
        application.sync_service = Mock()
        application.sync_service.sync.return_value = {
            "ok": True,
            "status": "success",
            "warnings": [],
        }
        application.sync_service.sync_current_item_histories.return_value = {
            "status": "success"
        }
        application.sync_meta = Mock(
            return_value={"status": "success", "failed_leagues": 0}
        )
        application.history_service = Mock()
        application.history_service.backfill.return_value = {
            "status": "success"
        }
        application.recommendation_engine = Mock()
        application.recommendation_engine.generate.side_effect = [
            {
                "rankings": [
                    {"key": "item:a"},
                    {"curve_key": "item:b"},
                ]
            },
            {
                "generated_at": "2026-07-31T00:00:00Z",
                "rankings": [{"key": "item:a"}, {"key": "item:b"}],
            },
        ]

        with patch(
            "poe_advisor.automation.AdvisorApplication.create",
            return_value=application,
        ):
            result = run_daily_update(
                database_path="archive.sqlite3",
                web_dir="web",
                history_hours=24,
                seasonal_items=10,
                current_history_items=2,
            )

        self.assertTrue(result["ok"])
        application.sync_service.sync.assert_called_once_with(
            backfill_hours=24
        )
        application.sync_service.sync_current_item_histories.assert_called_once_with(
            league,
            ["item:a", "item:b"],
            max_items=2,
        )
        application.history_service.backfill.assert_called_once_with(
            league,
            max_items=10,
        )
        self.assertEqual(result["recommendation_summary"]["rankings"], 2)

    def test_core_price_failure_stops_before_optional_stages(self) -> None:
        application = SimpleNamespace()
        application.sync_service = Mock()
        application.sync_service.sync.return_value = {
            "ok": False,
            "status": "failed",
        }
        application.storage = Mock()

        with patch(
            "poe_advisor.automation.AdvisorApplication.create",
            return_value=application,
        ):
            result = run_daily_update(
                database_path="archive.sqlite3",
                web_dir="web",
            )

        self.assertFalse(result["ok"])
        application.sync_service.sync.assert_called_once_with(
            backfill_hours=0
        )
        application.storage.get_current_league.assert_not_called()


if __name__ == "__main__":
    unittest.main()
