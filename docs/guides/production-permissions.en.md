> English translation of [中文](production-permissions.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Production Permission Configuration Guide

**English** · [简体中文](production-permissions.md)

> Corresponds to v0.25 audit item D-1: the `auto_approve_tools=True` default of `AgentCore` is a **development shape**
> (equivalent to `AllowlistPolicy(mode="allow_all")`, where every tool runs automatically without approval).
> Production / multi-user environments must configure a permission policy explicitly. All the code on this page can be
> run directly from `examples/production_policy.py` (it is referenced by tests, so the docs will not drift).

## 1. Minimal safe configuration (copy and use)

```python
from yai_core import AgentCore
from yai_core.policy import AllowlistPolicy
from yai_core.types import ToolGovernance

# 1) Allowlist: let through only read operations known to have no side effects; every other tool goes to approval or denial
policy = AllowlistPolicy(
    ("search_notes", "list_notes", "get_weather"),  # explicit allowlist
    mode="auto",  # auto-allow inside the allowlist; anything outside enters the approval channel
)

core = AgentCore(
    model,                       # your model provider
    policy=policy,               # explicit policy (passing it stops triggering the production safety warning)
    auto_approve_tools=False,    # semantic declaration: this is not the allow_all development shape
)
```

## 2. Write operations and egress: declare governance item by item

```python
spec = build_spec(send_direct_message)
spec.governance = ToolGovernance(
    # Parameter-level approval: calls with amount > 1000 must be confirmed by a human (ApprovalRule is data)
    approval=ApprovalPolicy(when=(ApprovalRule("amount", ">", 1000),)),
    # Egress text parameters: the URL-source gate trusts only the task's original text
    egress_text=("body",),
    # Declare resource scope / timeout / rate limit as needed
)
core.register_tools([spec])
```

Key points (see `docs/walkthrough/43-governance-triad.md` for details):

- Governance is **data**: it travels across processes with the capability manifest, and the importing side restores
  enforcement without any external protocol;
- An external declaration can only **raise** the gate (ALLOW → human confirmation); it can never raise a DENY;
- Redaction takes effect per the `governance.sensitive` declaration; fields that are not declared are never guessed at and redacted;
- A synchronous tool timeout is "a stop-loss for the model", not immediate process-level escape (see walkthrough 43).

## 3. Subject: trusted identity comes only from the host

```python
def subject_provider() -> Subject:
    # Supplied by the host's authentication system (e.g. AMBRACE's user system); the kernel has no built-in account system
    return Subject(id=current_user.id, ...)
```

- Model parameters **cannot** forge a Subject (the kernel does not read identity from tool parameters);
- Multi-user isolation: different users use different AgentCore instances, or the host injects a Subject per session;
- Tasks are serialized per instance (tasks in the same Core do not interleave); for concurrency, use multiple instances.

## 4. Browser / desktop adapters: use the secure preset in production

```python
from yai_core.integrations.browser import BrowserPolicy, BrowserProvider
from yai_core.integrations.desktop import DesktopPolicy, DesktopProvider

# Browser: list origins explicitly; click/type/select/open/switch_tab require approval by default;
# file:/javascript:/data:/about: are denied by default; password fields / downloads are off by default.
browser_policy = BrowserPolicy.secure(["https://app.example.com"])
browser = BrowserProvider(backend, policy=browser_policy)

# Desktop: declare at least one process identity (PID / image name / full path — any one is enough, and they can be combined);
# high-risk controls and coordinate clicks are off by default; invoke/set_value/select/click/focus require approval by default.
desktop_policy = DesktopPolicy.secure(
    allowed_process_names=["notepad.exe"],                    # case-insensitive
    allowed_process_paths=[r"C:\Windows\System32\notepad.exe"],  # full path; / and \ are equivalent
    allowed_window_titles=["记事本"],                          # an additional filter, not a security boundary
)
desktop = DesktopProvider(backend, policy=desktop_policy)
```

- Without a `policy`, both still work with permissive development/test defaults, but they emit
  `BrowserSecurityWarning` / `DesktopSecurityWarning`; passing one explicitly **without declaring the boundary** also warns:
  for the browser it looks at "origin allowlist empty or containing `*`" (`BrowserPolicy.origins_unrestricted`), for the desktop at
  "no process identity at all" (`DesktopPolicy.is_permissive`). A title allowlist and control-name keywords do not constitute
  a security boundary. Note also that `BrowserPolicy.is_permissive` requires "origins unrestricted **and** no approval configured", so
  a policy with approval configured but no origins returns False for it, yet origins are still unrestricted — the warning only follows the origin criterion.
- The process-identity allowlist is an "or" relation, and **PID matching is exact** (`123` does not allow `1234`, avoiding mistakes after PID reuse);
  image names and paths are case-insensitive, and path separators `/` and `\` are equivalent; an empty value is not treated as a wildcard.
  A PID is only valid for the lifetime of the current process; for long-running deployments, declare an image name or a full path.
- Control-name keywords (delete/uninstall/format, etc.) and window titles **are neither of them a security boundary on their own**: localization and
  unnamed icon buttons can both bypass them; you must use process identity (optionally combined with a title) to narrow the operable range.
- Browser navigation applies an origin guard to `document/subdocument` at the Playwright routing layer, so
  unauthorized redirects are aborted **before loading** rather than loading first and then inspecting the final URL.

## 5. Pre-launch self-check list

- [ ] `auto_approve_tools` is set explicitly (the default value triggers ProductionSafetyWarning)
- [ ] Write / egress / resource tools all have a `ToolGovernance` declaration
- [ ] Someone is on the receiving end of the approval channel for tools outside the allowlist (the host implementation of Channel.confirm)
- [ ] Browser/desktop adapters use `BrowserPolicy.secure(...)` / `DesktopPolicy.secure(...)`;
      the desktop declares at least one process identity (PID / image name / path); a title allowlist alone is not a security boundary
- [ ] Public exposure surfaces such as `YAI_API_KEY` have been assessed per the deployment doc (see `docs/guides/deployment-scaling.en.md`)
- [ ] The three budget caps (model / tools / token) are configured as needed (`Budget`)
