import subprocess
import unittest
from pathlib import Path


class FrontendTitleTests(unittest.TestCase):
    def test_home_archive_language_and_date_title_selection(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(["node", str(root / "tests/title_consistency.mjs")],
                                cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("title consistency: 8 language/date cases PASS", result.stdout)
