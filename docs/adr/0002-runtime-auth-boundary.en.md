> English translation of [中文](0002-runtime-auth-boundary.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# ADR 0002 · Runtime's authentication and authorization boundary (sidecar + FastAPI Battery)

**English** · [简体中文](0002-runtime-auth-boundary.md)

- Status: Accepted (2026-10-31, B3 batch)
- Related: v0.25 audit C-1 (sidecar zero authentication) / D-1 (default permissions) / pending decision "online `/v1/agent/run` has no authentication"

## Context

YAI's two external runtime surfaces:

1. **sidecar** (stdio NDJSON + JSON-RPC): lets hosts in any language embed it as a subprocess.
   The two ends of the stdio pipe are **trusted parent processes on the same machine** — the protocol layer has no network exposure.
2. **FastAPI Battery** (`/v1/agent/run` and other HTTP endpoints): can be deployed as a public service
   (it was once used for a Render demo instance, which was deleted on 2026-11-02). It still has no authentication when no token is set —
   that is a **pending product decision**, not an oversight.

## Decision

### D1 · Three-layer boundary ownership (written into the protocol docs)

| Layer | Owner | Notes |
|---|---|---|
| Network ingress authentication | **Host** (or an edge proxy) | sidecar is stdio, with no network surface; for HTTP deployment the host decides whether to enable D3 below |
| User identity authentication | **Host** | YAI has no built-in account system; identity is injected as `Subject` |
| Tool execution authorization | **YAI** (governance gate) | Based on the Subject injected by the host + the governance data declared by the host |

### D2 · Subject can only come from a trusted host, never from request parameters (sidecar)

- `initialize` may carry `subject` (a session-level trusted identity, mapped from the host's authentication system);
- once bound, a `subject` in the `agent/run` request parameters is **rejected outright**
  (`INVALID_PARAMS`, to prevent identity forgery in multi-tenant deployments);
- when unbound, the existing semantics are kept: the stdio parent process is the trusted host, and it is responsible for
  mapping the authenticated user to a Subject; in HTTP scenarios it is **forbidden** to bridge the sidecar's stdio to untrusted input.

### D3 · FastAPI Battery: opt-in token authentication (implemented; whether to enable it is a deployment decision)

- The capability has existed since v0.7 (`batteries/fastapi_server/auth.py`): after the deployer sets
  `YAI_API_KEY` / `YAI_API_KEYS`, `/v1/*` requires a Bearer Key
  (`hmac.compare_digest` constant-time comparison); failure returns 401 with zero business side effects;
- `/health` and `/.well-known/yai-verification.json` are on the public allowlist
  (the review hard gate is never disabled);
- **Conclusion of this round**: the mechanism has no gap; whether to set that env is a deployment decision — enabling it means
  adding one env line to render.yaml + Redeploy. The demo instance has been deleted, so the original pending item "no authentication online"
  converges to a deployment decision and is no longer a code gap.

## Consequences

- sidecar protocol is backward compatible: hosts that do not pass subject see zero change; hosts that bind it gain an anti-forgery guarantee;
- FastAPI deployments gain one more switch, closed by default; the docs explain how to enable it;
- no account system is introduced, and no session management UI is built (explicitly a red line).

## References

- host_e's token precedent (`YAI_TOOL_TOKEN`; non-loopback bindings must carry a token);
- task book P0-3; v0.25 audit report C-1.
