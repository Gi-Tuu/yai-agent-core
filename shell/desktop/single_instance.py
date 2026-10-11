"""桌面端单实例守护。

灵动岛是常驻托盘 + 全局热键的浮窗，不能同时跑两个实例：否则会出现两个悬浮
球、全局热键只有一个能注册成功、``QSettings`` 位置记忆与学习 / 工具状态文件被
两个进程互相覆盖。

用 Qt 自带的 :class:`QLocalServer` / :class:`QLocalSocket`（随 PySide6 的
QtNetwork，不引入第三方依赖，跨平台）实现：

- 第一个实例在本地命名端点（Windows 是 named pipe，类 Unix 是本地 socket）上
  监听；
- 之后再启动的进程能连上该端点，就发一条 ``raise`` 指令并自行退出；
- 第一个实例收到指令后把灵动岛拉到前台并展开——于是“再点一次图标 / 再启动”
  就等于“把已有的岛叫出来”，而不是开出第二个。

上次崩溃残留的端点，在监听前由 :meth:`QLocalServer.removeServer` 清理。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket

#: 单实例端点名；Windows 映射到 ``\\.\pipe\<name>``，类 Unix 映射到本地 socket。
DEFAULT_SERVER_NAME = "yai-agent-core-desktop"
#: 后续实例发给首个实例的唤起指令。
_RAISE_PAYLOAD = b"raise"
_TIMEOUT_MS = 300


class SingleInstance:
    """单实例协调器：首个实例监听，后续实例请求唤起后退出。"""

    def __init__(self, name: str = DEFAULT_SERVER_NAME) -> None:
        self._name = name
        self._server: QLocalServer | None = None
        self._activator: Callable[[], None] | None = None

    def acquire(self, activator: Callable[[], None] | None = None) -> bool:
        """抢占单实例席位。

        返回 ``True`` 表示当前进程是首个实例（已开始监听，应继续正常启动）；
        返回 ``False`` 表示已有实例在运行，本进程已请求唤起它，调用方应直接退出、
        不要再创建 Core / 托盘 / 热键。

        ``activator`` 是首个实例收到唤起指令时执行的回调；根窗口尚未创建时可先
        不传，之后用 :meth:`set_activator` 补绑。
        """
        self._activator = activator

        probe = QLocalSocket()
        probe.connectToServer(self._name)
        if probe.waitForConnected(_TIMEOUT_MS):
            # 已有实例：发唤起指令后让本进程退出。
            probe.write(_RAISE_PAYLOAD)
            probe.flush()
            probe.waitForBytesWritten(_TIMEOUT_MS)
            probe.disconnectFromServer()
            probe.deleteLater()
            return False
        probe.deleteLater()

        # 没有实例在跑：清掉可能的崩溃残留端点，再监听。
        QLocalServer.removeServer(self._name)
        server = QLocalServer()
        if not server.listen(self._name):
            # 监听失败不阻断启动，降级为允许运行（宁可多开，也不能让岛起不来）。
            server.deleteLater()
            return True
        self._server = server
        server.newConnection.connect(self._handle_connection)
        return True

    def set_activator(self, activator: Callable[[], None]) -> None:
        """补绑“唤起已有窗口”的回调（根窗口创建后调用）。"""
        self._activator = activator

    def _handle_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            client = self._server.nextPendingConnection()
            if client is None:
                continue
            client.waitForReadyRead(_TIMEOUT_MS)
            payload = bytes(client.readAll())
            client.disconnectFromServer()
            client.deleteLater()
            if _RAISE_PAYLOAD in payload and self._activator is not None:
                self._activator()

    def close(self) -> None:
        """停止监听（进程退出时调用）。"""
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
