"""DesktopPolicy 进程身份白名单：精确 PID、映像名、完整路径，以及宽松语义。

v0.27.1 Batch 7 收紧：
- ``allowed_processes`` 由 PID **前缀**匹配改为**精确**匹配（``123`` 不再放行 ``1234``）；
- 新增 ``allowed_process_names`` / ``allowed_process_paths`` 两个进程身份白名单；
- ``is_permissive`` 只看进程身份，**仅标题白名单不再构成安全边界**。
"""

from __future__ import annotations

import asyncio

import pytest

from yai_core.integrations.desktop import (
    DesktopObservation,
    DesktopPolicy,
    DesktopProvider,
    DesktopSecurityWarning,
    WindowInfo,
)


class _Backend:
    """最小假后端：只覆盖 list_windows / observe 两条只读路径。"""

    def __init__(
        self,
        windows: list[WindowInfo],
        observations: dict[str, DesktopObservation] | None = None,
    ) -> None:
        self.windows = list(windows)
        self.observations = dict(observations or {})

    async def list_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    async def observe(self, window_id: str) -> DesktopObservation:
        return self.observations[window_id]


NOTEPAD = WindowInfo(
    window_id="1",
    title="记事本 - 未命名",
    process_id="10",
    process_name="notepad.exe",
    process_path=r"C:\Windows\System32\notepad.exe",
)


# ---------- PID：精确匹配，不再前缀放行 ----------


def test_pid_match_is_exact_not_prefix() -> None:
    policy = DesktopPolicy(allowed_processes=frozenset({"123"}))
    assert policy.allows_window("1", "记事本", "123") is True
    # 旧语义是 startswith：123 会放行 1234 / 1230。PID 复用下这等于放行别人的窗口。
    assert policy.allows_window("1", "记事本", "1234") is False
    assert policy.allows_window("1", "记事本", "1230") is False
    assert policy.allows_window("1", "记事本", "12") is False


# ---------- 映像名 / 完整路径 ----------


def test_process_name_match_is_case_insensitive() -> None:
    policy = DesktopPolicy(allowed_process_names=frozenset({"notepad.exe"}))
    assert policy.allows_window("1", "记事本", "10", "Notepad.EXE") is True
    assert policy.allows_window("1", "记事本", "10", "NOTEPAD.EXE") is True
    assert policy.allows_window("1", "记事本", "10", "calc.exe") is False
    # 空映像名不得被当成通配（后端取不到时降级为空串）。
    assert policy.allows_window("1", "记事本", "10", "") is False


def test_process_path_match_normalizes_slashes_and_case() -> None:
    policy = DesktopPolicy(allowed_process_paths=frozenset({r"c:\windows\system32\notepad.exe"}))
    assert (
        policy.allows_window("1", "记事本", "10", "", r"C:\Windows\System32\notepad.exe") is True
    )
    # 正斜杠写法等价，宿主声明路径时不必关心分隔符风格。
    assert (
        policy.allows_window("1", "记事本", "10", "", "C:/Windows/System32/notepad.exe") is True
    )
    assert (
        policy.allows_window("1", "记事本", "10", "", r"C:\Windows\System32\mspaint.exe") is False
    )
    assert policy.allows_window("1", "记事本", "10", "", "") is False


def test_identity_matches_when_any_one_of_pid_name_path_hits() -> None:
    policy = DesktopPolicy(
        allowed_processes=frozenset({"10"}),
        allowed_process_names=frozenset({"other.exe"}),
        allowed_process_paths=frozenset({r"c:\other\other.exe"}),
    )
    # 仅 PID 命中也可放行；三条身份是"或"关系。
    assert policy.allows_window("1", "记事本", "10", "", "") is True
    assert policy.allows_window("1", "记事本", "99", "other.exe", "") is True
    assert policy.allows_window("1", "记事本", "99", "", r"C:\Other\other.exe") is True
    assert policy.allows_window("1", "记事本", "99", "evil.exe", r"C:\x\evil.exe") is False


# ---------- secure() 入口：校验与归一化 ----------


def test_secure_requires_at_least_one_identity() -> None:
    with pytest.raises(ValueError):
        DesktopPolicy.secure([])
    # 仅标题白名单不是安全边界：secure() 拒绝，避免宿主误以为已经收窄。
    with pytest.raises(ValueError):
        DesktopPolicy.secure([], allowed_window_titles=["记事本"])
    # 仅给映像名或路径即可通过。
    assert DesktopPolicy.secure([], allowed_process_names=["notepad.exe"]).is_permissive is False
    assert (
        DesktopPolicy.secure([], allowed_process_paths=[r"C:\Windows\notepad.exe"]).is_permissive
        is False
    )


def test_secure_normalizes_names_and_paths() -> None:
    policy = DesktopPolicy.secure(
        [],
        allowed_process_names=["Notepad.EXE", "  CALC.exe "],
        allowed_process_paths=[r"C:/Windows/Notepad.exe"],
    )
    assert policy.allowed_process_names == frozenset({"notepad.exe", "calc.exe"})
    assert policy.allowed_process_paths == frozenset({r"c:\windows\notepad.exe"})
    assert policy.allows_window("1", "记事本", "10", "notepad.exe", "") is True


def test_secure_accepts_string_pid_and_single_values() -> None:
    policy = DesktopPolicy.secure("123", allowed_process_names="notepad.exe")
    assert policy.allowed_processes == frozenset({"123"})
    assert policy.allowed_process_names == frozenset({"notepad.exe"})


# ---------- 题目：标题仍是过滤条件，但不再是身份 ----------


def test_title_filter_still_applies_after_identity_passes() -> None:
    policy = DesktopPolicy.secure(["123"], allowed_window_titles=["记事本"])
    assert policy.allows_window("1", "记事本 - 未命名", "123") is True
    # 身份通过但标题不符 → 拒绝。
    assert policy.allows_window("1", "计算器", "123") is False
    # 标题符合但身份不符 → 拒绝。
    assert policy.allows_window("1", "记事本 - 未命名", "456") is False


def test_title_only_policy_is_permissive_and_warns() -> None:
    # 宿主直接构造数据类（绕过 secure）时，仅标题白名单不构成进程身份。
    policy = DesktopPolicy(allowed_window_titles=frozenset({"记事本"}))
    assert policy.is_permissive is True
    with pytest.warns(DesktopSecurityWarning):
        DesktopProvider(_Backend([]), policy=policy)
    # 宽松不等于完全放行：标题过滤仍然生效。
    assert policy.allows_window("1", "记事本 - 未命名", "9999") is True
    assert policy.allows_window("2", "机密窗口", "9999") is False


def test_identity_policy_does_not_warn() -> None:
    policy = DesktopPolicy.secure([], allowed_process_names=["notepad.exe"])
    provider = DesktopProvider(_Backend([]), policy=policy)
    assert provider.policy.is_permissive is False


# ---------- Provider：白名单在元数据过滤上生效（不泄漏被禁窗口信息） ----------


def test_list_windows_respects_process_name_identity() -> None:
    calc = WindowInfo(
        window_id="2",
        title="计算器",
        process_id="20",
        process_name="calc.exe",
        process_path=r"C:\Windows\System32\calc.exe",
    )
    provider = DesktopProvider(
        _Backend([NOTEPAD, calc]),
        policy=DesktopPolicy.secure([], allowed_process_names=["notepad.exe"]),
    )
    result = asyncio.run(provider.list_windows())
    assert result["ok"] is True
    assert [window["title"] for window in result["windows"]] == ["记事本 - 未命名"]
    assert result["hidden_count"] == 1
    assert "calc.exe" not in str(result)


def test_list_windows_denies_path_spoof_with_allowed_name() -> None:
    # 同名不同路径：映像名白名单必须拒绝，避免宿主按名字声明却放行了冒充进程。
    spoof = WindowInfo(
        window_id="3",
        title="记事本 - 未命名",
        process_id="30",
        process_name="notepad.exe",
        process_path=r"C:\Temp\notepad.exe",
    )
    provider = DesktopProvider(
        _Backend([spoof]),
        policy=DesktopPolicy.secure([], allowed_process_paths=[r"C:\Windows\System32\notepad.exe"]),
    )
    result = asyncio.run(provider.list_windows())
    assert result["windows"] == []
    assert result["hidden_count"] == 1


def test_observe_denies_window_outside_process_identity() -> None:
    calc_observation = DesktopObservation(
        window=WindowInfo(
            window_id="2",
            title="计算器",
            process_id="20",
            process_name="calc.exe",
            process_path=r"C:\Windows\System32\calc.exe",
        ),
        fingerprint="fp",
    )
    provider = DesktopProvider(
        _Backend([calc_observation.window], {"2": calc_observation}),
        policy=DesktopPolicy.secure([], allowed_process_names=["notepad.exe"]),
    )
    result = asyncio.run(provider.observe("2"))
    assert result["ok"] is False
    assert result["status"] == "window_not_allowed"
    assert "controls" not in result


# ---------- 公开视图 ----------


def test_to_public_exposes_all_identity_allowlists() -> None:
    policy = DesktopPolicy.secure(
        ["123"],
        allowed_process_names=["notepad.exe"],
        allowed_process_paths=[r"C:\Windows\notepad.exe"],
    )
    public = policy.to_public()
    assert public["allowed_processes"] == ["123"]
    assert public["allowed_process_names"] == ["notepad.exe"]
    assert public["allowed_process_paths"] == [r"c:\windows\notepad.exe"]


def test_window_info_public_carries_process_identity() -> None:
    public = NOTEPAD.to_public()
    assert public["process_name"] == "notepad.exe"
    assert public["process_path"] == r"C:\Windows\System32\notepad.exe"
    # 长度上限：路径 300 / 映像名 120，防止异常长度挤爆模型上下文。
    long = WindowInfo(window_id="9", process_name="a" * 500, process_path="b" * 900)
    assert len(long.to_public()["process_name"]) == 120
    assert len(long.to_public()["process_path"]) == 300
