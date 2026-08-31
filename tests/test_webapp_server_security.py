from __future__ import annotations

import inspect
from io import BytesIO
from pathlib import Path
import unittest
from email.message import Message
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch

from src.panel_contract import PANEL_API_CAPABILITIES
from webapp import server as web_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DashboardServerSecurityTests(unittest.TestCase):
    @staticmethod
    def make_handler(**headers: str) -> web_server.DashboardHandler:
        handler = object.__new__(web_server.DashboardHandler)
        message = Message()
        for name, value in headers.items():
            message[name.replace("_", "-")] = value
        handler.headers = message
        return handler

    def assert_security_error(
        self,
        expected_code: str,
        **headers: str,
    ) -> None:
        handler = self.make_handler(**headers)
        with self.assertRaises(web_server.RequestSecurityError) as context:
            handler._validate_write_security()
        self.assertEqual(expected_code, context.exception.code)

    def test_valid_loopback_json_request_with_csrf_and_local_origin_is_allowed(self):
        for host, origin in (
            ("127.0.0.1:8765", "http://127.0.0.1:8765"),
            ("localhost:8765", "http://localhost:8765"),
            ("[::1]:8765", "http://[::1]:8765"),
        ):
            with self.subTest(host=host):
                handler = self.make_handler(
                    Host=host,
                    Content_Type="application/json; charset=utf-8",
                    Origin=origin,
                    X_Panel_CSRF=web_server.CSRF_TOKEN,
                )
                handler._validate_write_security()

    def test_write_request_requires_application_json(self):
        self.assert_security_error(
            "JSON_CONTENT_TYPE_REQUIRED",
            Host="127.0.0.1:8765",
            Content_Type="text/plain",
            Origin="http://127.0.0.1:8765",
            X_Panel_CSRF=web_server.CSRF_TOKEN,
        )

    def test_write_request_requires_current_csrf_token(self):
        common = {
            "Host": "127.0.0.1:8765",
            "Content_Type": "application/json",
            "Origin": "http://127.0.0.1:8765",
        }
        self.assert_security_error("CSRF_INVALID", **common)
        self.assert_security_error(
            "CSRF_INVALID",
            **common,
            X_Panel_CSRF="not-the-current-token",
        )

    def test_write_request_rejects_non_loopback_host_and_origin(self):
        self.assert_security_error(
            "LOCAL_HOST_REQUIRED",
            Host="panel.example.com",
            Content_Type="application/json",
            Origin="http://127.0.0.1:8765",
            X_Panel_CSRF=web_server.CSRF_TOKEN,
        )
        self.assert_security_error(
            "ORIGIN_REJECTED",
            Host="127.0.0.1:8765",
            Content_Type="application/json",
            Origin="https://attacker.example",
            X_Panel_CSRF=web_server.CSRF_TOKEN,
        )

    def test_write_request_requires_http_origin_with_exact_request_authority(self):
        common = {
            "Host": "127.0.0.1:8765",
            "Content_Type": "application/json",
            "X_Panel_CSRF": web_server.CSRF_TOKEN,
        }
        for origin in (
            "https://127.0.0.1:8765",
            "http://localhost:8765",
            "http://127.0.0.1:9999",
            "http://127.0.0.1",
        ):
            with self.subTest(origin=origin):
                self.assert_security_error(
                    "ORIGIN_REJECTED",
                    **common,
                    Origin=origin,
                )

    def test_api_get_rejects_non_loopback_host_before_dispatch(self):
        handler = self.make_handler(Host="panel.example.com")
        handler.path = "/api/health"
        captured: dict[str, object] = {}

        def capture(exc: Exception, *, status: int = HTTPStatus.BAD_REQUEST) -> None:
            captured.update(
                error_code=getattr(exc, "code", None),
                status=status,
            )

        handler._structured_error = capture
        handler.do_GET()

        self.assertEqual("LOCAL_HOST_REQUIRED", captured["error_code"])
        self.assertEqual(HTTPStatus.FORBIDDEN, captured["status"])

    def test_health_contract_exposes_minimal_ui_and_csrf_token(self):
        handler = self.make_handler(Host="127.0.0.1:8765")
        handler.path = "/api/health"
        captured: dict[str, object] = {}

        def capture(payload: object, status: int = HTTPStatus.OK) -> None:
            captured.update(payload=payload, status=status)

        handler._send_json = capture
        handler.do_GET()

        self.assertEqual(HTTPStatus.OK, captured["status"])
        payload = captured["payload"]
        self.assertIsInstance(payload, dict)
        self.assertEqual("ok", payload["status"])
        self.assertEqual("0.10", payload["version"])
        self.assertEqual("1.1.0", payload["product_version"])
        self.assertEqual("web-v1.1.0", payload["ui_version"])
        self.assertEqual("WEB-V1.1.0", payload["release_id"])
        self.assertEqual("WEB-20260901-001", payload["build_id"])
        self.assertEqual(
            "2026-09-01T02:15:16+08:00",
            payload["released_at"],
        )
        self.assertEqual("StockRuleReview/0.10", web_server.DashboardHandler.server_version)
        self.assertEqual("panel-domain-v1.8", payload["domain_contract_version"])
        self.assertEqual(web_server.CSRF_TOKEN, payload["csrf_token"])
        self.assertGreaterEqual(len(payload["csrf_token"]), 32)

    def test_transparent_rule_write_endpoints_are_known_paths(self):
        handler = self.make_handler()
        for path in (
            "/api/datasets/scan",
            "/api/market-data/update",
            "/api/market-data/import",
            "/api/stock-catalog/sync",
            "/api/local-stocks",
            "/api/rules",
            "/api/custom-rules",
            "/api/rule-editor/apply",
            "/api/rules/example-rule/validate",
            "/api/rules/example-rule/activate",
            "/api/rule-sets",
            "/api/rule-sets/apply-simple",
            "/api/rule-sets/after-close-v2/validate",
            "/api/rule-sets/after-close-v2/activate",
            "/api/rule-sets/after-close-v2/versions",
            "/api/rule-sets/after-close-v2/clone",
            "/api/screening/preflight",
            "/api/screening/run",
            "/api/trades",
        ):
            with self.subTest(path=path):
                self.assertTrue(handler._known_post_path(path))

        for path in (
            "/api/rules/example-rule/delete",
            "/api/rule-sets/after-close-v2/run",
            "/api/rule-sets/after-close-v2/delete",
            "/api/rule-sets/after-close-v2/retire",
            "/api/datasets/delete",
            "/api/stock-catalog/delete",
            "/api/local-stocks/delete",
            "/api/screening/execute",
        ):
            with self.subTest(path=path):
                self.assertFalse(handler._known_post_path(path))

    def test_every_declared_web_api_route_reaches_the_http_handler(self):
        replacements = {
            "{rule_set_id}": "rs_contract_probe",
            "{run_id}": "contract-probe",
            "{rule_id}": "user.rule_contract_probe",
            "{tag_id}": "tag_abcdef",
        }

        for name, capability in PANEL_API_CAPABILITIES.items():
            method = capability["method"]
            path = capability["web"]
            for placeholder, value in replacements.items():
                path = path.replace(placeholder, value)

            with self.subTest(capability=name, method=method, path=path):
                handler = self.make_handler(Host="127.0.0.1:8765")
                handler.path = path
                captured: dict[str, object] = {}
                handler._send_json = (
                    lambda payload, status=HTTPStatus.OK: captured.update(
                        payload=payload,
                        status=status,
                    )
                )
                handler.send_error = (
                    lambda status, *_args, **_kwargs: captured.update(
                        raw_error=status
                    )
                )

                getattr(handler, f"do_{method}")()

                self.assertNotIn("raw_error", captured)
                self.assertIn("payload", captured)
                if method == "GET":
                    self.assertNotEqual(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        captured["status"],
                    )
                else:
                    # Invalid write security stops before any mutation. Reaching
                    # the structured 403 proves the declared path was admitted
                    # by the real POST/DELETE handler instead of raw-404ing.
                    self.assertEqual(HTTPStatus.FORBIDDEN, captured["status"])

    def test_web_module_assets_reach_the_static_handler(self):
        expected_assets = {
            "/app.js?v=1.1.0": ("app.js", "text/javascript; charset=utf-8"),
            "/platform.js": ("platform.js", "text/javascript; charset=utf-8"),
            "/modules/data.js": ("modules/data.js", "text/javascript; charset=utf-8"),
            "/modules/rules.js": ("modules/rules.js", "text/javascript; charset=utf-8"),
            "/modules/screening.js": (
                "modules/screening.js",
                "text/javascript; charset=utf-8",
            ),
            "/modules/records.js": (
                "modules/records.js",
                "text/javascript; charset=utf-8",
            ),
        }

        for path, expected in expected_assets.items():
            with self.subTest(path=path):
                handler = self.make_handler(Host="127.0.0.1:8765")
                handler.path = path
                captured: dict[str, object] = {}
                handler._send_static = (
                    lambda filename, content_type: captured.update(
                        asset=(filename, content_type)
                    )
                )
                handler.send_error = (
                    lambda status, *_args, **_kwargs: captured.update(
                        raw_error=status
                    )
                )

                handler.do_GET()

                self.assertNotIn("raw_error", captured)
                self.assertEqual(expected, captured["asset"])

    def test_custom_rule_endpoint_dispatches_to_safe_one_shot_service(self):
        handler = self.make_handler()
        handler.path = "/api/custom-rules"
        handler._validate_write_security = lambda: None
        request = {
            "request_id": "request_custom_rule_api",
            "name": "独立规则",
            "description": "独立保存。",
            "expression": "close > ma20",
        }
        handler._read_json = lambda: request
        captured: dict[str, object] = {}
        handler._send_json = lambda payload, status=HTTPStatus.OK: captured.update(
            payload=payload,
            status=status,
        )

        with patch.object(
            web_server,
            "save_library_rule",
            return_value={
                "request_id": request["request_id"],
                "ref": "user.rule_request_custom_rule_api@1",
                "rule": {"status": "ACTIVE"},
            },
        ) as service:
            handler.do_POST()

        service.assert_called_once_with(web_server.PROJECT_ROOT, request)
        self.assertEqual(HTTPStatus.OK, captured["status"])
        self.assertEqual(
            "user.rule_request_custom_rule_api@1",
            captured["payload"]["ref"],
        )

    def test_custom_rule_idempotency_conflict_returns_http_conflict(self):
        handler = self.make_handler()
        handler.path = "/api/custom-rules"
        handler._validate_write_security = lambda: None
        handler._read_json = lambda: {
            "request_id": "request_custom_rule_conflict",
            "name": "冲突规则",
            "expression": "close > ma20",
        }
        captured: dict[str, object] = {}
        handler._structured_error = (
            lambda exc, status=HTTPStatus.BAD_REQUEST: captured.update(
                error_code=getattr(exc, "code", ""),
                status=status,
            )
        )

        with patch.object(
            web_server,
            "save_library_rule",
            side_effect=web_server.SimpleRuleEditorError(
                "同一请求标识对应了不同规则内容。",
                code="IDEMPOTENCY_CONFLICT",
                field="request_id",
            ),
        ):
            handler.do_POST()

        self.assertEqual("IDEMPOTENCY_CONFLICT", captured["error_code"])
        self.assertEqual(HTTPStatus.CONFLICT, captured["status"])

    def test_rule_set_delete_dispatches_at_family_path_with_hash_cas(self):
        handler = self.make_handler()
        handler.path = "/api/rule-sets/rs_0123456789ab"
        handler._validate_write_security = lambda: None
        handler._read_json = lambda: {"expected_rule_set_hash": "a" * 64}
        captured: dict[str, object] = {}
        handler._send_json = lambda payload, status=HTTPStatus.OK: captured.update(
            payload=payload,
            status=status,
        )

        class FakeRuleSetStore:
            def __init__(self, _root):
                pass

            def delete(self, rule_set_id, *, expected_rule_set_hash=None):
                captured.update(
                    rule_set_id=rule_set_id,
                    expected_rule_set_hash=expected_rule_set_hash,
                )
                return {"deleted": True, "deletion_mode": "tombstone"}

        with patch.object(web_server, "RuleSetStore", FakeRuleSetStore):
            handler.do_DELETE()

        self.assertEqual("rs_0123456789ab", captured["rule_set_id"])
        self.assertEqual("a" * 64, captured["expected_rule_set_hash"])
        self.assertTrue(captured["payload"]["deleted"])

    def test_activation_expectation_parses_strict_absent_and_exact_pointer(self):
        handler = self.make_handler()
        rule_hash = "a" * 64
        self.assertEqual(
            {"expected_active_absent": True},
            handler._activation_expectation({"expected_active_absent": True}),
        )
        self.assertEqual(
            {
                "expected_active_absent": False,
                "expected_active_rule_set_id": "rs_0123456789ab",
                "expected_active_rule_set_version": 3,
                "expected_active_rule_set_hash": rule_hash,
            },
            handler._activation_expectation(
                {
                    "expected_active_absent": False,
                    "expected_active_rule_set_id": "rs_0123456789ab",
                    "expected_active_rule_set_version": 3,
                    "expected_active_rule_set_hash": rule_hash,
                }
            ),
        )
        self.assertEqual(
            {"expected_active_rule_set_hash": ""},
            handler._activation_expectation(
                {"expected_active_rule_set_hash": ""}
            ),
        )

    def test_activation_expectation_rejects_ambiguous_strict_payload(self):
        handler = self.make_handler()
        rule_hash = "a" * 64
        invalid_payloads = [
            {"expected_active_absent": True, "expected_active_rule_set_hash": ""},
            {
                "expected_active_absent": False,
                "expected_active_rule_set_hash": rule_hash,
            },
            {
                "expected_active_rule_set_id": "rs_0123456789ab",
                "expected_active_rule_set_hash": rule_hash,
            },
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(
                web_server.RuleSetError
            ) as raised:
                handler._activation_expectation(payload)
            self.assertEqual(
                "ACTIVE_RULE_SET_EXPECTATION_INVALID", raised.exception.code
            )

    def test_legacy_conversion_conflicts_are_reported_as_conflicts(self):
        for code in (
            "RULE_SET_CONVERSION_CONFIRMATION_REQUIRED",
            "RULE_SET_CONVERSION_INVALID",
            "RULE_SET_CONVERSION_NOT_APPLICABLE",
            "RULE_SET_CONVERSION_VETO_UNCONFIRMED",
        ):
            with self.subTest(code=code):
                error = web_server.RuleSetError("conversion rejected", code=code)
                self.assertEqual(
                    HTTPStatus.CONFLICT,
                    web_server.DashboardHandler._rule_set_error_status(error),
                )

    def test_rules_management_payload_exposes_only_visible_rule_sets(self):
        handler = self.make_handler()
        captured: dict[str, object] = {}

        class FakeRegistry:
            def __init__(self, _root):
                pass

            def all_versions(self):
                return []

        class FakeRuleSets:
            def __init__(self, _root):
                pass

            def active(self):
                return {"id": "active_set", "version": 1}

            def summaries(self):
                captured["called"] = True
                return [{"id": "visible_set", "can_delete": True}]

        with patch.object(web_server, "get_editable_rules", return_value={}), patch.object(
            web_server, "RuleRegistry", FakeRegistry
        ), patch.object(web_server, "RuleSetStore", FakeRuleSets):
            payload = handler._rules_payload()

        self.assertTrue(captured["called"])
        self.assertEqual("visible_set", payload["rule_sets"]["items"][0]["id"])

    def test_read_handlers_never_write_to_the_project(self):
        """读接口不许有副作用。

        出厂规则导入是迁移，只能在启动时跑；一旦它溜进 GET 处理器，
        跑一次测试套件就会往开发者真实的 rules/ 里写文件。
        """

        source = (PROJECT_ROOT / "webapp" / "server.py").read_text(encoding="utf-8")
        read_section = source.split("def _rules_payload", 1)[1].split("def do_POST", 1)[0]
        self.assertNotIn("ensure_rule_catalog_seeded", read_section)
        startup_section = source.split("def main() -> None:", 1)[1]
        self.assertIn("ensure_rule_catalog_seeded(PROJECT_ROOT)", startup_section)

    def test_market_data_update_rejects_overlapping_request(self):
        class BusyLock:
            def acquire(self, *, blocking: bool) -> bool:
                self.blocking = blocking
                return False

            def release(self) -> None:
                raise AssertionError("busy lock must not be released by rejected request")

        lock = BusyLock()
        with patch.object(web_server, "MARKET_DATA_UPDATE_LOCK", lock):
            with self.assertRaises(web_server.MarketDataUpdateBusyError) as context:
                web_server.update_market_data_once({"symbol": "600760.SH"})

        self.assertFalse(lock.blocking)
        self.assertEqual("MARKET_DATA_UPDATE_BUSY", context.exception.code)

    def test_market_data_update_lock_is_released_when_provider_fails(self):
        class AcquiredLock:
            released = False

            def acquire(self, *, blocking: bool) -> bool:
                self.blocking = blocking
                return True

            def release(self) -> None:
                self.released = True

        lock = AcquiredLock()
        with patch.object(web_server, "MARKET_DATA_UPDATE_LOCK", lock), patch.object(
            web_server,
            "update_market_data",
            side_effect=web_server.MarketDataError("provider failed"),
        ):
            with self.assertRaisesRegex(web_server.MarketDataError, "provider failed"):
                web_server.update_market_data_once({"symbol": "600760.SH"})

        self.assertFalse(lock.blocking)
        self.assertTrue(lock.released)

    def test_manual_market_import_shares_market_data_lock(self):
        class RecordingLock:
            def __init__(self, acquired: bool) -> None:
                self.acquired = acquired
                self.released = False

            def acquire(self, *, blocking: bool) -> bool:
                self.blocking = blocking
                return self.acquired

            def release(self) -> None:
                self.released = True

        busy_lock = RecordingLock(False)
        with patch.object(
            web_server,
            "MARKET_DATA_UPDATE_LOCK",
            busy_lock,
        ), patch.object(web_server, "import_market_data") as importer:
            with self.assertRaises(web_server.MarketDataUpdateBusyError):
                web_server.import_market_data_once({"symbol": "600760.SH"})
        self.assertFalse(busy_lock.blocking)
        self.assertFalse(busy_lock.released)
        importer.assert_not_called()

        acquired_lock = RecordingLock(True)
        with patch.object(
            web_server,
            "MARKET_DATA_UPDATE_LOCK",
            acquired_lock,
        ), patch.object(
            web_server,
            "import_market_data",
            side_effect=web_server.MarketDataError("bad csv"),
        ):
            with self.assertRaisesRegex(web_server.MarketDataError, "bad csv"):
                web_server.import_market_data_once({"symbol": "600760.SH"})
        self.assertFalse(acquired_lock.blocking)
        self.assertTrue(acquired_lock.released)

    def test_manual_import_has_larger_json_envelope_but_other_writes_do_not(self):
        handler = self.make_handler(Content_Length=str(11 * 1024 * 1024))
        handler.rfile = BytesIO(b"{}")
        handler.path = "/api/market-data/import"
        self.assertEqual({}, handler._read_json())

        handler = self.make_handler(Content_Length=str(11 * 1024 * 1024))
        handler.rfile = BytesIO(b"{}")
        handler.path = "/api/market-data/update"
        with self.assertRaisesRegex(ValueError, "10 MB"):
            handler._read_json()

        handler = self.make_handler(Content_Length=str(13 * 1024 * 1024))
        handler.rfile = BytesIO(b"{}")
        handler.path = "/api/market-data/import"
        with self.assertRaisesRegex(ValueError, "12 MB"):
            handler._read_json()

    def test_stock_catalog_sync_rejects_overlap_and_releases_lock_after_failure(self):
        class BusyLock:
            def acquire(self, *, blocking: bool) -> bool:
                self.blocking = blocking
                return False

            def release(self) -> None:
                raise AssertionError("busy lock must not be released by rejected request")

        busy_lock = BusyLock()
        with patch.object(web_server, "STOCK_CATALOG_SYNC_LOCK", busy_lock):
            with self.assertRaises(web_server.StockCatalogSyncBusyError) as context:
                web_server.sync_stock_catalog_once()

        self.assertFalse(busy_lock.blocking)
        self.assertEqual("STOCK_CATALOG_SYNC_BUSY", context.exception.code)

        class AcquiredLock:
            released = False

            def acquire(self, *, blocking: bool) -> bool:
                self.blocking = blocking
                return True

            def release(self) -> None:
                self.released = True

        acquired_lock = AcquiredLock()
        with patch.object(
            web_server,
            "STOCK_CATALOG_SYNC_LOCK",
            acquired_lock,
        ), patch.object(
            web_server,
            "sync_stock_catalog",
            side_effect=web_server.StockCatalogError("provider failed"),
        ):
            with self.assertRaisesRegex(web_server.StockCatalogError, "provider failed"):
                web_server.sync_stock_catalog_once()

        self.assertFalse(acquired_lock.blocking)
        self.assertTrue(acquired_lock.released)

    def test_add_local_stock_shares_catalog_and_market_data_locks(self):
        class RecordingLock:
            def __init__(self, acquired: bool = True) -> None:
                self.acquired = acquired
                self.released = False
                self.blocking: bool | None = None

            def acquire(self, *, blocking: bool) -> bool:
                self.blocking = blocking
                return self.acquired

            def release(self) -> None:
                self.released = True

        catalog_busy = RecordingLock(acquired=False)
        market_unused = RecordingLock()
        with patch.object(
            web_server,
            "STOCK_CATALOG_SYNC_LOCK",
            catalog_busy,
        ), patch.object(
            web_server,
            "MARKET_DATA_UPDATE_LOCK",
            market_unused,
        ), patch.object(web_server, "add_stock_from_catalog") as add_stock:
            with self.assertRaises(web_server.StockCatalogSyncBusyError):
                web_server.add_local_stock_once({"symbol": "600760.SH"})
        self.assertFalse(catalog_busy.blocking)
        self.assertIsNone(market_unused.blocking)
        self.assertFalse(catalog_busy.released)
        add_stock.assert_not_called()

        catalog_lock = RecordingLock()
        market_busy = RecordingLock(acquired=False)
        with patch.object(
            web_server,
            "STOCK_CATALOG_SYNC_LOCK",
            catalog_lock,
        ), patch.object(
            web_server,
            "MARKET_DATA_UPDATE_LOCK",
            market_busy,
        ), patch.object(web_server, "add_stock_from_catalog") as add_stock:
            with self.assertRaises(web_server.MarketDataUpdateBusyError):
                web_server.add_local_stock_once({"symbol": "600760.SH"})
        self.assertFalse(catalog_lock.blocking)
        self.assertFalse(market_busy.blocking)
        self.assertTrue(catalog_lock.released)
        self.assertFalse(market_busy.released)
        add_stock.assert_not_called()

        catalog_lock = RecordingLock()
        market_lock = RecordingLock()
        with patch.object(
            web_server,
            "STOCK_CATALOG_SYNC_LOCK",
            catalog_lock,
        ), patch.object(
            web_server,
            "MARKET_DATA_UPDATE_LOCK",
            market_lock,
        ), patch.object(
            web_server,
            "add_stock_from_catalog",
            side_effect=web_server.StockCatalogError("import failed"),
        ):
            with self.assertRaisesRegex(web_server.StockCatalogError, "import failed"):
                web_server.add_local_stock_once({"symbol": "600760.SH"})
        self.assertFalse(catalog_lock.blocking)
        self.assertFalse(market_lock.blocking)
        self.assertTrue(catalog_lock.released)
        self.assertTrue(market_lock.released)

    def test_response_helpers_declare_no_store_and_restrictive_csp(self):
        json_source = inspect.getsource(web_server.DashboardHandler._send_json)
        static_source = inspect.getsource(web_server.DashboardHandler._send_static)

        self.assertIn('"Cache-Control", "no-store"', json_source)
        self.assertIn('"Content-Security-Policy"', json_source)
        self.assertIn("default-src 'none'; frame-ancestors 'none'", json_source)

        self.assertIn('"Cache-Control", "no-store, max-age=0"', static_source)
        self.assertIn('"Content-Security-Policy"', static_source)
        for directive in (
            "default-src 'self'",
            "script-src 'self'",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'self'",
        ):
            with self.subTest(directive=directive):
                self.assertIn(directive, static_source)

    def test_main_refuses_non_loopback_bind_address_before_server_start(self):
        main_source = inspect.getsource(web_server.main)
        self.assertIn('{"127.0.0.1", "localhost", "::1"}', main_source)
        self.assertIn("raise SystemExit", main_source)

        with patch.object(
            web_server.argparse.ArgumentParser,
            "parse_args",
            return_value=SimpleNamespace(host="0.0.0.0", port=8765),
        ), patch.object(web_server, "DashboardServer") as server_class:
            with self.assertRaisesRegex(SystemExit, "回环地址"):
                web_server.main()
            server_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
