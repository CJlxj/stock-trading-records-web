from __future__ import annotations

import re
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from src.full_review import get_bootstrap, run_full_review
from src.review_cases import ReviewCaseStore
from src.review_history import ReviewHistoryStore


LEGACY_REVIEW_GET_PATHS = {
    "/api/review-cases",
    "/api/reviews",
    "/api/reviews/groups",
    "/api/review-drafts",
}
LEGACY_REVIEW_POST_PATHS = {
    "/api/review",
    "/api/review-cases",
    "/api/review-drafts",
}
LEGACY_REVIEW_POST_ERRORS = {
    "/api/review": "生成正式审查结果失败，请查看运行终端。",
    "/api/review-cases": "保存股票档案失败，请查看运行终端。",
    "/api/review-drafts": "保存审查草稿失败，请查看运行终端。",
}
LEGACY_REVIEW_DETAIL_PATTERN = re.compile(r"/api/reviews/([^/]+)")
LEGACY_REVIEW_DRAFT_DELETE_PATTERN = re.compile(r"/api/review-drafts/(.*)")


def is_legacy_review_get_path(path: str) -> bool:
    return path in LEGACY_REVIEW_GET_PATHS or bool(
        LEGACY_REVIEW_DETAIL_PATTERN.fullmatch(path)
    )


def is_legacy_review_post_path(path: str) -> bool:
    return path in LEGACY_REVIEW_POST_PATHS


def is_legacy_review_delete_path(path: str) -> bool:
    return bool(LEGACY_REVIEW_DRAFT_DELETE_PATTERN.fullmatch(path))


def legacy_review_post_error(path: str) -> str | None:
    return LEGACY_REVIEW_POST_ERRORS.get(path)


def legacy_review_counts(project_root: str | Path) -> dict[str, int]:
    return ReviewHistoryStore(project_root).counts()


def _review_symbols(project_root: str | Path) -> list[dict[str, Any]]:
    return get_bootstrap(project_root)["symbols"]


def handle_legacy_review_get(
    handler: Any,
    *,
    project_root: str | Path,
    path: str,
    query: dict[str, list[str]],
) -> bool:
    if not is_legacy_review_get_path(path):
        return False
    if path == "/api/review-cases":
        handler._send_json(
            {"cases": ReviewCaseStore(project_root).list(_review_symbols(project_root))}
        )
    elif path == "/api/reviews":
        symbol = query.get("symbol", [None])[0]
        limit = int(query.get("limit", [30])[0])
        offset = int(query.get("offset", [0])[0])
        handler._send_json(
            {
                "reviews": ReviewHistoryStore(project_root).list(
                    symbol=symbol,
                    limit=limit,
                    offset=offset,
                )
            }
        )
    elif path == "/api/reviews/groups":
        limit = int(query.get("limit", [100])[0])
        handler._send_json(
            ReviewHistoryStore(project_root).list_groups(limit=limit)
        )
    elif path == "/api/review-drafts":
        symbol = query.get("symbol", [None])[0]
        limit = int(query.get("limit", [50])[0])
        handler._send_json(
            {
                "drafts": ReviewHistoryStore(project_root).list_drafts(
                    symbol=symbol,
                    limit=limit,
                )
            }
        )
    else:
        match = LEGACY_REVIEW_DETAIL_PATTERN.fullmatch(path)
        review = ReviewHistoryStore(project_root).get(unquote(match.group(1)))
        if review is None:
            handler._send_json(
                {"error": "没有找到该审查历史。"},
                HTTPStatus.NOT_FOUND,
            )
        else:
            handler._send_json(review)
    return True


def handle_legacy_review_post(
    *,
    project_root: str | Path,
    path: str,
    payload: dict[str, Any],
) -> tuple[bool, Any]:
    if path == "/api/review-cases":
        ReviewCaseStore(project_root).upsert(payload)
        cases = ReviewCaseStore(project_root).list(_review_symbols(project_root))
        symbol = str(payload.get("symbol") or "").upper()
        return True, {
            "case": next(
                (item for item in cases if item["symbol"] == symbol),
                None,
            ),
            "cases": cases,
        }
    if path == "/api/review-drafts":
        result = ReviewHistoryStore(project_root).save_draft(payload)
        ReviewCaseStore(project_root).ensure_from_review(payload)
        result["cases"] = ReviewCaseStore(project_root).list(
            _review_symbols(project_root)
        )
        return True, result
    if path == "/api/review":
        result = run_full_review(payload, project_root)
        result = ReviewHistoryStore(project_root).save(payload, result)
        ReviewCaseStore(project_root).ensure_from_review(payload)
        return True, result
    return False, None


def handle_legacy_review_delete(
    *,
    project_root: str | Path,
    path: str,
) -> tuple[bool, Any, HTTPStatus]:
    match = LEGACY_REVIEW_DRAFT_DELETE_PATTERN.fullmatch(path)
    if match is None:
        return False, None, HTTPStatus.OK
    draft_id = unquote(match.group(1))
    if not draft_id:
        return True, {"error": "草稿编号不能为空。"}, HTTPStatus.BAD_REQUEST
    result = ReviewHistoryStore(project_root).delete_draft(draft_id)
    result["cases"] = ReviewCaseStore(project_root).list(
        _review_symbols(project_root)
    )
    return True, result, HTTPStatus.OK
