"""Validate ClickBench log conversion independently of the large dataset."""

import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "benchmarks/clickbench/summarize.py"
SPEC = importlib.util.spec_from_file_location("summarize", PATH)
summarize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summarize)


class ResultTests(unittest.TestCase):
    def log(self):
        return "\n".join(
            [
                "installation output",
                "Load time: 10.5",
                "Load time: 2.5",
                *(["[1.5,0.5,null],"] * 43),
                "Data size: 20000000000",
                "Concurrent QPS: 2.4",
                "Concurrent error ratio: 0.1",
            ]
        )

    def test_complete_run_preserves_failures_and_accumulates_load(self):
        result = summarize.parse_log(self.log())
        self.assertEqual(result["load_time"], 13)
        self.assertEqual(result["result"], [[1.5, 0.5, None]] * 43)
        self.assertEqual(result["concurrent_error_ratio"], 0.1)

    def test_rejects_incomplete_or_invalid_runs(self):
        for log in (
            self.log().replace("[1.5,0.5,null],\n", "", 1),
            self.log().replace("Concurrent QPS: 2.4", ""),
            self.log().replace("[1.5,0.5,null],", "[-1,0.5,null],", 1),
        ):
            with self.subTest(log=log), self.assertRaises(ValueError):
                summarize.parse_log(log)


if __name__ == "__main__":
    unittest.main()
