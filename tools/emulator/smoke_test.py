#!/usr/bin/env python3
"""Boot firmware in a running emulator, drive the keypad, and save LCD screenshots.

Talks only to the emulator's HTTP API, so it works against the Docker container
started by run-ci.sh or any instance the desktop extension launched. Standard
library only, so CI needs nothing beyond python3.
"""

import argparse
import hashlib
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

EXPECTED_SIZE = (512, 256)
# The 8-row LCD status bar is scaled 4x in emulator screenshots.
STATUS_BAR_HEIGHT = 32
MAX_MAIN_DIFFERENCE_PERCENT = 1
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class Screen:
    png: bytes
    status: bytes
    main: bytes


class Emulator:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def _request(self, method, path, body=None, timeout=10):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()

    def json(self, method, path, body=None, timeout=10):
        status, _, raw = self._request(method, path, body, timeout)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"raw": raw.decode(errors="replace")}
        return status, payload

    def frame(self):
        status, ctype, raw = self._request("GET", "/frame.png")
        if status == 200 and ctype.startswith("image/png"):
            return raw
        return None

    def key(self, name, hold_ms=200):
        status, payload = self.json("POST", "/api/key",
                                    {"key": name, "action": "tap", "hold_ms": hold_ms})
        if status != 200:
            raise RuntimeError(f"key {name} rejected: HTTP {status} {payload}")


def decode_screen(png):
    """Decode the emulator's 8-bit grayscale PNG into status and main-screen pixels."""
    if not png.startswith(PNG_SIGNATURE):
        raise ValueError("screenshot is not a PNG")

    offset = len(PNG_SIGNATURE)
    header = None
    compressed = []
    ended = False
    while offset < len(png):
        if offset + 12 > len(png):
            raise ValueError("truncated PNG chunk")
        length = struct.unpack_from(">I", png, offset)[0]
        end = offset + 12 + length
        if end > len(png):
            raise ValueError("truncated PNG chunk")
        kind = png[offset + 4:offset + 8]
        data = png[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", png, offset + 8 + length)[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            raise ValueError(f"invalid PNG checksum in {kind!r}")
        if header is None:
            if kind != b"IHDR" or length != 13:
                raise ValueError("PNG header is missing or invalid")
            header = struct.unpack(">IIBBBBB", data)
        elif kind == b"IHDR":
            raise ValueError("duplicate PNG header")
        elif kind == b"IDAT":
            compressed.append(data)
        elif kind == b"IEND":
            if length or end != len(png):
                raise ValueError("invalid PNG end")
            ended = True
            break
        elif kind == b"tRNS":
            raise ValueError("transparent PNG screenshots are not supported")
        elif kind[0] & 0x20 == 0:
            raise ValueError(f"unsupported PNG chunk {kind!r}")
        offset = end

    if header is None or not compressed or not ended:
        raise ValueError("incomplete PNG screenshot")
    width, height, depth, color, compression, filtering, interlace = header
    if (width, height) != EXPECTED_SIZE:
        raise ValueError(f"unexpected PNG size {(width, height)}, expected {EXPECTED_SIZE}")
    if (depth, color, compression, filtering, interlace) != (8, 0, 0, 0, 0):
        raise ValueError("expected an 8-bit non-interlaced grayscale PNG")

    expected_length = height * (width + 1)
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(b"".join(compressed), expected_length + 1)
    except zlib.error as exc:
        raise ValueError("invalid PNG image data") from exc
    if (len(raw) != expected_length or not decompressor.eof
            or decompressor.unused_data or decompressor.unconsumed_tail):
        raise ValueError("invalid PNG image data length")

    pixels = bytearray()
    previous = bytes(width)
    for y in range(height):
        start = y * (width + 1)
        filter_type = raw[start]
        row = bytearray(raw[start + 1:start + 1 + width])
        if filter_type > 4:
            raise ValueError(f"unsupported PNG row filter {filter_type}")
        if filter_type:
            for x in range(width):
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
                row[x] = (row[x] + predictor) & 0xFF
        pixels.extend(row)
        previous = row

    status_end = width * STATUS_BAR_HEIGHT
    return Screen(png, bytes(pixels[:status_end]), bytes(pixels[status_end:]))


def main_difference(first, second):
    return sum(a != b for a, b in zip(first.main, second.main))


def main_matches(first, second):
    return main_difference(first, second) * 100 <= len(first.main) * MAX_MAIN_DIFFERENCE_PERCENT


def wait_for_stable_frame(emu, timeout, previous=None, settle=0.6, require_content=False):
    """Return the last screen and whether its main area changed and settled."""
    deadline = time.monotonic() + timeout
    last = None
    anchor = None
    candidate = False
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        frame = emu.frame()
        now = time.monotonic()
        if frame is None:
            candidate = False
        elif last is None or frame != last.png or not candidate:
            screen = last if last is not None and frame == last.png else decode_screen(frame)
            changed = previous is None or screen.main != previous.main
            has_content = not require_content or len(set(screen.main)) > 1
            if not changed or not has_content:
                candidate = False
            elif not candidate or not main_matches(screen, anchor):
                anchor, last_change, candidate = screen, now, True
            last = screen
        if frame is not None and candidate and now - last_change >= settle:
            return last, True
        time.sleep(0.15)
    return last, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--boot-timeout", type=float, default=90)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    emu = Emulator(args.url)
    steps = []
    frames = {}

    def record(name, ok, detail, screenshot=None):
        entry = {"step": name, "ok": bool(ok), "detail": detail}
        if screenshot is not None:
            path = args.out / f"{name}.png"
            path.write_bytes(screenshot.png)
            entry["screenshot"] = path.name
            entry["sha256"] = hashlib.sha256(screenshot.png).hexdigest()
            frames[name] = screenshot
        steps.append(entry)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
        return ok

    def ui_step(name, key, expect_change_from, expect_equal_to=None):
        before = frames[expect_change_from]
        # Under CI load the firmware can miss a key sent right after the last
        # screen change, so pause first and retry once.
        time.sleep(1.0)
        shot = None
        for attempt in (1, 2):
            emu.key(key, hold_ms=200)
            current, settled = wait_for_stable_frame(emu, timeout=8, previous=before, settle=1.0)
            if current is not None:
                shot = current
            if settled or (current is not None and current.main != before.main):
                break
        if not settled:
            detail = (f"main display changed after {key} but did not settle"
                      if shot is not None and shot.main != before.main
                      else f"main display did not change after {key} (2 attempts)")
            return record(name, False, detail, shot)
        retry = " on retry" if attempt == 2 else ""
        if expect_equal_to is not None:
            reference = frames[expect_equal_to]
            different = main_difference(shot, reference)
            limit = len(reference.main) * MAX_MAIN_DIFFERENCE_PERCENT // 100
            ok = main_matches(shot, reference)
            status = ("status bar differs and was ignored" if shot.status != reference.status
                      else "status bar ignored")
            detail = (f"{key} main display {'matches' if ok else 'differs from'} "
                      f"{expect_equal_to}{retry}: {different}/{len(reference.main)} pixels "
                      f"differ (limit {limit}; {status})")
        else:
            ok = True
            detail = f"{key} changed the main display{retry}"
        return record(name, ok, detail, shot)

    try:
        status, payload = emu.json("POST", "/api/power/on", timeout=30)
        powered = record("power_on", status == 200 and payload.get("powered"),
                         f"HTTP {status}, powered={payload.get('powered')}")

        if powered:
            # Init stalls ~3 s on flash setup, then a splash precedes the main screen.
            shot, settled = wait_for_stable_frame(
                emu, timeout=args.boot_timeout, settle=5, require_content=True)
            booted = record(
                "boot", settled,
                f"nonblank LCD content settled at {EXPECTED_SIZE}" if settled
                else "LCD did not show nonblank, stable content before timeout",
                shot)

            if (booted and ui_step("menu", "MENU", expect_change_from="boot")
                    and ui_step("menu_next", "DOWN", expect_change_from="menu")):
                ui_step("home", "EXIT", expect_change_from="menu_next", expect_equal_to="boot")
    except Exception as exc:  # report, never crash: CI and Copilot read results.json
        record("harness", False, f"{type(exc).__name__}: {exc}")

    for name, path in (("status", "/api/status"), ("panel", "/api/panel"),
                       ("firmware", "/api/firmware"), ("logs", "/api/logs")):
        try:
            _, payload = emu.json("GET", path)
            if name == "panel":
                payload.pop("gram", None)
            (args.out / f"{name}.json").write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            (args.out / f"{name}.json").write_text(json.dumps({"error": str(exc)}))

    passed = all(step["ok"] for step in steps) and bool(steps)
    (args.out / "results.json").write_text(
        json.dumps({"passed": passed, "steps": steps}, indent=2))
    print(f"Smoke test {'PASSED' if passed else 'FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
