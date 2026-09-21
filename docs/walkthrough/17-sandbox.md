# 逐行讲解 17 · 第六个插槽：ToolSandbox（代码工具沙箱，只定义契约）

> 对应源码：`src/yai_core/spi/sandbox.py`（新增，整文件很短）。
> 前置：第 02 篇（SPI 是什么）、第 16 篇（组合工具与两级能力模型）。

## 0. 为什么这一篇几乎"没有实现"

第 16 篇讲过，"造工具"分两级：

- **组合工具**：只编排已有工具，不执行模型写的代码，安全 → 已实现。
- **代码生成工具**：执行模型当场写出来的 Python 代码 → 可能产生宿主原本没有的行为，
  风险高，必须关进沙箱。

YAI 内核的原则是**零硬依赖、能进任意宿主**（移动、桌面、受限网络）。而"怎么安全执行
一段代码"高度依赖宿主环境：

| 宿主形态 | 可能的沙箱实现 |
|---|---|
| 桌面 / 嵌入式 | 子进程 + RestrictedPython 之类的受限运行时 |
| 服务端 | Docker / gVisor / 一次性容器 |
| 移动端 / 高安全 | **直接不实现**，该能力优雅降级为 `capability_missing` |

内核没法替所有宿主做这个决定，于是它只规定一件事：**沙箱必须长什么样**（一个
`Protocol`），具体怎么做留给宿主。这就是第六个 SPI。本版到此为止，**不写执行器**。

## 1. 结果类型：`SandboxResult`

```python
@dataclass
class SandboxResult:
    ok: bool
    output: Any = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
```

- `ok`：成功与否；
- `output`：成功时的结构化结果；
- `error`：失败时的可读原因；
- `meta`：执行元信息（耗时、资源用量等），键由实现方自定义。

这是一个"普通（非 frozen）"dataclass，因为它是一次性返回值，允许实现方填充 meta。

## 2. 契约：`ToolSandbox`

```python
@runtime_checkable
class ToolSandbox(Protocol):
    async def execute(
        self,
        code: str,
        inputs: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> SandboxResult:
        ...
```

逐点解释：

- `Protocol`：只要宿主的类有一个签名兼容的 `async execute`，不需要继承，就"算"一个
  `ToolSandbox`（结构化子类型，第 02 篇讲过）。
- `@runtime_checkable`：让 `isinstance(some_obj, ToolSandbox)` 可以用来检查宿主是否
  提供了沙箱。
- 入参：`code`（模型生成、**且已经过用户授权**的代码字符串）、`inputs`（代码可读的入参，
  由内核从工具调用参数构造）、`timeout`（可选超时）。
- 返回 `SandboxResult`，**不返回任何能继续操作宿主环境的句柄**。

## 3. 实现方必须遵守的四条安全约定

这些写在模块 docstring 里，是契约的一部分（虽然 Python 不会强制，但属于"不合规就不算
合格实现"）：

1. **隔离**：代码在隔离环境执行，默认无网络、不能读写敏感文件；
2. **资源上限**：必须有超时与 CPU / 内存上限；
3. **授权先行**：内核在调用沙箱前，除"无需审批"档外，都必须先走 `PermissionPolicy`
   取得用户授权——沙箱**不负责绕过授权**，它只是执行；
4. **只回结构化结果**：越权、超时、资源超限都应表现为 `ok=False` + 可读 `error`，
   而不是抛异常穿透到内核主循环。

第 4 条和内核一贯的"工具错误回灌给模型、而不是让进程崩溃"（见执行器的
`except Exception`）是一致的。

## 4. 代码工具的生命周期（已落地，见第 18 篇）

模块 docstring 末尾特意划清边界：这个执行契约只回答"怎么安全执行一次"，不回答
"生成的工具活多久"。后者——默认 48 小时 TTL、被多次调用刷新 TTL、后台可置为永久保留、
过期回收——由 `tools/code_tools.py` 的 `CodeToolManager` 负责，见第 18 篇逐行讲解。
本篇的 `ToolSandbox` 正是第 18 篇里代码工具真正执行时被调用的那个插槽。

## 5. 为什么这是好的工程取舍

- 内核依旧零硬依赖、不绑任何容器/沙箱技术；
- 安全敏感的执行环境由最了解部署形态的宿主决定；
- 不实现沙箱的宿主，遇到真正需要写代码的任务时，会走到第 15 篇的
  `capability_missing`：内核如实说"我缺这个能力"，而不是冒险硬执行；
- 契约先行，未来任何宿主（含 AMBRACE）都可以按这个接口接入自己的沙箱，内核主循环不用改。

## 6. 自检

1. 为什么代码工具沙箱要做成 SPI，而不是在内核里直接实现一个？
2. `ToolSandbox.execute` 为什么返回 `SandboxResult` 而不是直接返回结果或抛异常？
3. "沙箱不负责绕过授权"是什么意思？授权应该在调用沙箱之前还是之后由谁完成？
4. 如果一个宿主选择不实现 `ToolSandbox`，模型又确实需要新能力，系统应该如何优雅降级？
5. 组合工具（第 16 篇）为什么不需要沙箱，而代码工具需要？两者的能力边界差在哪？
