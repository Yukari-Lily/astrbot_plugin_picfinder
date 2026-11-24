"""Lens 精确标签判定测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engines.lens import _on_exact_tab  # noqa: E402


class TestExactTab(unittest.TestCase):
    def test_udm48_is_exact(self):
        self.assertTrue(_on_exact_tab("https://www.google.com/search?vsrid=abc&udm=48", True))

    def test_lens_default_tab_is_not_exact(self):
        self.assertFalse(_on_exact_tab("https://www.google.com/search?vsrid=abc&udm=26", True))

    def test_visual_request_never_exact(self):
        self.assertFalse(_on_exact_tab("https://www.google.com/search?vsrid=abc&udm=48", False))


if __name__ == "__main__":
    unittest.main()
