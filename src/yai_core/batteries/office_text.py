"""Office 文档文本抽取 Battery：docx / xlsx / pptx / pdf → 纯文本（v0.20，opt-in）。

定位与边界（对应台账计划 #13 的拍板口径"尽可能零依赖，顶多作为可选"）：

1. 零依赖层：OOXML 三件套（docx/xlsx/pptx）本质都是 ZIP + XML，用标准库
   ``zipfile`` + ``xml.etree.ElementTree`` 抽**文本级**内容，不给内核带来任何依赖；
2. PDF 没有标准库解析器，走可选 extra：``[office]`` 只引入 pypdf（BSD-3、
   纯 Python、wheel 0.4 MB），函数内懒加载，缺依赖给可操作报错；
3. 刻意不做的：版面/样式还原（那是 pdfminer 级别的需求）、公式/图表内容、
   xlsx 的非文本单元格（数字/公式列不进文本层）——本模块服务的是
   "把文档喂给模型/检索"，不是文档转换器。

安全闸（解析不可信文档的红线，全部有测试钉住）：
- **zip 炸弹**：只读白名单成员，且逐成员流式限量（超上限立刻中止）；
- **XML 实体攻击**（billion laughs / 外部实体 / XXE）：全量缓冲区拒绝
  ``<!DOCTYPE`` 与 ``<!ENTITY``——实体只能定义在 DTD 里，两条检查一起封死
  定义通道；正文抽取不需要 DTD，真实 OOXML 文件不会因这条被误伤；
  输入大小另受逐成员限量二次约束（"拒绝 + 限量"双保险，Mimosa 建议口径）；
- **输出失控**：结果文本截断到 max_chars，风格与工具结果上限一致（…[truncated]）。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree

# 抽取目标：元素本地名 == "t" 的文本节点（docx 的 w:t 与 pptx 的 a:t 同名）；
# 这些"段落容器"本地名结束换行，保证词与词不粘连。
_PARAGRAPH_TAGS = frozenset({"p"})

_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_DEFAULT_MAX_UNCOMPRESSED = 64 * 1024 * 1024
_DEFAULT_MAX_CHARS = 160_000

_TRUNCATION_MARK = "…[truncated]"

# 各格式要读的 ZIP 成员白名单：只读这些，包内其它成员（含未知路径）一律不碰。
_DOCX_MEMBERS = ("word/document.xml",)
_PPTX_SLIDE_PREFIX = "ppt/slides/"
_XLSX_SHARED_STRINGS = "xl/sharedStrings.xml"

_PDF_ERROR_HINT = (
    "PDF 文本抽取需要可选依赖：uv sync --extra office"
    "（或 pip install 'yai-agent-core[office]'）"
)


def extract_text(
    source: str | Path | bytes,
    *,
    format: str | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_uncompressed: int = _DEFAULT_MAX_UNCOMPRESSED,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """从 docx / xlsx / pptx / pdf 提取纯文本；格式由扩展名判断或显式指定。

    - source 传路径时按扩展名分发；传 bytes 时必须给 format="docx|xlsx|pptx|pdf"；
    - 超 max_bytes 的输入、解压超 max_uncompressed 的成员、带 DOCTYPE/ENTITY 的
      XML、不支持的格式，一律抛 ``ValueError``（可操作文案，不静默降级）；
    - 结果超 max_chars 截断并追加 …[truncated]。
    """
    data: bytes
    fmt: str
    if isinstance(source, bytes):
        if format is None:
            raise ValueError("传入 bytes 时必须显式指定 format='docx'|'xlsx'|'pptx'|'pdf'")
        data, fmt = source, format.lower()
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError(f"文件不存在或不是普通文件：{path}")
        data = path.read_bytes()
        fmt = (format or path.suffix.lstrip(".")).lower()

    if len(data) > max_bytes:
        raise ValueError(f"文档超过 {max_bytes} 字节上限，拒绝解析")

    if fmt == "pdf":
        text = _pdf_text(data)
    elif fmt in {"docx", "pptx"}:
        text = _ooxml_text(data, max_uncompressed)
    elif fmt == "xlsx":
        text = _xlsx_text(data, max_uncompressed)
    else:
        raise ValueError(f"不支持的格式：{fmt!r}（支持 docx / xlsx / pptx / pdf）")

    if len(text) > max_chars:
        return text[:max_chars] + _TRUNCATION_MARK
    return text


def _read_member_limited(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """流式读一个 ZIP 成员，超 limit 立即中止（zip 炸弹不靠事后发现）。"""
    with zf.open(name) as fp:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = fp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError(f"ZIP 成员 {name} 解压超过 {limit} 字节上限，拒绝解析")
            chunks.append(chunk)
        return b"".join(chunks)


def _parse_text_xml(data: bytes) -> ElementTree.Element:
    """解析成员 XML；实体定义通道整条封死（DOCTYPE 与 ENTITY 都拒绝，全量扫描）。

    ElementTree 默认不解析外部实体，但内部 DTD 实体（billion laughs）仍可放大
    资源消耗——因此除"拒绝声明"外，输入大小另受 _read_member_limited 的流式
    限量二次约束（Mimosa 建议的"拒绝 + 限量"双保险）。
    """
    for marker in (b"<!DOCTYPE", b"<!ENTITY"):
        if marker in data:
            raise ValueError(f"XML 含 {marker.decode()} 声明，拒绝解析（防实体攻击）")
    return ElementTree.fromstring(data)


def _ooxml_text(data: bytes, max_uncompressed: int) -> str:
    """docx（word/document.xml）与 pptx（ppt/slides/slide*.xml）共用：
    收集本地名为 t 的文本节点，段落容器（p）结束后换行。"""
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if any(n.filename.startswith("/") or ".." in n.filename for n in zf.infolist()):
            raise ValueError("ZIP 成员含绝对/相对上跳路径，拒绝解析")
        if _has_member(zf, _DOCX_MEMBERS):
            names: list[str] = list(_DOCX_MEMBERS)
        else:
            names = sorted(
                n.filename for n in zf.infolist()
                if n.filename.startswith(_PPTX_SLIDE_PREFIX) and n.filename.endswith(".xml")
            )
        if not names:
            raise ValueError("ZIP 内没有可抽取的文档成员（不是合法的 docx/pptx？）")
        for name in names:
            xml_bytes = _read_member_limited(zf, name, max_uncompressed)
            root = _parse_text_xml(xml_bytes)
            for element in root.iter():
                local = element.tag.rsplit("}", 1)[-1]
                if local == "t" and element.text:
                    parts.append(element.text)
                elif local in _PARAGRAPH_TAGS and parts and parts[-1] != "\n":
                    parts.append("\n")
    return "".join(parts).strip()


def _xlsx_text(data: bytes, max_uncompressed: int) -> str:
    """xlsx 取共享字符串表（xl/sharedStrings.xml）——文本单元格的全集；
    数字/公式单元格不进文本层（本模块边界，docstring 已声明）。"""
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if not _has_member(zf, (_XLSX_SHARED_STRINGS,)):
            raise ValueError("ZIP 内没有 xl/sharedStrings.xml（不是合法的 xlsx？）")
        xml_bytes = _read_member_limited(zf, _XLSX_SHARED_STRINGS, max_uncompressed)
        root = _parse_text_xml(xml_bytes)
        for element in root.iter():
            local = element.tag.rsplit("}", 1)[-1]
            if local == "t" and element.text:
                parts.append(element.text)
            elif local == "si" and parts and parts[-1] != "\n":
                parts.append("\n")  # si = shared string 条目，一条一行
    return "".join(parts).strip()


def _has_member(zf: zipfile.ZipFile, names: tuple[str, ...]) -> bool:
    return all(n in zf.namelist() for n in names)


def _pdf_text(data: bytes) -> str:
    try:
        import pypdf  # 懒加载：只有 PDF 才需要 [office] extra
    except ImportError as exc:
        raise ImportError(_PDF_ERROR_HINT) from exc
    reader = pypdf.PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages).strip()
