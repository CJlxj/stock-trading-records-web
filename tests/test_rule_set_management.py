from __future__ import annotations

import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

import yaml

from src.screening_history import ScreeningHistoryStore
from src.rules.simple_editor import (
    SimpleRuleEditorError,
    apply_named_simple_rule_set,
    save_library_rule,
)
from src.rules.version_store import (
    LEGACY_CONVERSION_POLICY,
    LEGACY_EDITOR_MODE,
    SIMPLE_EDITOR_MODE,
    RuleSetError,
    RuleSetStore,
)


def _custom_rule(root: Path, draft_id: str, name: str, expression: str) -> dict:
    """Put one rule in the library and return the combination's reference.

    Combinations only reference library rules, so every test that wants a
    combination first has to own a rule.
    """

    saved = save_library_rule(
        root,
        {
            # Content-derived so the same draft id can hold different rules in
            # different tests without tripping idempotency.
            "request_id": "library_" + sha256(
                f"{draft_id}|{name}|{expression}".encode("utf-8")
            ).hexdigest()[:24],
            "name": name,
            "expression": expression,
            "description": f"{name}，仅用于收盘后筛选。",
        },
    )
    return {"ref": saved["ref"]}


def _create_named(
    root: Path,
    *,
    request_id: str,
    name: str,
    draft_id: str,
    expression: str,
) -> dict:
    return apply_named_simple_rule_set(
        root,
        {
            "request_id": request_id,
            "name": name,
            "rules": [_custom_rule(root, draft_id, f"{name}规则", expression)],
        },
    )


class NamedRuleSetManagementTests(unittest.TestCase):
    def test_named_simple_create_is_explicit_all_and_has_stable_server_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = _create_named(
                root,
                request_id="named_create_a1",
                name="趋势组合",
                draft_id="trend_rule",
                expression="close > ma20",
            )
            second = _create_named(
                root,
                request_id="named_create_b1",
                name="量价组合",
                draft_id="volume_rule",
                expression="volume_ratio >= 1.2",
            )

            first_version = first["version"]
            self.assertRegex(first_version["id"], r"^rs_[a-f0-9]{12}$")
            self.assertNotEqual(first_version["id"], second["version"]["id"])
            self.assertEqual(SIMPLE_EDITOR_MODE, first_version["editor_mode"])
            self.assertEqual("TWO_STAGE_ALL", first_version["simple_semantics"])
            self.assertEqual(3, first_version["schema_version"])
            self.assertEqual("all", first_version["scored"]["mode"])
            self.assertEqual(1.0, first_version["scored"]["pass_ratio"])
            self.assertEqual([], first_version["secondary"]["rules"])
            self.assertEqual(0, first_version["secondary"]["minimum_match"])
            self.assertEqual("VALIDATED", first_version["status"])
            self.assertIsNone(first["active"])

            repeated = _create_named(
                root,
                request_id="named_create_a1",
                name="趋势组合",
                draft_id="trend_rule",
                expression="close > ma20",
            )
            self.assertTrue(repeated["deduplicated"])
            self.assertEqual(first_version["id"], repeated["version"]["id"])

    def test_named_simple_supports_optional_secondary_all_without_threshold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = apply_named_simple_rule_set(
                root,
                {
                    "request_id": "named_two_stage_create",
                    "name": "两级收盘组合",
                    "rules": [
                        _custom_rule(root, "two_stage_primary", "一级趋势", "close > ma20")
                    ],
                    "secondary_rules": [
                        _custom_rule(
                            root,
                            "two_stage_secondary",
                            "二级量价",
                            "volume_ratio >= 1.2",
                        )
                    ],
                },
            )

            version = created["version"]
            self.assertEqual(created["rule_refs"], version["scored"]["rules"])
            self.assertEqual(
                created["secondary_rule_refs"], version["secondary"]["rules"]
            )
            self.assertEqual("all", version["secondary"]["mode"])
            self.assertEqual(1, version["secondary"]["minimum_match"])
            self.assertEqual(1, RuleSetStore(root).summaries()[0]["secondary_rule_count"])

            edited = apply_named_simple_rule_set(
                root,
                {
                    "request_id": "named_two_stage_edit",
                    "base_version": 1,
                    "expected_rule_set_hash": version["rule_set_hash"],
                    "name": "两级收盘组合",
                    "rules": [{"ref": created["rule_refs"][0]}],
                    "secondary_rules": [
                        {"ref": created["secondary_rule_refs"][0]}
                    ],
                },
                rule_set_id=version["id"],
            )
            self.assertEqual(2, edited["version"]["version"])
            self.assertEqual(
                created["secondary_rule_refs"],
                edited["version"]["secondary"]["rules"],
            )

            cloned = RuleSetStore(root).clone(
                edited["version"]["id"],
                source_version=2,
                name="两级收盘组合副本",
                expected_rule_set_hash=edited["version"]["rule_set_hash"],
            )
            self.assertEqual(
                edited["version"]["secondary"]["rules"],
                cloned["version"]["secondary"]["rules"],
            )
            self.assertIsNone(cloned["conversion"])

    def test_named_simple_rejects_thresholds_and_cross_stage_duplicates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = _create_named(
                root,
                request_id="two_stage_duplicate_seed",
                name="重复检查来源",
                draft_id="two_stage_duplicate",
                expression="close > ma20",
            )

            with self.assertRaises(SimpleRuleEditorError) as threshold:
                apply_named_simple_rule_set(
                    root,
                    {
                        "request_id": "two_stage_bad_threshold",
                        "name": "非法门槛组合",
                        "rules": [{"ref": seed["rule_refs"][0]}],
                        "secondary_minimum_match": 0,
                    },
                )
            self.assertEqual("RULE_SET_SIMPLE_ALL_INVALID", threshold.exception.code)

            with self.assertRaises(SimpleRuleEditorError) as duplicate:
                apply_named_simple_rule_set(
                    root,
                    {
                        "request_id": "two_stage_bad_duplicate",
                        "name": "跨级重复组合",
                        "rules": [{"ref": seed["rule_refs"][0]}],
                        "secondary_rules": [{"ref": seed["rule_refs"][0]}],
                    },
                )
            self.assertEqual("RULE_STAGE_DUPLICATED", duplicate.exception.code)

            # 在规则库里给同一条规则存一个新版本，旧版本随之被取代；
            # 组合只能引用当前版本，所以「同一条规则的两个版本」根本进不来。
            newer = save_library_rule(
                root,
                {
                    "request_id": "library_two_stage_lineage_v2",
                    "name": "同源规则的新版本",
                    "description": "用于验证同源拦截",
                    "base_ref": seed["rule_refs"][0],
                    "expression": "close > ma60",
                },
            )
            self.assertEqual(
                seed["rule_refs"][0].split("@")[0],
                newer["ref"].split("@")[0],
            )
            with self.assertRaises(SimpleRuleEditorError) as same_lineage:
                apply_named_simple_rule_set(
                    root,
                    {
                        "request_id": "two_stage_bad_lineage",
                        "name": "跨级同源组合",
                        "rules": [{"ref": newer["ref"]}],
                        "secondary_rules": [{"ref": seed["rule_refs"][0]}],
                    },
                )
            self.assertEqual("RULE_NOT_ACTIVE", same_lineage.exception.code)

    def test_edit_creates_new_version_and_rejects_stale_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = _create_named(
                root,
                request_id="named_edit_seed",
                name="可编辑组合",
                draft_id="editable_rule",
                expression="close > ma20",
            )
            rule_set_id = created["version"]["id"]
            original_path = root / created["version"]["source_path"]
            original_bytes = original_path.read_bytes()

            edited = apply_named_simple_rule_set(
                root,
                {
                    "request_id": "named_edit_v2",
                    "base_version": 1,
                    "expected_rule_set_hash": created["version"]["rule_set_hash"],
                    "name": "可编辑组合",
                    "rules": [
                        _custom_rule(root, "editable_rule", "可编辑规则", "close > ma60")
                    ],
                },
                rule_set_id=rule_set_id,
            )
            self.assertEqual(2, edited["version"]["version"])
            self.assertEqual(original_bytes, original_path.read_bytes())

            with self.assertRaises(RuleSetError) as raised:
                apply_named_simple_rule_set(
                    root,
                    {
                        "request_id": "named_edit_stale",
                        "base_version": 1,
                        "expected_rule_set_hash": created["version"]["rule_set_hash"],
                        "name": "可编辑组合",
                        "rules": [{"ref": created["rule_refs"][0]}],
                    },
                    rule_set_id=rule_set_id,
                )
            self.assertEqual("RULE_SET_CHANGED", raised.exception.code)

    def test_global_activation_can_switch_a_b_a_and_supersedes_same_family(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            a = _create_named(
                root,
                request_id="switch_combo_a",
                name="组合 A",
                draft_id="switch_a",
                expression="close > ma20",
            )
            b = _create_named(
                root,
                request_id="switch_combo_b",
                name="组合 B",
                draft_id="switch_b",
                expression="rsi14 <= 70",
            )
            store = RuleSetStore(root)
            a_id = a["version"]["id"]
            b_id = b["version"]["id"]

            active_a = store.activate(a_id, 1)
            active_b = store.activate(
                b_id,
                1,
                expected_active_rule_set_hash=active_a["rule_set_hash"],
            )
            self.assertEqual("VALIDATED", store.get(a_id, 1)["status"])
            active_a_again = store.activate(
                a_id,
                1,
                expected_active_rule_set_hash=active_b["rule_set_hash"],
            )
            self.assertEqual(a_id, active_a_again["id"])
            self.assertEqual("VALIDATED", store.get(b_id, 1)["status"])
            self.assertEqual(1, sum(item["is_current"] for item in store.summaries()))

            edited = apply_named_simple_rule_set(
                root,
                {
                    "request_id": "switch_combo_a_v2",
                    "base_version": 1,
                    "expected_rule_set_hash": active_a_again["rule_set_hash"],
                    "name": "组合 A",
                    "rules": [{"ref": a["rule_refs"][0]}],
                },
                rule_set_id=a_id,
            )
            active_v2 = store.activate(
                a_id,
                2,
                expected_active_rule_set_hash=active_a_again["rule_set_hash"],
            )
            self.assertEqual(2, active_v2["version"])
            self.assertEqual("SUPERSEDED", store.get(a_id, 1)["status"])
            self.assertEqual("ACTIVE", edited["rule_set"]["status"])

    def test_strict_activation_cas_distinguishes_absent_and_exact_pointer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = _create_named(
                root,
                request_id="strict_cas_first",
                name="严格并发组合一",
                draft_id="strict_cas_rule_one",
                expression="close > ma20",
            )["version"]
            second = _create_named(
                root,
                request_id="strict_cas_second",
                name="严格并发组合二",
                draft_id="strict_cas_rule_two",
                expression="close > ma60",
            )["version"]
            store = RuleSetStore(root)

            active_first = store.activate(
                first["id"],
                1,
                expected_active_absent=True,
            )
            with self.assertRaises(RuleSetError) as stale_absent:
                store.activate(
                    second["id"],
                    1,
                    expected_active_absent=True,
                )
            self.assertEqual("ACTIVE_RULE_SET_CHANGED", stale_absent.exception.code)
            self.assertEqual(first["id"], store.active()["id"])

            with self.assertRaises(RuleSetError) as wrong_pointer_id:
                store.activate(
                    second["id"],
                    1,
                    expected_active_absent=False,
                    expected_active_rule_set_id=second["id"],
                    expected_active_rule_set_version=active_first["version"],
                    expected_active_rule_set_hash=active_first["rule_set_hash"],
                )
            self.assertEqual(
                "ACTIVE_RULE_SET_CHANGED", wrong_pointer_id.exception.code
            )

            active_second = store.activate(
                second["id"],
                1,
                expected_active_absent=False,
                expected_active_rule_set_id=active_first["id"],
                expected_active_rule_set_version=active_first["version"],
                expected_active_rule_set_hash=active_first["rule_set_hash"],
            )
            self.assertEqual(second["id"], active_second["id"])

            with self.assertRaises(RuleSetError) as stale_pointer:
                store.activate(
                    first["id"],
                    1,
                    expected_active_absent=False,
                    expected_active_rule_set_id=active_first["id"],
                    expected_active_rule_set_version=active_first["version"],
                    expected_active_rule_set_hash=active_first["rule_set_hash"],
                )
            self.assertEqual("ACTIVE_RULE_SET_CHANGED", stale_pointer.exception.code)
            self.assertEqual(second["id"], store.active()["id"])

    def test_strict_activation_cas_rejects_incomplete_pointer_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            version = _create_named(
                root,
                request_id="strict_cas_invalid",
                name="严格合同校验",
                draft_id="strict_cas_invalid_rule",
                expression="close > ma20",
            )["version"]
            store = RuleSetStore(root)

            with self.assertRaises(RuleSetError) as incomplete:
                store.activate(
                    version["id"],
                    1,
                    expected_active_absent=False,
                    expected_active_rule_set_hash=version["rule_set_hash"],
                )
            self.assertEqual(
                "ACTIVE_RULE_SET_EXPECTATION_INVALID", incomplete.exception.code
            )

            # 旧调用不带严格字段仍保持原兼容语义；新面板不得使用这种形式。
            activated = store.activate(
                version["id"],
                1,
                expected_active_rule_set_hash="",
            )
            self.assertEqual(version["id"], activated["id"])

    def test_yaml_only_legacy_active_in_another_family_becomes_reusable_validated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = _create_named(
                root,
                request_id="legacy_shape_current",
                name="当前组合",
                draft_id="legacy_shape_rule",
                expression="close > ma20",
            )
            store = RuleSetStore(root)
            store.activate(current["version"]["id"], 1)
            legacy_path = root / "rules" / "rule_sets" / "migrated_after_close" / "v1.yaml"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text(
                yaml.safe_dump(
                    {
                        "id": "migrated_after_close",
                        "version": 1,
                        "name": "迁移后的旧组合",
                        "status": "ACTIVE",
                        "base_gates": {"rules": []},
                        "required": {"rules": []},
                        "veto": {"rules": []},
                        "scored": {
                            "mode": "equal_score",
                            "rules": current["rule_refs"],
                            "pass_ratio": 0.65,
                        },
                        "secondary": {"rules": [], "minimum_match": 0},
                        "near_policy": {"score_ratio": 0.45},
                        "group_policy": {
                            "minimum_groups": 1,
                            "group_pass_ratio": 0.5,
                        },
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            legacy = store.get("migrated_after_close", 1)
            summary = store.detail("migrated_after_close")
            self.assertEqual("VALIDATED", legacy["status"])
            self.assertEqual(1, summary["usable_version"])
            self.assertTrue(summary["can_activate"])
            self.assertEqual(1, sum(item["is_current"] for item in store.summaries()))

    def test_legacy_direct_simple_write_is_blocked_and_conversion_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            simple = _create_named(
                root,
                request_id="legacy_seed_one",
                name="来源一",
                draft_id="legacy_primary",
                expression="close > ma20",
            )
            second = _create_named(
                root,
                request_id="legacy_seed_two",
                name="来源二",
                draft_id="legacy_secondary",
                expression="volume_ratio >= 1.1",
            )
            store = RuleSetStore(root)
            legacy = store.create(
                {
                    "id": "legacy_combo",
                    "name": "旧高级组合",
                    "base_gates": {"rules": []},
                    "required": {"rules": []},
                    "veto": {"rules": []},
                    "scored": {
                        "mode": "equal_score",
                        "rules": simple["rule_refs"],
                        "pass_ratio": 0.5,
                    },
                    "secondary": {
                        "rules": second["rule_refs"],
                        "minimum_match": 1,
                    },
                    "near_policy": {"score_ratio": 0.25},
                    "group_policy": {"minimum_groups": 1, "group_pass_ratio": 0.5},
                }
            )
            legacy = store.validate("legacy_combo", int(legacy["version"]))
            self.assertEqual(LEGACY_EDITOR_MODE, legacy["editor_mode"])
            self.assertEqual(0.5, legacy["scored"]["pass_ratio"])
            self.assertEqual(second["rule_refs"], legacy["secondary"]["rules"])
            legacy_detail = store.detail("legacy_combo")
            self.assertTrue(legacy_detail["can_edit"])
            self.assertTrue(legacy_detail["conversion_required"])
            self.assertEqual(
                "new_simple_version_same_id", legacy_detail["edit_strategy"]
            )

            with self.assertRaises(RuleSetError) as raised:
                store.create_simple_version(
                    "legacy_combo",
                    base_version=1,
                    expected_rule_set_hash=legacy["rule_set_hash"],
                    name="旧高级组合",
                    rules=simple["rule_refs"],
                )
            self.assertEqual("RULE_SET_ADVANCED_READ_ONLY", raised.exception.code)

            cloned = store.clone(
                "legacy_combo",
                source_version=1,
                name="主动复制为简单组合",
                editor_mode=SIMPLE_EDITOR_MODE,
                expected_rule_set_hash=legacy["rule_set_hash"],
            )
            self.assertEqual(SIMPLE_EDITOR_MODE, cloned["version"]["editor_mode"])
            self.assertEqual("TWO_STAGE_ALL", cloned["version"]["simple_semantics"])
            self.assertEqual([], cloned["version"]["secondary"]["rules"])
            self.assertTrue(cloned["conversion"]["dropped_thresholds"])
            self.assertEqual(
                second["rule_refs"], cloned["conversion"]["dropped_secondary_rules"]
            )

    def test_legacy_edit_requires_explicit_confirmation_and_creates_same_id_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary_one = _create_named(
                root,
                request_id="legacy_convert_source_one",
                name="转换来源一",
                draft_id="legacy_convert_primary_one",
                expression="close > ma20",
            )
            primary_two = _create_named(
                root,
                request_id="legacy_convert_source_two",
                name="转换来源二",
                draft_id="legacy_convert_primary_two",
                expression="close > ma60",
            )
            secondary = _create_named(
                root,
                request_id="legacy_convert_source_three",
                name="转换来源三",
                draft_id="legacy_convert_secondary",
                expression="volume_ratio >= 1.1",
            )
            refs = [primary_one["rule_refs"][0], primary_two["rule_refs"][0]]
            store = RuleSetStore(root)
            source = store.create(
                {
                    "id": "legacy_convert_family",
                    "name": "待转换旧组合",
                    "base_gates": {"rules": []},
                    "required": {"rules": []},
                    "veto": {"rules": []},
                    "scored": {
                        "mode": "equal_score",
                        "rules": refs,
                        "pass_ratio": 0.5,
                    },
                    "secondary": {
                        "rules": [secondary["rule_refs"][0]],
                        "minimum_match": 1,
                    },
                    "near_policy": {"score_ratio": 0.25},
                    "group_policy": {"minimum_groups": 1, "group_pass_ratio": 0.5},
                }
            )
            source = store.validate(source["id"], 1, allow_validated_rules=True)
            source_path = root / source["source_path"]
            source_bytes = source_path.read_bytes()
            store.activate(source["id"], 1)
            history = ScreeningHistoryStore(root)
            frozen = history.save(
                {
                    "schema_version": 2,
                    "meta": {
                        "created_at": "2026-08-13T18:00:00+08:00",
                        "snapshot_date": "2026-08-13",
                        "as_of_trade_date": "2026-08-13",
                        "candidate_for_trade_date": None,
                        "universe_source": "project_symbols",
                        "universe_scope": "local_library",
                        "is_full_market": False,
                        "rule_hash": source["rule_set_hash"],
                        "rule_set_hash": source["rule_set_hash"],
                        "rule_set_id": source["id"],
                        "rule_set_version": 1,
                        "input_fingerprint": "legacy-conversion-history-v1",
                    },
                    "summary": {
                        "universe_count": 1,
                        "pass_count": 1,
                        "fail_count": 0,
                        "data_gap_count": 0,
                        "technical_ready_count": 1,
                        "technical_candidate_count": 1,
                    },
                    "rows": [
                        {
                            "symbol": "000001.SZ",
                            "selection_status": "PASS",
                            "technical_status": "BUY_CANDIDATE",
                        }
                    ],
                    "disclaimer": "候选不是买入命令。",
                }
            )
            frozen_before = history.get(frozen["meta"]["history_id"])

            base_payload = {
                "base_version": 1,
                "expected_rule_set_hash": source["rule_set_hash"],
                "name": "待转换旧组合",
                "rules": [{"ref": ref} for ref in refs],
                "secondary_rules": [{"ref": secondary["rule_refs"][0]}],
            }
            with self.assertRaises(RuleSetError) as missing_confirmation:
                apply_named_simple_rule_set(
                    root,
                    {**base_payload, "request_id": "legacy_convert_missing_confirm"},
                    rule_set_id=source["id"],
                )
            self.assertEqual(
                "RULE_SET_CONVERSION_CONFIRMATION_REQUIRED",
                missing_confirmation.exception.code,
            )
            self.assertEqual([1], [item["version"] for item in store.versions(source["id"] )])

            result = apply_named_simple_rule_set(
                root,
                {
                    **base_payload,
                    "request_id": "legacy_convert_confirmed",
                    "legacy_conversion": {
                        "policy": LEGACY_CONVERSION_POLICY,
                        "confirmed": True,
                        "source_version": 1,
                        "source_rule_set_hash": source["rule_set_hash"],
                        "dropped_veto_refs": [],
                    },
                },
                rule_set_id=source["id"],
            )

            self.assertEqual(source["id"], result["version"]["id"])
            self.assertEqual(2, result["version"]["version"])
            self.assertEqual(SIMPLE_EDITOR_MODE, result["version"]["editor_mode"])
            self.assertEqual("all", result["version"]["scored"]["mode"])
            self.assertEqual(1.0, result["version"]["scored"]["pass_ratio"])
            self.assertEqual(
                len(result["secondary_rule_refs"]),
                result["version"]["secondary"]["minimum_match"],
            )
            self.assertEqual(source_bytes, source_path.read_bytes())
            self.assertEqual(source["rule_set_hash"], store.get(source["id"], 1)["rule_set_hash"])
            self.assertEqual(1, store.active()["version"])
            self.assertEqual(
                frozen_before,
                history.get(frozen["meta"]["history_id"]),
            )
            self.assertEqual(1, result["conversion"]["threshold_changes"]["primary"]["from_minimum"])
            self.assertEqual(2, result["conversion"]["threshold_changes"]["primary"]["to_minimum"])

            repeated = apply_named_simple_rule_set(
                root,
                {
                    **base_payload,
                    "request_id": "legacy_convert_confirmed",
                    "legacy_conversion": {
                        "policy": LEGACY_CONVERSION_POLICY,
                        "confirmed": True,
                        "source_version": 1,
                        "source_rule_set_hash": source["rule_set_hash"],
                        "dropped_veto_refs": [],
                    },
                },
                rule_set_id=source["id"],
            )
            self.assertTrue(repeated["deduplicated"])
            self.assertEqual(2, repeated["version"]["version"])

            with self.assertRaises(SimpleRuleEditorError) as changed_request:
                apply_named_simple_rule_set(
                    root,
                    {
                        **base_payload,
                        "request_id": "legacy_convert_confirmed",
                        "name": "同一请求编号但内容不同",
                        "legacy_conversion": {
                            "policy": LEGACY_CONVERSION_POLICY,
                            "confirmed": True,
                            "source_version": 1,
                            "source_rule_set_hash": source["rule_set_hash"],
                            "dropped_veto_refs": [],
                        },
                    },
                    rule_set_id=source["id"],
                )
            self.assertEqual("IDEMPOTENCY_CONFLICT", changed_request.exception.code)

            audit_events = [
                json.loads(line)
                for line in (root / "history" / "rule_audit.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            conversion_event = next(
                item for item in audit_events
                if item["event"] == "RULE_SET_LEGACY_CONVERTED"
            )
            self.assertEqual(source["rule_set_hash"], conversion_event["details"]["source_rule_set_hash"])
            self.assertEqual(2, conversion_event["version"])

    def test_legacy_conversion_rejects_wrong_veto_acknowledgement_without_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = _create_named(
                root,
                request_id="legacy_veto_source_seed",
                name="否决来源",
                draft_id="legacy_veto_source_rule",
                expression="close > ma20",
            )
            ref = seed["rule_refs"][0]
            store = RuleSetStore(root)
            legacy = store.create(
                {
                    "id": "legacy_veto_family",
                    "name": "含旧否决条件",
                    "base_gates": {"rules": []},
                    "required": {"rules": []},
                    "veto": {"rules": [ref]},
                    "scored": {"mode": "equal_score", "rules": [ref], "pass_ratio": 1},
                    "secondary": {"rules": [], "minimum_match": 0},
                    "near_policy": {"score_ratio": 0.5},
                    "group_policy": {"minimum_groups": 1, "group_pass_ratio": 0.5},
                }
            )
            legacy = store.validate(legacy["id"], 1, allow_validated_rules=True)

            with self.assertRaises(RuleSetError) as raised:
                apply_named_simple_rule_set(
                    root,
                    {
                        "request_id": "legacy_veto_wrong_ack",
                        "base_version": 1,
                        "expected_rule_set_hash": legacy["rule_set_hash"],
                        "name": legacy["name"],
                        "rules": [{"ref": ref}],
                        "legacy_conversion": {
                            "policy": LEGACY_CONVERSION_POLICY,
                            "confirmed": True,
                            "source_version": 1,
                            "source_rule_set_hash": legacy["rule_set_hash"],
                            "dropped_veto_refs": [],
                        },
                    },
                    rule_set_id=legacy["id"],
                )
            self.assertEqual("RULE_SET_CONVERSION_VETO_UNCONFIRMED", raised.exception.code)
            self.assertEqual([1], [item["version"] for item in store.versions(legacy["id"] )])

    def test_family_delete_blocks_current_physically_deletes_only_pure_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = _create_named(
                root,
                request_id="delete_seed_rule",
                name="可启用组合",
                draft_id="delete_rule",
                expression="close > ma20",
            )
            store = RuleSetStore(root)
            ref = seed["rule_refs"][0]
            active = store.activate(seed["version"]["id"], 1)
            self.assertFalse(store.detail(active["id"])["can_delete"])
            with self.assertRaises(RuleSetError) as raised:
                store.delete(active["id"], expected_rule_set_hash=active["rule_set_hash"])
            self.assertEqual("RULE_SET_ACTIVE", raised.exception.code)

            draft = store.create_simple(name="纯草稿", rules=[ref])
            draft_path = root / draft["source_path"]
            self.assertEqual("physical", store.detail(draft["id"])["delete_mode"])
            with self.assertRaises(RuleSetError) as stale_delete:
                store.delete(
                    draft["id"],
                    expected_rule_set_hash="0" * 64,
                )
            self.assertEqual("RULE_SET_CHANGED", stale_delete.exception.code)
            self.assertTrue(draft_path.exists())
            deleted = store.delete(
                draft["id"],
                expected_rule_set_hash=draft["rule_set_hash"],
            )
            self.assertTrue(deleted["deleted"])
            self.assertEqual("physical", deleted["deletion_mode"])
            self.assertFalse(draft_path.exists())
            with self.assertRaises(RuleSetError) as missing:
                store.get(draft["id"], int(draft["version"]))
            self.assertEqual("RULE_SET_NOT_FOUND", missing.exception.code)

            other = store.create_simple(name="切换目标", rules=[ref])
            other = store.validate(other["id"], 1)
            store.activate(
                other["id"],
                1,
                expected_active_rule_set_hash=active["rule_set_hash"],
            )
            source_path = root / active["source_path"]
            source_bytes = source_path.read_bytes()
            self.assertTrue(store.detail(active["id"])["can_delete"])
            deleted_used = store.delete(
                active["id"], expected_rule_set_hash=active["rule_set_hash"]
            )
            self.assertEqual("tombstone", deleted_used["deletion_mode"])
            self.assertEqual(source_bytes, source_path.read_bytes())
            self.assertNotIn(active["id"], {item["id"] for item in store.summaries()})
            self.assertEqual(
                source_bytes,
                source_path.read_bytes(),
            )
            with self.assertRaises(RuleSetError) as missing_used:
                store.get(active["id"], 1)
            self.assertEqual("RULE_SET_NOT_FOUND", missing_used.exception.code)
            repeated = store.delete(
                active["id"], expected_rule_set_hash=active["rule_set_hash"]
            )
            self.assertEqual("tombstone", repeated["deletion_mode"])

    def test_multiple_version_family_is_never_marked_as_physical_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = _create_named(
                root,
                request_id="multi_version_delete_seed",
                name="多版本组合",
                draft_id="multi_version_delete_rule",
                expression="close > ma20",
            )
            store = RuleSetStore(root)
            version_two = store.create_simple_version(
                created["version"]["id"],
                base_version=1,
                expected_rule_set_hash=created["version"]["rule_set_hash"],
                name="多版本组合",
                rules=created["rule_refs"],
            )
            detail = store.detail(created["version"]["id"])
            self.assertEqual("tombstone", detail["delete_mode"])
            paths = [root / item["source_path"] for item in store.versions(detail["id"])]

            deleted = store.delete(
                detail["id"],
                expected_rule_set_hash=version_two["rule_set_hash"],
            )

            self.assertEqual("tombstone", deleted["deletion_mode"])
            self.assertTrue(all(path.exists() for path in paths))

    def test_screening_history_keeps_exact_version_frozen_after_family_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = _create_named(
                root,
                request_id="history_frozen_original",
                name="历史引用组合",
                draft_id="history_frozen_rule",
                expression="close > ma20",
            )
            store = RuleSetStore(root)
            active_v1 = store.activate(original["version"]["id"], 1)
            v1_path = root / active_v1["source_path"]
            v1_bytes = v1_path.read_bytes()

            history = ScreeningHistoryStore(root)
            saved = history.save(
                {
                    "schema_version": 2,
                    "meta": {
                        "created_at": "2026-08-09T18:00:00+08:00",
                        "snapshot_date": "2026-08-09",
                        "as_of_trade_date": "2026-08-09",
                        "candidate_for_trade_date": "2026-08-10",
                        "universe_source": "project_symbols",
                        "universe_scope": "local_library",
                        "is_full_market": False,
                        "rule_hash": active_v1["rule_set_hash"],
                        "rule_set_hash": active_v1["rule_set_hash"],
                        "rule_set_id": active_v1["id"],
                        "rule_set_version": 1,
                        "input_fingerprint": "history-frozen-input-v1",
                    },
                    "summary": {
                        "universe_count": 1,
                        "pass_count": 1,
                        "fail_count": 0,
                        "data_gap_count": 0,
                        "technical_ready_count": 1,
                        "technical_candidate_count": 1,
                    },
                    "rows": [
                        {
                            "symbol": "000001.SZ",
                            "selection_status": "PASS",
                            "technical_status": "BUY_CANDIDATE",
                        }
                    ],
                    "disclaimer": "候选不是买入命令。",
                }
            )
            history_id = saved["meta"]["history_id"]
            frozen_before = history.get(history_id)

            edited = apply_named_simple_rule_set(
                root,
                {
                    "request_id": "history_frozen_edit_v2",
                    "base_version": 1,
                    "expected_rule_set_hash": active_v1["rule_set_hash"],
                    "name": "历史引用组合",
                    "rules": [{"ref": original["rule_refs"][0]}],
                },
                rule_set_id=active_v1["id"],
            )
            replacement = _create_named(
                root,
                request_id="history_frozen_replacement",
                name="当前替换组合",
                draft_id="history_replacement_rule",
                expression="rsi14 <= 70",
            )
            store.activate(
                replacement["version"]["id"],
                1,
                expected_active_rule_set_hash=active_v1["rule_set_hash"],
            )
            detail_before_delete = store.detail(active_v1["id"])
            deleted = store.delete(
                active_v1["id"],
                expected_rule_set_hash=edited["version"]["rule_set_hash"],
            )

            frozen_after = history.get(history_id)
            self.assertEqual(frozen_before, frozen_after)
            self.assertEqual(active_v1["id"], frozen_after["meta"]["rule_set_id"])
            self.assertEqual(1, frozen_after["meta"]["rule_set_version"])
            self.assertEqual(
                active_v1["rule_set_hash"], frozen_after["meta"]["rule_hash"]
            )
            self.assertEqual(
                active_v1["rule_set_hash"],
                next(
                    item["rule_set_hash"]
                    for item in store.all_versions(include_deleted=True)
                    if item["id"] == active_v1["id"] and int(item["version"]) == 1
                ),
            )
            self.assertEqual(v1_bytes, v1_path.read_bytes())
            self.assertEqual("tombstone", deleted["deletion_mode"])
            self.assertTrue(detail_before_delete["has_history"])
            self.assertEqual(1, detail_before_delete["usage_count"])
            self.assertTrue(detail_before_delete["can_delete"])
            with self.assertRaises(RuleSetError) as raised:
                store.detail(active_v1["id"])
            self.assertEqual("RULE_SET_NOT_FOUND", raised.exception.code)
            with self.assertRaises(RuleSetError) as clone_deleted:
                store.clone(
                    active_v1["id"],
                    source_version=1,
                    name="不能恢复的副本",
                    expected_rule_set_hash=active_v1["rule_set_hash"],
                )
            self.assertEqual("RULE_SET_NOT_FOUND", clone_deleted.exception.code)
            for operation in (
                lambda: store.validate(active_v1["id"], 1),
                lambda: store.activate(active_v1["id"], 1),
                lambda: store.create_simple_version(
                    active_v1["id"],
                    base_version=2,
                    expected_rule_set_hash=edited["version"]["rule_set_hash"],
                    name="历史引用组合",
                    rules=original["rule_refs"],
                ),
            ):
                with self.assertRaises(RuleSetError) as inaccessible:
                    operation()
                self.assertEqual("RULE_SET_NOT_FOUND", inaccessible.exception.code)

    def test_legacy_archived_family_is_hidden_and_does_not_claim_fallback_current(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed = _create_named(
                root,
                request_id="legacy_archived_seed",
                name="规则来源",
                draft_id="legacy_archived_rule",
                expression="close > ma20",
            )
            store = RuleSetStore(root)
            state = yaml.safe_load(store.state_path.read_text(encoding="utf-8"))
            seed_id = seed["version"]["id"]
            state["sets"][seed_id]["versions"]["1"]["status"] = "ACTIVE"
            state["sets"]["aaa_deleted"] = {
                "archived": True,
                "versions": {"1": {"status": "ACTIVE"}},
            }
            state["active"] = None
            store.state_path.write_text(
                yaml.safe_dump(state, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            deleted_path = root / "rules" / "rule_sets" / "aaa_deleted" / "v1.yaml"
            deleted_path.parent.mkdir(parents=True, exist_ok=True)
            source = yaml.safe_load(
                (root / seed["version"]["source_path"]).read_text(encoding="utf-8")
            )
            source.update({"id": "aaa_deleted", "name": "旧归档组合", "status": "ACTIVE"})
            deleted_path.write_text(
                yaml.safe_dump(source, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            visible = store.summaries()
            self.assertEqual([seed_id], [item["id"] for item in visible])
            self.assertTrue(visible[0]["is_current"])
            deleted_versions = [
                item
                for item in store.all_versions(include_deleted=True)
                if item["id"] == "aaa_deleted"
            ]
            self.assertEqual(1, len(deleted_versions))
            self.assertTrue(deleted_versions[0]["deleted"])
            self.assertFalse(deleted_versions[0]["is_current"])

    def test_simple_endpoint_rejects_legacy_threshold_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(SimpleRuleEditorError) as raised:
                apply_named_simple_rule_set(
                    Path(temp_dir),
                    {
                        "request_id": "reject_thresholds",
                        "name": "错误组合",
                        "minimum_match": 1,
                        "rules": [
                            _custom_rule(Path(temp_dir), "invalid_rule", "错误规则", "close > ma20")
                        ],
                    },
                )
            self.assertEqual("RULE_SET_SIMPLE_ALL_INVALID", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
