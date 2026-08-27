from __future__ import annotations

from copy import deepcopy
from typing import Any

from src.personal_data import (
    DISCIPLINE_CHECK_LABELS,
    EMOTION_FLAG_LABELS,
    TRADE_REASON_LABELS,
)
from src.rules.tags import TAG_LABEL_MAXIMUM, UNASSIGNED_LABEL


DOMAIN_CONTRACT_VERSION = "panel-domain-v1.8"

CORE_TASKS = (
    "data",
    "rules",
    "screening",
    "records",
)

RESULT_STATUS_LABELS = {
    "CANDIDATE": "候选",
    "SECONDARY_FILTERED": "二级未通过",
    "SECONDARY_DATA_GAP": "二级待补",
    "REJECTED": "基础未通过",
    "DATA_GAP": "数据待补",
    "NEAR_MISS": "接近门槛",
    "NOT_SELECTED": "未入选",
}


PANEL_API_CAPABILITIES: dict[str, dict[str, str]] = {
    "health": {"method": "GET", "web": "/api/health"},
    "contract": {"method": "GET", "web": "/api/panel-contract"},
    "bootstrap": {"method": "GET", "web": "/api/bootstrap"},
    "data_status": {"method": "GET", "web": "/api/datasets/status"},
    "catalog_status": {"method": "GET", "web": "/api/stock-catalog/status"},
    "catalog_search": {"method": "GET", "web": "/api/stock-catalog/search"},
    "rules": {"method": "GET", "web": "/api/rules"},
    "rule_sets": {"method": "GET", "web": "/api/rule-sets"},
    "rule_set_detail": {"method": "GET", "web": "/api/rule-sets/{rule_set_id}"},
    "screening_latest": {"method": "GET", "web": "/api/screenings/latest"},
    "screening_detail": {"method": "GET", "web": "/api/screenings/{run_id}"},
    "trades_list": {"method": "GET", "web": "/api/trades"},
    "data_update": {"method": "POST", "web": "/api/market-data/update"},
    "data_import": {"method": "POST", "web": "/api/market-data/import"},
    "data_scan": {"method": "POST", "web": "/api/datasets/scan"},
    "catalog_sync": {"method": "POST", "web": "/api/stock-catalog/sync"},
    "stock_add": {"method": "POST", "web": "/api/local-stocks"},
    "rule_set_create": {"method": "POST", "web": "/api/rule-sets"},
    "custom_rule_create": {"method": "POST", "web": "/api/custom-rules"},
    "custom_rule_delete": {"method": "DELETE", "web": "/api/custom-rules/{rule_id}"},
    "rule_defaults_import": {"method": "POST", "web": "/api/rule-defaults"},
    "rule_tag_create": {"method": "POST", "web": "/api/rule-tags"},
    "rule_tag_rename": {"method": "POST", "web": "/api/rule-tags/{tag_id}"},
    "rule_tag_delete": {"method": "DELETE", "web": "/api/rule-tags/{tag_id}"},
    "rule_set_edit": {"method": "POST", "web": "/api/rule-sets/{rule_set_id}/versions"},
    "rule_set_clone": {"method": "POST", "web": "/api/rule-sets/{rule_set_id}/clone"},
    "rule_set_activate": {"method": "POST", "web": "/api/rule-sets/{rule_set_id}/activate"},
    "rule_set_delete": {"method": "DELETE", "web": "/api/rule-sets/{rule_set_id}"},
    "screening_preflight": {"method": "POST", "web": "/api/screening/preflight"},
    "screening_run": {"method": "POST", "web": "/api/screening/run"},
    "trade_create": {"method": "POST", "web": "/api/trades"},
}


def screening_result_status(row: dict[str, Any]) -> str:
    selection = str(row.get("selection_status") or "").upper()
    technical = str(row.get("technical_status") or "").upper()
    history = str(row.get("history_status") or "").upper()
    if selection == "PASS" and technical == "BUY_CANDIDATE":
        secondary = str(row.get("secondary_status") or "INACTIVE").upper()
        if secondary == "FAIL":
            return "SECONDARY_FILTERED"
        if secondary in {"DATA_GAP", "ERROR", "SKIPPED"}:
            return "SECONDARY_DATA_GAP"
        return "CANDIDATE"
    if selection == "FAIL":
        return "REJECTED"
    if selection == "DATA_GAP" or technical == "DATA_GAP" or history == "MISSING":
        return "DATA_GAP"
    if selection == "PASS" and technical == "WATCH":
        return "NEAR_MISS"
    return "NOT_SELECTED"


def enrich_screening_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    result = deepcopy(payload)
    for key in ("row_results", "rows"):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        enriched: list[Any] = []
        for raw in rows:
            if not isinstance(raw, dict):
                enriched.append(raw)
                continue
            status = screening_result_status(raw)
            primary_candidate = (
                str(raw.get("selection_status") or "").upper() == "PASS"
                and str(raw.get("technical_status") or "").upper()
                == "BUY_CANDIDATE"
            )
            enriched.append(
                {
                    **raw,
                    "result_status": status,
                    "result_status_label": RESULT_STATUS_LABELS[status],
                    "primary_candidate": bool(
                        raw.get("primary_candidate", primary_candidate)
                    ),
                    "final_candidate": status == "CANDIDATE",
                    "is_candidate": status == "CANDIDATE",
                    "record_eligible": status == "CANDIDATE",
                }
            )
        result[key] = enriched
    result["domain_contract_version"] = DOMAIN_CONTRACT_VERSION
    return result


def panel_contract_payload() -> dict[str, Any]:
    return {
        "domain_contract_version": DOMAIN_CONTRACT_VERSION,
        "core_tasks": list(CORE_TASKS),
        "result_statuses": RESULT_STATUS_LABELS,
        "api_capabilities": deepcopy(PANEL_API_CAPABILITIES),
        "rule_set_editor": {
            "editor_mode": "simple_all",
            "primary": {
                "payload_field": "rules",
                "required": True,
                "minimum_rules": 1,
                "maximum_rules": 30,
                "match_mode": "ALL",
            },
            "secondary": {
                "payload_field": "secondary_rules",
                "required": False,
                "minimum_rules": 0,
                "maximum_rules": 30,
                "match_mode": "ALL",
                "runs_only_after_primary_pass": True,
            },
            "forbidden_payload_fields": [
                "minimum_match",
                "secondary_minimum_match",
            ],
            "formal_screen_uses_global_current_only": True,
            "activation_cas": {
                "current_absent": {"expected_active_absent": True},
                "current_exists_fields": [
                    "expected_active_absent",
                    "expected_active_rule_set_id",
                    "expected_active_rule_set_version",
                    "expected_active_rule_set_hash",
                ],
                "current_exists_expected_active_absent": False,
                "legacy_empty_hash_is_not_strict": True,
            },
            "default_top_n": 5,
            "legacy_edit_strategy": "new_simple_version_same_id",
            "legacy_conversion_policy": "legacy_to_two_stage_all_v1",
            "requires_explicit_conversion_confirmation": True,
            "preserves_source_versions": True,
            "deletion": {
                "scope": "rule_set_family",
                "current": "blocked",
                "pure_single_version_draft": "physical",
                "all_other_noncurrent": "tombstone",
                "restorable": False,
                "history_snapshots_preserved": True,
            },
        },
        "rule_library": {
            # One catalog, one kind of rule.  The factory rules ship as seed
            # data and are imported into the editable catalog at startup,
            # so nothing downstream needs a "built-in vs personal" branch.
            "single_catalog": True,
            "rule_operations": ["create", "edit", "delete"],
            "edit_creates": "new_version_of_same_rule",
            "delete": {
                "scope": "all_versions_of_one_rule",
                "blocked_by": "rule_set_versions",
                "blocked_by_screening_batches": False,
                "mode": "physical",
                "restorable_for_factory_rules_via": "rule_defaults_import",
            },
            "defaults_import": {
                "seeded_on_startup": True,
                "additive_only": True,
                "restores_tags": True,
            },
            "tags": {
                "storage": "rules/tags.yaml",
                "assignment_field": "tag_id",
                "operations": ["create", "rename", "delete"],
                "label_maximum_length": TAG_LABEL_MAXIMUM,
                "label_forbids_whitespace": True,
                "labels_are_unique": True,
                "rules_per_tag": 1,
                "delete_releases_rules_to_unassigned": True,
                "assignment_never_creates_a_rule_version": True,
                "unassigned_label": UNASSIGNED_LABEL,
                "listed_by": "rules",
                "listed_field": "rule_tags",
            },
        },
        "trade_checklist": {
            "reason_labels": deepcopy(TRADE_REASON_LABELS),
            "discipline_labels": deepcopy(DISCIPLINE_CHECK_LABELS),
            "emotion_labels": deepcopy(EMOTION_FLAG_LABELS),
            "emotion_clear_value": "NO_RISK",
        },
        "platform_only": {
            "web": ["loopback_only"],
        },
    }
