#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from go2_dashboard.blueprints import d1_pick_teach


class FocusRgbStreamTests(unittest.TestCase):
    def test_wrist_defaults_to_sdk_color_only(self) -> None:
        self.assertEqual(d1_pick_teach._color_stream_source_setting("wrist"), "realsense_only")
        self.assertEqual(d1_pick_teach._color_stream_source_order("wrist"), ["realsense"])

    def test_accepts_only_available_confirmed_rgb(self) -> None:
        self.assertTrue(
            d1_pick_teach._cache_stats_rgb_usable(
                {"available": True, "rgb_like": True, "stream_kind": "rgb"}
            )
        )

    def test_rejects_mono_or_ir_cache_frame(self) -> None:
        self.assertFalse(
            d1_pick_teach._cache_stats_rgb_usable(
                {"available": True, "rgb_like": False, "stream_kind": "mono_or_ir"}
            )
        )

    def test_rejects_unavailable_or_incomplete_diagnostics(self) -> None:
        self.assertFalse(d1_pick_teach._cache_stats_rgb_usable({"available": False, "rgb_like": True, "stream_kind": "rgb"}))
        self.assertFalse(d1_pick_teach._cache_stats_rgb_usable({"available": True}))
        self.assertFalse(d1_pick_teach._cache_stats_rgb_usable(None))


if __name__ == "__main__":
    unittest.main()
