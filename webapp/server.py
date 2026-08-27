from __future__ import annotations

import argparse
import hmac
import json
import re
import secrets
import sys
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
SHARED_UI_ROOT = PROJECT_ROOT / "shared_ui"
sys.path.insert(0, str(PROJECT_ROOT))

WEB_RELEASE = {
    "channel": "web",
    "version": "1.0.1",
    "release_id": "WEB-V1.0.1",
    "build_id": "WEB-20260827-002",
    "released_at": "2026-08-27T10:00:00+08:00",
    "ui_version": "web-v1.0.1",
    "backend_api_version": "0.9",
}

from src.fundamental_data import (
    FundamentalDataError,
    fetch_fundamental_data,
    provider_status as fundamental_provider_status,
)
from src.market_data import (
    MarketDataError,
    import_market_data,
    provider_status,
    update_market_data,
)
from src.market_screening import ScreeningError, universe_status
from src.personal_data import (
    PersonalDataError,
    append_trade_record,
    import_personal_data,
    list_trade_records,
    personal_data_status,
)
from src.panel_contract import (
    DOMAIN_CONTRACT_VERSION,
    enrich_screening_payload,
    panel_contract_payload,
)
from src.rule_manager import (
    RuleValidationError,
    get_editable_rules,
    save_editable_rules,
)
from src.rules.registry import RuleRegistry, RuleRegistryError
from src.rules.simple_editor import (
    SimpleRuleEditorError,
    apply_named_simple_rule_set,
    apply_simple_rule_editor,
    delete_library_rule,
    ensure_rule_catalog_seeded,
    import_default_rules,
    save_library_rule,
    simple_rule_editor_contract,
)
from src.rules.tags import (
    RuleTagError,
    create_tag,
    delete_tag,
    rename_tag,
    tag_payload,
)
from src.rules.version_store import RuleSetError, RuleSetStore
from src.screening.dataset_manifest import (
    DatasetManifestError,
    build_dataset_manifest,
)
from src.screening.engine import ScreeningCoordinator, ScreeningPreflightError
from src.screening_history import (
    DuplicateScreeningError,
    ScreeningHistoryStore,
)
from src.stock_catalog import (
    StockCatalogError,
    add_stock_from_catalog,
    search_stock_catalog,
    stock_catalog_status,
    sync_stock_catalog,
)
from src.stock_library import LocalStockLibraryError
from webapp.bootstrap import build_web_bootstrap
from webapp.legacy_review_routes import (
    handle_legacy_review_delete,
    handle_legacy_review_get,
    handle_legacy_review_post,
    is_legacy_review_delete_path,
    is_legacy_review_post_path,
    legacy_review_counts,
    legacy_review_post_error,
)


STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/platform.js": ("platform.js", "text/javascript; charset=utf-8"),
    "/modules/data.js": ("modules/data.js", "text/javascript; charset=utf-8"),
    "/modules/rules.js": ("modules/rules.js", "text/javascript; charset=utf-8"),
    "/modules/screening.js": ("modules/screening.js", "text/javascript; charset=utf-8"),
    "/modules/records.js": ("modules/records.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/shared/panel-core.js": ("@panel-core", "text/javascript; charset=utf-8"),
}

POST_ERROR_MESSAGES = {
    "/api/market-data/update": "更新行情失败，请查看运行终端。",
    "/api/market-data/import": "手动导入行情失败，请检查文件内容。",
    "/api/stock-catalog/sync": "同步完整股票目录失败，请查看运行终端。",
    "/api/local-stocks": "新增本地股票失败，请查看运行终端。",
    "/api/rules": "创建规则失败，请查看运行终端。",
    "/api/custom-rules": "保存规则失败，请检查规则内容。",
    "/api/rule-tags": "保存分类标签失败，请查看运行终端。",
    "/api/rule-defaults": "导入默认规则失败，请查看运行终端。",
    "/api/rule-editor/apply": "校验并应用规则失败，请查看运行终端。",
    "/api/rules/save": "保存交易纪律失败，请查看运行终端。",
    "/api/rule-sets": "创建规则方案失败，请查看运行终端。",
    "/api/rule-sets/apply-simple": "保存并应用规则失败，请查看运行终端。",
    "/api/screening/preflight": "筛选预检失败，请查看运行终端。",
    "/api/screening/run": "运行候选筛选失败，请查看运行终端。",
    "/api/fundamentals/fetch": "读取基本面资料失败，请查看运行终端。",
    "/api/personal-data/import": "导入个人数据失败，请查看运行终端。",
    "/api/trades": "保存操作记录失败，请查看运行终端。",
}

CSRF_TOKEN = secrets.token_urlsafe(32)
SCREENING_COORDINATOR = ScreeningCoordinator(PROJECT_ROOT)
MARKET_DATA_UPDATE_LOCK = threading.Lock()
STOCK_CATALOG_SYNC_LOCK = threading.Lock()
LOCAL_HOST_PATTERN = re.compile(r"^(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$")
RULE_TAG_PATH_PATTERN = r"/api/rule-tags/(tag_[a-f0-9]{6,32})"
# Accepts any well-formed rule identifier so an unknown rule answers 404
# rather than falling through to the static file handler.
LIBRARY_RULE_PATH_PATTERN = (
    r"/api/custom-rules/((?:[a-z][a-z0-9_]*\.)?[a-z][a-z0-9_]{1,80})"
)


class RequestSecurityError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class MarketDataUpdateBusyError(MarketDataError):
    code = "MARKET_DATA_UPDATE_BUSY"


class StockCatalogSyncBusyError(StockCatalogError):
    code = "STOCK_CATALOG_SYNC_BUSY"


def update_market_data_once(payload: dict[str, Any]) -> dict[str, Any]:
    if not MARKET_DATA_UPDATE_LOCK.acquire(blocking=False):
        raise MarketDataUpdateBusyError("另一个行情更新仍在进行，请等待完成后再试。")
    try:
        return update_market_data(
            PROJECT_ROOT,
            symbol=payload.get("symbol", ""),
            timeframe=payload.get("timeframe", "day"),
            adjust=payload.get("adjust", "qfq"),
            history_years=payload.get("history_years", 3),
        )
    finally:
        MARKET_DATA_UPDATE_LOCK.release()


def import_market_data_once(payload: dict[str, Any]) -> dict[str, Any]:
    if not MARKET_DATA_UPDATE_LOCK.acquire(blocking=False):
        raise MarketDataUpdateBusyError("另一个行情更新仍在进行，请等待完成后再试。")
    try:
        result = import_market_data(PROJECT_ROOT, payload)
        result["dataset"] = build_dataset_manifest(PROJECT_ROOT)
        return result
    finally:
        MARKET_DATA_UPDATE_LOCK.release()


def sync_stock_catalog_once() -> dict[str, Any]:
    if not STOCK_CATALOG_SYNC_LOCK.acquire(blocking=False):
        raise StockCatalogSyncBusyError("完整股票目录正在同步，请等待完成后再试。")
    try:
        return sync_stock_catalog(PROJECT_ROOT)
    finally:
        STOCK_CATALOG_SYNC_LOCK.release()


def add_local_stock_once(payload: dict[str, Any]) -> dict[str, Any]:
    if not STOCK_CATALOG_SYNC_LOCK.acquire(blocking=False):
        raise StockCatalogSyncBusyError("完整股票目录正在同步，请等待完成后再试。")
    try:
        if not MARKET_DATA_UPDATE_LOCK.acquire(blocking=False):
            raise MarketDataUpdateBusyError("另一个行情更新仍在进行，请等待完成后再试。")
        try:
            return add_stock_from_catalog(PROJECT_ROOT, payload)
        finally:
            MARKET_DATA_UPDATE_LOCK.release()
    finally:
        STOCK_CATALOG_SYNC_LOCK.release()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "StockRuleReview/0.9"

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _send_download(
        self,
        body: str,
        *,
        content_type: str,
        filename: str,
    ) -> None:
        encoded = body.encode("utf-8-sig" if "csv" in content_type else "utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_static(self, filename: str, content_type: str) -> None:
        path = SHARED_UI_ROOT / "panel-core.js" if filename == "@panel-core" else STATIC_ROOT / filename
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _local_host(self) -> bool:
        return bool(LOCAL_HOST_PATTERN.fullmatch(self.headers.get("Host", "")))

    def _validate_write_security(self) -> None:
        if not self._local_host():
            raise RequestSecurityError("仅允许从本机面板访问写接口。", "LOCAL_HOST_REQUIRED")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise RequestSecurityError(
                "写接口只接受 application/json。",
                "JSON_CONTENT_TYPE_REQUIRED",
            )
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            request_host = self.headers.get("Host", "").lower()
            if (
                parsed.scheme != "http"
                or parsed.netloc.lower() != request_host
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            ):
                raise RequestSecurityError("拒绝非本机页面发起的写请求。", "ORIGIN_REJECTED")
        token = self.headers.get("X-Panel-CSRF", "")
        if not token or not hmac.compare_digest(token, CSRF_TOKEN):
            raise RequestSecurityError("页面安全令牌失效，请刷新面板。", "CSRF_INVALID")

    def _read_json(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        upload_limit = (
            12 * 1024 * 1024
            if urlparse(getattr(self, "path", "")).path == "/api/market-data/import"
            else 10 * 1024 * 1024
        )
        if content_length <= 0 or content_length > upload_limit:
            raise ValueError(
                f"请求内容为空或超过 {upload_limit // (1024 * 1024)} MB。"
            )
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求格式不正确。")
        return payload

    @staticmethod
    def _parts(path: str) -> list[str]:
        return [unquote(part) for part in path.strip("/").split("/") if part]

    def _structured_error(
        self,
        exc: Exception,
        *,
        status: int = HTTPStatus.BAD_REQUEST,
    ) -> None:
        payload: dict[str, Any] = {
            "error": str(exc),
            "error_code": getattr(exc, "code", "BAD_REQUEST"),
        }
        field = getattr(exc, "field", None)
        if field:
            payload["field_errors"] = [{"field": field, "message": str(exc)}]
        details = getattr(exc, "details", None)
        if isinstance(details, dict):
            payload.update(details)
        if isinstance(exc, DuplicateScreeningError):
            payload["duplicate_run_id"] = exc.run_id
        self._send_json(payload, status)

    @staticmethod
    def _custom_rule_error_status(exc: SimpleRuleEditorError) -> int:
        if exc.code == "RULE_NOT_FOUND":
            return HTTPStatus.NOT_FOUND
        if exc.code == "RULE_IN_USE":
            return HTTPStatus.CONFLICT
        return HTTPStatus.BAD_REQUEST

    @staticmethod
    def _rule_tag_error_status(exc: RuleTagError) -> int:
        if exc.code == "RULE_TAG_NOT_FOUND":
            return HTTPStatus.NOT_FOUND
        if exc.code == "RULE_TAG_NAME_EXISTS":
            return HTTPStatus.CONFLICT
        return HTTPStatus.BAD_REQUEST

    @staticmethod
    def _rule_set_error_status(exc: RuleSetError) -> int:
        if exc.code == "RULE_SET_NOT_FOUND":
            return HTTPStatus.NOT_FOUND
        if exc.code in {
            "ACTIVE_RULE_SET_CHANGED",
            "RULE_SET_ACTIVE",
            "RULE_SET_ADVANCED_READ_ONLY",
            "RULE_SET_CHANGED",
            "RULE_SET_CONVERSION_CONFIRMATION_REQUIRED",
            "RULE_SET_CONVERSION_INVALID",
            "RULE_SET_CONVERSION_NOT_APPLICABLE",
            "RULE_SET_CONVERSION_VETO_UNCONFIRMED",
            "RULE_SET_DELETED",
            "RULE_SET_NAME_EXISTS",
        }:
            return HTTPStatus.CONFLICT
        return HTTPStatus.BAD_REQUEST

    @staticmethod
    def _active_rule_set_or_none(store: RuleSetStore) -> dict[str, Any] | None:
        try:
            return store.active()
        except RuleSetError as exc:
            if exc.code != "NO_ACTIVE_RULE_SET":
                raise
            return None

    def _managed_rule_set_response(
        self,
        store: RuleSetStore,
        rule_set_id: str,
        version: int,
        *,
        conversion: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "rule_set": store.detail(rule_set_id),
            "version": store.get(rule_set_id, int(version)),
            "active": self._active_rule_set_or_none(store),
        }
        if conversion is not None:
            result["conversion"] = conversion
        return result

    @staticmethod
    def _required_rule_set_hash(
        payload: dict[str, Any], field: str, *, allow_empty: bool = False
    ) -> str:
        if field not in payload:
            raise RuleSetError(
                "缺少并发校验摘要，请重新读取组合后再操作。",
                code="RULE_SET_HASH_REQUIRED",
            )
        value = str(payload.get(field) or "").strip()
        if not value and not allow_empty:
            raise RuleSetError(
                "并发校验摘要不能为空，请重新读取组合后再操作。",
                code="RULE_SET_HASH_REQUIRED",
            )
        return value

    @classmethod
    def _activation_expectation(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Parse strict current-pointer CAS while retaining the legacy hash call."""

        strict_field = "expected_active_absent"
        id_field = "expected_active_rule_set_id"
        version_field = "expected_active_rule_set_version"
        hash_field = "expected_active_rule_set_hash"
        if strict_field not in payload:
            if id_field in payload or version_field in payload:
                raise RuleSetError(
                    "提交当前组合 ID 或版本时必须明确 expected_active_absent。",
                    code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
                )
            return {
                "expected_active_rule_set_hash": cls._required_rule_set_hash(
                    payload,
                    hash_field,
                    allow_empty=True,
                )
            }

        expected_absent = payload.get(strict_field)
        if not isinstance(expected_absent, bool):
            raise RuleSetError(
                "expected_active_absent 必须是布尔值。",
                code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
            )
        if expected_absent:
            if any(field in payload for field in (id_field, version_field, hash_field)):
                raise RuleSetError(
                    "期望当前组合不存在时，不得同时提交 ID、版本或摘要。",
                    code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
                )
            return {"expected_active_absent": True}

        active_hash = cls._required_rule_set_hash(payload, hash_field)
        active_id = str(payload.get(id_field) or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", active_id):
            raise RuleSetError(
                "当前组合 ID 格式不正确。",
                code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
            )
        if not re.fullmatch(r"[a-f0-9]{64}", active_hash):
            raise RuleSetError(
                "当前组合摘要格式不正确。",
                code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
            )
        if version_field not in payload:
            raise RuleSetError(
                "缺少当前组合版本。",
                code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
            )
        if isinstance(payload.get(version_field), bool):
            raise RuleSetError(
                "当前组合版本格式不正确。",
                code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
            )
        try:
            active_version = int(payload.get(version_field))
        except (TypeError, ValueError):
            raise RuleSetError(
                "当前组合版本格式不正确。",
                code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
            ) from None
        if active_version < 1:
            raise RuleSetError(
                "当前组合版本格式不正确。",
                code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
            )
        return {
            "expected_active_absent": False,
            "expected_active_rule_set_id": active_id,
            "expected_active_rule_set_version": active_version,
            "expected_active_rule_set_hash": active_hash,
        }

    def _rules_payload(self) -> dict[str, Any]:
        payload = get_editable_rules(PROJECT_ROOT)
        registry = RuleRegistry(PROJECT_ROOT)
        rule_sets = RuleSetStore(PROJECT_ROOT)
        active = self._active_rule_set_or_none(rule_sets)
        rule_versions = registry.all_versions()
        # Tag counts describe what the rule library actually shows: one usable
        # scored rule per identifier, system gates excluded.
        catalog_ids = sorted(
            {
                str(item["id"])
                for item in rule_versions
                if item.get("status") == "ACTIVE"
                and item.get("kind") == "scored"
                and not str(item.get("id") or "").startswith("system.")
            }
        )
        tags = tag_payload(PROJECT_ROOT, catalog_ids)
        assignments = tags.pop("assignments")
        payload["registry"] = {
            "items": [
                {**item, "tag_id": assignments.get(str(item["id"]), "")}
                for item in rule_versions
            ],
            "count": len(rule_versions),
            "lifecycle": [
                "DRAFT",
                "VALIDATED",
                "ACTIVE",
                "SUPERSEDED",
                "ARCHIVED",
            ],
            "result_states": ["PASS", "FAIL", "DATA_GAP", "ERROR", "SKIPPED"],
        }
        payload["rule_tags"] = tags
        payload["editor_contract"] = simple_rule_editor_contract()
        payload["rule_sets"] = {
            "items": rule_sets.summaries(),
            "active": active,
        }
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/") and not self._local_host():
            self._structured_error(
                RequestSecurityError(
                    "仅允许从本机面板访问 API。",
                    "LOCAL_HOST_REQUIRED",
                ),
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return
        try:
            if path == "/api/health":
                self._send_json(
                    {
                        "status": "ok",
                        "app": "stock-rule-review",
                        "version": WEB_RELEASE["backend_api_version"],
                        "product_version": WEB_RELEASE["version"],
                        "ui_version": WEB_RELEASE["ui_version"],
                        "release_id": WEB_RELEASE["release_id"],
                        "build_id": WEB_RELEASE["build_id"],
                        "released_at": WEB_RELEASE["released_at"],
                        "domain_contract_version": DOMAIN_CONTRACT_VERSION,
                        "csrf_token": CSRF_TOKEN,
                    }
                )
                return
            if path == "/api/panel-contract":
                self._send_json(panel_contract_payload())
                return
            if path == "/api/bootstrap":
                self._send_json(
                    build_web_bootstrap(PROJECT_ROOT, csrf_token=CSRF_TOKEN)
                )
                return
            if handle_legacy_review_get(
                self,
                project_root=PROJECT_ROOT,
                path=path,
                query=parse_qs(parsed.query),
            ):
                return
            if path == "/api/market-data/status":
                self._send_json(provider_status())
                return
            if path == "/api/stock-catalog/status":
                self._send_json(stock_catalog_status(PROJECT_ROOT))
                return
            if path == "/api/stock-catalog/search":
                query = parse_qs(parsed.query)
                self._send_json(
                    search_stock_catalog(
                        PROJECT_ROOT,
                        query.get("q", [""])[0],
                        limit=int(query.get("limit", [20])[0]),
                    )
                )
                return
            if path == "/api/fundamentals/status":
                self._send_json(fundamental_provider_status())
                return
            if path == "/api/datasets/status":
                self._send_json(build_dataset_manifest(PROJECT_ROOT))
                return
            if path == "/api/screening/status":
                status = universe_status(PROJECT_ROOT)
                status["latest_run"] = next(
                    iter(ScreeningHistoryStore(PROJECT_ROOT).list(limit=1)),
                    None,
                )
                status["dataset"] = build_dataset_manifest(PROJECT_ROOT)
                self._send_json(status)
                return
            if path == "/api/data-center/status":
                screening_store = ScreeningHistoryStore(PROJECT_ROOT)
                self._send_json(
                    {
                        "personal": personal_data_status(PROJECT_ROOT),
                        "market": universe_status(PROJECT_ROOT),
                        "dataset": build_dataset_manifest(PROJECT_ROOT),
                        "history": {
                            **legacy_review_counts(PROJECT_ROOT),
                            "screenings": screening_store.count(),
                        },
                    }
                )
                return
            if path == "/api/screenings":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", [20])[0])
                self._send_json(
                    {"screenings": ScreeningHistoryStore(PROJECT_ROOT).list(limit=limit)}
                )
                return
            if path == "/api/screenings/latest":
                latest = ScreeningHistoryStore(PROJECT_ROOT).latest(
                    universe_scope="local_library"
                )
                if latest is None:
                    self._send_json(
                        {"error": "还没有保存的筛选批次。"},
                        HTTPStatus.NOT_FOUND,
                    )
                else:
                    self._send_json(enrich_screening_payload(latest))
                return
            parts = self._parts(path)
            if len(parts) >= 3 and parts[:2] == ["api", "screenings"]:
                store = ScreeningHistoryStore(PROJECT_ROOT)
                run_id = parts[2]
                if len(parts) == 5 and parts[3] == "evidence":
                    evidence = store.evidence(run_id, parts[4])
                    if evidence is None:
                        self._send_json(
                            {"error": "没有找到该股票的批次证据。"},
                            HTTPStatus.NOT_FOUND,
                        )
                    else:
                        self._send_json(evidence)
                    return
                if len(parts) == 4 and parts[3] == "export.csv":
                    body = store.export_csv(run_id)
                    if body is None:
                        self._send_json(
                            {"error": "没有找到该筛选批次。"},
                            HTTPStatus.NOT_FOUND,
                        )
                    else:
                        self._send_download(
                            body,
                            content_type="text/csv; charset=utf-8",
                            filename=f"screening-{run_id}.csv",
                        )
                    return
                if len(parts) == 4 and parts[3] == "audit.json":
                    body = store.audit_json(run_id)
                    if body is None:
                        self._send_json(
                            {"error": "没有找到该筛选批次。"},
                            HTTPStatus.NOT_FOUND,
                        )
                    else:
                        self._send_download(
                            body,
                            content_type="application/json; charset=utf-8",
                            filename=f"screening-{run_id}-audit.json",
                        )
                    return
                if len(parts) == 3:
                    screening = store.get(run_id)
                    if screening is None:
                        self._send_json(
                            {"error": "没有找到该筛选批次。"},
                            HTTPStatus.NOT_FOUND,
                        )
                    else:
                        self._send_json(enrich_screening_payload(screening))
                    return
            if path == "/api/rules":
                self._send_json(self._rules_payload())
                return
            if len(parts) >= 3 and parts[:2] == ["api", "rules"]:
                registry = RuleRegistry(PROJECT_ROOT)
                rule_id = parts[2]
                query = parse_qs(parsed.query)
                version_raw = query.get("version", [None])[0]
                version = None if version_raw is None else int(version_raw)
                if len(parts) == 4 and parts[3] == "versions":
                    self._send_json({"versions": registry.versions(rule_id)})
                    return
                if len(parts) == 4 and parts[3] == "source":
                    self._send_json(registry.source(rule_id, version))
                    return
                if len(parts) == 3:
                    self._send_json(registry.get(rule_id, version))
                    return
            if path == "/api/rule-sets":
                store = RuleSetStore(PROJECT_ROOT)
                self._send_json(
                    {
                        "items": store.summaries(),
                        "active": self._active_rule_set_or_none(store),
                    }
                )
                return
            if path == "/api/trades":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", [50])[0])
                symbol = query.get("symbol", [None])[0]
                self._send_json(
                    list_trade_records(
                        PROJECT_ROOT,
                        symbol=symbol,
                        limit=limit,
                    )
                )
                return
            if len(parts) >= 3 and parts[:2] == ["api", "rule-sets"]:
                store = RuleSetStore(PROJECT_ROOT)
                query = parse_qs(parsed.query)
                version_raw = query.get("version", [None])[0]
                version = None if version_raw is None else int(version_raw)
                if len(parts) == 4 and parts[3] == "versions":
                    self._send_json({"versions": store.versions(parts[2])})
                    return
                if len(parts) == 3:
                    self._send_json(
                        store.detail(parts[2])
                        if version is None
                        else store.get(parts[2], version)
                    )
                    return
        except RuleSetError as exc:
            self._structured_error(exc, status=self._rule_set_error_status(exc))
            return
        except (
            ValueError,
            FileNotFoundError,
            DatasetManifestError,
            StockCatalogError,
            LocalStockLibraryError,
            RuleRegistryError,
        ) as exc:
            self._structured_error(exc)
            return
        except Exception:
            traceback.print_exc()
            self._send_json(
                {"error": "读取项目数据失败，请查看运行终端。"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        static_file = STATIC_FILES.get(path)
        if static_file:
            self._send_static(*static_file)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _known_post_path(self, path: str) -> bool:
        if is_legacy_review_post_path(path):
            return True
        if path in {
            "/api/market-data/update",
            "/api/market-data/import",
            "/api/stock-catalog/sync",
            "/api/local-stocks",
            "/api/datasets/scan",
            "/api/rules",
            "/api/custom-rules",
            "/api/rule-tags",
            "/api/rule-defaults",
            "/api/rule-editor/apply",
            "/api/rules/save",
            "/api/rule-sets",
            "/api/rule-sets/apply-simple",
            "/api/screening/preflight",
            "/api/screening/run",
            "/api/fundamentals/fetch",
            "/api/personal-data/import",
            "/api/trades",
        }:
            return True
        return bool(
            re.fullmatch(r"/api/rules/[^/]+/(?:validate|activate)", path)
            or re.fullmatch(RULE_TAG_PATH_PATTERN, path)
            or re.fullmatch(
                r"/api/rule-sets/[^/]+/(?:versions|clone|validate|activate)",
                path,
            )
        )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._known_post_path(path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            self._validate_write_security()
            payload = self._read_json()
            parts = self._parts(path)
            legacy_handled, result = handle_legacy_review_post(
                project_root=PROJECT_ROOT,
                path=path,
                payload=payload,
            )
            if legacy_handled:
                pass
            elif path == "/api/market-data/update":
                result = update_market_data_once(payload)
            elif path == "/api/market-data/import":
                result = import_market_data_once(payload)
            elif path == "/api/stock-catalog/sync":
                result = sync_stock_catalog_once()
            elif path == "/api/local-stocks":
                result = add_local_stock_once(payload)
            elif path == "/api/fundamentals/fetch":
                result = fetch_fundamental_data(str(payload.get("symbol") or ""))
            elif path == "/api/personal-data/import":
                result = import_personal_data(PROJECT_ROOT, payload)
            elif path == "/api/trades":
                result = append_trade_record(PROJECT_ROOT, payload)
            elif path == "/api/datasets/scan":
                result = build_dataset_manifest(
                    PROJECT_ROOT,
                    as_of_trade_date=payload.get("as_of_trade_date"),
                )
            elif path == "/api/screening/preflight":
                result = SCREENING_COORDINATOR.preflight(
                    {**payload, "universe_scope": "local_library"}
                )
            elif path == "/api/screening/run":
                preflight_id = str(payload.get("preflight_id") or "")
                if not preflight_id:
                    preflight = SCREENING_COORDINATOR.preflight(
                        {**payload, "universe_scope": "local_library"}
                    )
                    preflight_id = preflight["preflight_id"]
                result = SCREENING_COORDINATOR.run(
                    preflight_id,
                    confirm_duplicate=bool(payload.get("confirm_duplicate", False)),
                )
                result = enrich_screening_payload(result)
            # Legacy loopback-only compatibility route. It is intentionally
            # absent from the current panel capability contract and UI.
            elif path == "/api/rule-editor/apply":
                result = apply_simple_rule_editor(PROJECT_ROOT, payload)
            elif path == "/api/rules":
                result = RuleRegistry(PROJECT_ROOT).create(payload)
            elif path == "/api/custom-rules":
                result = save_library_rule(PROJECT_ROOT, payload)
            elif path == "/api/rule-tags":
                result = {"tag": create_tag(PROJECT_ROOT, payload.get("label"))}
            elif path == "/api/rule-defaults":
                result = import_default_rules(PROJECT_ROOT)
            elif rule_tag_match := re.fullmatch(RULE_TAG_PATH_PATTERN, path):
                result = {
                    "tag": rename_tag(
                        PROJECT_ROOT,
                        rule_tag_match.group(1),
                        payload.get("label"),
                    )
                }
            elif (
                len(parts) == 4
                and parts[:2] == ["api", "rules"]
                and parts[3] in {"validate", "activate"}
            ):
                registry = RuleRegistry(PROJECT_ROOT)
                version = payload.get("version")
                result = (
                    registry.validate(parts[2], version)
                    if parts[3] == "validate"
                    else registry.activate(parts[2], version)
                )
            # Legacy auto-activate route; current clients save and activate
            # through separate named rule-set commands.
            elif path == "/api/rule-sets/apply-simple":
                store = RuleSetStore(PROJECT_ROOT)
                created = store.create(payload)
                store.validate(created["id"], int(created["version"]))
                result = store.activate(created["id"], int(created["version"]))
            elif path == "/api/rule-sets":
                if "rules" in payload and "scored" not in payload:
                    result = apply_named_simple_rule_set(PROJECT_ROOT, payload)
                else:
                    result = RuleSetStore(PROJECT_ROOT).create(payload)
            elif (
                len(parts) == 4
                and parts[:2] == ["api", "rule-sets"]
                and parts[3]
                in {"versions", "clone", "validate", "activate"}
            ):
                store = RuleSetStore(PROJECT_ROOT)
                rule_set_id = parts[2]
                action = parts[3]
                if action == "versions":
                    result = apply_named_simple_rule_set(
                        PROJECT_ROOT,
                        payload,
                        rule_set_id=rule_set_id,
                    )
                elif action == "clone":
                    source_hash = self._required_rule_set_hash(
                        payload, "expected_rule_set_hash"
                    )
                    source_version = int(
                        payload.get("source_version")
                        or store.detail(rule_set_id)["latest_version"]
                    )
                    cloned = store.clone(
                        rule_set_id,
                        source_version=source_version,
                        name=str(payload.get("name") or ""),
                        description=payload.get("description"),
                        editor_mode=payload.get("editor_mode"),
                        expected_rule_set_hash=source_hash,
                    )
                    cloned_version = cloned["version"]
                    validated = store.validate(
                        cloned_version["id"],
                        int(cloned_version["version"]),
                        allow_validated_rules=True,
                    )
                    result = self._managed_rule_set_response(
                        store,
                        validated["id"],
                        int(validated["version"]),
                        conversion=cloned.get("conversion"),
                    )
                elif action == "validate":
                    validated = store.validate(rule_set_id, payload.get("version"))
                    result = self._managed_rule_set_response(
                        store,
                        validated["id"],
                        int(validated["version"]),
                    )
                elif action == "activate":
                    active_expectation = self._activation_expectation(payload)
                    activated = store.activate(
                        rule_set_id,
                        payload.get("version"),
                        **active_expectation,
                    )
                    result = self._managed_rule_set_response(
                        store,
                        activated["id"],
                        int(activated["version"]),
                    )
            elif path == "/api/rules/save":
                result = save_editable_rules(PROJECT_ROOT, payload)
            else:
                raise RuntimeError(f"已登记但未处理的 POST 路径：{path}")
            self._send_json(result)
        except RequestSecurityError as exc:
            self._structured_error(exc, status=HTTPStatus.FORBIDDEN)
        except DuplicateScreeningError as exc:
            self._structured_error(exc, status=HTTPStatus.CONFLICT)
        except SimpleRuleEditorError as exc:
            self._structured_error(
                exc,
                status=(
                    HTTPStatus.CONFLICT
                    if exc.code in {"IDEMPOTENCY_CONFLICT", "RULE_SET_CHANGED"}
                    else HTTPStatus.BAD_REQUEST
                ),
            )
        except RuleTagError as exc:
            self._structured_error(exc, status=self._rule_tag_error_status(exc))
        except RuleSetError as exc:
            self._structured_error(exc, status=self._rule_set_error_status(exc))
        except MarketDataUpdateBusyError as exc:
            self._structured_error(exc, status=HTTPStatus.CONFLICT)
        except StockCatalogSyncBusyError as exc:
            self._structured_error(exc, status=HTTPStatus.CONFLICT)
        except (
            ValueError,
            FileNotFoundError,
            json.JSONDecodeError,
            MarketDataError,
            ScreeningError,
            ScreeningPreflightError,
            RuleValidationError,
            RuleRegistryError,
            DatasetManifestError,
            FundamentalDataError,
            PersonalDataError,
            StockCatalogError,
            LocalStockLibraryError,
        ) as exc:
            self._structured_error(exc)
        except Exception:
            traceback.print_exc()
            self._send_json(
                {
                    "error": POST_ERROR_MESSAGES.get(
                        path
                    )
                    or legacy_review_post_error(path)
                    or "请求处理失败，请查看运行终端。"
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        rule_set_match = re.fullmatch(
            r"/api/rule-sets/([a-z][a-z0-9_]{1,80})", path
        )
        library_rule_match = re.fullmatch(LIBRARY_RULE_PATH_PATTERN, path)
        rule_tag_match = re.fullmatch(RULE_TAG_PATH_PATTERN, path)
        if (
            not is_legacy_review_delete_path(path)
            and not rule_set_match
            and not library_rule_match
            and not rule_tag_match
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            self._validate_write_security()
            legacy_handled, result, status = handle_legacy_review_delete(
                project_root=PROJECT_ROOT,
                path=path,
            )
            if legacy_handled:
                self._send_json(result, status)
                return
            if library_rule_match:
                self._read_json()
                result = delete_library_rule(
                    PROJECT_ROOT,
                    unquote(library_rule_match.group(1)),
                )
            elif rule_tag_match:
                self._read_json()
                result = delete_tag(PROJECT_ROOT, rule_tag_match.group(1))
            elif rule_set_match:
                payload = self._read_json()
                target_hash = self._required_rule_set_hash(
                    payload, "expected_rule_set_hash"
                )
                result = RuleSetStore(PROJECT_ROOT).delete(
                    unquote(rule_set_match.group(1)),
                    expected_rule_set_hash=target_hash,
                )
            self._send_json(result)
        except RequestSecurityError as exc:
            self._structured_error(exc, status=HTTPStatus.FORBIDDEN)
        except SimpleRuleEditorError as exc:
            self._structured_error(exc, status=self._custom_rule_error_status(exc))
        except RuleTagError as exc:
            self._structured_error(exc, status=self._rule_tag_error_status(exc))
        except RuleSetError as exc:
            self._structured_error(exc, status=self._rule_set_error_status(exc))
        except (ValueError, RuleRegistryError) as exc:
            self._structured_error(exc)
        except Exception:
            traceback.print_exc()
            self._send_json(
                {"error": "删除失败，请查看运行终端。"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the local after-close transparent rule screener."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("为保护本地规则与数据，面板只允许绑定本机回环地址。")

    # Importing the factory rules is a migration, so it runs once at startup
    # rather than inside a read handler: GET /api/rules must never write.
    ensure_rule_catalog_seeded(PROJECT_ROOT)

    server = DashboardServer((args.host, args.port), DashboardHandler)
    print(f"收盘后透明规则筛选台已启动：http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
