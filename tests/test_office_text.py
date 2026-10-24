"""office_text Battery 测试：OOXML 文本抽取、PDF、安全闸（zip 炸弹/实体/截断）。

全程离线：测试内程序化构造最小 docx/xlsx/pptx/pdf，不触网、不需要真实文档。
pypdf 由 dev extra 提供（与 [office] extra 同一包）。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from yai_core.batteries.office_text import extract_text

# ---------- 测试夹具：程序化构造最小 OOXML ----------

def _make_ooxml(path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, xml in members.items():
            zf.writestr(name, xml)


_DOCX_XML = (
    '<?xml version="1.0"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>第一段内容</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>second paragraph</w:t></w:r></w:p></w:body></w:document>"
)

_PPTX_SLIDE_1 = (
    '<?xml version="1.0"?>'
    '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>标题页</a:t></a:r></a:p>"
    "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
)

_PPTX_SLIDE_2 = (
    '<?xml version="1.0"?>'
    '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>第二页正文</a:t></a:r></a:p>"
    "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
)

_XLSX_SST = (
    '<?xml version="1.0"?>'
    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<si><t>客户名称</t></si><si><t>合同金额</t></si>"
    "<si><r><t>多段</t></r><r><t>富文本</t></r></si></sst>"
)


@pytest.fixture
def docx_path(tmp_path):
    p = tmp_path / "demo.docx"
    _make_ooxml(p, {"word/document.xml": _DOCX_XML, "docProps/core.xml": "<x/>"})
    return p


@pytest.fixture
def pptx_path(tmp_path):
    p = tmp_path / "demo.pptx"
    _make_ooxml(
        p,
        {
            "ppt/slides/slide2.xml": _PPTX_SLIDE_2,  # 乱序写入，验证按名字排序
            "ppt/slides/slide1.xml": _PPTX_SLIDE_1,
            "ppt/media/image1.png": b"\x89PNG fake",
        },
    )
    return p


@pytest.fixture
def xlsx_path(tmp_path):
    p = tmp_path / "demo.xlsx"
    _make_ooxml(
        p,
        {
            "xl/sharedStrings.xml": _XLSX_SST,
            "xl/worksheets/sheet1.xml": "<worksheet><sheetData/></worksheet>",
        },
    )
    return p


def _minimal_pdf(text: str) -> bytes:
    """手工构造带正确 xref 的最小 PDF（单页、Helvetica、一行文本）。"""
    content = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for idx, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{idx} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n".encode()
    )
    return out.getvalue()


# ---------- 1. 三种 OOXML 的文本抽取 ----------

def test_docx_extracts_text_with_paragraph_breaks(docx_path) -> None:
    text = extract_text(docx_path)
    assert "第一段内容" in text and "second paragraph" in text
    assert "第一段内容\nsecond paragraph" in text


def test_pptx_reads_all_slides_sorted(pptx_path) -> None:
    text = extract_text(pptx_path)
    assert text == "标题页\n第二页正文"  # slide1 在前，成员按名字排序


def test_xlsx_reads_shared_strings(xlsx_path) -> None:
    text = extract_text(xlsx_path)
    assert "客户名称\n合同金额\n多段富文本" == text


def test_bytes_input_requires_format(docx_path) -> None:
    data = docx_path.read_bytes()
    assert "第一段内容" in extract_text(data, format="docx")
    with pytest.raises(ValueError, match="显式指定 format"):
        extract_text(data)


# ---------- 2. PDF（pypdf，dev/office extra 提供） ----------

def test_pdf_extraction(tmp_path) -> None:
    pytest.importorskip("pypdf")
    p = tmp_path / "demo.pdf"
    p.write_bytes(_minimal_pdf("Hello YAI"))
    assert "Hello YAI" in extract_text(p)


def test_pdf_missing_dependency_message_is_actionable() -> None:
    """缺 pypdf 时报错文案必须指向 [office] extra（字符串契约，仿 MCP/OpenAPI 先例）。"""
    source = Path(__file__).parents[1] / "src/yai_core/batteries/office_text.py"
    assert "uv sync --extra office" in source.read_text(encoding="utf-8")


# ---------- 3. 安全闸 ----------

def test_doctype_and_entity_rejected(tmp_path) -> None:
    for marker in (b"<!DOCTYPE x SYSTEM \"x.dtd\">", b"<!ENTITY a \"b\">"):
        p = tmp_path / "evil.docx"
        prolog = "<?xml version=\"1.0\"?>" + marker.decode()
        evil = _DOCX_XML.replace("<?xml version=\"1.0\"?>", prolog)
        _make_ooxml(p, {"word/document.xml": evil})
        with pytest.raises(ValueError, match="实体攻击"):
            extract_text(p)


def test_zip_member_size_guard(tmp_path) -> None:
    p = tmp_path / "big.docx"
    _make_ooxml(p, {"word/document.xml": _DOCX_XML})
    with pytest.raises(ValueError, match="上限"):
        extract_text(p, max_uncompressed=10)  # 故意给不可能的极限


def test_input_size_guard(docx_path) -> None:
    with pytest.raises(ValueError, match="字节上限"):
        extract_text(docx_path, max_bytes=5)


def test_result_truncation(docx_path) -> None:
    out = extract_text(docx_path, max_chars=5)
    assert out.endswith("…[truncated]")


def test_unsupported_format(tmp_path) -> None:
    p = tmp_path / "x.rar"
    p.write_bytes(b"whatever")
    with pytest.raises(ValueError, match="不支持的格式"):
        extract_text(p)


def test_path_traversal_member_rejected(tmp_path) -> None:
    p = tmp_path / "evil.docx"
    _make_ooxml(p, {"word/document.xml": _DOCX_XML, "../evil.txt": "x"})
    with pytest.raises(ValueError, match="上跳路径"):
        extract_text(p)
