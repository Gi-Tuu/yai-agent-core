# 文档总索引（Document Map）

[English](DOCUMENT_MAP.en.md) · **简体中文**

> 回答"这个仓库有哪些文档、各管什么、以哪份为准"。能力做到什么程度看
> [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md)；怎么读代码看
> [`reading-guide.md`](reading-guide.md) 与 [`walkthrough/00-index.md`](walkthrough/00-index.md)。
>
> 维护约定：新增 / 移动 / 改名文档必须同步本表与 `pyproject.toml` 的 sdist 白名单；
> 链接有效性由 `tests/test_sdist_link_integrity.py` 自动把关。除特别注明外，
> 公开文档全随 sdist 分发（规则：**被 git 跟踪的公开文档全进包**）。
>
> **线上文档站**：https://gi-tuu.github.io/yai-agent-core/ —— 本目录由 `mkdocs.yml`
> 构建发布到 GitHub Pages；新增公开文档时同步 nav 或 `not_in_nav`，
> `tests/test_docs_site.py` 会校验 nav 路径与"docs 内链接不逃出站点根"。

## 一、根目录门面（中英成对维护，事实性改动必须两边同步）

| 文档 | 语言 | 一句话 |
|---|---|---|
| [README.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/README.md) / [README.en.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/README.en.md) | 中 / 英 | 项目门面：定位、30 秒上手、核心能力、宿主示例 |
| [CONTRIBUTING.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CONTRIBUTING.md) / [CONTRIBUTING.en.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CONTRIBUTING.en.md) | 中 / 英 | 贡献流程 + 维护者操作手册（发版、门禁） |
| [SECURITY.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/SECURITY.md) / [SECURITY.zh-CN.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/SECURITY.zh-CN.md) | 英 / 中 | 安全政策：支持版本表、漏洞报告渠道（路径被测试钉住） |
| [CHANGELOG.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CHANGELOG.md) / [CHANGELOG.en.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CHANGELOG.en.md) | 中 / 英 | 逐版本变更（中文为最终准绳；英文为版本级摘要） |
| [ROADMAP.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/ROADMAP.md) / [ROADMAP.en.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/ROADMAP.en.md) | 中 / 英 | 方向与边界：已完成各版本、候选、明确不做（英文为摘要版） |

## 二、docs/ 根层：架构与定位

| 文档 | 一句话 |
|---|---|
| [architecture.md](architecture.md) / [architecture.en.md](architecture.en.md) | 架构总纲：分层、数据流、SPI、治理模型（中英） |
| [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) | **能力与证据索引**：已验证 / 未真机验证 / 不做 三档，证据落到测试文件 |
| [whitepaper.md](whitepaper.md) / [whitepaper.en.md](whitepaper.en.md) | 技术白皮书（**v0.25.0 发布快照口径**，现值以 IMPLEMENTATION_STATUS 为准；中英） |
| [comparison.md](comparison.md) / [comparison.en.md](comparison.en.md) | 与五类同类产品的诚实对比（按类别、不点名，中英） |
| [reading-guide.md](reading-guide.md) | 代码学习路线（7 站 + 小练习），与 walkthrough 配套 |
| [BILINGUAL.md](BILINGUAL.md) | **双语文档覆盖与规则**：哪些文档有英文版、哪些按约定不翻译、新增翻译的步骤 |
| DOCUMENT_MAP.md / [DOCUMENT_MAP.en.md](DOCUMENT_MAP.en.md) | 本文件：全部文档的总索引（中英） |

## 三、docs/evaluations/ —— 评测与证据（8 篇）

| 文档 | 一句话 |
|---|---|
| [agentdojo-evaluation.md](evaluations/agentdojo-evaluation.md) | AgentDojo 注入攻击评测（0.24.0 时点，injection guard 结果 + 复跑） |
| [memory-evaluation.md](evaluations/memory-evaluation.md) | 记忆检索质量评测（v0.27 扩量：114 条 / 60 query，BGE-M3 实测） |
| [browser-task-e2e.md](evaluations/browser-task-e2e.md) | 受控浏览器真实任务 E2E（v0.27 B3，真实 Chromium，收据链完整） |
| [windows-real-apps.md](evaluations/windows-real-apps.md) | Windows Desktop Provider 真实应用验证（字符映射表 12/12，含下拉选择） |
| [sidepanel-manual-acceptance.md](evaluations/sidepanel-manual-acceptance.md) | 侧边栏真实 Side Panel 人工验收清单 |
| [ecosystem-interop.md](evaluations/ecosystem-interop.md) | 随机第三方开源项目嵌入实测（2026-09-27） |
| [competitor-openmuse-audit.md](evaluations/competitor-openmuse-audit.md) | 竞品 OpenMuse-Win 只读源码核验 |
| [openssf-best-practices-self-assessment.md](evaluations/openssf-best-practices-self-assessment.md) | OpenSSF Best Practices Passing 自评清单（2026-11-05 复核） |

## 四、docs/guides/ —— 宿主与运维指南（12 篇）

| 文档 | 一句话 |
|---|---|
| [production-permissions.md](guides/production-permissions.md) | 生产权限配置指南（路径被内核生产安全警告文案引用，**勿随意改名**） |
| [demo-deployment.md](guides/demo-deployment.md) | 公网部署手册（**2026-11-05 起公网实例必须配置 `YAI_API_KEY`**） |
| [deployment-scaling.md](guides/deployment-scaling.md) / [英文版](guides/deployment-scaling.en.md) | 水平扩展与多租户隔离（中英，事实基线 v0.11.0 已注明） |
| [tracing.md](guides/tracing.md) / [英文版](guides/tracing.en.md) | OpenTelemetry 结构化追踪接入（中英） |
| [multilang-interop.md](guides/multilang-interop.md) | 多语言宿主接入设计（sidecar / MCP / OpenAPI，事实基线 v0.15.0） |
| [browser-extension-bridge.md](guides/browser-extension-bridge.md) | 浏览器扩展（MV3）与 Native Bridge：设计、安装、排障、真机验收记录 |
| [execution-audit-persistence.md](guides/execution-audit-persistence.md) | 执行审计持久化边界（宿主接入指南） |
| [federation.md](guides/federation.md) | **有向父子核设计稿**：产品生态里父核调子核、子核不可反调；协议 v1 / grant / 治理继承 |
| [openapi-31-compat.md](guides/openapi-31-compat.md) | **OpenAPI 3.1 / JSON Schema 2020-12 兼容矩阵**：翻译规则、未翻译构造的 notes 与边界 |
| [ag-ui.md](guides/ag-ui.md) | **AG-UI 1.0 适配器**：事件映射、SSE 帧、FastAPI 示例与边界 |
| [mcp-registry.md](guides/mcp-registry.md) | **MCP Registry 只读发现**：搜索 API、构建配置、安全边界 |
| [a2a-positioning.md](guides/a2a-positioning.md) | **A2A 1.0 定位**：有向父子核 ≠ Multi-Agent；宿主层适配器方案 |

## 五、docs/reports/ —— 时点报告（2 篇）

| 文档 | 一句话 |
|---|---|
| [audit-remediation-2026-11.md](reports/audit-remediation-2026-11.md) | v0.26 审计整改计划与问题→修复映射（5 批次，2026-11-03） |
| [v026-交付报告.md](reports/v026-交付报告.md) | v0.26 发布前的分支交付报告（**时点记录**，现状见 IMPLEMENTATION_STATUS） |

## 六、docs/adr/ —— 架构决策记录

索引见 [adr/README.md](adr/README.md)（0002–0008，编号自 0002 起）。

## 七、docs/walkthrough/ —— 逐行讲义（57 篇）

[00-index.md](walkthrough/00-index.md) 是总览与分篇目录（01–56），仅中文、不翻译；
构建 Word 讲义：`scripts/build_walkthrough_docx.py`（产物在 gitignore 的 `docs/exports/`）。

## 八、docs/releases/ —— GitHub Release 正文源稿（44 篇）

v0.6.0 与 v0.10.0–v0.45.0 的人话版 Release 稿（v0.7–v0.9 无独立稿，当时未单独成稿）；每篇顶部带一行 **English summary**；
唯一入口是 [CHANGELOG.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CHANGELOG.md) 头部的 `docs/releases/` 链接，逐条变更以 CHANGELOG 为准。

## 九、其它子目录 README

随包分发（可直接点）：
[benchmarks/agentdojo](https://github.com/Gi-Tuu/yai-agent-core/blob/main/benchmarks/agentdojo/README.md) ·
[benchmarks/memory](https://github.com/Gi-Tuu/yai-agent-core/blob/main/benchmarks/memory/README.md) ·
[examples/host_g_sandbox](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_g_sandbox/README.md) ·
[examples/child_cores](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/child_cores/README.md) ·
[examples/host_i_mealie](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_i_mealie/README.md) ·
[examples/host_j_actual](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_j_actual/README.md) ·
[examples/host_k_multimodal](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_k_multimodal/README.md) ·
[examples/host_l_sidecar_node](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_l_sidecar_node/README.md) ·
[shell/desktop/fonts](https://github.com/Gi-Tuu/yai-agent-core/blob/main/shell/desktop/fonts/README.md)。

仅仓库可见（**不在 sdist 白名单，只列名字不发链接**）：`packaging/extension/README.md`、
`packaging/windows/README.md`、`fuzzing/README.md`、`models/README.md`、
`clients/node/README.md`（TS sidecar 客户端）、`tests/fixtures/acc/SOURCE.md`（ACC 语料出处与哈希）、
`.github/` 三个协作模板。

## 十、资源与产物目录

| 目录 | 内容 | 是否随包 |
|---|---|---|
| `docs/assets/` | README / 讲义引用的截图与配图 | ✅ 随 sdist |
| `docs/figs/` | 白皮书配图（`scripts/draw_figs.py` 产物） | ✅ 随 sdist |
| `docs/exports/` | 讲义 Word / PDF 构建产物（`scripts/build_walkthrough_docx.py`） | ❌ 已被 `.gitignore` 排除，不进仓库也不进包 |

## 十一、仅本机存在的过程稿（不随仓库分发，克隆者不可见）

以下文件与目录被 `.gitignore` / `.git/info/exclude` 排除，只在本机出现，不放链接、不进包：

- **工作流水**：`docs/项目台账.md` 及 4 个归档分卷、`docs/v026-执行记录.md`
- **方案 / 任务书**：`docs/v025-执行落地方案.md`、`docs/v025-audit-report.md`、
  `docs/YAI员工薄壳-落地方案.md`、`docs/YAI灵动岛多专员-落地方案.md`、
  `docs/YAI项目全景分析与发展规划.md`、`docs/产品化更新方案.md`、
  `docs/工具管理永久保留-落地方案.md`、`docs/检查点A-架构审查-通用能力与ACC.md`
- **素材 / 内部**：`docs/内部-主张与证据对照.md`、`docs/宣传视频-代码生成提示词.md`、
  `docs/终端工艺视觉-设计系统与改造任务书.md`
- **工具过程文件**：`.zcode/`（其它 AI 协作者的计划 / 会话草稿，已排除）
- **根目录**：`AGENTS.md`（AI 协作约定，刻意只留本地）、`YAI开源增长方案-修订版.md`
