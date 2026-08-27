from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from src.screening.dataset_manifest import build_dataset_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WebOnlyPackageTests(unittest.TestCase):
    def test_unrelated_project_surfaces_are_not_packaged(self):
        for name in (
            "mobile_app",
            "prompts",
            "workflows",
            "backtest",
            "notebooks",
            "reports",
            "releases",
        ):
            self.assertFalse((PROJECT_ROOT / name).exists(), name)

    def test_runtime_source_has_no_mobile_api_adapter(self):
        sources = []
        for root_name in ("webapp", "shared_ui", "src"):
            sources.extend((PROJECT_ROOT / root_name).rglob("*.py"))
            sources.extend((PROJECT_ROOT / root_name).rglob("*.js"))
        joined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        self.assertNotIn("/mobile-api/", joined)
        self.assertNotIn("build_mobile_compat_bootstrap", joined)

    def test_clean_checkout_has_a_valid_empty_dataset_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_dataset_manifest(Path(temp_dir))

        self.assertEqual("EMPTY", result["alignment_status"])
        self.assertEqual(0, result["discovered_count"])
        self.assertEqual([], result["entries"])


if __name__ == "__main__":
    unittest.main()
