#!/usr/bin/env node
/**
 * 零依赖 Node 宿主：真实拉起 Python sidecar，走完整会话协议。
 *
 * 三个进程/服务都在本机、零外网：
 *   1. 本文件里的 HTTP 服务：OpenAPI 工具（GET /orders）+ OpenAI 兼容 mock 模型；
 *   2. `python -m yai_core.sidecar`：内核 + Agent loop；
 *   3. 本文件：stdio NDJSON 客户端，转发审批。
 *
 * 运行：
 *   YAI_PYTHON=.venv/Scripts/python.exe node examples/host_l_sidecar_node/host.mjs
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");

function pythonExecutable() {
  if (process.env.YAI_PYTHON) return process.env.YAI_PYTHON;
  const venv = path.join(
    ROOT,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  return existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3";
}

function json(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
}

/** 本地业务服务：一个只读 OpenAPI 工具 + 一个 OpenAI 兼容 mock 模型。 */
async function startLocalServices() {
  const server = http.createServer(async (req, res) => {
    try {
      if (req.method === "GET" && req.url === "/orders") {
        json(res, 200, [
          { id: "A001", amount: 100 },
          { id: "A002", amount: 200 },
        ]);
        return;
      }
      if (req.method === "POST" && req.url === "/v1/chat/completions") {
        const body = await readJson(req);
        const tools = Array.isArray(body.tools) ? body.tools : [];
        const hasToolResult = (body.messages || []).some((m) => m.role === "tool");
        const message = !hasToolResult && tools.length
          ? {
              role: "assistant",
              content: null,
              tool_calls: [
                {
                  id: "call_demo_1",
                  type: "function",
                  function: { name: tools[0].function.name, arguments: "{}" },
                },
              ],
            }
          : {
              role: "assistant",
              content: "订单共 2 笔：A001（¥100）、A002（¥200）。",
            };
        json(res, 200, {
          id: "chatcmpl-demo",
          object: "chat.completion",
          created: Math.floor(Date.now() / 1000),
          model: body.model || "demo",
          choices: [
            {
              index: 0,
              message,
              finish_reason: message.tool_calls ? "tool_calls" : "stop",
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        });
        return;
      }
      json(res, 404, { error: "not found" });
    } catch (error) {
      json(res, 500, { error: String(error) });
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { server, port: server.address().port };
}

class SidecarClient {
  constructor(child) {
    this.child = child;
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
    this.waiters = [];
    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      if (!line.trim()) return;
      this._onMessage(JSON.parse(line));
    });
  }

  send(message) {
    this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  request(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.send({ jsonrpc: "2.0", id, method, params });
    });
  }

  waitFor(predicate, timeoutMs = 20_000) {
    const existing = this.events.find(predicate);
    if (existing) return Promise.resolve(existing);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("等待 sidecar 事件超时")), timeoutMs);
      this.waiters.push({ predicate, resolve: (msg) => {
        clearTimeout(timer);
        resolve(msg);
      }});
    });
  }

  _onMessage(message) {
    if (message.id !== undefined && message.method === undefined) {
      const pending = this.pending.get(message.id);
      if (pending) {
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result);
      }
      return;
    }

    if (message.method === "approval/request") {
      console.log(`[host] 审批：${message.params.tool} -> 自动放行`);
      this.send({
        jsonrpc: "2.0",
        id: message.id,
        result: { approved: true },
      });
      return;
    }
    if (message.method === "clarify/request") {
      this.send({ jsonrpc: "2.0", id: message.id, result: { answer: "" } });
      return;
    }

    this.events.push(message);
    if (message.method === "agent/event") {
      const { type, data } = message.params;
      console.log(`[sidecar] ${type}${type === "done" ? `: ${data.final_text}` : ""}`);
    } else {
      console.log(`[sidecar] ${message.method}: ${JSON.stringify(message.params)}`);
    }
    for (const waiter of [...this.waiters]) {
      if (waiter.predicate(message)) {
        this.waiters.splice(this.waiters.indexOf(waiter), 1);
        waiter.resolve(message);
      }
    }
  }
}

async function main() {
  const { server, port } = await startLocalServices();
  const python = pythonExecutable();
  console.log(`[host] 本地服务 127.0.0.1:${port}；Python：${python}`);

  const child = spawn(python, ["-m", "yai_core.sidecar"], {
    cwd: ROOT,
    env: {
      ...process.env,
      PYTHONPATH: [path.join(ROOT, "src"), process.env.PYTHONPATH]
        .filter(Boolean)
        .join(path.delimiter),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  child.stderr.on("data", (chunk) => process.stderr.write(`[sidecar:err] ${chunk}`));

  const client = new SidecarClient(child);
  try {
    const init = await client.request("initialize", {
      model: {
        provider: "openai-compatible",
        base_url: `http://127.0.0.1:${port}/v1`,
        api_key: "demo-not-a-real-key",
        model: "demo",
      },
      llm_router: false,
      openapi: [
        {
          alias: "orders",
          spec: {
            openapi: "3.0.0",
            info: { title: "Orders", version: "1.0" },
            servers: [{ url: `http://127.0.0.1:${port}` }],
            paths: {
              "/orders": {
                get: {
                  operationId: "list_orders",
                  responses: {
                    "200": {
                      description: "ok",
                      content: {
                        "application/json": {
                          schema: { type: "array", items: { type: "object" } },
                        },
                      },
                    },
                  },
                },
              },
            },
          },
        },
      ],
    });
    console.log(`[host] 已初始化，工具：${init.tools.map((t) => t.name).join(", ")}`);

    const run = await client.request("agent/run", { task: "查一下订单列表" });
    console.log(`[host] run_id=${run.run_id}`);
    const done = await client.waitFor(
      (m) => m.method === "agent/event" && m.params.type === "done",
    );
    console.log(`[host] 完成：${done.params.data.final_text}`);
  } finally {
    await client.request("shutdown").catch(() => {});
    child.kill();
    server.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
