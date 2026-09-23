"""host_h sherlock 嵌入演示：离线测试，不联网。

用 unittest.mock 替换 sherlock 的站点枚举与查询函数，验证：
1. ``lookup_username`` 正确过滤 "Claimed" 结果；
2. 脚本化模型能驱动 Core 自动调用 lookup_username 并拿到汇总结果。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from host_h_sherlock import capabilities as cap  # noqa: E402
from host_h_sherlock.demo_agent import build_core  # noqa: E402


class _FakeSite:
    def __init__(self, name: str) -> None:
        self.name = name
        self.information = {"url": f"https://{name.lower()}.example.com/{{}}"}


def _fake_sites(*names: str):
    """返回可迭代的假站点列表。"""
    return [_FakeSite(n) for n in names]


def _make_result(site: str, verbose: str, url: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        site_name=site,
        url_user=url,
        status=SimpleNamespace(verbose_name=verbose),
    )


def _run_sherlock(*, claimed: list[str]):
    """构造一个假的 sherlock()：调用时往 notify 里塞结果。"""

    def _fake_sherlock(username, site_data, query_notify, **_):
        for site in site_data:
            if site in claimed:
                query_notify.update(
                    _make_result(site, "Claimed", f"https://{site.lower()}.example.com/{username}")
                )
            else:
                query_notify.update(_make_result(site, "Available"))

    return _fake_sherlock


def test_lookup_filters_claimed_only() -> None:
    """只保留 Claimed 站点，Available/WAF 丢弃。"""
    fake_sites = _fake_sites("GitHub", "9GAG", "Reddit")
    with (
        patch("sherlock_project.sites.SitesInformation", return_value=fake_sites),
        patch(
            "sherlock_project.sherlock.sherlock",
            side_effect=_run_sherlock(claimed=["GitHub", "9GAG"]),
        ),
    ):
        result = cap.lookup_username("torvalds", limit=5)

    assert result["username"] == "torvalds"
    assert result["checked"] == 3
    assert {r["site"] for r in result["found"]} == {"GitHub", "9GAG"}
    assert "github" in result["found"][0]["url"] or "9gag" in result["found"][0]["url"]


def test_lookup_respects_limit() -> None:
    """limit 生效：只取前 N 个站点。"""
    fake_sites = _fake_sites("A", "B", "C", "D")
    with (
        patch("sherlock_project.sites.SitesInformation", return_value=fake_sites),
        patch("sherlock_project.sherlock.sherlock", side_effect=_run_sherlock(claimed=[])),
    ):
        result = cap.lookup_username("u", limit=2)

    assert result["checked"] == 2


def test_lookup_accepts_string_limit() -> None:
    """LLM 常把数字传成字符串，wrapper 应归一化。"""
    fake_sites = _fake_sites("A", "B", "C")
    with (
        patch("sherlock_project.sites.SitesInformation", return_value=fake_sites),
        patch("sherlock_project.sherlock.sherlock", side_effect=_run_sherlock(claimed=[])),
    ):
        result = cap.lookup_username("u", limit="2", timeout="5")  # type: ignore[arg-type]

    assert result["checked"] == 2


def test_core_drives_lookup_tool() -> None:
    """脚本化模型 → Core 自动调 lookup_username → 最终结果。"""
    fake_sites = _fake_sites("GitHub", "9GAG")

    async def _run() -> str:
        with (
            patch("sherlock_project.sites.SitesInformation", return_value=fake_sites),
            patch(
                "sherlock_project.sherlock.sherlock",
                side_effect=_run_sherlock(claimed=["GitHub"]),
            ),
        ):
            core = build_core(username="torvalds", limit=2)
            result = await core.run("查 torvalds 的社交账号")
        return getattr(result, "content", str(result))

    final = asyncio.run(_run())
    assert "torvalds" in final or "sherlock" in final
