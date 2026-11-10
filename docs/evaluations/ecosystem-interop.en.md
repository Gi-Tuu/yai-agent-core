> English translation of [中文](ecosystem-interop.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Ecosystem Interoperability Verification: Live Tests on Randomly Sampled Open-Source Projects

**English** · [简体中文](ecosystem-interop.md)

> Purpose: to verify YAI Agent Core's general-purpose embedding capability using **randomly sampled, neutral third-party open-source projects** (not projects owned by the YAI repository, and not tailored for YAI). Every number in this article comes from a real run and can be reproduced per Section 7.
>
> Test date: 2026-09-27　Runtime: CPython 3.13.15 (separate throwaway virtual environment)　YAI: main branch

## 1. Sampling Method

- Random sampling from two hosting platforms, GitHub and Gitee, **across categories, platforms and sizes** of "tooling / desktop application / management system";
- A small project with only 24 stars was deliberately included, to avoid the "survivorship bias" caused by picking only top-tier star projects;
- Source code was not read and no assumption about whether a project was "easy to integrate" was made before sampling; results were recorded faithfully (including cases that could not be integrated directly).

## 2. Sampled Projects

| Project | Type | Platform | Size | Stack | License |
|---|---|---|---|---|---|
| **cookiecutter** 2.7.1 | CLI tool (project template scaffolding) | GitHub | ~25k stars | Python, module-level functions + jinja2/click | See repository LICENSE |
| **inventarium** (SQLite Edition I) | Desktop inventory management system | GitHub | ~24 stars | Tkinter + SQLite, class/mixin architecture | GPL-3.0 |
| **FastapiAdmin** | Admin / back-office management system | Gitee | — | FastAPI + SQLAlchemy + Pydantic | See repository LICENSE |

## 3. Overview of Live-Test Results

| Project | Code shape | Host prerequisite action | What YAI gets automatically | Conclusion |
|---|---|---|---|---|
| cookiecutter | module-level functions | install project dependencies (a precondition for using any library) | **8 tools** | module functions are plug-and-play |
| inventarium | class/mixin instance methods | instantiate the object (one line, passing the db path) | **5 methods** | instance methods are introspectable; domain methods must be distinguished from generic methods |
| FastapiAdmin | framework + ORM + auth coupling | must expose thin business functions at the API boundary | 0 (no direct introspection) | internal CRUD is not suitable as a tool directly |

## 4. Per-Project Evidence

### 4.1 cookiecutter — module-level functions

After installing dependencies, the `cookiecutter.utils` module was introspected and **all 8 public functions automatically became tools**, with no hand-written schema:

```
CC_UTILS_TOOLS 8
 - create_env_with_context   - create_tmp_repo_dir   - force_delete
 - make_executable           - make_sure_path_exists - rmtree
 - simple_filter             - work_in
```

The JSON Schema is generated entirely automatically from type hints + docstrings. Take `make_sure_path_exists(path: Path | str) -> None` as an example:

```json
{"type": "function", "function": {"name": "make_sure_path_exists",
  "description": "Ensure that a directory exists.",
  "parameters": {"type": "object",
    "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
```

> Note: of the 8 functions, `rmtree / make_sure_path_exists / make_executable / create_tmp_repo_dir / force_delete` are business tools that can be called directly; `work_in` (a context manager), `simple_filter`, and `create_env_with_context` (which returns an internal jinja object) are framework-internal facilities, and the host may filter whether to expose them to the Agent as needed.

### 4.2 inventarium — class instance methods

The project uses a `Tools → DBMS → Controller → Engine` mixin architecture, and its business capabilities are carried as **class methods**. The data layer was instantiated with normal constructor arguments (without starting the GUI):

```python
db = DBMS("/path/to/inventarium.db")   # one line, connects SQLite only
discover(db)
```

YAI automatically collected 5 public bound methods:

```
INVENTARIUM_DBMS_METHODS 5
 - build_sql   - close   - on_log   - read   - write
```

Two boundaries recorded faithfully:

1. These 5 methods are mostly **generic low-level database operations** (`read/write` take raw SQL, `build_sql`, `close`, `on_log`), not domain actions such as "add a reagent / query expiring batches"; the domain methods are defined in `controller.py`. **The best practice is for the host to expose domain methods rather than hand the Agent a raw SQL channel** (raw SQL is neither intuitive nor safe).
2. The static method `test_connection` was not collected automatically this time because it does not constitute a bound method on the instance (`inspect.ismethod` does not match); the host can register it explicitly, or the kernel can later enhance introspection of static/class methods.

### 4.3 FastapiAdmin — the framework coupling boundary

Its generic CRUD base class has the constructor signature:

```python
class CRUDBase(ModelType, CreateSchemaType, UpdateSchemaType):
    def __init__(self, model, auth: AuthSchema, db: AsyncSession) -> None: ...
```

All methods are `async` and depend hard on the SQLAlchemy `AsyncSession`, the auth context `auth` and Pydantic schemas, and are only meaningful within an HTTP request lifecycle. Therefore **treating internal CRUD/service methods directly as in-process tools is unrealistic** (it would require forging a session, connecting to the database, and bypassing the permission layer).

The correct integration point is the **controller/API boundary**: the host wraps the actions that "already have a session and permissions internally and only need business parameters externally" into ordinary functions (for example `create_ai_chat(content: str) -> str`, `get_health() -> dict`), and YAI introspects those functions. Modules such as `modules/ai/chat` and `modules/monitor/health` in that project are naturally suited as sources for such thin wrappers.

## 5. Integration Maturity Spectrum

From the random sampling, three forms of **in-process embedding** for YAI were induced, plus one **cross-language connection** form:

| Form | Host code characteristics | Host prerequisite action | Evidence |
|---|---|---|---|
| A. module-level functions | ordinary functions + type hints + docstring | install project dependencies | cookiecutter (8 tools) |
| B. class instance methods | capabilities carried on a class's public methods | instantiate the object with normal arguments | inventarium (5 methods) |
| C. framework-coupled system | methods bound to session/auth/request context | expose thin business functions at the API boundary | FastapiAdmin (recommended path) |
| D. cross-language connection | non-Python, or a separate process | expose capabilities via standard MCP, YAI acts as MCP Client | YAI built-in MCP Client (`[mcp]` extra) |

In forms A and B, YAI completes "introspect → JSON Schema → register" automatically; form C requires the host to add a thin function layer at the boundary; form D goes through a standard protocol and is language-agnostic.

## 6. Boundaries and Security

- YAI **does not claim that "any software can be integrated without modification"**: a closed application with no external interface and not written in Python requires the other side to expose capabilities first (this is a precondition of system integration itself, not a defect of YAI).
- Generic raw SQL, shell and internal-facility methods are not used as end-user-facing tools by default; the host should expose domain actions with clear semantics.
- All tools (including those obtained by automatic introspection) remain constrained by the kernel's **three-tier permission policy** (approve all / partially approve / no approval required) and the allowlist; automatic integration does not bypass authorization.

## 7. Reproduction Steps

```powershell
# Separate throwaway environment, does not pollute YAI's own virtual environment
uv venv "$env:TEMP\yai-recon\.venv-recon"
uv pip install --python "$env:TEMP\yai-recon\.venv-recon\Scripts\python.exe" cookiecutter

git clone --depth 1 https://github.com/cookiecutter/cookiecutter "$env:TEMP\yai-recon\cookiecutter"
git clone --depth 1 https://github.com/1966bc/inventarium "$env:TEMP\yai-recon\inventarium"
git clone --depth 1 https://gitee.com/fastapiadmin/FastapiAdmin "$env:TEMP\yai-recon\FastapiAdmin"
```

The introspection call is uniformly: `from yai_core.discovery.introspect import discover`; for a module pass `discover(module)`, for an object pass `discover(instance)`. The clones and the throwaway environment above live in the system temp directory, are **not checked into the YAI repository**, and can be deleted once verification is done.

## 8. Sources

- cookiecutter: https://github.com/cookiecutter/cookiecutter
- inventarium: https://github.com/1966bc/inventarium
- FastapiAdmin: https://gitee.com/fastapiadmin/FastapiAdmin
