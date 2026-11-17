#!/usr/bin/env bash
# Boot a firmware image in the Docker emulator, run the smoke test, and leave
# screenshots plus JSON diagnostics in OUT_DIR. Used by the Copilot code review
# setup workflow; also runs locally once tools/emulator/setup.sh has built the image.
#
# Usage: tools/emulator/run-ci.sh [FIRMWARE_BIN] [OUT_DIR]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIRMWARE="${1:-$ROOT/build/Fusion/f4hwn.fusion.bin}"
OUT_DIR="${2:-$ROOT/ccr-evidence}"
UPSTREAM_REV=ab84ee1fb8e0957fbf469c85c4b12dcc4e747395
IMAGE="uvk5-emulator:${UPSTREAM_REV:0:7}"
STATE_DIR="${UVK5_EMULATOR_HOME:-${COPILOT_HOME:-${HOME:?HOME must be set}/.copilot}/extensions/uvk5-emulator/artifacts}"
PORT="${UVK5_CI_PORT:-18080}"
CONTAINER="uvk5-ci-$$"

[[ -f "$FIRMWARE" ]] || { echo "Firmware not found: $FIRMWARE" >&2; exit 2; }
FIRMWARE="$(cd "$(dirname "$FIRMWARE")" && pwd)/$(basename "$FIRMWARE")"
[[ -f "$STATE_DIR/flash-pristine.img" ]] || { echo "Run tools/emulator/setup.sh first" >&2; exit 2; }

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
WORK="$(mktemp -d)"
cp "$STATE_DIR/flash-pristine.img" "$WORK/flash.img"
mkdir -p "$WORK/uploads"

cleanup() {
    docker logs "$CONTAINER" > "$OUT_DIR/emulator.log" 2>&1 || true
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

docker run -d --init --name "$CONTAINER" \
    --publish "127.0.0.1:$PORT:8080" \
    --mount "type=bind,source=$WORK/flash.img,target=/state/flash.img" \
    --mount "type=bind,source=$WORK/uploads,target=/state/uploads" \
    --mount "type=bind,source=$FIRMWARE,target=/opt/firmware.bin,readonly" \
    --env UVK5_UPLOAD_DIR=/state/uploads \
    "$IMAGE" \
    --host 0.0.0.0 --port 8080 \
    --qmp /tmp/uvk5-qmp.sock --qemu /usr/local/bin/qemu-system-arm \
    --flash /state/flash.img --firmware /opt/firmware.bin >/dev/null

for _ in $(seq 1 120); do
    curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1 && break
    sleep 0.5
done

python3 "$ROOT/tools/emulator/smoke_test.py" --url "http://127.0.0.1:$PORT" --out "$OUT_DIR"
