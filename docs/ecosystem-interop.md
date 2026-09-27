# 生态互操作验证：随机开源项目实测

> 目的：用**随机抽取的中立第三方开源项目**（非任何赛事的参赛作品、非为 YAI 量身定制）验证 YAI Agent Core 的普适嵌入能力。本文所有数字均来自真实运行，可按第 7 节复现。
>
> 实测时间：2026-09-27　运行环境：CPython 3.13.15（独立临时虚拟环境）　YAI：main 分支

## 1. 抽样方法

- 从 GitHub、Gitee 两类托管平台，按「工具类 / 桌面应用 / 管理系统」**跨类型、跨平台、跨规模**随机抽样；
- 刻意包含一个仅 24 stars 的小型项目，避免只挑选头部明星项目造成的"幸存者偏差"；
- 抽样前不阅读源码、不预设其是否"好接入"，结果如实记录（包括无法直接接入的情况）。

## 2. 抽样项目

| 项目 | 类型 | 平台 | 规模 | 技术栈 | 许可证 |
|---|---|---|---|---|---|
| **cookiecutter** 2.7.1 | CLI 工具（项目模板脚手架） | GitHub | ~25k stars | Python，模块级函数 + jinja2/click | 见仓库 LICENSE |
| **inventarium**（SQLite Edition I） | 桌面库存管理系统 | GitHub | ~24 stars | Tkinter + SQLite，类/mixin 架构 | GPL-3.0 |
| **FastapiAdmin** | 中后台管理系统 | Gitee | — | FastAPI + SQLAlchemy + Pydantic | 见仓库 LICENSE |

## 3. 实测结果总览

| 项目 | 代码形态 | 宿主前置动作 | YAI 自动得到 | 结论 |
|---|---|---|---|---|
| cookiecutter | 模块级函数 | 安装项目依赖（使用任何库的前提） | **8 个工具** | 模块函数即插即用 |
| inventarium | 类/mixin 实例方法 | 实例化对象（一行，传入 db 路径） | **5 个方法** | 实例方法可内省，需区分领域方法/通用方法 |
| FastapiAdmin | 框架 + ORM + 认证耦合 | 需在 API 边界暴露薄业务函数 | 0（未直接内省） | 内部 CRUD 不适合直接当工具 |

## 4. 逐项目证据

### 4.1 cookiecutter —— 模块级函数

安装依赖后对 `cookiecutter.utils` 模块做内省，**8 个公开函数全部自动成工具**，未手写任何 schema：

```
CC_UTILS_TOOLS 8
 - create_env_with_context   - create_tmp_repo_dir   - force_delete
 - make_executable           - make_sure_path_exists - rmtree
 - simple_filter             - work_in
```

JSON Schema 完全由 type hints + docstring 自动生成。以 `make_sure_path_exists(path: Path | str) -> None` 为例：

```json
{"type": "function", "function": {"name": "make_sure_path_exists",
  "description": "Ensure that a directory exists.",
  "parameters": {"type": "object",
    "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
```

> 说明：8 个函数中，`rmtree / make_sure_path_exists / make_executable / create_tmp_repo_dir / force_delete` 是可直接调用的业务工具；`work_in`（上下文管理器）、`simple_filter`、`create_env_with_context`（返回内部 jinja 对象）属于框架内部设施，宿主可按需筛选是否暴露给 Agent。

### 4.2 inventarium —— 类实例方法

该项目采用 `Tools → DBMS → Controller → Engine` 的 mixin 架构，业务能力以**类的方法**承载。以正常构造参数实例化数据层（不启动 GUI）：

```python
db = DBMS("/path/to/inventarium.db")   # 一行，仅连接 SQLite
discover(db)
```

YAI 自动收录 5 个公开绑定方法：

```
INVENTARIUM_DBMS_METHODS 5
 - build_sql   - close   - on_log   - read   - write
```

两个如实记录的边界：

1. 这 5 个方法多为**通用底层数据库操作**（`read/write` 接收裸 SQL、`build_sql`、`close`、`on_log`），并非"添加试剂 / 查询临期批次"等领域动作；领域方法定义在 `controller.py`。**最佳实践是让宿主暴露领域方法，而非把裸 SQL 通道交给 Agent**（裸 SQL 既不直观也有风险）。
2. 静态方法 `test_connection` 因在实例上不构成绑定方法（`inspect.ismethod` 不匹配）本次未自动收录；宿主可显式注册，或由内核后续增强静态/类方法的内省。

### 4.3 FastapiAdmin —— 框架耦合边界

其通用 CRUD 基类构造签名为：

```python
class CRUDBase(ModelType, CreateSchemaType, UpdateSchemaType):
    def __init__(self, model, auth: AuthSchema, db: AsyncSession) -> None: ...
```

所有方法均为 `async`，且强依赖 SQLAlchemy `AsyncSession`、认证上下文 `auth` 与 Pydantic schema，在 HTTP 请求生命周期内才有意义。因此**直接把内部 CRUD/service 方法当作进程内工具不现实**（需要伪造会话、连接数据库、绕过权限层）。

正确接入点是 **controller/API 边界**：宿主把"内部已具备会话与权限、对外只需业务参数"的动作封装成普通函数（例如 `create_ai_chat(content: str) -> str`、`get_health() -> dict`），YAI 内省这些函数即可。该项目 `modules/ai/chat`、`modules/monitor/health` 等模块天然适合作为这类薄封装的来源。

## 5. 集成成熟度谱系

由随机抽样归纳出 YAI 的三种**进程内嵌入**形态，外加一种**跨语言连接**形态：

| 形态 | 宿主代码特征 | 宿主前置动作 | 实证 |
|---|---|---|---|
| A. 模块级函数 | 普通函数 + type hints + docstring | 安装项目依赖 | cookiecutter（8 工具） |
| B. 类实例方法 | 能力承载于类的公开方法 | 以正常参数实例化对象 | inventarium（5 方法） |
| C. 框架耦合系统 | 方法绑定会话/认证/请求上下文 | 在 API 边界暴露薄业务函数 | FastapiAdmin（推荐路径） |
| D. 跨语言连接 | 非 Python、或独立进程 | 经标准 MCP 暴露能力，YAI 作 MCP Client | YAI 内置 MCP Client（`[mcp]` extra） |

形态 A、B 中 YAI 自动完成"内省 → JSON Schema → 注册"；形态 C 需要宿主在边界处加一层薄函数；形态 D 走标准协议，与语言无关。

## 6. 边界与安全

- YAI **不声称"任何软件都能零改造接入"**：无任何对外接口、且非 Python 的封闭应用，需要对方先开放能力（这是系统集成本身的前提，而非 YAI 的缺陷）。
- 通用裸 SQL、shell、内部设施方法不默认作为面向最终用户的工具；宿主应暴露语义明确的领域动作。
- 所有工具（含自动内省所得）仍受内核**权限三档策略**（全部审批 / 部分审批 / 无需审批）与白名单约束，自动接入不绕过授权。

## 7. 复现步骤

```powershell
# 独立临时环境，不污染 YAI 自身虚拟环境
uv venv "$env:TEMP\yai-recon\.venv-recon"
uv pip install --python "$env:TEMP\yai-recon\.venv-recon\Scripts\python.exe" cookiecutter

git clone --depth 1 https://github.com/cookiecutter/cookiecutter "$env:TEMP\yai-recon\cookiecutter"
git clone --depth 1 https://github.com/1966bc/inventarium "$env:TEMP\yai-recon\inventarium"
git clone --depth 1 https://gitee.com/fastapiadmin/FastapiAdmin "$env:TEMP\yai-recon\FastapiAdmin"
```

内省调用统一为：`from yai_core.discovery.introspect import discover`；模块传 `discover(module)`，对象传 `discover(instance)`。上述克隆与临时环境位于系统临时目录，**不纳入 YAI 仓库**，验证结束即可删除。

## 8. 出处

- cookiecutter：https://github.com/cookiecutter/cookiecutter
- inventarium：https://github.com/1966bc/inventarium
- FastapiAdmin：https://gitee.com/fastapiadmin/FastapiAdmin
