/**
 * Node 内置测试（node --test）：用假 sidecar fixture 验证会话协议客户端。
 * 零网络、零 API Key；真实 sidecar 的协议语义由 Python 侧 tests/test_sidecar_protocol.py 钉住。
 */

import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { SidecarClient, SidecarError } from "../dist/index.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..", "..");
const FIXTURE = path.join(HERE, "fixtures", "fake_sidecar.py");

function pythonExecutable() {
  if (process.env.YAI_PYTHON) return process.env.YAI_PYTHON;
  const venv = path.join(
    ROOT,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  return existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3";
}

function spawnClient(options = {}) {
  return SidecarClient.spawn({
    command: [pythonExecutable(), "-X", "utf8", FIXTURE],
    ...options,
  });
}

async function withClient(options, body) {
  const client = spawnClient(options);
  try {
    return await body(client);
  } finally {
    await client.shutdown().catch(() => {});
    client.close();
  }
}

test("initialize returns the registered tools", async () => {
  await withClient({}, async (client) => {
    const init = await client.initialize();
    assert.equal(init.protocol, 1);
    assert.equal(init.kernel_version, "test");
    assert.equal(init.tools[0]?.name, "echo");
  });
});

test("run streams events and auto-approval reaches done", async () => {
  const frames = [];
  await withClient(
    { onApproval: () => true, onEvent: (event) => frames.push(event) },
    async (client) => {
      await client.initialize();
      const run = await client.run("demo");
      assert.match(run.run_id, /^run-\d+$/);
      const done = await client.waitForEvent((event) => event.type === "done");
      assert.equal(done.data.final_text, "approved");
      assert.equal(done.seq, 1);
      assert.ok(frames.some((frame) => frame.type === "strategy_selected"));
    },
  );
});

test("approval defaults to deny (fail closed)", async () => {
  await withClient({}, async (client) => {
    await client.initialize();
    await client.run("demo");
    const done = await client.waitForEvent((event) => event.type === "done");
    assert.equal(done.data.final_text, "denied");
  });
});

test("unknown methods surface JSON-RPC errors", async () => {
  await withClient({}, async (client) => {
    await client.initialize();
    await assert.rejects(
      client.request("does/not-exist", {}),
      (error) => error instanceof SidecarError && error.code === -32601,
    );
  });
});

test("waitForEvent rejects on timeout and close rejects pending requests", async () => {
  const client = spawnClient();
  try {
    await client.initialize();
    await assert.rejects(
      client.waitForEvent((event) => event.type === "never", 100),
      (error) => error instanceof SidecarError && error.code === -32000,
    );
    client.close();
    await assert.rejects(
      client.ping(),
      (error) => error instanceof SidecarError && error.code === -32003,
    );
  } finally {
    await client.shutdown().catch(() => {});
    client.close();
  }
});
