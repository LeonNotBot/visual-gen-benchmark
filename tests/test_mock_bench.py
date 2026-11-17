"""Offline tests: real CLI/exporter, no generation platform or model credentials."""

import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_bench
import report


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MockBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "run"
        self.source = ROOT / "testset" / "提示词测试集.xlsx"
        self.template = ROOT / "docs" / "评测体系_v1.5.xlsx"
        self.originals = {p: digest(p) for p in (self.source, self.template)}
        self.addCleanup(self.check_sources)

    def check_sources(self):
        for path, before in self.originals.items():
            self.assertEqual(digest(path), before)

    def generate(self, *extra):
        argv = ["run_bench.py", "--mock-test", "--out", str(self.output), *extra]
        with patch.object(sys, "argv", argv), \
                patch.object(run_bench, "run_script", side_effect=AssertionError("real pipeline called")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("network called")), \
                patch.object(subprocess, "run", side_effect=AssertionError("subprocess called")):
            run_bench.main()

    def test_generation_preserves_workbook_fields_and_requires_review(self):
        progress = Path(self.temporary.name) / "bridge-progress.json"
        self.generate("--ids", "IMG-01,VID-01", "--progress-file", str(progress))
        state = json.loads((self.output / "status.json").read_text())
        self.assertEqual(state["state"], "waiting_review")
        self.assertEqual(set(state["cases"]), {"IMG-01", "VID-01"})
        self.assertEqual(json.loads(progress.read_text())["completed"], 2)
        self.assertTrue((self.output / "review.required").exists())
        self.assertIn("Mock", (self.output / "report.md").read_text())
        with open(self.output / state["cases"]["VID-01"]["result_file"], "rb") as file:
            self.assertEqual(file.read(8), b"\x89PNG\r\n\x1a\n")
        template = openpyxl.load_workbook(self.template)
        actual = openpyxl.load_workbook(self.output / "评测记录.xlsx")
        self.addCleanup(template.close)
        self.addCleanup(actual.close)
        self.assertEqual(actual.sheetnames[:-1], template.sheetnames)
        self.assertEqual(list(actual.worksheets[2].values)[3][:14], list(template.worksheets[2].values)[3])
        records = actual.worksheets[2]
        ids = {row[0].value for row in records.iter_rows(min_row=5) if row[0].value}
        self.assertEqual(ids, {"IMG-01-01", "VID-01-01"})
        self.assertEqual(records["H5"].value, None)
        self.assertTrue(str(actual.worksheets[3]["B5"].value).startswith("="))
        self.assertTrue(records.data_validations.dataValidation)

    def test_rejects_reused_directory_without_overwriting_scores(self):
        self.output.mkdir()
        existing = self.output / "评测记录.xlsx"
        existing.write_bytes(b"existing reviewed workbook")
        with self.assertRaises(SystemExit) as result:
            self.generate()
        self.assertEqual(result.exception.code, 2)
        self.assertEqual(existing.read_bytes(), b"existing reviewed workbook")

    def test_invalid_filter_does_not_produce_success_report(self):
        with self.assertRaises(SystemExit) as result:
            self.generate("--ids", "missing")
        self.assertEqual(result.exception.code, 2)
        self.assertFalse((self.output / "report.md").exists())

    def test_tags_and_model_names_do_not_load_real_models(self):
        self.generate("--tags", "文生图", "--model-img", "nonexistent-mock-model")
        state = json.loads((self.output / "status.json").read_text())
        self.assertEqual(set(state["cases"]), {"IMG-01"})

    def test_progress_cannot_overwrite_input(self):
        with self.assertRaises(SystemExit):
            self.generate("--progress-file", str(self.source))

    def test_real_mode_remains_default(self):
        with patch.object(sys, "argv", ["run_bench.py", "--out", str(self.output), "--file", "missing.xlsx"]), \
                patch("mock_bench.run_mock", side_effect=AssertionError("mock must be opt-in")):
            with self.assertRaises(SystemExit) as result:
                run_bench.main()
        self.assertEqual(result.exception.code, 2)

    def test_review_report_uses_saved_inputs_without_formula_cache(self):
        self.generate("--ids", "IMG-01")
        path = self.output / "评测记录.xlsx"
        records, summaries = report.read_eval(path)
        self.assertFalse(summaries)  # openpyxl does not recalculate Excel formulas.
        with patch.object(sys, "argv", ["report.py", "--eval", str(path), "--mock-test"]):
            with self.assertRaises(SystemExit):
                report.main()
        self.assertTrue((self.output / "review.required").exists())
        workbook = openpyxl.load_workbook(path)
        for row in workbook.worksheets[2].iter_rows(min_row=5):
            if row[0].value:
                row[8 if row[5].value == "固定加评" else 7].value = "L2" if row[5].value == "固定加评" else "不满足"
        workbook.save(path)
        workbook.close()
        image_hashes = {p: digest(p) for p in self.output.glob("outputs/**/*.png")}
        with patch.object(sys, "argv", ["report.py", "--eval", str(path), "--mock-test"]), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("network called")):
            report.main()
        text = (self.output / "report.md").read_text()
        self.assertIn("不满足", text)
        self.assertIn("L2", text)
        self.assertFalse((self.output / "review.required").exists())
        self.assertEqual({p: digest(p) for p in image_hashes}, image_hashes)

    def test_cli_runs_from_another_working_directory(self):
        result = subprocess.run([sys.executable, str(ROOT / "run_bench.py"), "--mock-test",
                                 "--ids", "IMG-01", "--out", str(self.output)],
                                cwd=self.temporary.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.output / "评测记录.xlsx").is_file())


if __name__ == "__main__":
    unittest.main()
