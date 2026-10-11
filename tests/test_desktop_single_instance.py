"""桌面端单实例守护的离线测试（QLocalServer/QLocalSocket，不依赖 GUI 平台）。"""

from __future__ import annotations

import os
import sys
import time
import uuid

import pytest

pytest.importorskip("PySide6.QtCore", reason="需要 PySide6（uv sync --extra desktop）")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtNetwork import QLocalServer  # noqa: E402
from shell.desktop.single_instance import SingleInstance  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv)
    yield app


def _unique_name() -> str:
    return f"yai-test-single-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _pump(qapp, predicate, timeout: float = 2.0) -> bool:
    """驱动事件循环直到 predicate 为真或超时。"""
    end = time.time() + timeout
    while not predicate() and time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    return bool(predicate())


def test_first_acquires_second_is_redirected_and_activates(qapp):
    name = _unique_name()
    first = SingleInstance(name)
    raised = {"count": 0}
    assert first.acquire(
        activator=lambda: raised.__setitem__("count", raised["count"] + 1)
    )

    second = SingleInstance(name)
    try:
        # 已有实例在跑：第二个实例不应抢占席位。
        assert second.acquire() is False
    finally:
        second.close()

    # 首个实例应异步收到第二个实例的唤起指令。
    assert _pump(qapp, lambda: raised["count"] >= 1)
    first.close()


def test_can_acquire_again_after_close(qapp):
    name = _unique_name()
    first = SingleInstance(name)
    assert first.acquire()
    first.close()

    # 正常关闭（或崩溃残留被 removeServer 清掉）后，新进程能重新监听，不被锁死。
    restarted = SingleInstance(name)
    try:
        assert restarted.acquire()
    finally:
        restarted.close()


def test_listen_failure_degrades_to_allow_running(qapp, monkeypatch):
    """监听端点失败时降级为允许运行，绝不能让灵动岛因此启动不了。"""
    name = _unique_name()

    def _fail_listen(self, *args, **kwargs):
        return False

    monkeypatch.setattr(QLocalServer, "listen", _fail_listen)
    si = SingleInstance(name)
    assert si.acquire()
