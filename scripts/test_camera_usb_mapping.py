#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from go2_dashboard import cameras


class CameraUsbMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        cameras._usb_auto_v4l_cache = None

    def tearDown(self) -> None:
        cameras._usb_auto_v4l_cache = None

    def test_usb_auto_map_prefers_orbbec_rgb_probe_for_logical_zero(self) -> None:
        rows = [
            (0, "2bc5", "080b"),
            (8, "2bc5", "080b"),
            (10, "2bc5", "080b"),
            (12, "2bc5", "080b"),
            (4, "8086", "0b3a"),
            (6, "8086", "0b3a"),
        ]
        with patch("go2_dashboard.cameras.platform.system", return_value="Linux"), patch(
            "go2_dashboard.cameras._enumerate_v4l_usb_bindings", return_value=rows
        ), patch("go2_dashboard.cameras._probe_orbbec_rgb_v4l", return_value=10) as probe0, patch(
            "go2_dashboard.cameras._probe_realsense_rgb_v4l", return_value=6
        ):
            mapping = cameras.usb_auto_v4l_mapping()
        self.assertEqual(mapping[0], 10)
        self.assertEqual(mapping[6], 6)
        probe0.assert_called_once()

    def test_usb_auto_map_keeps_legacy_sonix_support(self) -> None:
        rows = [
            (0, "0735", "0269"),
            (2, "8086", "0b3a"),
            (6, "8086", "0b3a"),
        ]
        with patch("go2_dashboard.cameras.platform.system", return_value="Linux"), patch(
            "go2_dashboard.cameras._enumerate_v4l_usb_bindings", return_value=rows
        ), patch("go2_dashboard.cameras._probe_generic_rgb_v4l", return_value=0), patch(
            "go2_dashboard.cameras._probe_realsense_rgb_v4l", return_value=6
        ):
            mapping = cameras.usb_auto_v4l_mapping()
        self.assertEqual(mapping[0], 0)
        self.assertEqual(mapping[6], 6)

    def test_logical_zero_fallback_honors_configured_default_index(self) -> None:
        rows = [
            (0, "2bc5", "080b"),
            (8, "2bc5", "080b"),
            (10, "2bc5", "080b"),
        ]
        with patch.dict("os.environ", {"GO2_ARM_CAMERA_V4L_DEFAULT": "10"}, clear=False), patch(
            "go2_dashboard.cameras.platform.system", return_value="Linux"
        ), patch("go2_dashboard.cameras._enumerate_v4l_usb_bindings", return_value=rows), patch(
            "go2_dashboard.cameras._probe_orbbec_rgb_v4l", return_value=None
        ), patch("go2_dashboard.cameras._v4l_sysfs_card_name", return_value=""), patch(
            "go2_dashboard.cameras._probe_realsense_rgb_v4l", return_value=None
        ):
            mapping = cameras.usb_auto_v4l_mapping()
        self.assertEqual(mapping[0], 10)

    def test_logical_zero_prefers_orbbec_when_orbbec_and_sonix_are_both_present(self) -> None:
        rows = [
            (0, "0735", "0269"),
            (10, "2bc5", "080b"),
        ]
        with patch("go2_dashboard.cameras.platform.system", return_value="Linux"), patch(
            "go2_dashboard.cameras._enumerate_v4l_usb_bindings", return_value=rows
        ), patch("go2_dashboard.cameras._probe_orbbec_rgb_v4l", return_value=None), patch(
            "go2_dashboard.cameras._probe_realsense_rgb_v4l", return_value=None
        ):
            mapping = cameras.usb_auto_v4l_mapping()
        self.assertEqual(mapping[0], 10)

    def test_orbbec_fallback_prefers_rgb_sysfs_over_depth_node(self) -> None:
        rows = [
            (0, "2bc5", "080b"),
            (8, "2bc5", "080b"),
        ]

        def fake_name(idx: int) -> str:
            if int(idx) == 0:
                return "Orbbec Gemini Depth"
            if int(idx) == 8:
                return "Orbbec Gemini RGB Camera"
            return ""

        with patch("go2_dashboard.cameras.platform.system", return_value="Linux"), patch(
            "go2_dashboard.cameras._enumerate_v4l_usb_bindings", return_value=rows
        ), patch("go2_dashboard.cameras._probe_orbbec_rgb_v4l", return_value=None), patch(
            "go2_dashboard.cameras._probe_realsense_rgb_v4l", return_value=None
        ), patch("go2_dashboard.cameras._v4l_sysfs_card_name", side_effect=fake_name):
            mapping = cameras.usb_auto_v4l_mapping()
        self.assertEqual(mapping[0], 8)


if __name__ == "__main__":
    unittest.main()
