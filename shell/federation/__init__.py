"""父子核（有向联邦）——相位 1：进程内、可独立运行的两个 core 之间的调用。

设计见 `docs/guides/federation.md` 与 ADR 0008。边界（Phase 1）：

- **只在同一进程**：没有网络 / IPC / 发现文件；跨进程见 Phase 2。
- **只在宿主层**：内核零改动，不新增 SPI 与事件类型。
- **方向性**：父核把一个或多个子核声明的工具注册成自己的工具（带前缀）；
  子核对象里**没有任何父核引用**，反向调用无路可走。
- **治理**：父核的权限 / 审批仍由父核 `ToolExecutor` 先判；子核的 `ToolExecutor` 再判一次；
  `CoreGrant` 限定"哪条边、哪些工具、代表谁、到什么时候"。
- **硬闸**：最大深度、单父扇出、环检测、调用超时；子核失败以工具错误形式回流，不拖垮父核。
"""

from shell.federation.grants import (
    ChildCallFailed,
    ChildUnavailable,
    CoreGrant,
    FederationError,
    GrantDenied,
)
from shell.federation.link import LinkChild
from shell.federation.node import ChildCore
from shell.federation.parent import Federation
from shell.federation.server import (
    PROTOCOL_VERSION,
    ChildServer,
    DiscoveryInfo,
    FederationProtocolError,
)

__all__ = [
    "ChildCallFailed",
    "ChildCore",
    "ChildServer",
    "ChildUnavailable",
    "CoreGrant",
    "DiscoveryInfo",
    "Federation",
    "FederationError",
    "FederationProtocolError",
    "GrantDenied",
    "LinkChild",
    "PROTOCOL_VERSION",
]
