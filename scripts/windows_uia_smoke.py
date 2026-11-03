"""Windows UIA 真机冒烟：用 pywinauto 后端完成一组可复现的观察 + 语义操作。

为什么自带窗口：不操作任何用户程序，避免误触真实数据。脚本用 Windows Forms
起一个**纯测试窗口**（含一个原生 Button），再用 ``PywinautoDesktopBackend`` +
``DesktopProvider`` 通过 UIA 找到窗口与按钮、语义 Invoke 按钮，最后验证按钮
回调写出的标记文件存在——这是真实 UIA 路径，不是模拟。

用法::

    python scripts/windows_uia_smoke.py            # 需要 Windows 交互桌面 + pywinauto
    python scripts/windows_uia_smoke.py --json     # 机器可读结果

退出码：0 = 真机验证通过；2 = 环境不具备（非 Windows / 无 pywinauto / 无交互桌面）；
1 = 具备环境但验证失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_FIXTURE_PS1 = r"""
param([string]$Marker)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = 'YAI UIA Smoke'
$form.Width = 420
$form.Height = 200
$button = New-Object System.Windows.Forms.Button
$button.Text = 'YAI_SMOKE_BUTTON'
$button.Name = 'YAI_SMOKE_BUTTON'
$button.Location = New-Object System.Drawing.Point(40, 60)
$button.Width = 200
$button.Add_Click({ Set-Content -LiteralPath $Marker -Value 'clicked' })
$form.Controls.Add($button)
[void][System.Windows.Forms.Application]::Run($form)
"""


async def _run_smoke(marker: Path) -> tuple[bool, str]:
    from yai_core.integrations.desktop import DesktopPolicy, DesktopProvider
    from yai_core.integrations.desktop.pywinauto_backend import PywinautoDesktopBackend

    backend = PywinautoDesktopBackend()
    provider = DesktopProvider(backend, policy=DesktopPolicy(require_approval_for=frozenset()))

    windows = await provider.list_windows()
    target = next(
        (item for item in windows.get("windows", []) if item["title"] == "YAI UIA Smoke"),
        None,
    )
    if target is None:
        return False, "UIA 没找到测试窗口（可能没有交互桌面 / UIA 不可用）"
    window_id = target["window_id"]

    observed = await provider.observe(window_id)
    if not observed.get("ok"):
        return False, f"观察窗口失败：{observed}"
    refs = [
        item["ref"]
        for item in observed.get("controls", [])
        if "SMOKE" in str(item.get("name", "")).upper()
    ]
    if not refs:
        return False, f"UIA 控件树里没有找到测试按钮：{observed.get('controls')}"

    invoked = await provider.invoke(refs[0])
    if not invoked.get("ok") or invoked.get("status") != "executed":
        return False, f"UIA invoke 未成功：{invoked}"

    for _ in range(60):
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == "clicked":
            return True, f"UIA invoke 触发按钮回调（window={window_id}，ref={refs[0]}）"
        await asyncio.sleep(0.05)
    return False, "UIA invoke 返回成功但没有观察到按钮回调痕迹"


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows UIA 真机冒烟")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    def _report(ok: bool, detail: str, *, code: int) -> int:
        if args.json:
            print(json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False))
        else:
            print(f"[uia-smoke] {'通过' if ok else '跳过/失败'}：{detail}")
        return code

    if sys.platform != "win32":
        return _report(False, "非 Windows", code=2)
    try:
        import pywinauto  # noqa: F401
    except ImportError:
        return _report(
            False,
            "未安装 pywinauto（pip install 'yai-agent-core[windows]'）",
            code=2,
        )

    tmpdir = Path(tempfile.mkdtemp(prefix="yai-uia-smoke-"))
    marker = tmpdir / "clicked.txt"
    script = tmpdir / "fixture.ps1"
    script.write_text(_FIXTURE_PS1, encoding="utf-8")
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            str(marker),
        ]
    )
    try:
        time.sleep(1.5)
        ok, detail = asyncio.run(_run_smoke(marker))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    return _report(ok, detail, code=0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
