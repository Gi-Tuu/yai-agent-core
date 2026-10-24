# 逐行讲解 55 · Office 文本抽取：让"文档"变成模型能读的文本

> 对应代码：`batteries/office_text.py`（新增）。`[office]` extra 只为 PDF 引入 pypdf
> （BSD-3、纯 Python、0.4 MB）；docx/xlsx/pptx 用标准库，零依赖。

## A. 为什么是"文本级"而不是"文档转换"

本模块服务的场景是"把文档喂给模型或检索"：模型要的是正文文字，不是版式。
所以刻意的边界是——docx/pptx 抽全部文本节点、xlsx 只取共享字符串表；
版面还原、公式、图表、数字单元格**不进文本层**。这条边界让零依赖方案成立：
一旦要求"还原版面"，就得引入 pdfminer 级别的依赖树，拍板的"尽可能零依赖"就破了。

## B. OOXML 就是 ZIP + XML

docx/xlsx/pptx 都是 ZIP 包：

```text
demo.docx ── word/document.xml   ← 正文（<w:t> 是文本节点，<w:p> 是段落）
demo.pptx ── ppt/slides/slideN.xml（<a:t> 文本，按文件名排序即页序）
demo.xlsx ── xl/sharedStrings.xml ← 文本单元格的全集（<si> 一条一格）
```

所以 `zipfile` 打包、`ElementTree` 解析、按**本地名**（`}` 后那段）匹配 `t`/`p`/`si`
就能抽文本——不依赖任何第三方 XML 库。

## C. 三条安全闸（每条都有测试钉住）

解析不可信文档的攻击面主要三个，各自的对策：

1. **zip 炸弹**（解压放大）：只读白名单成员 + `_read_member_limited` 流式限量，
   超限立即中止——不靠"读完了再看多大"；
2. **XML 实体攻击**（billion laughs / XXE）：全量缓冲区拒绝 `<!DOCTYPE` 与
   `<!ENTITY`。实体只能定义在 DTD 里，两条检查把**定义通道**整条封死；
   ElementTree 默认不解析外部实体，加上限量即"拒绝 + 限量"双保险；
3. **输出失控**：结果截断到 `max_chars` 加 `…[truncated]`，与工具结果上限同一风格。

另有一条小闸：ZIP 成员名带绝对路径或 `..` 上跳直接拒绝（防御性，读白名单成员
本来碰不到它们，但闸在就该有测试）。

## D. PDF 与懒加载

PDF 没有标准库解析器。选型对照（2026-10 PyPI 实测）：pypdf（BSD-3、纯 Python、
0.4 MB）是唯一"小而许可干净"的选择；python-docx 会拖进 lxml（C 扩展），
PyMuPDF 是 AGPL 且 60+ MB，都回避。`_pdf_text` 函数内懒加载，缺依赖报
`uv sync --extra office`——与 MCP/OpenAPI 缺依赖报错同一先例。

## E. 测试怎么做到零真实文档

三个 OOXML 夹具全部在测试内程序化构造（`zipfile.writestr` 写最小成员集），
PDF 夹具是手工拼的最小 PDF（对象偏移算出正确 xref）。这同时验证了"本模块
只依赖结构约定，不依赖 Word/Excel 的完整产物"——真实文件只会更大，不会不同。

## 自检

1. 为什么 xlsx 只取 sharedStrings？数字单元格去哪了？
2. 拒绝 DOCTYPE 为什么能同时挡住 billion laughs 和 XXE？
3. zip 炸弹的闸为什么是"流式限量"而不是"读完检查"？
4. 为什么 PDF 走 extra 而 OOXML 不走？
5. 如果宿主要版面还原，应该用什么？（答：不该用本模块，那是 pdfminer/pypdfium2
   级别的需求——边界在第 A 节。）
