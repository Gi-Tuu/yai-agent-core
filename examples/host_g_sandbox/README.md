# 宿主 G：可现场"造工具"的代码沙箱（ToolSandbox 最小实现）

这个宿主演示 YAI Agent Core 的**代码工具（Code Tool）**闭环：当宿主没有
某个能力、组合已有工具也无法完成时，模型可以在运行中用 `create_code_tool`
**现场生成一段 Python 代码**，由**宿主提供的沙箱**隔离执行，结果再回到
Agent 主循环。内核本体**不内置任何代码执行器**——它只负责登记、授权与
TTL 生命周期，真正"跑代码"的是宿主实现的 `ToolSandbox` SPI。

## 演示了什么

宿主 `capabilities.py` 只给了一个只读工具 `list_candidates`，**刻意没有**
加权评分能力。离线确定性模型（`run.py`，无需 API Key）依次：

1. 调用 meta-tool `create_code_tool`，生成 `weighted_score`（技能×0.6 +
   经验×0.4 算综合分并降序排序）；
2. 调用原生工具 `list_candidates` 取候选人；
3. 调用刚创建的 `weighted_score` —— 它在**一次性隔离子进程**里执行；
4. 基于沙箱返回的分数给出排序：林晓 81.6 > 周岚 81.0 > 陈默 80.0。

运行：

```powershell
.\.venv\Scripts\python.exe examples\host_g_sandbox\run.py
```

## 文件

| 文件 | 职责 |
| --- | --- |
| `capabilities.py` | 宿主原生能力（只读列候选人），刻意不含加权评分 |
| `_worker.py` | 子进程受限执行器：内置白名单 + 捕获 print + JSON 协议 |
| `sandbox.py` | `SubprocessSandbox`，实现内核的 `ToolSandbox` 协议 |
| `run.py` | 离线确定性模型，演示 create → 取数 → 沙箱执行 → 收尾 |

## 隔离手段（教学级，不是容器）

`SubprocessSandbox` 每次调用都启动一个全新的子进程运行 `_worker.py`：

- 以 `python -I -S -X utf8` 启动：忽略 `PYTHONPATH`、环境变量、user site
  与 site-packages，并强制 UTF-8；
- 子进程内置函数白名单：没有 `__import__`、`open`、`eval`、`exec`、
  `compile`、`getattr`、`__build_class__` 等，因此模型代码无法 `import os`
  /`socket`、无法读写文件、无法定义类或做内省反射（实测见
  `tests/test_sandbox_example.py`）；
- 一次性独立进程 + 强制超时（默认 10s）：死循环会被杀死，崩溃不影响内核；
- 模型代码的 `print` 被捕获到结果的 `meta.stdout`，不污染协议。

> **诚实的边界**：仅靠 Python 内置白名单无法 100% 防住蓄意的沙箱逃逸
> （历史上存在通过异常链、子类化等绕过的手法）。本示例的定位是"教学级、
> 能挡住模型常规的越界尝试、零第三方依赖、跨平台"，用来讲清 SPI 契约。
> **生产环境要跑不可信代码时，请把同一 `ToolSandbox` 协议换成容器 /
> gVisor / 微 VM（如 Firecracker）实现**，内核与 Agent 主循环无需改动。

## 如何在你自己的宿主里接入

实现 `yai_core.spi.sandbox.ToolSandbox` 协议的一个方法即可：

```python
from yai_core import SandboxResult

class MySandbox:
    async def execute(self, code: str, inputs: dict | None,
                      *, timeout: float | None = None) -> SandboxResult:
        ...  # 容器 / 微 VM / 子进程，任选其一
        return SandboxResult(ok=True, output=result, meta={"elapsed_ms": 12})

core = AgentCore(model, sandbox=MySandbox(), ...)
```

传入 `sandbox=` 后，内核会自动注册 `create_code_tool`，并把代码工具的
执行分流到你的沙箱；不传则完全没有该能力（内核保持零执行器）。

## 安全设计要点

- **创建与首次执行双重授权**：除"无需审批"档外，`create_code_tool` 与
  第一次执行都要过权限闸（`policy`）。
- **48h TTL**：代码工具默认 48 小时有效，每被调用一次刷新存活期，过期回收；
  被多次调用说明有用，后台可 `core.retain_code_tool(name)` 永久保留。
- **优先组合工具**：能用 `compose_tool` 编排已有工具完成的，不生成代码，
  从源头降低风险（见 `docs/walkthrough/18-code-tools.md`）。
