from __future__ import annotations

from email.message import Message
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp.bootstrap import build_web_bootstrap
from webapp import legacy_review_routes
from webapp import server as web_server


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WebRoutingBoundaryTests(unittest.TestCase):
    @staticmethod
    def make_handler(path: str) -> web_server.DashboardHandler:
        handler = object.__new__(web_server.DashboardHandler)
        headers = Message()
        headers["Host"] = "127.0.0.1:8765"
        handler.headers = headers
        handler.path = path
        return handler

    def test_web_bootstrap_contains_only_current_panel_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            (root / "records").mkdir()
            (root / "data" / "000005.SZ").mkdir(parents=True)
            (root / "config" / "local_stock_library.csv").write_text(
                "symbol,stock_name\n000001.SZ,本地股票\n",
                encoding="utf-8",
            )
            (root / "watchlist.csv").write_text(
                "symbol,stock_name\n000002.SZ,观察股票\n",
                encoding="utf-8",
            )
            (root / "records" / "positions_snapshot.csv").write_text(
                "symbol,stock_name\n000003.SZ,持仓股票\n",
                encoding="utf-8",
            )
            (root / "records" / "my_trades.csv").write_text(
                "symbol,stock_name\n000004.SZ,成交股票\n",
                encoding="utf-8",
            )

            payload = build_web_bootstrap(root, csrf_token="csrf-test")

        self.assertEqual({"today", "symbols", "csrf_token"}, set(payload))
        self.assertEqual("csrf-test", payload["csrf_token"])
        self.assertEqual(
            [
                {"symbol": "000001.SZ", "stock_name": "本地股票"},
                {"symbol": "000002.SZ", "stock_name": "观察股票"},
                {"symbol": "000003.SZ", "stock_name": "持仓股票"},
                {"symbol": "000004.SZ", "stock_name": "成交股票"},
                {"symbol": "000005.SZ", "stock_name": "000005.SZ"},
            ],
            payload["symbols"],
        )
        self.assertTrue(
            all(set(item) == {"symbol", "stock_name"} for item in payload["symbols"])
        )

    def test_server_uses_the_minimal_web_bootstrap_builder(self):
        handler = self.make_handler("/api/bootstrap")
        captured: dict[str, object] = {}
        handler._send_json = lambda payload, status=HTTPStatus.OK: captured.update(
            payload=payload,
            status=status,
        )
        expected = {
            "today": "2026-08-26",
            "symbols": [{"symbol": "000001.SZ", "stock_name": "平安银行"}],
            "csrf_token": "csrf-test",
        }
        with patch.object(
            web_server,
            "build_web_bootstrap",
            return_value=expected,
        ) as builder:
            handler.do_GET()

        self.assertEqual(expected, captured["payload"])
        builder.assert_called_once_with(
            web_server.PROJECT_ROOT,
            csrf_token=web_server.CSRF_TOKEN,
        )

    def test_legacy_review_routes_are_outside_the_current_route_file(self):
        server_source = (PROJECT_ROOT / "webapp" / "server.py").read_text(
            encoding="utf-8"
        )
        for dependency in (
            "from src.full_review",
            "from src.review_cases",
            "from src.review_history",
        ):
            self.assertNotIn(dependency, server_source)
        for path in legacy_review_routes.LEGACY_REVIEW_GET_PATHS:
            self.assertTrue(legacy_review_routes.is_legacy_review_get_path(path))
        self.assertTrue(
            legacy_review_routes.is_legacy_review_get_path("/api/reviews/review-1")
        )
        for path in legacy_review_routes.LEGACY_REVIEW_POST_PATHS:
            self.assertTrue(legacy_review_routes.is_legacy_review_post_path(path))
        self.assertTrue(
            legacy_review_routes.is_legacy_review_delete_path(
                "/api/review-drafts/draft-1"
            )
        )

    def test_legacy_review_routes_remain_reachable_for_all_verbs(self):
        get_handler = self.make_handler("/api/reviews?limit=5")
        with patch.object(
            web_server,
            "handle_legacy_review_get",
            return_value=True,
        ) as get_route:
            get_handler.do_GET()
        get_route.assert_called_once()

        post_handler = self.make_handler("/api/review-drafts")
        post_handler._validate_write_security = lambda: None
        post_handler._read_json = lambda: {"symbol": "000001.SZ"}
        post_result: dict[str, object] = {}
        post_handler._send_json = (
            lambda payload, status=HTTPStatus.OK: post_result.update(
                payload=payload,
                status=status,
            )
        )
        with patch.object(
            web_server,
            "handle_legacy_review_post",
            return_value=(True, {"record": {"id": "draft-1"}}),
        ) as post_route:
            post_handler.do_POST()
        self.assertEqual("draft-1", post_result["payload"]["record"]["id"])
        post_route.assert_called_once()

        delete_handler = self.make_handler("/api/review-drafts/draft-1")
        delete_handler._validate_write_security = lambda: None
        delete_result: dict[str, object] = {}
        delete_handler._send_json = (
            lambda payload, status=HTTPStatus.OK: delete_result.update(
                payload=payload,
                status=status,
            )
        )
        with patch.object(
            web_server,
            "handle_legacy_review_delete",
            return_value=(True, {"deleted": True}, HTTPStatus.OK),
        ) as delete_route:
            delete_handler.do_DELETE()
        self.assertEqual({"deleted": True}, delete_result["payload"])
        delete_route.assert_called_once()


if __name__ == "__main__":
    unittest.main()
