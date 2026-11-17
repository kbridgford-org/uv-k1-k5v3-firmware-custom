import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { mkdir, copyFile, access, realpath, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { CanvasError, createCanvas, joinSession } from "@github/copilot-sdk/extension";

const upstreamRevision = "ab84ee1fb8e0957fbf469c85c4b12dcc4e747395";
const imageName = `uvk5-emulator:${upstreamRevision.slice(0, 7)}`;
const stateRoot = process.env.UVK5_EMULATOR_HOME
    ?? path.join(process.env.COPILOT_HOME ?? path.join(os.homedir(), ".copilot"),
        "extensions", "uvk5-emulator", "artifacts");
const upstreamRoot = path.join(stateRoot, `upstream-${upstreamRevision}`);
const instances = new Map();
let workspacePath;

function reservePort() {
    return new Promise((resolve, reject) => {
        const server = createServer();
        server.once("error", reject);
        server.listen(0, "127.0.0.1", () => {
            const address = server.address();
            if (!address || typeof address === "string") {
                server.close();
                reject(new Error("Could not reserve a local port"));
                return;
            }
            const { port } = address;
            server.close((error) => error ? reject(error) : resolve(port));
        });
    });
}

async function ensureReadable(filePath) {
    try {
        await access(filePath);
        return true;
    } catch (error) {
        if (error.code === "ENOENT") return false;
        throw error;
    }
}

async function findFirmware(preset) {
    const roots = [...new Set([workspacePath, process.cwd()].filter(Boolean).map((root) => path.resolve(root)))];
    for (const root of roots) {
        const candidate = path.resolve(root, "build", preset, `f4hwn.${preset.toLowerCase()}.bin`);
        let resolved;
        try {
            resolved = await realpath(candidate);
        } catch (error) {
            if (error.code === "ENOENT") continue;
            throw error;
        }
        const relative = path.relative(root, resolved);
        if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
            throw new CanvasError("invalid_firmware_path", "The built firmware path resolves outside this repository.");
        }
        if (!(await stat(resolved)).isFile()) {
            throw new CanvasError("invalid_firmware_path", "The expected firmware build is not a regular file.");
        }
        return resolved;
    }
    return undefined;
}

async function ensureInstalled() {
    const flashImage = path.join(stateRoot, "flash-pristine.img");
    if (!(await ensureReadable(flashImage))) {
        throw new CanvasError(
            "emulator_not_setup",
            "Run `bash tools/emulator/setup.sh` from the firmware repository, then reopen the canvas.",
        );
    }

    let result;
    try {
        result = await runCommand("docker", ["image", "inspect", imageName]);
    } catch {
        result = { code: 1 };
    }
    if (result.code !== 0) {
        const qemuPath = path.join(stateRoot, "qemu-7.2.0", "build", "qemu-system-arm");
        const pythonPath = path.join(stateRoot, ".venv", "bin", "python");
        if (await ensureReadable(path.join(upstreamRoot, "tools/webui.py"))
            && await ensureReadable(qemuPath) && await ensureReadable(pythonPath)) {
            return { kind: "native", flashImage, qemuPath, pythonPath };
        }
        throw new CanvasError("emulator_not_setup",
            "Run `bash tools/emulator/setup.sh` from the firmware repository, then reopen the canvas.");
    }
    return { kind: "docker", flashImage };
}

function runCommand(command, args, options = {}) {
    return new Promise((resolve, reject) => {
        const child = spawn(command, args, {
            stdio: ["ignore", "ignore", "pipe"],
            ...options,
        });
        let stderr = "";
        child.stderr?.on("data", (chunk) => {
            stderr = (stderr + chunk.toString()).slice(-8000);
        });
        child.once("error", reject);
        child.once("close", (code, signal) => resolve({
            code: code ?? -1,
            signal,
            stderr: stderr.trim(),
        }));
    });
}

async function requestJson(entry, route, init) {
    const response = await fetch(`${entry.url}${route}`, init);
    const body = await response.json();
    if (!response.ok) {
        throw new CanvasError("emulator_request_failed", body.error ?? `Emulator returned HTTP ${response.status}`);
    }
    return body;
}

async function stopInstance(instanceId) {
    const entry = instances.get(instanceId);
    if (!entry) return;
    instances.delete(instanceId);

    try {
        await fetch(`${entry.url}/api/power/off`, {
            method: "POST",
            signal: AbortSignal.timeout(1500),
        });
    } catch {
        // The container is stopped below even if the web UI is already unavailable.
    }

    if (entry.backend.kind === "docker") {
        const stopped = await runCommand("docker", ["stop", "--time", "1", entry.containerName]);
        if (stopped.code !== 0 && !/No such container/i.test(stopped.stderr)) {
            entry.session.log(`Could not stop emulator container: ${stopped.stderr || stopped.code}`, { level: "warning" });
        }
    }
    entry.child.kill("SIGTERM");
}

async function startInstance(instanceId, input) {
    const backend = await ensureInstalled();
    const instanceDir = path.join(stateRoot, "instances", randomUUID());
    const flashPath = path.join(instanceDir, "flash.img");
    const uploadDir = path.join(instanceDir, "uploads");
    await mkdir(uploadDir, { recursive: true });
    await copyFile(backend.flashImage, flashPath);

    const port = await reservePort();
    const preset = input?.preset ?? "Fusion";
    const firmwarePath = await findFirmware(preset);
    const [qmpPort, serialPort, gdbPort] = backend.kind === "native"
        ? await Promise.all([reservePort(), reservePort(), reservePort()])
        : [];
    const containerName = `uvk5-${process.pid}-${randomUUID().slice(0, 8)}`;
    let command;
    let args;
    let options = { stdio: ["ignore", "ignore", "pipe"] };

    if (backend.kind === "docker") {
        args = [
            "run", "--rm", "--init", "--name", containerName,
            "--publish", `127.0.0.1:${port}:8080`,
            "--mount", `type=bind,source=${flashPath},target=/state/flash.img`,
            "--mount", `type=bind,source=${uploadDir},target=/state/uploads`,
            "--env", "UVK5_UPLOAD_DIR=/state/uploads",
        ];
        if (firmwarePath) {
            args.push("--mount", `type=bind,source=${firmwarePath},target=/opt/firmware.bin,readonly`);
        }
        args.push(
            imageName,
            "--host", "0.0.0.0",
            "--port", "8080",
            "--qmp", "/tmp/uvk5-qmp.sock",
            "--qemu", "/usr/local/bin/qemu-system-arm",
            "--flash", "/state/flash.img",
        );
        if (firmwarePath) args.push("--firmware", "/opt/firmware.bin");
        command = "docker";
    } else {
        command = backend.pythonPath;
        args = [
            path.join(upstreamRoot, "tools/webui.py"),
            "--host", "127.0.0.1",
            "--port", String(port),
            "--qmp", `127.0.0.1:${qmpPort}`,
            "--qemu", backend.qemuPath,
            "--flash", flashPath,
            "--serial-port", String(serialPort),
            "--gdb-port", String(gdbPort),
        ];
        if (firmwarePath) args.push("--firmware", firmwarePath);
        options = {
            cwd: upstreamRoot,
            env: { ...process.env, UVK5_UPLOAD_DIR: uploadDir },
            stdio: ["ignore", "ignore", "pipe"],
        };
    }

    const child = spawn(command, args, {
        ...options,
    });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
        stderr = (stderr + chunk.toString()).slice(-8000);
    });

    const entry = {
        child,
        backend,
        containerName,
        url: `http://127.0.0.1:${port}`,
        session: input.session,
        flashPath,
    };
    instances.set(instanceId, entry);
    child.once("close", () => {
        if (instances.get(instanceId) === entry) instances.delete(instanceId);
    });

    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
        if (child.exitCode !== null || child.signalCode !== null) {
            instances.delete(instanceId);
            throw new CanvasError(
                "emulator_start_failed",
                `Docker exited with code ${child.exitCode}: ${stderr.trim() || "no diagnostic output"}`,
            );
        }
        try {
            const response = await fetch(`${entry.url}/api/status`, {
                signal: AbortSignal.timeout(700),
            });
            if (response.ok) {
                const status = await response.json();
                entry.firmwarePath = firmwarePath;
                entry.status = status;
                return entry;
            }
        } catch {
            // The container is still starting.
        }
        await delay(250);
    }

    await stopInstance(instanceId);
    throw new CanvasError(
        "emulator_start_timeout",
        `Emulator web UI did not start in time.${stderr ? ` ${stderr.trim()}` : ""}`,
    );
}

const session = await joinSession({
    canvases: [
        createCanvas({
            id: "uvk5-emulator",
            displayName: "UV-K1 / UV-K5 V3 Emulator",
            description: "View and control the UV-K1 / UV-K5 V3 firmware emulator.",
            inputSchema: {
                type: "object",
                properties: {
                    preset: {
                        type: "string",
                        enum: ["Fusion", "Transfer", "FieldOps", "Labs", "Max"],
                        description: "Select a locally built firmware preset; defaults to Fusion.",
                    },
                },
                additionalProperties: false,
            },
            actions: [
                {
                    name: "status",
                    description: "Get emulator power and firmware status.",
                    handler: async (ctx) => {
                        const entry = instances.get(ctx.instanceId);
                        if (!entry) throw new CanvasError("emulator_not_open", "Open the emulator canvas first.");
                        return requestJson(entry, "/api/status");
                    },
                },
                {
                    name: "power",
                    description: "Power the emulated radio on, off, reset, pause, or resume.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            action: { type: "string", enum: ["on", "off", "reset", "pause", "resume"] },
                        },
                        required: ["action"],
                        additionalProperties: false,
                    },
                    handler: async (ctx) => {
                        const entry = instances.get(ctx.instanceId);
                        if (!entry) throw new CanvasError("emulator_not_open", "Open the emulator canvas first.");
                        return requestJson(entry, `/api/power/${ctx.input.action}`, { method: "POST" });
                    },
                },
                {
                    name: "press_key",
                    description: "Tap a key on the emulated radio. Holds of 400 ms or longer follow the firmware's long-press behavior.",
                    inputSchema: {
                        type: "object",
                        properties: {
                            key: {
                                type: "string",
                                enum: ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "MENU", "UP", "DOWN", "EXIT", "F", "STAR", "SIDE1", "SIDE2"],
                            },
                            hold_ms: { type: "integer", minimum: 60, maximum: 5000, default: 100 },
                        },
                        required: ["key"],
                        additionalProperties: false,
                    },
                    handler: async (ctx) => {
                        const entry = instances.get(ctx.instanceId);
                        if (!entry) throw new CanvasError("emulator_not_open", "Open the emulator canvas first.");
                        return requestJson(entry, "/api/key", {
                            method: "POST",
                            headers: { "content-type": "application/json" },
                            body: JSON.stringify({
                                key: ctx.input.key,
                                action: "tap",
                                hold_ms: ctx.input.hold_ms ?? 100,
                            }),
                        });
                    },
                },
                {
                    name: "ptt",
                    description: "Press or release the emulated push-to-talk control.",
                    inputSchema: {
                        type: "object",
                        properties: { held: { type: "boolean" } },
                        required: ["held"],
                        additionalProperties: false,
                    },
                    handler: async (ctx) => {
                        const entry = instances.get(ctx.instanceId);
                        if (!entry) throw new CanvasError("emulator_not_open", "Open the emulator canvas first.");
                        return requestJson(entry, "/api/ptt", {
                            method: "POST",
                            headers: { "content-type": "application/json" },
                            body: JSON.stringify({ held: ctx.input.held }),
                        });
                    },
                },
                {
                    name: "screenshot",
                    description: "Capture the emulator screen and return it as an image.",
                    handler: async (ctx) => {
                        const entry = instances.get(ctx.instanceId);
                        if (!entry) throw new CanvasError("emulator_not_open", "Open the emulator canvas first.");
                        const response = await fetch(`${entry.url}/frame.png`);
                        if (!response.ok) {
                            const body = await response.json();
                            throw new CanvasError("screenshot_unavailable", body.error ?? "The emulator has no screen frame yet.");
                        }
                        const image = Buffer.from(await response.arrayBuffer());
                        return {
                            textResultForLlm: "Current UV-K1 / UV-K5 V3 emulator screen.",
                            resultType: "success",
                            binaryResultsForLlm: [{
                                type: "image",
                                mimeType: "image/png",
                                data: image.toString("base64"),
                                description: "Emulated radio LCD",
                            }],
                        };
                    },
                },
            ],
            open: async (ctx) => {
                let entry = instances.get(ctx.instanceId);
                if (!entry) {
                    entry = await startInstance(ctx.instanceId, {
                        preset: ctx.input?.preset,
                        session,
                    });
                    instances.set(ctx.instanceId, entry);
                }
                return {
                    title: `UV-K1 / UV-K5 V3 - ${ctx.input?.preset ?? "Fusion"}`,
                    url: entry.url,
                    status: entry.firmwarePath
                        ? "Ready. Use the web UI's On button to boot the emulator."
                        : "No local firmware build found. Compile a preset or upload a firmware image in the web UI.",
                };
            },
            onClose: async (ctx) => stopInstance(ctx.instanceId),
        }),
    ],
});

workspacePath = session.workspacePath ?? process.cwd();

async function cleanup() {
    await Promise.all([...instances.keys()].map((instanceId) => stopInstance(instanceId)));
}

process.once("SIGTERM", () => void cleanup().finally(() => process.exit(0)));
process.once("SIGINT", () => void cleanup().finally(() => process.exit(0)));
