> English translation of [中文](openapi-31-compat.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# OpenAPI 3.1 / JSON Schema 2020-12 Compatibility Matrix

**English** · [简体中文](openapi-31-compat.md)

> Status: **supported since v0.32** (evidence: `tests/test_openapi_31_compat.py`, 10 cases).
> This page only covers "the translation boundary of tool input schemas": of the full OpenAPI JSON Schema 2020-12 vocabulary, only
> the subset that tool calls need is translated; **untranslated constructs are only recorded in notes and never cause the whole description to be rejected**.

## 1. Support matrix

| OpenAPI / 2020-12 construct | Handling | Result seen by the model |
|---|---|---|
| `openapi: 3.1.x` | Accepted (3.0.x / 3.1.x take the same path) | — |
| `type: ["string", "null"]` | Kept as-is | Standard JSON Schema union type; null is not wrongly rejected by input validation |
| `const: value` | Normalized to `enum: [value]` (semantically equivalent) | Single-value enum (some backends do not recognize `const`) |
| `nullable: true` (3.0) | Converted to `type: [..., "null"]` | Standard union type |
| `$ref` + sibling keys (allowed in 3.1) | Inline the target first, then override same-named fields with the sibling keys | The complete schema after the override |
| `exclusiveMinimum/Maximum` (numeric) | Kept as-is | Global input validation **does not enforce** this layer (see §3) |
| `anyOf` / `oneOf` / `allOf` / `not` | Kept | When global validation hits a loosening combination, it skips that layer to avoid false rejection |
| `prefixItems` | **Not translated** (arrays keep only `items`) | Array type only; a note flags it |
| `if` / `then` / `else` | **Not translated** | None; a note flags it |
| `patternProperties` / `dependentSchemas` / `contains` / `unevaluatedProperties` | **Not translated** | None; a note flags it |
| `webhooks` (3.1) | **Not registered as tools** (only `paths` is handled) | None; a note flags it |
| path-level `servers` (3.1) | Takes no effect (the global `servers` is still used) | None; a note flags it |
| external `$ref` (cross-file/URL) | Not resolved; kept as-is and a note is recorded | That parameter's schema may be incomplete |

## 2. Why "not translated" is not the same as "an error"

OpenAPI 3.1 allows the full JSON Schema 2020-12, while tool input parameters need only a narrow subset of it.
Rejecting an entire description over a handful of unused keywords (for example `patternProperties`, which only appears in responses)
would block operations that are otherwise usable. Hence the policy: **translate whatever can be translated safely; record notes for
what cannot; the validation layer would rather be lenient than wrongly reject** (consistent with `tests/test_schema_compatibility.py`).

The `notes` returned by `plan_operations()` carry this information, so the host can surface it in capability health checks/logs.

## 3. Known boundaries

- **Global input validation is a subset**: `exclusiveMinimum/Maximum`, `const` (validated as enum after normalization),
  `patternProperties` and the like are not enforced in the validator; the schema is handed to the model as-is, but the final line of defense remains the host/server.
- **Full 2020-12 coverage is not a goal**: `$dynamicRef`, `unevaluatedItems`, `contentEncoding` and others are unsupported,
  and they will not appear in the model schema (they are cleaned out by the whitelist).
- **`$ref` is not resolved across documents**: external references stay as-is, and the parameter schema may be incomplete.
- **webhooks are never tools**: this integration only translates `paths` into tools.

## 4. Reproduction

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_openapi_31_compat.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_schema_compatibility.py tests\test_schema_sanitize.py -q
```
