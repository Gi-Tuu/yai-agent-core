"""浏览器扩展真机验收（Chrome / Edge）：打包校验 + 真实加载检查。

检查项：
1. 用系统浏览器自身的 ``--pack-extension`` 校验 manifest 与扩展目录能被接受；
2. 真实启动浏览器（有头模式：Chrome 的 --load-extension 在新版 headless 下不加载扩展），
   加载 ``extension/``，再打开 ``chrome://extensions`` 读取 shadow DOM，确认扩展出现在
   扩展列表里并拿到扩展 ID；
3. 记录浏览器版本作为证据。

**限制（如实说明）**：脚本不点击工具栏图标/侧边栏（无法在自动化里模拟该用户手势），
Native Messaging 还需要在注册表登记 native host 后由真实用户会话触发；这两项仍属
人工验收清单（docs/browser-extension-bridge.md）。扩展加载失败会明确报失败，不假通过。

用法::

    python scripts/extension_smoke.py                 # 系统 Chrome，有头
    python scripts/extension_smoke.py --browser msedge
    python scripts/extension_smoke.py --json

退出码：0 通过；2 环境不具备（无可用浏览器 / 无 playwright / 仅要求 headless）；1 失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXT_SRC = ROOT / "extension"

_BROWSERS = {
    "chrome": [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
    ],
    "msedge": [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ],
}

_EXTENSIONS_JS = """() => {
  const manager = document.querySelector('extensions-manager');
  if (!manager) return {error: 'no-manager'};
  const list = manager.shadowRoot.querySelector('extensions-item-list');
  if (!list) return {error: 'no-list'};
  const items = Array.from(list.shadowRoot.querySelectorAll('extensions-item'));
  return {
    count: items.length,
    items: items.map((item) => ({
      id: item.id,
      name: item.shadowRoot?.querySelector('#name')?.textContent?.trim() || '',
    })),
  };
}"""


def _find_browser(name: str) -> Path | None:
    for candidate in _BROWSERS[name]:
        if candidate.is_file():
            return candidate
    return None


def _version(executable: Path) -> str:
    try:
        info = executable.parent.parent.parent / "Application" / executable.name
        return str(subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"(Get-Item '{info}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=30,
        ).stdout).strip()
    except Exception:  # noqa: BLE001
        return ""


def _pack(executable: Path, extension_dir: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(executable),
            "--headless=new",
            "--disable-gpu",
            "--no-message-box",
            "--no-first-run",
            f"--pack-extension={extension_dir}",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    crx = extension_dir.with_suffix(".crx")
    pem = extension_dir.with_suffix(".pem")
    return {
        "exit_code": completed.returncode,
        "crx_created": crx.is_file(),
        "pem_created": pem.is_file(),
        "stderr_tail": (completed.stderr or "")[-400:],
    }


async def _load_and_probe(
    executable: Path, extension_dir: Path, *, channel: str | None
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    profile = extension_dir.parent / "profile"
    args = [
        f"--disable-extensions-except={extension_dir}",
        f"--load-extension={extension_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # 新版 Chrome 默认忽略 --load-extension；这两个开关用于允许自动化加载。
        "--enable-unsafe-extension-debugging",
        "--disable-features=DisableLoadExtensionCommandLineSwitch",
    ]
    evidence: dict[str, Any] = {}
    scheme = "edge" if "msedge" in executable.name.lower() else "chrome"
    async with async_playwright() as playwright:
        kwargs: dict[str, Any] = {
            "user_data_dir": str(profile),
            "headless": False,  # --load-extension 在 headless 下不生效，必须真实窗口
            "ignore_default_args": ["--disable-extensions"],
            "args": args,
        }
        if channel is not None:
            kwargs["channel"] = channel
        context = await playwright.chromium.launch_persistent_context(**kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"{scheme}://version")
            await asyncio.sleep(0.5)
            evidence["browser_version"] = (await page.inner_text("body"))[:80].splitlines()[0]

            # 首选证据：CDP 目标里出现扩展的 MV3 service worker（Chrome / Edge 通用）。
            cdp = await context.new_cdp_session(page)
            deadline = __import__("time").monotonic() + 20
            extension_targets: list[dict[str, Any]] = []
            while __import__("time").monotonic() < deadline:
                targets = (await cdp.send("Target.getTargets")).get("targetInfos", [])
                extension_targets = [
                    {"type": info.get("type"), "url": info.get("url")}
                    for info in targets
                    if str(info.get("url", "")).startswith("chrome-extension://")
                ]
                if extension_targets:
                    break
                await asyncio.sleep(0.3)
            evidence["extension_targets"] = extension_targets
            workers = [
                item for item in extension_targets if item["type"] == "service_worker"
            ]
            if workers:
                evidence["extension_id"] = workers[0]["url"].split("/")[2]
                evidence["service_worker_loaded"] = True
            else:
                evidence["service_worker_loaded"] = False

            # 次选证据：扩展管理页（Chrome 是 extensions-manager；Edge 是 root-app，可能读不到）。
            try:
                await page.goto(f"{scheme}://extensions")
                await asyncio.sleep(1.2)
                found = await page.evaluate(_EXTENSIONS_JS)
                evidence["extensions_page"] = found
                items = found.get("items", []) if isinstance(found, dict) else []
                if items and not evidence.get("extension_id"):
                    evidence["extension_id"] = items[0]["id"]
                if items:
                    evidence["extension_name"] = items[0]["name"]
            except Exception as exc:  # noqa: BLE001 - 页面结构差异不掩盖 CDP 证据
                evidence["extensions_page"] = {"error": f"{type(exc).__name__}: {exc}"}
            evidence["extension_loaded"] = bool(evidence.get("extension_id"))
        finally:
            await context.close()
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="浏览器扩展真机验收")
    parser.add_argument("--browser", choices=sorted(_BROWSERS), default="chrome")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    executable = _find_browser(args.browser)
    if executable is None:
        payload = {"ok": False, "skipped": f"{args.browser}-not-found"}
        if args.json:
            print(json.dumps(payload))
        else:
            print(f"[extension-smoke] 跳过：没找到 {args.browser}")
        return 2
    try:
        import playwright  # noqa: F401
    except ImportError:
        payload = {"ok": False, "skipped": "playwright-missing"}
        print(json.dumps(payload) if args.json else "[extension-smoke] 跳过：未安装 playwright")
        return 2

    tmpdir = Path(tempfile.mkdtemp(prefix="yai-ext-smoke-"))
    workdir = tmpdir / "extension"
    try:
        shutil.copytree(EXT_SRC, workdir)
        evidence: dict[str, Any] = {
            "browser": args.browser,
            "executable": str(executable),
            "executable_version": _version(executable),
        }
        evidence["pack"] = _pack(executable, workdir)
        try:
            evidence.update(
                asyncio.run(
                    _load_and_probe(
                        executable,
                        workdir,
                        channel=None if args.browser == "chrome" else "msedge",
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 - 环境/版本差异要如实记录
            evidence["load_error"] = f"{type(exc).__name__}: {exc}"

        ok = bool(
            evidence["pack"]["exit_code"] == 0
            and evidence["pack"]["crx_created"]
            and evidence.get("extension_loaded")
            and evidence.get("extension_id")
        )
        detail = (
            "扩展被浏览器接受、真实加载并出现在扩展列表（ID 已记录）"
            if ok
            else "扩展未在真实浏览器加载成功（见 evidence）"
        )
        payload = {"ok": ok, "detail": detail, **evidence}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[extension-smoke] {'通过' if payload['ok'] else '失败'}：{payload['detail']}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
