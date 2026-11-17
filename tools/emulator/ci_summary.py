#!/usr/bin/env python3
"""Turn smoke-test results into the Markdown summary Copilot code review reads.

Writes EVIDENCE_DIR/SUMMARY.md and prints it, so the workflow can also append it
to the job summary. Screenshot links come from upload-artifact outputs, passed as
--shot NAME=URL.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--bundle-url", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--shot", action="append", default=[], metavar="NAME=URL")
    args = parser.parse_args()
    out = args.evidence
    shots = dict(s.split("=", 1) for s in args.shot if "=" in s and s.split("=", 1)[1])

    results_path = out / "results.json"
    if results_path.exists():
        results = json.loads(results_path.read_text())
    else:
        results = {"passed": False, "steps": [
            {"step": "harness", "ok": False,
             "detail": "results.json missing: the firmware build or emulator start failed"}]}

    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip() or "unknown"
    fw = args.firmware
    fw_hash = hashlib.sha256(fw.read_bytes()).hexdigest() if fw and fw.exists() else "not built"

    lines = [
        "# Emulator evidence",
        "",
        f"- Result: **{'PASSED' if results['passed'] else 'FAILED'}**",
        f"- Tested commit: `{commit}`",
        f"- Firmware (Fusion) SHA-256: `{fw_hash}`",
        f"- Full evidence bundle (ZIP): {args.bundle_url or 'upload failed'}",
        f"- Workflow run: {args.run_url}",
        "",
        "| Step | Result | Detail | Screenshot |",
        "| --- | --- | --- | --- |",
    ]
    for step in results["steps"]:
        name = step.get("screenshot", "")
        link = f"[{name}]({shots[name]})" if name in shots else name
        lines.append("| {} | {} | {} | {} |".format(
            step["step"], "✅" if step["ok"] else "❌",
            step["detail"].replace("|", "\\|"), link))
    if not results["passed"]:
        for log in ("build.log", "emulator.log"):
            if (out / log).exists():
                tail = (out / log).read_text(errors="replace").splitlines()[-15:]
                lines += ["", f"<details><summary>{log} (last 15 lines)</summary>", "",
                          "```", *tail, "```", "</details>"]

    text = "\n".join(lines) + "\n"
    out.mkdir(parents=True, exist_ok=True)
    (out / "SUMMARY.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
