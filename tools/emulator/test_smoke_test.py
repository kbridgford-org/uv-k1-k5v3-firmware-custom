import contextlib
import io
import itertools
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import ci_summary
import smoke_test


WIDTH, HEIGHT = smoke_test.EXPECTED_SIZE
STATUS_SIZE = WIDTH * smoke_test.STATUS_BAR_HEIGHT
MAIN_SIZE = WIDTH * HEIGHT - STATUS_SIZE


def png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def make_png(status, main, mixed_filters=False, compression=6):
    pixels = status + main
    assert len(pixels) == WIDTH * HEIGHT
    raw = bytearray()
    previous = bytes(WIDTH)
    for y in range(HEIGHT):
        row = pixels[y * WIDTH:(y + 1) * WIDTH]
        filter_type = y % 5 if mixed_filters else 0
        raw.append(filter_type)
        if not filter_type:
            raw.extend(row)
        else:
            for x, pixel in enumerate(row):
                left = row[x - 1] if x else 0
                up = previous[x]
                upper_left = previous[x - 1] if x else 0
                if filter_type == 1:
                    predictor = left
                elif filter_type == 2:
                    predictor = up
                elif filter_type == 3:
                    predictor = (left + up) // 2
                else:
                    value = left + up - upper_left
                    distances = (abs(value - left), abs(value - up),
                                 abs(value - upper_left))
                    predictor = (left if distances[0] <= distances[1]
                                 and distances[0] <= distances[2]
                                 else up if distances[1] <= distances[2]
                                 else upper_left)
                raw.append((pixel - predictor) & 0xFF)
        previous = row
    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 0, 0, 0, 0)
    return (smoke_test.PNG_SIGNATURE + png_chunk(b"IHDR", header)
            + png_chunk(b"IDAT", zlib.compress(raw, compression))
            + png_chunk(b"IEND", b""))


class FakeEmulator:
    def __init__(self):
        self.keys = []

    def json(self, method, path, body=None, timeout=10):
        if path == "/api/power/on":
            return 200, {"powered": True}
        return 200, {}

    def key(self, name, hold_ms=200):
        self.keys.append(name)


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blank_status = bytes([255]) * STATUS_SIZE
        cls.full_status = bytes([0]) * STATUS_SIZE
        cls.boot_main = bytes([0]) * 4096 + bytes([255]) * (MAIN_SIZE - 4096)
        cls.menu_main = bytes([0]) * 16384 + bytes([255]) * (MAIN_SIZE - 16384)
        cls.next_main = bytes([255]) * 8192 + bytes([0]) * (MAIN_SIZE - 8192)
        cls.boot = smoke_test.decode_screen(make_png(cls.blank_status, cls.boot_main))
        cls.home = smoke_test.decode_screen(make_png(cls.full_status, cls.boot_main))
        cls.menu = smoke_test.decode_screen(make_png(cls.blank_status, cls.menu_main))
        cls.menu_next = smoke_test.decode_screen(make_png(cls.blank_status, cls.next_main))

    def run_smoke(self, captures):
        emulator = FakeEmulator()
        with tempfile.TemporaryDirectory() as output:
            with (mock.patch.object(smoke_test, "Emulator", return_value=emulator),
                  mock.patch.object(smoke_test, "wait_for_stable_frame", side_effect=captures),
                  mock.patch.object(smoke_test.time, "sleep"),
                  mock.patch.object(sys, "argv", ["smoke_test.py", "--out", output]),
                  contextlib.redirect_stdout(io.StringIO())):
                exit_code = smoke_test.main()
            result = json.loads((Path(output) / "results.json").read_text())
            screenshots = {path.name: path.read_bytes() for path in Path(output).glob("*.png")}
        return exit_code, result, screenshots, emulator.keys

    def test_status_only_difference_passes_and_reports_what_was_ignored(self):
        captures = [(self.boot, True), (self.menu, True),
                    (self.menu_next, True), (self.home, True)]
        exit_code, result, screenshots, keys = self.run_smoke(captures)

        self.assertEqual(exit_code, 0)
        self.assertTrue(result["passed"])
        self.assertEqual(keys, ["MENU", "DOWN", "EXIT"])
        home = result["steps"][-1]
        self.assertTrue(home["ok"])
        self.assertIn("0/114688 pixels differ", home["detail"])
        self.assertIn("status bar differs and was ignored", home["detail"])
        self.assertEqual(screenshots["home.png"], self.home.png)

    def test_main_area_difference_over_one_percent_fails_with_screenshot(self):
        different_main = bytes([255]) * 1147 + self.boot_main[1147:]
        wrong_home = smoke_test.decode_screen(make_png(self.full_status, different_main))
        captures = [(self.boot, True), (self.menu, True),
                    (self.menu_next, True), (wrong_home, True)]
        exit_code, result, screenshots, _ = self.run_smoke(captures)

        self.assertEqual(exit_code, 1)
        self.assertFalse(result["passed"])
        self.assertIn("1147/114688 pixels differ (limit 1146", result["steps"][-1]["detail"])
        self.assertEqual(screenshots["home.png"], wrong_home.png)

    def test_failed_comparison_summary_links_both_screenshots(self):
        wrong_home = smoke_test.decode_screen(make_png(self.blank_status, self.menu_main))
        captures = [(self.boot, True), (self.menu, True),
                    (self.menu_next, True), (wrong_home, True)]
        _, result, screenshots, _ = self.run_smoke(captures)

        with tempfile.TemporaryDirectory() as output:
            path = Path(output)
            (path / "results.json").write_text(json.dumps(result))
            for name, data in screenshots.items():
                (path / name).write_bytes(data)
            args = ["ci_summary.py", output,
                    "--shot", "boot.png=https://example.test/boot",
                    "--shot", "home.png=https://example.test/home"]
            with (mock.patch.object(sys, "argv", args),
                  mock.patch.object(ci_summary.subprocess, "run",
                                    return_value=mock.Mock(stdout="test-commit\n")),
                  contextlib.redirect_stdout(io.StringIO())):
                ci_summary.main()
            summary = (path / "SUMMARY.md").read_text()

        self.assertIn("Result: **FAILED**", summary)
        self.assertIn("[boot.png](https://example.test/boot)", summary)
        self.assertIn("[home.png](https://example.test/home)", summary)
        self.assertIn("12288/114688 pixels differ (limit 1146", summary)

    def test_one_percent_threshold_includes_the_last_allowed_pixel(self):
        allowed_main = bytes([255]) * 1146 + self.boot_main[1146:]
        allowed = smoke_test.decode_screen(make_png(self.blank_status, allowed_main))

        self.assertEqual(smoke_test.main_difference(self.boot, allowed), 1146)
        self.assertTrue(smoke_test.main_matches(self.boot, allowed))

    def test_no_change_keeps_last_screenshot_and_stops_navigation(self):
        captures = [(self.boot, True), (self.home, False), (None, False)]
        exit_code, result, screenshots, keys = self.run_smoke(captures)

        self.assertEqual(exit_code, 1)
        self.assertEqual(keys, ["MENU", "MENU"])
        self.assertEqual(result["steps"][-1]["step"], "menu")
        self.assertIn("did not change", result["steps"][-1]["detail"])
        self.assertEqual(screenshots["menu.png"], self.home.png)

    def test_changed_but_unstable_display_is_not_pressed_twice(self):
        captures = [(self.boot, True), (self.menu, False)]
        exit_code, result, screenshots, keys = self.run_smoke(captures)

        self.assertEqual(exit_code, 1)
        self.assertEqual(keys, ["MENU"])
        self.assertIn("changed after MENU but did not settle", result["steps"][-1]["detail"])
        self.assertEqual(screenshots["menu.png"], self.menu.png)

    def test_status_only_changes_do_not_count_as_navigation(self):
        emulator = mock.Mock()
        emulator.frame.side_effect = itertools.chain(
            [self.boot.png], itertools.repeat(self.home.png))
        clock = itertools.count()
        with (mock.patch.object(smoke_test.time, "monotonic",
                                side_effect=lambda: next(clock) / 10),
              mock.patch.object(smoke_test.time, "sleep")):
            last, settled = smoke_test.wait_for_stable_frame(
                emulator, timeout=1.5, previous=self.boot, settle=0.2)

        self.assertFalse(settled)
        self.assertEqual(last.png, self.home.png)

    def test_navigation_can_settle_while_status_bar_changes(self):
        menu_status_changed = make_png(self.full_status, self.menu_main)
        emulator = mock.Mock()
        emulator.frame.side_effect = itertools.cycle([self.menu.png, menu_status_changed])
        clock = itertools.count()
        with (mock.patch.object(smoke_test.time, "monotonic",
                                side_effect=lambda: next(clock) / 10),
              mock.patch.object(smoke_test.time, "sleep")):
            last, settled = smoke_test.wait_for_stable_frame(
                emulator, timeout=2, previous=self.boot, settle=0.3)

        self.assertTrue(settled)
        self.assertEqual(last.main, self.menu.main)

    def test_missing_frames_do_not_complete_the_stability_wait(self):
        emulator = mock.Mock()
        emulator.frame.side_effect = itertools.chain(
            [self.menu.png], itertools.repeat(None))
        clock = itertools.count()
        with (mock.patch.object(smoke_test.time, "monotonic",
                                side_effect=lambda: next(clock) / 10),
              mock.patch.object(smoke_test.time, "sleep")):
            last, settled = smoke_test.wait_for_stable_frame(
                emulator, timeout=1.5, previous=self.boot, settle=0.2)

        self.assertFalse(settled)
        self.assertEqual(last.png, self.menu.png)

    def test_intermittent_missing_frames_restart_the_stability_wait(self):
        emulator = mock.Mock()
        emulator.frame.side_effect = itertools.cycle([self.menu.png, None])
        clock = itertools.count()
        with (mock.patch.object(smoke_test.time, "monotonic",
                                side_effect=lambda: next(clock) / 10),
              mock.patch.object(smoke_test.time, "sleep")):
            last, settled = smoke_test.wait_for_stable_frame(
                emulator, timeout=1.5, previous=self.boot, settle=0.2)

        self.assertFalse(settled)
        self.assertEqual(last.png, self.menu.png)

    def test_wait_does_not_accept_a_blank_boot_screen(self):
        blank = make_png(self.blank_status, bytes([255]) * MAIN_SIZE)
        emulator = mock.Mock()
        emulator.frame.side_effect = itertools.chain(
            [blank, blank, self.boot.png], itertools.repeat(self.boot.png))
        clock = itertools.count()
        with (mock.patch.object(smoke_test.time, "monotonic",
                                side_effect=lambda: next(clock) / 10),
              mock.patch.object(smoke_test.time, "sleep")):
            last, settled = smoke_test.wait_for_stable_frame(
                emulator, timeout=2, settle=0.2, require_content=True)

        self.assertTrue(settled)
        self.assertEqual(last.png, self.boot.png)

    def test_png_encoding_and_row_filters_do_not_affect_comparison(self):
        pixels = bytes((x * 13 + y * 7) % 256 for y in range(HEIGHT) for x in range(WIDTH))
        status, main = pixels[:STATUS_SIZE], pixels[STATUS_SIZE:]
        plain = smoke_test.decode_screen(make_png(status, main, compression=1))
        filtered = smoke_test.decode_screen(make_png(status, main, mixed_filters=True))

        self.assertNotEqual(plain.png, filtered.png)
        self.assertEqual(filtered.status, status)
        self.assertEqual(filtered.main, main)
        self.assertEqual(smoke_test.main_difference(plain, filtered), 0)

    def test_truncated_or_corrupt_png_is_reported(self):
        with self.assertRaisesRegex(ValueError, "truncated|incomplete"):
            smoke_test.decode_screen(self.boot.png[:-4])
        corrupt = bytearray(self.boot.png)
        corrupt[20] ^= 1
        with self.assertRaisesRegex(ValueError, "checksum"):
            smoke_test.decode_screen(bytes(corrupt))


if __name__ == "__main__":
    unittest.main()
