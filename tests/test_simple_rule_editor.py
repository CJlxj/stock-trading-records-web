from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.rules import tags
from src.rules.references import describe_rule_references, rule_references
from src.rules.registry import RuleRegistry, RuleRegistryError
from src.rules.simple_editor import (
    SimpleRuleEditorError,
    apply_named_simple_rule_set,
    apply_simple_rule_editor,
    delete_library_rule,
    ensure_rule_catalog_seeded,
    import_default_rules,
    save_library_rule,
)
from src.rules.version_store import RuleSetStore
from src.screening_history import ScreeningHistoryStore


def _custom_rule(
    *,
    draft_id: str,
    name: str,
    expression: str,
    base_ref: str | None = None,
) -> dict:
    return {
        "draft_id": draft_id,
        "base_ref": base_ref,
        "name": name,
        "expression": expression,
        "description": f"{name}，用于收盘后观察候选。",
    }


FACTORY_CATALOG = """
schema_version: 1
rules:
  - id: close_above_ma20
    version: 1
    name: 收盘站上 MA20
    description: 出厂规则。
    kind: scored
    group: trend_structure
    group_label: 趋势结构
    family: moving_average
    timeframe: day
    lookback: 20
    inputs: [close, ma20]
    implementation: {type: expression, expression: "close > ma20"}
    missing_policy: DATA_GAP
    plain_template: 收盘价高于二十日均线
    status: ACTIVE
  - id: volume_ratio_above_1_3
    version: 1
    name: 成交量达到均量门槛
    description: 出厂规则。
    kind: scored
    group: price_volume
    group_label: 量价确认
    family: volume
    timeframe: day
    lookback: 20
    inputs: [volume_ratio]
    implementation: {type: expression, expression: "volume_ratio >= 1.3"}
    missing_policy: DATA_GAP
    plain_template: 量比不低于 1.3
    status: ACTIVE
""".lstrip()


def _write_factory_catalog(root: Path) -> Path:
    """Lay down the shipped seed catalog the panel imports on first read."""

    path = root / "rules" / "catalog" / "builtin" / "candidate_rules.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FACTORY_CATALOG, encoding="utf-8")
    return path


class SimpleRuleEditorTests(unittest.TestCase):
    def test_independent_custom_rule_is_activated_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "request_id": "request_custom_rule_001",
                "name": "独立收盘规则",
                "description": "收盘价站上二十日均线。",
                "expression": "close > ma20",
            }

            first = save_library_rule(root, payload)
            second = save_library_rule(root, payload)

            self.assertEqual("request_custom_rule_001", first["request_id"])
            self.assertEqual("user.rule_request_custom_rule_001@1", first["ref"])
            self.assertEqual("ACTIVE", first["rule"]["status"])
            self.assertFalse(first["deduplicated"])
            self.assertTrue(second["deduplicated"])
            self.assertEqual(first["ref"], second["ref"])
            self.assertEqual(
                1,
                len(
                    RuleRegistry(root).versions(
                        "user.rule_request_custom_rule_001"
                    )
                ),
            )
            receipt = (
                root
                / "history"
                / "custom_rule_requests"
                / "request_custom_rule_001.json"
            )
            self.assertTrue(receipt.is_file())

    def test_independent_custom_rule_rejects_idempotency_conflict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "request_id": "request_custom_rule_002",
                "name": "独立量比规则",
                "description": "量比达到门槛。",
                "expression": "volume_ratio >= 1.3",
            }
            save_library_rule(root, payload)

            with self.assertRaises(SimpleRuleEditorError) as raised:
                save_library_rule(
                    root,
                    {**payload, "expression": "volume_ratio >= 1.5"},
                )

            self.assertEqual("IDEMPOTENCY_CONFLICT", raised.exception.code)
            self.assertEqual("request_id", raised.exception.field)
            self.assertEqual(
                1,
                len(
                    RuleRegistry(root).versions(
                        "user.rule_request_custom_rule_002"
                    )
                ),
            )

    def test_independent_custom_rule_base_ref_creates_new_active_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = save_library_rule(
                root,
                {
                    "request_id": "request_custom_rule_base",
                    "name": "本人趋势规则",
                    "description": "收盘价站上二十日均线。",
                    "expression": "close > ma20",
                },
            )
            edited = save_library_rule(
                root,
                {
                    "request_id": "request_custom_rule_edit",
                    "name": "本人趋势规则",
                    "description": "收盘价站上六十日均线。",
                    "expression": "close > ma60",
                    "base_ref": original["ref"],
                },
            )

            self.assertEqual(
                "user.rule_request_custom_rule_base@2",
                edited["ref"],
            )
            versions = RuleRegistry(root).versions(
                "user.rule_request_custom_rule_base"
            )
            self.assertEqual([2, 1], [item["version"] for item in versions])
            self.assertEqual(
                ["ACTIVE", "SUPERSEDED"],
                [item["status"] for item in versions],
            )

    def test_independent_custom_rule_uses_safe_expression_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(SimpleRuleEditorError) as raised:
                save_library_rule(
                    root,
                    {
                        "request_id": "request_custom_rule_unsafe",
                        "name": "不安全规则",
                        "description": "不应保存。",
                        "expression": "__import__('os').system('id')",
                    },
                )

            self.assertEqual("expression", raised.exception.field)
            self.assertFalse((root / "rules").exists())

    def test_secondary_rules_and_minimum_are_persisted_and_resolved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "request_id": "request_secondary_rules",
                "minimum_match": 1,
                "rules": [
                    _custom_rule(
                        draft_id="primary_close_ma20",
                        name="一级价格规则",
                        expression="close > ma20",
                    )
                ],
                "secondary_minimum_match": 2,
                "secondary_rules": [
                    _custom_rule(
                        draft_id="secondary_volume",
                        name="二级量比规则",
                        expression="volume_ratio >= 1.2",
                    ),
                    _custom_rule(
                        draft_id="secondary_rsi",
                        name="二级 RSI 规则",
                        expression="rsi14 <= 70",
                    ),
                ],
            }

            first = apply_simple_rule_editor(root, payload)
            active = RuleSetStore(root).active()
            resolved = RuleSetStore(root).resolve(active)

            self.assertEqual(
                ["user.rule_primary_close_ma20@1"],
                first["rule_refs"],
            )
            self.assertEqual(
                [
                    "user.rule_secondary_volume@1",
                    "user.rule_secondary_rsi@1",
                ],
                first["secondary_rule_refs"],
            )
            self.assertEqual(first["secondary_rule_refs"], active["secondary"]["rules"])
            self.assertEqual(2, active["secondary"]["minimum_match"])
            self.assertEqual("minimum_match", active["secondary"]["mode"])
            self.assertEqual(5, active["ranking"]["top_n"])
            self.assertEqual(1, len(resolved["scored"]))
            self.assertEqual(2, len(resolved["secondary"]))
            self.assertTrue(
                all(item["status"] == "ACTIVE" for item in resolved["secondary"])
            )
            self.assertEqual(
                ["secondary", "secondary"],
                [
                    item.get("stage")
                    for item in first["changed_rules"]
                    if item.get("stage") == "secondary"
                ],
            )

            second = apply_simple_rule_editor(root, payload)
            self.assertTrue(second["deduplicated"])
            self.assertEqual(first["secondary_rule_refs"], second["secondary_rule_refs"])
            self.assertEqual(1, len(RuleSetStore(root).versions("simple_after_close")))

    def test_secondary_minimum_must_match_secondary_rule_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(SimpleRuleEditorError) as raised:
                apply_simple_rule_editor(
                    root,
                    {
                        "request_id": "request_secondary_limit",
                        "minimum_match": 1,
                        "rules": [
                            _custom_rule(
                                draft_id="primary_limit",
                                name="一级规则",
                                expression="close > ma20",
                            )
                        ],
                        "secondary_minimum_match": 2,
                        "secondary_rules": [
                            _custom_rule(
                                draft_id="secondary_limit",
                                name="二级规则",
                                expression="volume_ratio >= 1.2",
                            )
                        ],
                    },
                )

            self.assertEqual("RULE_EDITOR_INVALID", raised.exception.code)
            self.assertEqual("secondary_minimum_match", raised.exception.field)
            self.assertFalse((root / "rules").exists())

    def test_same_rule_cannot_be_used_in_primary_and_secondary_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            initial = apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_stage_base",
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="shared_stage_rule",
                            name="共用规则",
                            expression="close > ma20",
                        )
                    ],
                },
            )
            shared_ref = initial["rule_refs"][0]
            active_before = RuleSetStore(root).active()

            with self.assertRaises(SimpleRuleEditorError) as raised:
                apply_simple_rule_editor(
                    root,
                    {
                        "request_id": "request_stage_duplicate",
                        "minimum_match": 1,
                        "rules": [{"ref": shared_ref}],
                        "secondary_minimum_match": 1,
                        "secondary_rules": [{"ref": shared_ref}],
                    },
                )

            self.assertEqual("RULE_STAGE_DUPLICATED", raised.exception.code)
            self.assertEqual("secondary_rules", raised.exception.field)
            self.assertEqual(
                active_before["rule_set_hash"],
                RuleSetStore(root).active()["rule_set_hash"],
            )

    def test_legacy_payload_without_secondary_fields_keeps_secondary_inactive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_legacy_payload",
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="legacy_primary_rule",
                            name="旧请求一级规则",
                            expression="close > ma20",
                        )
                    ],
                },
            )
            active = RuleSetStore(root).active()

            self.assertEqual([], result["secondary_rule_refs"])
            self.assertEqual([], active["secondary"]["rules"])
            self.assertEqual(0, active["secondary"]["minimum_match"])
            self.assertEqual("priority_strength", active["ranking"]["mode"])
            self.assertEqual(5, active["ranking"]["top_n"])

    def test_editing_a_seeded_rule_versions_it_in_place(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_factory_catalog(root)
            ensure_rule_catalog_seeded(root)

            edited = save_library_rule(
                root,
                {
                    "request_id": "request_edit_seeded_rule",
                    "name": "我的长期均线规则",
                    "expression": "close > ma60",
                    "base_ref": "close_above_ma20@1",
                },
            )

            # 编辑不派生副本：同一条规则多一个版本，旧版本原样保留。
            self.assertEqual("close_above_ma20@2", edited["ref"])
            registry = RuleRegistry(root)
            self.assertEqual(
                [(2, "ACTIVE"), (1, "SUPERSEDED")],
                [
                    (int(item["version"]), item["status"])
                    for item in registry.versions("close_above_ma20")
                ],
            )
            self.assertEqual("close > ma20", registry.get("close_above_ma20", 1)[
                "implementation"
            ]["expression"])
            # 出厂种子文件从头到尾没有被改写。
            self.assertIn(
                "close > ma20",
                (root / "rules" / "catalog" / "builtin" / "candidate_rules.yaml")
                .read_text(encoding="utf-8"),
            )

    def test_new_rule_is_versioned_activated_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "request_id": "request_new_rule_001",
                "minimum_match": 1,
                "rules": [
                    _custom_rule(
                        draft_id="draft_close_ma20",
                        name="收盘高于二十日均线",
                        expression="close > ma20",
                    )
                ],
            }

            first = apply_simple_rule_editor(root, payload)
            ref = first["rule_refs"][0]
            self.assertEqual("user.rule_draft_close_ma20@1", ref)
            self.assertEqual(
                [ref],
                RuleSetStore(root).active()["scored"]["rules"],
            )
            definition = RuleRegistry(root).get("user.rule_draft_close_ma20", 1)
            self.assertEqual("ACTIVE", definition["status"])
            self.assertEqual(
                "close > ma20",
                definition["implementation"]["normalized_expression"],
            )

            second = apply_simple_rule_editor(root, payload)
            self.assertTrue(second["deduplicated"])
            self.assertEqual(first["rule_refs"], second["rule_refs"])
            self.assertEqual(
                1,
                len(RuleRegistry(root).versions("user.rule_draft_close_ma20")),
            )
            self.assertEqual(
                1,
                len(RuleSetStore(root).versions("simple_after_close")),
            )

            changed_payload = {
                **payload,
                "rules": [
                    _custom_rule(
                        draft_id="draft_close_ma20",
                        name="收盘高于六十日均线",
                        expression="close > ma60",
                    )
                ],
            }
            with self.assertRaises(SimpleRuleEditorError) as raised:
                apply_simple_rule_editor(root, changed_payload)
            self.assertEqual("IDEMPOTENCY_CONFLICT", raised.exception.code)
            self.assertEqual(
                "close > ma20",
                RuleRegistry(root)
                .get("user.rule_draft_close_ma20", 1)["implementation"][
                    "normalized_expression"
                ],
            )

    def test_two_chinese_named_rules_keep_distinct_stable_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_two_rules_001",
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="first_stable_key",
                            name="规则甲",
                            expression="close > ma20",
                        ),
                        _custom_rule(
                            draft_id="second_stable_key",
                            name="规则乙",
                            expression="rsi14 <= 70",
                        ),
                    ],
                },
            )

            self.assertEqual(2, len(set(result["rule_refs"])))
            self.assertEqual(
                {
                    "user.rule_first_stable_key",
                    "user.rule_second_stable_key",
                },
                {
                    item["id"]
                    for item in RuleRegistry(root).list_rules()
                },
            )

    def test_active_custom_rule_resolves_and_evaluates_with_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_evidence_rule",
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="evidence_rule",
                            name="收盘高于均线",
                            expression="close > ma20",
                        )
                    ],
                },
            )
            store = RuleSetStore(root)
            active = store.active()
            resolved = store.resolve(active)
            self.assertEqual(result["rule_refs"][0], "user.rule_evidence_rule@1")
            self.assertEqual(1, len(resolved["scored"]))

            evidence = RuleRegistry(root).evaluate(
                resolved["scored"][0],
                pd.DataFrame(
                    {
                        "close": [10.0, 12.0],
                        "ma20": [11.0, 11.0],
                    }
                ),
                data_date="2026-07-30",
            )
            self.assertEqual("PASS", evidence["status"])
            self.assertEqual("user.rule_evidence_rule", evidence["rule_id"])
            self.assertEqual("close > ma20", evidence["normalized_expression"])
            self.assertTrue(evidence["definition_hash"])

    def test_edit_replaces_active_rule_set_reference_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_edit_rule_v1",
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="editable_rule",
                            name="价格结构",
                            expression="close > ma20",
                        )
                    ],
                },
            )
            ref_v1 = first["rule_refs"][0]
            registry = RuleRegistry(root)
            v1 = registry.get("user.rule_editable_rule", 1)
            v1_path = root / v1["source_path"]
            v1_bytes = v1_path.read_bytes()

            second = apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_edit_rule_v2",
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="editable_rule",
                            base_ref=ref_v1,
                            name="价格结构",
                            expression="close > ma60",
                        )
                    ],
                },
            )
            ref_v2 = second["rule_refs"][0]

            self.assertEqual("user.rule_editable_rule@2", ref_v2)
            self.assertEqual(v1_bytes, v1_path.read_bytes())
            self.assertEqual("SUPERSEDED", registry.get("user.rule_editable_rule", 1)["status"])
            self.assertEqual("ACTIVE", registry.get("user.rule_editable_rule", 2)["status"])
            active_refs = RuleSetStore(root).active()["scored"]["rules"]
            self.assertEqual([ref_v2], active_refs)
            self.assertNotIn(ref_v1, active_refs)

    def test_invalid_expression_does_not_replace_active_rule_or_scheme(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_safe_rule_001",
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="safe_rule",
                            name="安全规则",
                            expression="close > ma20",
                        )
                    ],
                },
            )
            active_before = RuleSetStore(root).active()

            with self.assertRaises(SimpleRuleEditorError) as raised:
                apply_simple_rule_editor(
                    root,
                    {
                        "request_id": "request_unsafe_rule",
                        "minimum_match": 1,
                        "rules": [
                            _custom_rule(
                                draft_id="safe_rule",
                                base_ref=first["rule_refs"][0],
                                name="恶意规则",
                                expression="close.__class__",
                            )
                        ],
                    },
                )

            self.assertEqual("DSL_ATTRIBUTE_FORBIDDEN", raised.exception.code)
            self.assertEqual(
                active_before["rule_set_hash"],
                RuleSetStore(root).active()["rule_set_hash"],
            )
            self.assertEqual(
                "ACTIVE",
                RuleRegistry(root).get("user.rule_safe_rule", 1)["status"],
            )
            self.assertFalse(
                (
                    root
                    / "history"
                    / "rule_editor_requests"
                    / "request_unsafe_rule.json"
                ).exists()
            )

    def test_failure_after_valid_draft_keeps_previous_active_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            initial = apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_atomic_base",
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="atomic_rule",
                            name="原规则",
                            expression="close > ma20",
                        )
                    ],
                },
            )
            active_before = RuleSetStore(root).active()

            with self.assertRaises(SimpleRuleEditorError):
                apply_simple_rule_editor(
                    root,
                    {
                        "request_id": "request_atomic_fail",
                        "minimum_match": 1,
                        "rules": [
                            _custom_rule(
                                draft_id="atomic_rule",
                                base_ref=initial["rule_refs"][0],
                                name="待应用新版本",
                                expression="close > ma60",
                            ),
                            _custom_rule(
                                draft_id="bad_rule",
                                name="错误规则",
                                expression="__import__(1)",
                            ),
                        ],
                    },
                )

            registry = RuleRegistry(root)
            self.assertEqual("ACTIVE", registry.get("user.rule_atomic_rule", 1)["status"])
            self.assertEqual("VALIDATED", registry.get("user.rule_atomic_rule", 2)["status"])
            self.assertEqual(
                active_before["rule_set_hash"],
                RuleSetStore(root).active()["rule_set_hash"],
            )

    def test_rejects_stale_rule_set_hash_from_another_panel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_shared_base",
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="shared_base",
                            name="初始规则",
                            expression="close > ma20",
                        )
                    ],
                },
            )
            client_hash = RuleSetStore(root).active()["rule_set_hash"]

            apply_simple_rule_editor(
                root,
                {
                    "request_id": "request_client_a",
                    "expected_rule_set_hash": client_hash,
                    "minimum_match": 1,
                    "rules": [
                        _custom_rule(
                            draft_id="client_a_rule",
                            name="网页端新规则",
                            expression="close > ma60",
                        )
                    ],
                },
            )
            active_after_client_a = RuleSetStore(root).active()

            with self.assertRaises(SimpleRuleEditorError) as raised:
                apply_simple_rule_editor(
                    root,
                    {
                        "request_id": "request_client_b",
                        "expected_rule_set_hash": client_hash,
                        "minimum_match": 1,
                        "rules": [
                            _custom_rule(
                                draft_id="client_b_rule",
                                name="手机端旧草稿",
                                expression="close > ma10",
                            )
                        ],
                    },
                )

            self.assertEqual("RULE_SET_CHANGED", raised.exception.code)
            self.assertEqual(
                active_after_client_a["rule_set_hash"],
                RuleSetStore(root).active()["rule_set_hash"],
            )


def _screening_batch(rule_id: str, version: int = 1) -> dict:
    """One saved batch whose frozen evidence names ``rule_id``."""

    ref = f"{rule_id}@{version}"
    return {
        "schema_version": 2,
        "meta": {
            "created_at": "2026-08-20T18:00:00+08:00",
            "snapshot_date": "2026-08-20",
            "as_of_trade_date": "2026-08-20",
            "universe_source": "project_symbols",
            "universe_scope": "local_library",
            "is_full_market": False,
            "rule_hash": "batch-rule-hash",
            "rule_set_id": "rs_history",
            "rule_set_version": 1,
            "rule_set_name": "历史组合",
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
                "symbol": "600001.SH",
                "selection_status": "PASS",
                "rule_evidence": [{"rule_id": ref, "status": "PASS"}],
            }
        ],
        "rule_set_snapshot": {
            "rule_set": {"scored": {"rules": [ref]}},
            "rules": [{"id": rule_id, "version": version}],
        },
        "disclaimer": "test",
    }




class RuleCatalogSeedingTests(unittest.TestCase):
    def test_first_read_imports_factory_rules_with_their_categories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_factory_catalog(root)

            first = ensure_rule_catalog_seeded(root)
            second = ensure_rule_catalog_seeded(root)

            self.assertTrue(first["seeded"])
            self.assertEqual(2, first["imported_count"])
            self.assertFalse(second["seeded"])
            self.assertEqual(0, second["imported_count"])

            registry = RuleRegistry(root)
            rules = registry.list_rules()
            self.assertEqual(
                ["close_above_ma20", "volume_ratio_above_1_3"],
                sorted(item["id"] for item in rules),
            )
            # 导入后规则库只有一个目录、一种来源。
            self.assertEqual({"user"}, {item["origin"] for item in rules})
            payload = tags.tag_payload(root, [item["id"] for item in rules])
            self.assertEqual(
                [("趋势结构", 1), ("量价确认", 1)],
                [(tag["label"], tag["count"]) for tag in payload["items"]],
            )
            self.assertEqual(0, payload["unassigned_count"])

    def test_a_deleted_factory_rule_stays_deleted_until_it_is_imported_again(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_factory_catalog(root)
            ensure_rule_catalog_seeded(root)
            tag_id = tags.rule_tag_id(root, "close_above_ma20")
            self.assertTrue(tag_id)

            delete_library_rule(root, "close_above_ma20")
            self.assertEqual(
                ["volume_ratio_above_1_3"],
                [item["id"] for item in RuleRegistry(root).list_rules()],
            )
            self.assertEqual("", tags.rule_tag_id(root, "close_above_ma20"))
            # 只是再读一次规则库，不能让删掉的规则自己回来。
            ensure_rule_catalog_seeded(root)
            self.assertEqual(
                ["volume_ratio_above_1_3"],
                [item["id"] for item in RuleRegistry(root).list_rules()],
            )

            restored = import_default_rules(root)

            self.assertEqual(1, restored["imported_count"])
            self.assertEqual(
                ["close_above_ma20"],
                [item["id"] for item in restored["imported_rules"]],
            )
            self.assertEqual(["趋势结构"], restored["tag_labels"])
            self.assertEqual(
                "趋势结构",
                next(
                    tag["label"]
                    for tag in tags.tag_payload(root)["items"]
                    if tag["id"] == tags.rule_tag_id(root, "close_above_ma20")
                ),
            )

    def test_importing_defaults_never_overwrites_an_edited_rule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_factory_catalog(root)
            ensure_rule_catalog_seeded(root)
            save_library_rule(
                root,
                {
                    "request_id": "request_edit_before_import",
                    "name": "我改过的规则",
                    "expression": "close > ma60",
                    "base_ref": "close_above_ma20@1",
                },
            )

            result = import_default_rules(root)

            self.assertEqual(0, result["imported_count"])
            current = RuleRegistry(root).get("close_above_ma20")
            self.assertEqual(2, int(current["version"]))
            self.assertEqual("我改过的规则", current["name"])


class RuleLibraryDeletionTests(unittest.TestCase):
    def test_unreferenced_rule_is_deleted_with_all_versions_and_untagged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tag = tags.create_tag(root, "我的重点")
            original = save_library_rule(
                root,
                {
                    "request_id": "request_delete_free_0001",
                    "name": "可删规则",
                    "expression": "close > ma20",
                    "tag_id": tag["id"],
                },
            )
            save_library_rule(
                root,
                {
                    "request_id": "request_delete_free_0002",
                    "name": "可删规则",
                    "expression": "close > ma60",
                    "base_ref": original["ref"],
                    "tag_id": tag["id"],
                },
            )
            rule_id = "user.rule_request_delete_free_0001"
            self.assertEqual(2, len(RuleRegistry(root).versions(rule_id)))

            result = delete_library_rule(root, rule_id)

            self.assertTrue(result["deleted"])
            self.assertEqual([1, 2], result["removed_versions"])
            with self.assertRaises(RuleRegistryError):
                RuleRegistry(root).versions(rule_id)
            self.assertFalse((root / "rules" / "catalog" / "user" / rule_id).exists())
            self.assertEqual("", tags.rule_tag_id(root, rule_id))
            self.assertIn(
                "RULE_DELETED",
                (root / "history" / "rule_audit.jsonl").read_text(encoding="utf-8"),
            )

    def test_rule_used_by_any_combination_version_is_refused_and_kept(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            used = save_library_rule(
                root,
                {
                    "request_id": "request_delete_used_0001",
                    "name": "在用规则",
                    "expression": "close > ma20",
                },
            )
            spare = save_library_rule(
                root,
                {
                    "request_id": "request_delete_used_0002",
                    "name": "备用规则",
                    "expression": "close > ma60",
                },
            )
            created = apply_named_simple_rule_set(
                root,
                {
                    "request_id": "request_delete_set_0001",
                    "name": "日常组合",
                    "rules": [{"ref": used["ref"]}],
                },
            )
            # 换掉最新版里的规则，旧版本仍然引用它——跨版本都算引用。
            apply_named_simple_rule_set(
                root,
                {
                    "request_id": "request_delete_set_0002",
                    "name": "日常组合",
                    "base_version": created["version"]["version"],
                    "expected_rule_set_hash": created["version"]["rule_set_hash"],
                    "rules": [{"ref": spare["ref"]}],
                },
                rule_set_id=created["rule_set"]["id"],
            )
            rule_id = "user.rule_request_delete_used_0001"

            with self.assertRaises(SimpleRuleEditorError) as raised:
                delete_library_rule(root, rule_id)

            self.assertEqual("RULE_IN_USE", raised.exception.code)
            references = raised.exception.details["references"]
            self.assertEqual(["rule_set"], sorted({item["kind"] for item in references}))
            self.assertEqual([1], [item["version"] for item in references])
            self.assertIn("日常组合（一级）", str(raised.exception))
            self.assertEqual(1, len(RuleRegistry(root).versions(rule_id)))

    def test_a_saved_screening_batch_never_blocks_a_deletion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            saved = save_library_rule(
                root,
                {
                    "request_id": "request_delete_batch_0001",
                    "name": "历史批次规则",
                    "expression": "close > ma20",
                },
            )
            rule_id = saved["ref"].split("@")[0]
            # 批次自带规则快照，删掉规则不影响它的显示与审计。
            ScreeningHistoryStore(root).save(
                {
                    "meta": {
                        "created_at": "2026-08-20T18:00:00+08:00",
                        "universe_source": "project_symbols",
                        "is_full_market": False,
                        "rule_hash": "batch-rule-hash",
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
                            "symbol": "600001.SH",
                            "rule_evidence": [{"rule_id": saved["ref"], "status": "PASS"}],
                        }
                    ],
                    "rule_set_snapshot": {"rules": [{"id": rule_id, "version": 1}]},
                    "disclaimer": "test",
                }
            )

            self.assertEqual([], rule_references(root, rule_id))
            self.assertTrue(delete_library_rule(root, rule_id)["deleted"])
            self.assertEqual(1, ScreeningHistoryStore(root).count())

    def test_unknown_rule_ids_are_rejected_without_touching_the_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for rule_id in ("user.rule_missing", "close_above_ma20", "Not A Rule Id"):
                with self.subTest(rule_id=rule_id):
                    with self.assertRaises(SimpleRuleEditorError) as raised:
                        delete_library_rule(root, rule_id)
                    self.assertEqual("RULE_NOT_FOUND", raised.exception.code)
            self.assertFalse((root / "rules").exists())

    def test_reference_summary_collapses_versions_of_one_combination(self):
        references = [
            {"kind": "rule_set", "id": "rs_a", "name": "日常筛选规则", "version": 1, "stages": ["一级"]},
            {"kind": "rule_set", "id": "rs_a", "name": "日常筛选规则", "version": 2, "stages": ["一级"]},
            {"kind": "rule_set", "id": "rs_b", "name": "备用组合", "version": 1, "stages": ["二级"]},
        ]

        self.assertEqual(
            "日常筛选规则（一级）、备用组合（二级）",
            describe_rule_references(references),
        )


if __name__ == "__main__":
    unittest.main()
