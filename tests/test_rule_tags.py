from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from src.rules import tags
from src.rules.simple_editor import (
    SimpleRuleEditorError,
    delete_library_rule,
    save_library_rule,
)


def _save_rule(root: Path, key: str, name: str, tag_id: str = "") -> str:
    saved = save_library_rule(
        root,
        {
            "request_id": f"library_{key}"[:80],
            "name": name,
            "expression": "close > ma20",
            "tag_id": tag_id,
        },
    )
    return saved["ref"].split("@")[0]


class RuleTagStoreTests(unittest.TestCase):
    def test_tags_are_created_renamed_and_kept_unique(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = tags.create_tag(root, "趋势结构")
            second = tags.create_tag(root, "量价确认")

            self.assertRegex(first["id"], r"^tag_[a-f0-9]{12}$")
            self.assertNotEqual(first["id"], second["id"])
            # 顺序按创建先后，界面读起来才稳定。
            self.assertEqual(
                ["趋势结构", "量价确认"],
                [tag["label"] for tag in tags.tag_payload(root)["items"]],
            )

            renamed = tags.rename_tag(root, first["id"], "我的趋势")
            self.assertEqual(first["id"], renamed["id"])
            self.assertEqual(
                ["我的趋势", "量价确认"],
                [tag["label"] for tag in tags.tag_payload(root)["items"]],
            )

            with self.assertRaises(tags.RuleTagError) as duplicate:
                tags.create_tag(root, "量价确认")
            self.assertEqual("RULE_TAG_NAME_EXISTS", duplicate.exception.code)

            with self.assertRaises(tags.RuleTagError) as renamed_duplicate:
                tags.rename_tag(root, first["id"], "量价确认")
            self.assertEqual("RULE_TAG_NAME_EXISTS", renamed_duplicate.exception.code)

    def test_invalid_labels_are_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for label in ("", "   ", "我 的", "我\t的", "一" * 13, "未分类"):
                with self.subTest(label=label):
                    with self.assertRaises(tags.RuleTagError):
                        tags.create_tag(root, label)
            self.assertEqual([], tags.tag_payload(root)["items"])

    def test_unknown_tag_ids_are_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for call in (
                lambda: tags.rename_tag(root, "tag_missing0000", "新名字"),
                lambda: tags.delete_tag(root, "tag_missing0000"),
                lambda: tags.assign_tag(root, "user.rule_x", "tag_missing0000"),
            ):
                with self.assertRaises(tags.RuleTagError) as raised:
                    call()
                self.assertEqual("RULE_TAG_NOT_FOUND", raised.exception.code)

    def test_assignment_counts_only_the_rules_it_is_asked_about(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tag = tags.create_tag(root, "我的重点")
            tags.assign_tag(root, "rule_a", tag["id"])
            tags.assign_tag(root, "rule_b", tag["id"])

            payload = tags.tag_payload(root, ["rule_a", "rule_c"])

            self.assertEqual(1, payload["items"][0]["count"])
            self.assertEqual(1, payload["unassigned_count"])
            self.assertEqual("未分类", payload["unassigned_label"])
            self.assertEqual(tag["id"], tags.rule_tag_id(root, "rule_a"))
            self.assertEqual("", tags.rule_tag_id(root, "rule_c"))

            # 传空标签就是解绑。
            tags.assign_tag(root, "rule_a", "")
            self.assertEqual("", tags.rule_tag_id(root, "rule_a"))


class RuleTagLifecycleTests(unittest.TestCase):
    def test_deleting_a_tag_releases_its_rules_without_touching_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tag = tags.create_tag(root, "我的重点")
            kept = tags.create_tag(root, "另一类")
            first = _save_rule(root, "tag_release_a", "回踩规则", tag["id"])
            second = _save_rule(root, "tag_release_b", "放量规则", tag["id"])
            other = _save_rule(root, "tag_release_c", "无关规则", kept["id"])
            rule_files = {
                path: path.read_bytes()
                for path in (root / "rules" / "catalog" / "user").glob("*/*.yaml")
            }
            self.assertEqual(3, len(rule_files))

            result = tags.delete_tag(root, tag["id"])

            self.assertTrue(result["deleted"])
            self.assertEqual([first, second], result["released_rule_ids"])
            self.assertEqual("", tags.rule_tag_id(root, first))
            self.assertEqual("", tags.rule_tag_id(root, second))
            self.assertEqual(kept["id"], tags.rule_tag_id(root, other))
            self.assertEqual(
                ["另一类"],
                [item["label"] for item in tags.tag_payload(root)["items"]],
            )
            # 规则本身一个字节都没被改写。
            for path, original in rule_files.items():
                self.assertEqual(original, path.read_bytes())

    def test_retagging_a_rule_never_creates_a_new_rule_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = tags.create_tag(root, "我的重点")
            second = tags.create_tag(root, "备选")
            payload = {
                "request_id": "library_retag_rule",
                "name": "回踩规则",
                "expression": "close > ma20",
                "tag_id": first["id"],
            }
            created = save_library_rule(root, payload)
            rule_id = created["ref"].split("@")[0]

            # 改分类走的是同一条「保存规则」通道：内容没变就没有新版本。
            retagged = save_library_rule(
                root,
                {
                    **payload,
                    "request_id": "library_retag_rule_2",
                    "base_ref": created["ref"],
                    "tag_id": second["id"],
                },
            )

            self.assertEqual(created["ref"], retagged["ref"])
            self.assertEqual(second["id"], tags.rule_tag_id(root, rule_id))
            self.assertEqual(
                1,
                len(list((root / "rules" / "catalog" / "user" / rule_id).glob("*.yaml"))),
            )

    def test_saving_a_rule_with_an_unknown_tag_is_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(SimpleRuleEditorError) as raised:
                save_library_rule(
                    root,
                    {
                        "request_id": "library_bad_tag_rule",
                        "name": "标签不存在",
                        "expression": "close > ma20",
                        "tag_id": "tag_missing0000",
                    },
                )
            self.assertEqual("RULE_TAG_NOT_FOUND", raised.exception.code)
            self.assertEqual("tag_id", raised.exception.field)

    def test_deleting_a_rule_drops_only_its_own_binding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tag = tags.create_tag(root, "我的重点")
            doomed = _save_rule(root, "tag_drop_a", "待删规则", tag["id"])
            kept = _save_rule(root, "tag_drop_b", "保留规则", tag["id"])

            delete_library_rule(root, doomed)

            store = yaml.safe_load(
                (root / "rules" / "tags.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual({kept: tag["id"]}, store["assignments"])
            self.assertEqual(1, tags.tag_payload(root, [kept])["items"][0]["count"])


if __name__ == "__main__":
    unittest.main()
