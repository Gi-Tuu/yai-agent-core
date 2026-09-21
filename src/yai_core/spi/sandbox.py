"""代码工具沙箱契约（第六个 SPI，本版本只定义、不实现）。

工具能力分两级，风险模型不同：

- **组合工具（composite，已实现）**：只编排宿主已注册的工具，不执行任何
  模型生成的代码，能力上限 = 被组合工具的并集，物理上不越界，零沙箱风险。
- **代码生成工具（code tool，本契约）**：需要执行模型当场生成的代码，
  可能产生宿主原本没有的行为，风险高得多。

内核坚持零硬依赖、自身不内置任何代码执行器，而是把"在隔离环境里安全执行
一段代码"定义为**由宿主实现的 SPI**：

- 嵌入式 / 桌面宿主：子进程 + RestrictedPython 之类的受限运行时；
- 服务端宿主：Docker / gVisor / 一次性容器；
- 移动端或高安全宿主：可以直接不实现，该能力优雅降级为 capability_missing。

实现方必须遵守的安全约定：
1. 代码在隔离环境执行，默认无网络、无敏感文件读写；
2. 必须有超时与资源（CPU / 内存）上限；
3. 内核在调用沙箱前，除"无需审批"权限档外，都必须先走 PermissionPolicy
   取得用户授权——沙箱不负责绕过授权；
4. 沙箱只返回结构化结果，不返回可继续操作宿主环境的句柄。

代码工具的生命周期管理（默认 48h TTL、被调用刷新 TTL、后台可置为永久
保留、过期回收）由 ``yai_core.tools.code_tools.CodeToolManager`` 负责；
本契约只约定"如何在隔离环境里安全执行一段代码"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class SandboxResult:
    """沙箱执行结果：成功取 output，失败取 error。"""

    ok: bool
    output: Any = None
    error: str | None = None
    # 执行元信息：耗时、资源用量等，键由实现方自定义。
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ToolSandbox(Protocol):
    """宿主提供的代码执行隔离环境。内核不提供默认实现。"""

    async def execute(
        self,
        code: str,
        inputs: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> SandboxResult:
        """在隔离沙箱中执行模型生成的代码。

        Args:
            code: 由模型生成、已经过用户授权（无需审批档除外）的代码字符串。
            inputs: 代码可读取的入参（由内核从工具调用参数构造）。
            timeout: 可选的最长执行秒数；实现方应在超时后终止并返回 ok=False。

        Returns:
            SandboxResult；任何越权、超时、资源超限都应表现为 ok=False +
            可读 error，而不是抛异常穿透到内核主循环。
        """
        ...
