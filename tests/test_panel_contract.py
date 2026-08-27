from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from src.panel_contract import (
    CORE_TASKS,
    DOMAIN_CONTRACT_VERSION,
    PANEL_API_CAPABILITIES,
    enrich_screening_payload,
    panel_contract_payload,
    screening_result_status,
)
from src.personal_data import (
    DISCIPLINE_CHECK_LABELS,
    EMOTION_FLAG_LABELS,
    TRADE_REASON_LABELS,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PanelContractTests(unittest.TestCase):
    def test_authoritative_result_status_matrix(self):
        cases = [
            ({"selection_status": "PASS", "technical_status": "BUY_CANDIDATE"}, "CANDIDATE"),
            (
                {
                    "selection_status": "PASS",
                    "technical_status": "BUY_CANDIDATE",
                    "secondary_status": "FAIL",
                },
                "SECONDARY_FILTERED",
            ),
            (
                {
                    "selection_status": "PASS",
                    "technical_status": "BUY_CANDIDATE",
                    "secondary_status": "DATA_GAP",
                },
                "SECONDARY_DATA_GAP",
            ),
            ({"selection_status": "FAIL", "technical_status": "BUY_CANDIDATE"}, "REJECTED"),
            ({"selection_status": "DATA_GAP", "technical_status": "WATCH"}, "DATA_GAP"),
            ({"selection_status": "PASS", "technical_status": "DATA_GAP"}, "DATA_GAP"),
            ({"selection_status": "PASS", "technical_status": "WATCH"}, "NEAR_MISS"),
            ({"selection_status": "PASS", "technical_status": "NONE"}, "NOT_SELECTED"),
            ({"selection_status": "", "technical_status": "", "history_status": "MISSING"}, "DATA_GAP"),
        ]
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(expected, screening_result_status(row))

    def test_enrichment_is_non_mutating_and_controls_record_eligibility(self):
        source = {
            "row_results": [
                {"symbol": "000001.SZ", "selection_status": "PASS", "technical_status": "BUY_CANDIDATE"},
                {"symbol": "000002.SZ", "selection_status": "FAIL", "technical_status": "BUY_CANDIDATE"},
            ]
        }
        result = enrich_screening_payload(source)
        self.assertNotIn("result_status", source["row_results"][0])
        self.assertEqual(DOMAIN_CONTRACT_VERSION, result["domain_contract_version"])
        self.assertTrue(result["row_results"][0]["is_candidate"])
        self.assertTrue(result["row_results"][0]["primary_candidate"])
        self.assertTrue(result["row_results"][0]["final_candidate"])
        self.assertTrue(result["row_results"][0]["record_eligible"])
        self.assertFalse(result["row_results"][1]["is_candidate"])
        self.assertFalse(result["row_results"][1]["record_eligible"])

    def test_capability_contract_covers_all_four_tasks(self):
        self.assertEqual(("data", "rules", "screening", "records"), CORE_TASKS)
        required = {
            "health", "contract", "bootstrap", "data_status", "catalog_status",
            "catalog_search", "rules", "screening_latest", "screening_detail",
            "trades_list", "data_update", "data_import", "data_scan",
            "catalog_sync", "stock_add", "screening_preflight",
            "screening_run", "trade_create", "rule_sets", "rule_set_detail",
            "rule_set_create", "rule_set_edit", "rule_set_clone",
            "rule_set_activate", "rule_set_delete", "custom_rule_create",
            "custom_rule_delete", "rule_defaults_import",
            "rule_tag_create", "rule_tag_rename", "rule_tag_delete",
        }
        self.assertEqual(required, set(PANEL_API_CAPABILITIES))
        for capability in PANEL_API_CAPABILITIES.values():
            self.assertIn(capability["method"], {"GET", "POST", "DELETE"})
            self.assertTrue(capability["web"].startswith("/api/"))
        deletion = panel_contract_payload()["rule_set_editor"]["deletion"]
        self.assertEqual("rule_set_family", deletion["scope"])
        self.assertEqual("blocked", deletion["current"])
        self.assertEqual("physical", deletion["pure_single_version_draft"])
        self.assertEqual("tombstone", deletion["all_other_noncurrent"])
        self.assertFalse(deletion["restorable"])
        self.assertTrue(deletion["history_snapshots_preserved"])

    def test_rule_library_contract_is_one_catalog_with_separate_tags(self):
        library = panel_contract_payload()["rule_library"]
        self.assertTrue(library["single_catalog"])
        self.assertEqual(["create", "edit", "delete"], library["rule_operations"])
        self.assertEqual("new_version_of_same_rule", library["edit_creates"])

        deletion = library["delete"]
        self.assertEqual("all_versions_of_one_rule", deletion["scope"])
        self.assertEqual("rule_set_versions", deletion["blocked_by"])
        self.assertFalse(deletion["blocked_by_screening_batches"])
        self.assertEqual("physical", deletion["mode"])
        self.assertEqual(
            "rule_defaults_import",
            deletion["restorable_for_factory_rules_via"],
        )

        defaults = library["defaults_import"]
        self.assertTrue(defaults["seeded_on_startup"])
        self.assertTrue(defaults["additive_only"])
        self.assertTrue(defaults["restores_tags"])

        tags = library["tags"]
        self.assertEqual("tag_id", tags["assignment_field"])
        self.assertEqual(["create", "rename", "delete"], tags["operations"])
        self.assertEqual(12, tags["label_maximum_length"])
        self.assertTrue(tags["label_forbids_whitespace"])
        self.assertTrue(tags["labels_are_unique"])
        self.assertEqual(1, tags["rules_per_tag"])
        self.assertTrue(tags["delete_releases_rules_to_unassigned"])
        self.assertTrue(tags["assignment_never_creates_a_rule_version"])
        self.assertEqual("未分类", tags["unassigned_label"])
        self.assertEqual("rule_tags", tags["listed_field"])

        self.assertEqual(
            "DELETE", PANEL_API_CAPABILITIES["custom_rule_delete"]["method"]
        )
        self.assertEqual(
            "/api/custom-rules/{rule_id}",
            PANEL_API_CAPABILITIES["custom_rule_delete"]["web"],
        )
        self.assertEqual(
            "/api/rule-defaults",
            PANEL_API_CAPABILITIES["rule_defaults_import"]["web"],
        )
        self.assertEqual(
            "/api/rule-tags/{tag_id}",
            PANEL_API_CAPABILITIES["rule_tag_delete"]["web"],
        )

    def test_web_version_matches_the_domain_contract(self):
        web = json.loads((PROJECT_ROOT / "webapp" / "VERSION.json").read_text(encoding="utf-8"))
        self.assertEqual(DOMAIN_CONTRACT_VERSION, web["domain_contract_version"])
        self.assertEqual("web", web["channel"])
        self.assertEqual(3, web["rounds_completed"])

    def test_trade_checklist_is_exported_from_the_domain_source(self):
        checklist = panel_contract_payload()["trade_checklist"]
        self.assertEqual(TRADE_REASON_LABELS, checklist["reason_labels"])
        self.assertEqual(DISCIPLINE_CHECK_LABELS, checklist["discipline_labels"])
        self.assertEqual(EMOTION_FLAG_LABELS, checklist["emotion_labels"])
        self.assertEqual("NO_RISK", checklist["emotion_clear_value"])

    def test_rule_set_editor_contract_is_two_stage_all_without_thresholds(self):
        editor = panel_contract_payload()["rule_set_editor"]
        self.assertEqual("simple_all", editor["editor_mode"])
        self.assertTrue(editor["primary"]["required"])
        self.assertEqual("ALL", editor["primary"]["match_mode"])
        self.assertFalse(editor["secondary"]["required"])
        self.assertEqual("ALL", editor["secondary"]["match_mode"])
        self.assertTrue(editor["secondary"]["runs_only_after_primary_pass"])
        self.assertEqual(
            ["minimum_match", "secondary_minimum_match"],
            editor["forbidden_payload_fields"],
        )
        self.assertTrue(editor["formal_screen_uses_global_current_only"])
        self.assertEqual(
            "new_simple_version_same_id", editor["legacy_edit_strategy"]
        )
        self.assertEqual(
            "legacy_to_two_stage_all_v1", editor["legacy_conversion_policy"]
        )
        self.assertTrue(editor["requires_explicit_conversion_confirmation"])
        self.assertTrue(editor["preserves_source_versions"])
        self.assertEqual(
            {"expected_active_absent": True},
            editor["activation_cas"]["current_absent"],
        )
        self.assertEqual(
            False,
            editor["activation_cas"]["current_exists_expected_active_absent"],
        )
        self.assertTrue(editor["activation_cas"]["legacy_empty_hash_is_not_strict"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for shared frontend contract validation")
    def test_shared_frontend_accepts_initial_null_dataset(self):
        core_path = PROJECT_ROOT / "shared_ui" / "panel-core.js"
        script = (
            "const fs=require('fs'),vm=require('vm');"
            f"vm.runInThisContext(fs.readFileSync({json.dumps(str(core_path))},'utf8'));"
            "process.stdout.write(String(StockPanelCore.dataGapCount(null)));"
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual("0", result)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for shared frontend contract validation")
    def test_javascript_and_python_apply_the_same_status_fixture(self):
        rows = [
            {"selection_status": "PASS", "technical_status": "BUY_CANDIDATE"},
            {"selection_status": "FAIL", "technical_status": "BUY_CANDIDATE"},
            {"selection_status": "DATA_GAP", "technical_status": "WATCH"},
            {"selection_status": "PASS", "technical_status": "DATA_GAP"},
            {"selection_status": "PASS", "technical_status": "WATCH"},
        ]
        core_path = PROJECT_ROOT / "shared_ui" / "panel-core.js"
        script = (
            "const fs=require('fs'),vm=require('vm');"
            f"vm.runInThisContext(fs.readFileSync({json.dumps(str(core_path))},'utf8'));"
            f"const rows={json.dumps(rows)};"
            "process.stdout.write(JSON.stringify(rows.map(StockPanelCore.resultStatus)));"
        )
        javascript_statuses = json.loads(
            subprocess.run(
                ["node", "-e", script],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        self.assertEqual(
            [screening_result_status(row) for row in rows],
            javascript_statuses,
        )


if __name__ == "__main__":
    unittest.main()
