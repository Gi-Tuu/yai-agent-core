"""用 LibreOffice（UNO）打开 docx，更新目录/索引后导出 PDF。

与 build_competition_docx 的普通 --pdf 不同：普通转换不会更新 TOC 域，
本脚本通过 UNO 显式 update 所有文档索引，保证 PDF 中目录带正确页码。

运行（必须用 LibreOffice 自带 python，它才有 uno）：
  "C:\\Program Files\\LibreOffice\\program\\python.exe" \\
      scripts/lo_pdf_export.py docs/exports/作品介绍.docx docs/exports/作品介绍.pdf
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import uno  # type: ignore
from com.sun.star.beans import PropertyValue  # type: ignore
from com.sun.star.connection import NoConnectException  # type: ignore

SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/opt/homebrew/bin/soffice",
)
PORT = 2010


def find_soffice() -> str:
    for p in SOFFICE_CANDIDATES:
        if Path(p).exists():
            return p
    found = subprocess.which("soffice")
    if not found:
        raise SystemExit("未找到 LibreOffice soffice")
    return found


def prop(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def file_url(path: str) -> str:
    return uno.systemPathToFileUrl(os.path.abspath(path))


def connect(resolver, tries: int = 40):
    last = None
    for _ in range(tries):
        try:
            return resolver.resolve(
                f"uno:socket,host=127.0.0.1,port={PORT};urp;StarOffice.ComponentContext"
            )
        except NoConnectException as exc:  # type: ignore
            last = exc
            time.sleep(0.5)
    raise SystemExit(f"无法连接 LibreOffice: {last}")


def main(docx: str, pdf: str) -> None:
    soffice = find_soffice()
    proc = subprocess.Popen(
        [
            soffice, "--headless", "--invisible", "--nodefault", "--norestore",
            "--nologo", "--nofirststartwizard",
            f"--accept=socket,host=127.0.0.1,port={PORT};urp;StarOffice.ComponentContext",
        ]
    )
    try:
        local_ctx = uno.getComponentContext()
        resolver = local_ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local_ctx
        )
        ctx = connect(resolver)
        smgr = ctx.ServiceManager
        desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

        doc = desktop.loadComponentFromURL(
            file_url(docx), "_blank", 0, (prop("Hidden", True),)
        )
        # 先刷新字段与整体布局（重排分页），再更新目录，保证目录页码与最终布局一致
        try:
            doc.getTextFields().refresh()
        except Exception:
            pass
        doc.refresh()
        indexes = doc.getDocumentIndexes()
        for i in range(indexes.getCount()):
            indexes.getByIndex(i).update()

        out = Path(pdf)
        out.parent.mkdir(parents=True, exist_ok=True)
        doc.storeToURL(
            file_url(str(out)),
            (prop("FilterName", "writer_pdf_Export"),),
        )
        doc.close(False)
        print("saved:", out)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法: lo_pdf_export.py <docx> <pdf>")
    main(sys.argv[1], sys.argv[2])
