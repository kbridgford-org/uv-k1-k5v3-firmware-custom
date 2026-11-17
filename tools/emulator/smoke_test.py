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
from pathlib import Path

EXPECTED_SIZE = (512, 256)


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

    def key(self, name, hold_ms=100):
        status, payload = self.json("POST", "/api/key",
                                    {"key": name, "action": "tap", "hold_ms": hold_ms})
        if status != 200:
            raise RuntimeError(f"key {name} rejected: HTTP {status} {payload}")


def png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def wait_for_stable_frame(emu, timeout, previous=None, settle=0.6):
    """Return the first frame that differs from `previous` and stays put for `settle` s, else None."""
    deadline = time.monotonic() + timeout
    last = None
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        frame = emu.frame()
        if frame is not None and frame != last:
            last, last_change = frame, time.monotonic()
        if (last is not None and last != previous
                and time.monotonic() - last_change >= settle):
            return last
        time.sleep(0.15)
    return None


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
            path.write_bytes(screenshot)
            entry["screenshot"] = path.name
            entry["sha256"] = hashlib.sha256(screenshot).hexdigest()
            frames[name] = screenshot
        steps.append(entry)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
        return ok

    def ui_step(name, key, expect_change_from, expect_equal_to=None):
        before = frames.get(expect_change_from)
        emu.key(key)
        shot = wait_for_stable_frame(emu, timeout=8, previous=before)
        if shot is None:
            return record(name, False, f"no settled frame after {key}")
        size = png_size(shot)
        if size != EXPECTED_SIZE:
            return record(name, False, f"unexpected PNG size {size}", shot)
        if expect_equal_to is not None:
            ok = shot == frames.get(expect_equal_to)
            detail = (f"{key} returned to the {expect_equal_to} screen" if ok
                      else f"{key} did not return to the {expect_equal_to} screen")
        else:
            ok = shot != before
            detail = (f"{key} changed the display" if ok
                      else f"{key} left the display unchanged")
        return record(name, ok, detail, shot)

    try:
        status, payload = emu.json("POST", "/api/power/on", timeout=30)
        record("power_on", status == 200 and payload.get("powered"),
               f"HTTP {status}, powered={payload.get('powered')}")

        # Init stalls ~3 s on flash setup, then a splash precedes the main screen.
        shot = wait_for_stable_frame(emu, timeout=args.boot_timeout, settle=5)
        size = png_size(shot) if shot else None
        record("boot", shot is not None and size == EXPECTED_SIZE,
               f"main screen rendered at {size}" if shot else "LCD did not settle before timeout",
               shot)

        if "boot" in frames:
            ui_step("menu", "MENU", expect_change_from="boot")
            ui_step("menu_next", "DOWN", expect_change_from="menu")
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
