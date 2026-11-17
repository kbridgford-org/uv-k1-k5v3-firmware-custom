#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_REV=ab84ee1fb8e0957fbf469c85c4b12dcc4e747395
QEMU_REV=b67b00e6b4c7831a3f5bc684bc0df7a9bfd1bd56
IMAGE="uvk5-emulator:${UPSTREAM_REV:0:7}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${UVK5_EMULATOR_HOME:-${COPILOT_HOME:-${HOME:?HOME must be set}/.copilot}/extensions/uvk5-emulator/artifacts}"
UPSTREAM_DIR="$STATE_DIR/upstream-$UPSTREAM_REV"
CONTEXT_DIR="$STATE_DIR/docker-context-$UPSTREAM_REV"
QEMU_DIR="$STATE_DIR/qemu-7.2.0"
VENV_DIR="$STATE_DIR/.venv"
FLASH_IMAGE="$STATE_DIR/flash-pristine.img"
IMAGE_HASH_FILE="$STATE_DIR/docker-image-$UPSTREAM_REV.sha256"

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

command -v git >/dev/null || die "git is required"
command -v gzip >/dev/null || die "gzip is required"
if command -v shasum >/dev/null; then
    hash_file() { shasum -a 256 "$1" | awk '{print $1}'; }
    hash_stream() { shasum -a 256 | awk '{print $1}'; }
elif command -v sha256sum >/dev/null; then
    hash_file() { sha256sum "$1" | awk '{print $1}'; }
    hash_stream() { sha256sum | awk '{print $1}'; }
else
    die "shasum or sha256sum is required"
fi
mkdir -p "$STATE_DIR"

if [[ ! -d "$UPSTREAM_DIR/.git" ]]; then
    git clone --filter=blob:none --no-checkout \
        https://github.com/MCKero6423/uv-k5-v3-emulator.git "$UPSTREAM_DIR"
    git -C "$UPSTREAM_DIR" fetch --depth 1 origin "$UPSTREAM_REV"
    git -C "$UPSTREAM_DIR" checkout --detach "$UPSTREAM_REV"
fi

actual_upstream_rev="$(git -C "$UPSTREAM_DIR" rev-parse HEAD)"
[[ "$actual_upstream_rev" == "$UPSTREAM_REV" ]] \
    || die "Cached emulator source has revision $actual_upstream_rev, expected $UPSTREAM_REV: $UPSTREAM_DIR"

if [[ ! -f "$FLASH_IMAGE" ]]; then
    temporary_flash="$FLASH_IMAGE.tmp.$$"
    if ! gzip -dc "$UPSTREAM_DIR/assets/pristine/flash-pristine.img.gz" > "$temporary_flash"; then
        rm -f -- "$temporary_flash"
        die "Could not unpack the pristine flash image"
    fi
    mv -- "$temporary_flash" "$FLASH_IMAGE"
fi

expected_flash_hash=933d697426baee836ff15a6eea6ab7c23f98be3d73e3c5ec9957f6818f393d62
actual_flash_hash="$(hash_file "$FLASH_IMAGE")"
[[ "$actual_flash_hash" == "$expected_flash_hash" ]] \
    || die "Pristine flash checksum mismatch at $FLASH_IMAGE"

if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    docker_config_hash="$(
        cat "$ROOT/tools/emulator/Dockerfile" \
            "$ROOT/tools/emulator/patches/uvk5-emulator-runtime.patch" | hash_stream
    )"
    cached_docker_config_hash=""
    if [[ -f "$IMAGE_HASH_FILE" ]]; then
        cached_docker_config_hash="$(<"$IMAGE_HASH_FILE")"
    fi
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1 \
        || [[ "$cached_docker_config_hash" != "$docker_config_hash" ]]; then
        mkdir -p "$CONTEXT_DIR/upstream"
        if [[ ! -f "$CONTEXT_DIR/upstream/tools/webui.py" ]]; then
            git -C "$UPSTREAM_DIR" archive --format=tar "$UPSTREAM_REV" \
                | tar -xf - -C "$CONTEXT_DIR/upstream"
        fi
        mkdir -p "$CONTEXT_DIR/patches"
        cp "$ROOT/tools/emulator/patches/uvk5-emulator-runtime.patch" \
            "$CONTEXT_DIR/patches/"
        docker build \
            --build-arg "QEMU_REV=$QEMU_REV" \
            --tag "$IMAGE" \
            --file "$ROOT/tools/emulator/Dockerfile" \
            "$CONTEXT_DIR"
        printf '%s\n' "$docker_config_hash" > "$IMAGE_HASH_FILE"
    fi
    backend="Docker image $IMAGE"
else
    command -v pkg-config >/dev/null || die "Install pkg-config, then rerun setup."
    command -v ninja >/dev/null || die "Install Ninja, then rerun setup."
    command -v patch >/dev/null || die "Install patch, then rerun setup."
    command -v python3 >/dev/null || die "Install Python 3, then rerun setup."
    pkg-config --exists glib-2.0 pixman-1 libfdt \
        || die "Native QEMU build needs GLib, Pixman, and libfdt development files visible to pkg-config. On macOS, install them with: brew install glib pixman dtc pkg-config ninja"

    if patch --dry-run --batch -d "$UPSTREAM_DIR" -p1 \
        < "$ROOT/tools/emulator/patches/uvk5-emulator-runtime.patch" >/dev/null 2>&1; then
        patch --batch -d "$UPSTREAM_DIR" -p1 \
            < "$ROOT/tools/emulator/patches/uvk5-emulator-runtime.patch"
    elif ! patch --dry-run --reverse --batch -d "$UPSTREAM_DIR" -p1 \
        < "$ROOT/tools/emulator/patches/uvk5-emulator-runtime.patch" >/dev/null 2>&1; then
        die "Pinned emulator source does not match the runtime compatibility patch."
    fi

    if [[ ! -d "$QEMU_DIR/.git" ]]; then
        git init "$QEMU_DIR"
        git -C "$QEMU_DIR" remote add origin https://gitlab.com/qemu-project/qemu.git
        git -C "$QEMU_DIR" fetch --depth 1 origin "$QEMU_REV"
        git -C "$QEMU_DIR" checkout --detach FETCH_HEAD
    fi
    actual_qemu_rev="$(git -C "$QEMU_DIR" rev-parse HEAD)"
    [[ "$actual_qemu_rev" == "$QEMU_REV" ]] \
        || die "Cached QEMU source has revision $actual_qemu_rev, expected $QEMU_REV: $QEMU_DIR"

    if ! python3 -c 'import distutils' >/dev/null 2>&1 \
        && [[ -x /usr/bin/python3 ]] \
        && /usr/bin/python3 -c 'import distutils' >/dev/null 2>&1; then
        PATH="/usr/bin:$PATH" QEMU_SRC="$QEMU_DIR" \
            bash "$UPSTREAM_DIR/tools/setup_qemu.sh"
    else
        QEMU_SRC="$QEMU_DIR" bash "$UPSTREAM_DIR/tools/setup_qemu.sh"
    fi
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --no-cache-dir --timeout 120 --retries 5 \
        -r "$UPSTREAM_DIR/requirements-dev.txt"
    backend="native QEMU build at $QEMU_DIR/build/qemu-system-arm"
fi

printf '\nEmulator ready.\n'
printf '  Backend: %s\n' "$backend"
printf '  State: %s\n' "$STATE_DIR"
printf 'Open the UV-K1 / UV-K5 V3 Emulator canvas from this repository.\n'
