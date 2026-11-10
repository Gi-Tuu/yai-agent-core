/**
 * Typed client for the YAI Agent Core **sidecar session protocol**.
 *
 * Transport: stdio NDJSON + JSON-RPC 2.0. This client only drives the session plane
 * (`initialize` / `agent/run` / `agent/cancel` / approval / clarify / events).
 * There is deliberately no `tool/call`: host tools are exposed through MCP or OpenAPI
 * and the Python sidecar acts as their client (see docs/guides/multilang-interop.md).
 *
 * ```ts
 * import { SidecarClient } from "yai-agent-core-sidecar-client";
 *
 * const client = SidecarClient.spawn({ onApproval: () => true });
 * try {
 *   const init = await client.initialize({ model: { model: "deepseek-chat" } });
 *   await client.run("查一下订单");
 *   const done = await client.waitForEvent((event) => event.type === "done");
 *   console.log(done.data.final_text);
 * } finally {
 *   await client.shutdown().catch(() => {});
 *   client.close();
 * }
 * ```
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import * as readline from "node:readline";

export const PROTOCOL_VERSION = 1;

export interface ModelConfig {
  provider?: string;
  base_url?: string;
  api_key?: string;
  model?: string;
  strong_model?: string;
  [key: string]: unknown;
}

export interface ToolInfo {
  name: string;
  description?: string;
  source?: string;
  [key: string]: unknown;
}

export interface InitializeParams {
  model?: ModelConfig;
  policy?: Record<string, unknown>;
  llm_router?: boolean | "auto";
  mcp?: Array<Record<string, unknown>>;
  openapi?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface InitializeResult {
  protocol?: number;
  kernel_version?: string;
  tools: ToolInfo[];
  [key: string]: unknown;
}

/** One `agent/event` notification payload. */
export interface AgentEventFrame {
  run_id: string;
  /** Monotonic per run; lets hosts detect dropped frames. */
  seq: number;
  /** Kernel event type: strategy_selected / tool_call / tool_result / done / ... */
  type: string;
  data: Record<string, unknown>;
}

export type EventPredicate = (event: AgentEventFrame) => boolean;

export interface ApprovalRequest {
  tool: string;
  arguments: Record<string, unknown>;
  context?: Record<string, unknown>;
}

export interface ClarifyRequest {
  question: string;
  context?: Record<string, unknown>;
}

export interface RunResult {
  run_id: string;
}

export interface SidecarOptions {
  /** Full command override (e.g. a test fixture); defaults to `python -m yai_core.sidecar`. */
  command?: string[];
  /** Python executable; defaults to `YAI_PYTHON`, then `python` (win32) / `python3`. */
  python?: string;
  cwd?: string;
  env?: Record<string, string>;
  /** Approval handler; **defaults to deny** (fail closed). */
  onApproval?: (request: ApprovalRequest) => boolean | Promise<boolean>;
  /** Clarification handler; defaults to an empty answer. */
  onClarify?: (request: ClarifyRequest) => string | Promise<string>;
  onEvent?: (event: AgentEventFrame) => void;
  onNotification?: (method: string, params: Record<string, unknown>) => void;
  onStderr?: (line: string) => void;
  /** Per-request timeout; defaults to 60s (initialize can be slow). */
  requestTimeoutMs?: number;
  /** In-memory event buffer used by `waitForEvent`; defaults to 256. */
  eventBufferSize?: number;
}

/** JSON-RPC error surfaced as an exception (code + message from the sidecar). */
export class SidecarError extends Error {
  readonly code: number;
  readonly data: unknown;

  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.name = "SidecarError";
    this.code = code;
    this.data = data;
  }
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout | undefined;
}

interface EventWaiter {
  predicate: EventPredicate;
  resolve: (event: AgentEventFrame) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

interface JsonRpcMessage {
  jsonrpc?: string;
  id?: number | string;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: { code?: number; message?: string; data?: unknown };
}

const REQUEST_TIMEOUT_CODE = -32000;
const CLOSED_CODE = -32003;

export class SidecarClient {
  private readonly child: ChildProcessWithoutNullStreams;
  private readonly options: SidecarOptions;
  private readonly lines: readline.Interface;
  private readonly pending = new Map<number, PendingRequest>();
  private readonly waiters: EventWaiter[] = [];
  private readonly listeners = new Set<(event: AgentEventFrame) => void>();
  private readonly events: AgentEventFrame[] = [];
  private readonly defaultTimeout: number;
  private readonly eventBufferSize: number;
  private nextId = 1;
  private closed = false;
  private exitError: Error | undefined;

  private constructor(child: ChildProcessWithoutNullStreams, options: SidecarOptions) {
    this.child = child;
    this.options = options;
    this.defaultTimeout = options.requestTimeoutMs ?? 60_000;
    this.eventBufferSize = options.eventBufferSize ?? 256;
    this.lines = readline.createInterface({ input: child.stdout });
    this.lines.on("line", (line: string) => this.onLine(line));
    child.stderr.on("data", (chunk: Buffer) => {
      for (const line of chunk.toString("utf8").split(/\r?\n/)) {
        if (line.trim()) this.options.onStderr?.(line);
      }
    });
    child.on("error", (error: Error) => this.onExit(error));
    child.on("exit", (code: number | null, signal: NodeJS.Signals | null) => {
      this.onExit(
        new SidecarError(
          CLOSED_CODE,
          `sidecar 进程已退出（code=${code ?? "null"}, signal=${signal ?? "null"}）`,
        ),
      );
    });
  }

  /** Spawn a sidecar process and return a connected client. */
  static spawn(options: SidecarOptions = {}): SidecarClient {
    const python =
      options.python ??
      process.env.YAI_PYTHON ??
      (process.platform === "win32" ? "python" : "python3");
    const command = options.command ?? [python, "-m", "yai_core.sidecar"];
    if (command.length === 0) throw new Error("command 不能为空");
    const child = spawn(command[0]!, command.slice(1), {
      cwd: options.cwd ?? process.cwd(),
      env: { ...process.env, ...options.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    return new SidecarClient(child, options);
  }

  get pid(): number | undefined {
    return this.child.pid ?? undefined;
  }

  get alive(): boolean {
    return !this.closed && !this.exitError;
  }

  // ---------- session methods ----------

  initialize(params: InitializeParams = {}, timeoutMs?: number): Promise<InitializeResult> {
    return this.request<InitializeResult>("initialize", params, timeoutMs);
  }

  run(task: string, options: { timeoutMs?: number } = {}): Promise<RunResult> {
    return this.request<RunResult>("agent/run", { task }, options.timeoutMs);
  }

  cancel(): Promise<unknown> {
    return this.request("agent/cancel", {});
  }

  ping(): Promise<unknown> {
    return this.request("ping", {});
  }

  shutdown(timeoutMs = 5_000): Promise<unknown> {
    return this.request("shutdown", {}, timeoutMs);
  }

  /** Escape hatch for extensions to the session protocol. */
  request<T = unknown>(
    method: string,
    params: Record<string, unknown> = {},
    timeoutMs?: number,
  ): Promise<T> {
    if (this.closed || this.exitError) {
      return Promise.reject(this.exitError ?? new SidecarError(CLOSED_CODE, "sidecar 已关闭"));
    }
    const id = this.nextId++;
    const timeout = timeoutMs ?? this.defaultTimeout;
    return new Promise<T>((resolve, reject) => {
      const timer =
        timeout > 0
          ? setTimeout(() => {
              this.pending.delete(id);
              reject(new SidecarError(REQUEST_TIMEOUT_CODE, `请求超时：${method}`));
            }, timeout)
          : undefined;
      this.pending.set(id, {
        resolve: resolve as (value: unknown) => void,
        reject,
        timer,
      });
      this.sendMessage({ jsonrpc: "2.0", id, method, params });
    });
  }

  // ---------- events ----------

  /** Subscribe to `agent/event` frames; returns an unsubscribe function. */
  onEvent(listener: (event: AgentEventFrame) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Resolve on the first matching frame (checks the buffer first). */
  waitForEvent(
    predicate: EventPredicate,
    timeoutMs = 20_000,
  ): Promise<AgentEventFrame> {
    const existing = this.events.find(predicate);
    if (existing) return Promise.resolve(existing);
    if (this.closed || this.exitError) {
      return Promise.reject(this.exitError ?? new SidecarError(CLOSED_CODE, "sidecar 已关闭"));
    }
    return new Promise<AgentEventFrame>((resolve, reject) => {
      const timer = setTimeout(() => {
        const index = this.waiters.findIndex((waiter) => waiter.timer === timer);
        if (index >= 0) this.waiters.splice(index, 1);
        reject(new SidecarError(REQUEST_TIMEOUT_CODE, "等待 sidecar 事件超时"));
      }, timeoutMs);
      this.waiters.push({ predicate, resolve, reject, timer });
    });
  }

  /** Close the client and terminate the sidecar process (idempotent). */
  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.lines.close();
    for (const pending of this.pending.values()) {
      if (pending.timer) clearTimeout(pending.timer);
      pending.reject(new SidecarError(CLOSED_CODE, "sidecar 已关闭"));
    }
    this.pending.clear();
    for (const waiter of this.waiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(new SidecarError(CLOSED_CODE, "sidecar 已关闭"));
    }
    if (!this.child.killed) this.child.kill();
  }

  // ---------- internals ----------

  private sendMessage(message: JsonRpcMessage): void {
    if (this.closed) return;
    try {
      this.child.stdin.write(`${JSON.stringify(message)}\n`);
    } catch (error) {
      this.onExit(error instanceof Error ? error : new Error(String(error)));
    }
  }

  private onLine(line: string): void {
    const trimmed = line.trim();
    if (!trimmed) return;
    let message: JsonRpcMessage;
    try {
      message = JSON.parse(trimmed) as JsonRpcMessage;
    } catch {
      return; // 非 JSON 行不进协议流（sidecar 日志应走 stderr）
    }
    if (typeof message !== "object" || message === null) return;

    if (message.method === undefined) {
      if (message.id === undefined) return;
      const pending = this.pending.get(Number(message.id));
      if (!pending) return;
      this.pending.delete(Number(message.id));
      if (pending.timer) clearTimeout(pending.timer);
      if (message.error) {
        pending.reject(
          new SidecarError(
            Number(message.error.code ?? -32603),
            String(message.error.message ?? "sidecar error"),
            message.error.data,
          ),
        );
      } else {
        pending.resolve(message.result);
      }
      return;
    }

    if (message.id !== undefined) {
      // 反向请求：审批 / 澄清。宿主失联或处理器抛错都按"失败关闭"回答。
      void this.handleReverseRequest(message.id, message.method, message.params ?? {});
      return;
    }
    this.handleNotification(message.method, message.params ?? {});
  }

  private async handleReverseRequest(
    id: number | string,
    method: string,
    params: Record<string, unknown>,
  ): Promise<void> {
    try {
      if (method === "approval/request") {
        let approved = false;
        if (this.options.onApproval) {
          try {
            approved = Boolean(await this.options.onApproval(params as unknown as ApprovalRequest));
          } catch {
            approved = false;
          }
        }
        this.sendMessage({ jsonrpc: "2.0", id, result: { approved } });
        return;
      }
      if (method === "clarify/request") {
        let answer = "";
        if (this.options.onClarify) {
          try {
            answer = String(
              await this.options.onClarify(params as unknown as ClarifyRequest),
            );
          } catch {
            answer = "";
          }
        }
        this.sendMessage({ jsonrpc: "2.0", id, result: { answer } });
        return;
      }
      this.sendMessage({
        jsonrpc: "2.0",
        id,
        error: { code: -32601, message: `未支持的反向请求：${method}` },
      });
    } catch (error) {
      this.sendMessage({
        jsonrpc: "2.0",
        id,
        error: { code: -32603, message: String(error) },
      });
    }
  }

  private handleNotification(method: string, params: Record<string, unknown>): void {
    this.options.onNotification?.(method, params);
    if (method !== "agent/event") return;
    const frame: AgentEventFrame = {
      run_id: String(params.run_id ?? ""),
      seq: Number(params.seq ?? 0),
      type: String(params.type ?? ""),
      data: (params.data ?? {}) as Record<string, unknown>,
    };
    this.events.push(frame);
    if (this.events.length > this.eventBufferSize) this.events.shift();
    try {
      this.options.onEvent?.(frame);
    } catch {
      // 订阅者异常不能打断协议读出
    }
    for (const listener of [...this.listeners]) {
      try {
        listener(frame);
      } catch {
        // 订阅者异常不能打断协议读出
      }
    }
    for (const waiter of [...this.waiters]) {
      if (!waiter.predicate(frame)) continue;
      const index = this.waiters.indexOf(waiter);
      if (index >= 0) this.waiters.splice(index, 1);
      clearTimeout(waiter.timer);
      waiter.resolve(frame);
    }
  }

  private onExit(error: Error): void {
    if (this.exitError) return;
    this.exitError = error;
    this.closed = true;
    for (const pending of this.pending.values()) {
      if (pending.timer) clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
    for (const waiter of this.waiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
  }
}
