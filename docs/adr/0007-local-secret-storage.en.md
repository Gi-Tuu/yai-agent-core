> English translation of [中文](0007-local-secret-storage.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# ADR 0007: Storage boundary for local model keys and the SecretStore plan

**English** · [简体中文](0007-local-secret-storage.md)

- Status: Proposed (2026-11-03, v0.27 Batch 4)
- Related: `src/yai_core/sidecar/native_bridge.py`, `scripts/register_native_host.py`,
  `scripts/configure_bridge_llm.py`, `docs/guides/browser-extension-bridge.md`
- Related task book: v0.27 §8 (secure storage of local API keys)

## Context

Model keys currently live on the local machine in `%LOCALAPPDATA%\YAI\bridge.json` under
`initialize.model.api_key`:

- they do not enter browser storage; the panel always reads them back masked (`***last4`);
- configuration writes have a cross-process lock + atomic replacement + `.bak` backup;
- **but they are written to disk in plaintext**: masked display is not disk encryption. Processes of the same user, backup files, a mistakenly uploaded
  `bridge.json.bak`, and any program with read permission on that user directory can all read the key.

So we must not advertise "the key is stored encrypted". This ADR records the cross-platform plan and the current boundary.

## Decision

1. **In the short term (v0.27) do not introduce OS-specific dependencies into Core**, but state the risk clearly:
   - the docs make clear that `bridge.json` / `.bak` are plaintext sensitive files, along with their location and ACL responsibility;
   - logs/exceptions/script output continue to guarantee that the key is never printed (already covered by tests);
   - uninstall/rotation is documented: delete the configuration and `.bak`, or overwrite with a new key.
2. **In the medium term add an optional `SecretStore` SPI** (not a hard kernel dependency), Candidates:
   - Windows: DPAPI `CryptProtectData` (current-user bound) or Credential Manager
     (`wincred`); both provided as an optional module/extra, with Core depending only on the Protocol.
   - Linux: Secret Service (libsecret) / file permissions + an explicit warning.
   - macOS: Keychain.
   - Unified interface (draft):
     ```python
     class SecretStore(Protocol):
         async def get(self, key: str) -> str | None: ...
         async def set(self, key: str, value: str) -> None: ...
         async def delete(self, key: str) -> None: ...
     ```
3. **Bridge configuration migration**: in the future `bridge.json` will store only non-sensitive fields such as provider/base_url/model;
   `api_key` becomes `secret_ref` (e.g. `dpapi:model_api_key`), resolved by the Bridge through
   `SecretStore` at startup. Old configurations migrate automatically (write the SecretStore once, remove the key from the file, keep
   a new `.bak` backup that does not contain the key); if migration fails, keep the old format with an explicit warning, and never lose the key.
4. **Compatibility**: the existing API by which `configure_llm`/the CLI takes a key is unchanged; the internal write path can be switched to
   SecretStore; read-back remains masked.

## Current risks (must be honestly communicated to users)

| Risk | Status |
|---|---|
| Plaintext key on disk (bridge.json) | Present; file permissions depend on the user directory ACL |
| `.bak` backup contains the old key | Present; after rotation the old key may still be in `.bak` |
| Key leakage through logs/exceptions | Held off by masking and tests; this ADR does not claim coverage of all future logs |
| Multiple users / shared machines | No secure isolation; each OS user installs/configures independently |

## Acceptance (when SecretStore is implemented in the future)

- Windows DPAPI round trip, decryption failure with a wrong key / across users, delete/rotate;
- bridge.json no longer contains a plaintext key (including after `.bak` migration);
- when no SecretStore is available, fall back to the old format and emit an explicit warning, without silently degrading;
- all tests are offline and do not depend on real cloud keys.

## Rejected/deferred

- **Putting DPAPI directly into Core**: breaks the zero third-party hard dependency and cross-platform design; deferred as an optional module.
- **Rolling our own encryption and putting the key in the same directory**: equivalent to plaintext, not adopted.
