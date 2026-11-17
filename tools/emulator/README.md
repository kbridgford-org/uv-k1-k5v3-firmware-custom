# UV-K1 / UV-K5 V3 emulator canvas

The project extension in `.github/extensions/uvk5-emulator/` opens the existing
web UI from the Apache-2.0
[`uv-k5-v3-emulator`](https://github.com/MCKero6423/uv-k5-v3-emulator) project.
The UI shows the emulated 128x64 LCD and keypad, and provides power controls,
firmware upload, and screenshots. Canvas actions can power/reset the emulator,
tap a key, or return a screen capture.

## Setup

Requirements are Git, Bash, Python 3, and either a running Docker engine or
native QEMU build dependencies. When Docker is running, the setup script fetches
the emulator at a fixed commit, verifies its pristine flash image, and builds a
local Docker image. Otherwise, it builds QEMU and installs the web UI in a
Python virtual environment under the external state directory. Both backends
use the pinned QEMU 7.2.0 revision.

From the repository root:

```sh
bash tools/emulator/setup.sh
```

For the canvas to start with a firmware image loaded, build a supported preset
first. Fusion is the default:

```sh
./compile-firmware.sh Fusion
```

Then open **UV-K1 / UV-K5 V3 Emulator** from this repository's canvas list. Use
the web UI's **On** control to boot. If the selected preset is not built, upload
a `.bin` or `.elf` image in the web UI instead.

The canvas can select a locally built `Fusion`, `Transfer`, `FieldOps`, `Labs`,
or `Max` preset when opened. Its agent actions are `status`, `power`,
`press_key`, `ptt`, and `screenshot`. The on-screen key controls support hold
duration; firmware treats presses of 400 ms or longer as long presses.

## State and limits

Builds, emulator source, downloaded QEMU sources, and the pristine flash image
are kept under `$COPILOT_HOME/extensions/uvk5-emulator/artifacts/` (or
`~/.copilot/extensions/uvk5-emulator/artifacts/` when `COPILOT_HOME` is unset).
Each canvas gets a separate writable flash copy and upload directory. Those
artifacts are not committed. For a native build, install GLib, Pixman, libfdt,
pkg-config, and Ninja. On macOS, the script reports the Homebrew command if any
native dependency is missing.

The emulator is intended for firmware UI and keypad testing. It does not model
real RF or audio, and timing is approximate. This canvas binds its published
web port to `127.0.0.1`; do not change that to a public interface because the
web UI has no authentication and can control the emulated radio.

The setup script does not copy QEMU's GPL-2.0-or-later PY32F071 machine source
into this Apache-2.0 repository. It fetches that source with the pinned
third-party emulator checkout and builds it into the local image or native QEMU
executable. The setup applies a small compatibility patch for a QEMU
`-Werror` declaration, optional firmware startup, and Unix-socket QMP handling.
