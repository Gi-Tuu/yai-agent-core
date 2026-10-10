# Security Policy

**English** · [简体中文](SECURITY.zh-CN.md)

## Supported versions

| Version | Security updates |
| --- | --- |
| 0.8.x | ✅ |
| 0.7.x | ✅ |
| < 0.7 | ❌ |

## Secure deployment defaults (read before exposing to the internet)

1. **You must configure API-key authentication.** When `YAI_API_KEY` (or `YAI_API_KEYS`) is not set,
   the FastAPI service ships with authentication **disabled** — this is the zero-config experience for
   local/review environments. On a public deployment without it, anyone can call your endpoint and spend
   your model quota. Set `YAI_API_KEY=<a long random string>` through a secure channel and send
   `Authorization: Bearer <key>` on requests.
2. **Code-tool sandbox boundaries**:
   - `SubprocessSandbox` is demo-grade isolation (restricted builtins, CPU/memory and timeout limits).
     It does **not** isolate the network and does **not** claim to stop a determined escape; it is only
     suitable for pure-computation code.
   - When exposing to the internet or handling untrusted input, use `DockerSandbox` (no network,
     read-only root filesystem, drops ALL capabilities, memory/PID limits), or implement the same
     `ToolSandbox` contract with gVisor / Firecracker.
3. **Discovery is not authorization.** Tools registered via dynamic discovery (MCP / OpenAPI) still
   execute through the unified `PermissionPolicy`; risky operations (writes / network / shell) require
   explicit host approval by default.
4. **Secrets and data.** `.env` is in `.gitignore` — never commit real API keys. All SQLite access uses
   parameterized queries; SQL is never string-concatenated.

## Reporting a vulnerability

If you discover a security issue, please **do not** open a public issue. Report it privately via:

- GitHub: repository → Security → Report a vulnerability;
- Email: zixuan19252270581@gmail.com (please include reproduction steps, impact, and a suggested fix).

After confirmation we will reply as soon as possible and keep details private until a fix is released.
