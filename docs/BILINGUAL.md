# Bilingual documentation coverage

> **中文摘要**：本页记录哪些文档有英文版、哪些按约定只保留中文，以及新增文档时的双语规则。
> 当前已补齐"国际面"核心文档的英文版；逐行讲义（walkthrough）与内部过程稿按约定不翻译。

This page is the source of truth for **what is bilingual**. The public-facing rule is simple:

- **International-facing docs must ship an English version** (`.en.md` next to the Chinese file).
  The set is enforced by [`tests/test_bilingual_docs.py`](https://github.com/Gi-Tuu/yai-agent-core/blob/main/tests/test_bilingual_docs.py).
- **Chinese remains canonical.** English translations may lag behind; when the two disagree, the
  Chinese file wins. Every English file starts with a translation notice linking back to Chinese.
- **Never translate** the learning walkthrough (`docs/walkthrough/`, 57 chapters, Chinese-only by
  repo policy) or internal project ledgers/plans (see below).

## 1. Bilingual now (enforced)

| Area | Chinese | English |
|---|---|---|
| Facade | [`README.md`](https://github.com/Gi-Tuu/yai-agent-core/blob/main/README.md) · [`CONTRIBUTING.md`](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CONTRIBUTING.md) · [`SECURITY.zh-CN.md`](https://github.com/Gi-Tuu/yai-agent-core/blob/main/SECURITY.zh-CN.md) | [`README.en.md`](https://github.com/Gi-Tuu/yai-agent-core/blob/main/README.en.md) · [`CONTRIBUTING.en.md`](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CONTRIBUTING.en.md) · [`SECURITY.md`](https://github.com/Gi-Tuu/yai-agent-core/blob/main/SECURITY.md) |
| Site home | [index.md](index.md) | [index.en.md](index.en.md) |
| Capability evidence | [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) | [IMPLEMENTATION_STATUS.en.md](IMPLEMENTATION_STATUS.en.md) |
| Architecture / comparison | [architecture.md](architecture.md) · [comparison.md](comparison.md) | [architecture.en.md](architecture.en.md) · [comparison.en.md](comparison.en.md) |
| Reading guide | [reading-guide.md](reading-guide.md) | [reading-guide.en.md](reading-guide.en.md) |
| Whitepaper / roadmap | [whitepaper.md](whitepaper.md) · [ROADMAP.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/ROADMAP.md) | [whitepaper.en.md](whitepaper.en.md) · [ROADMAP.en.md](https://github.com/Gi-Tuu/yai-agent-core/blob/main/ROADMAP.en.md) |
| Guides | [production-permissions.md](guides/production-permissions.md) · [demo-deployment.md](guides/demo-deployment.md) · [browser-extension-bridge.md](guides/browser-extension-bridge.md) · [tracing.md](guides/tracing.md) | [production-permissions.en.md](guides/production-permissions.en.md) · [demo-deployment.en.md](guides/demo-deployment.en.md) · [browser-extension-bridge.en.md](guides/browser-extension-bridge.en.md) · [tracing.en.md](guides/tracing.en.md) |
| Guides | [multilang-interop.md](guides/multilang-interop.md) · [execution-audit-persistence.md](guides/execution-audit-persistence.md) · [deployment-scaling.md](guides/deployment-scaling.md) · [federation.md](guides/federation.md) · [openapi-31-compat.md](guides/openapi-31-compat.md) | [multilang-interop.en.md](guides/multilang-interop.en.md) · [execution-audit-persistence.en.md](guides/execution-audit-persistence.en.md) · [deployment-scaling.en.md](guides/deployment-scaling.en.md) · [federation.en.md](guides/federation.en.md) · [openapi-31-compat.en.md](guides/openapi-31-compat.en.md) |
| Evaluations (8) | [memory-evaluation.md](evaluations/memory-evaluation.md) · [browser-task-e2e.md](evaluations/browser-task-e2e.md) · [windows-real-apps.md](evaluations/windows-real-apps.md) · [agentdojo-evaluation.md](evaluations/agentdojo-evaluation.md) · [ecosystem-interop.md](evaluations/ecosystem-interop.md) · [competitor-openmuse-audit.md](evaluations/competitor-openmuse-audit.md) · [openssf-best-practices-self-assessment.md](evaluations/openssf-best-practices-self-assessment.md) · [sidepanel-manual-acceptance.md](evaluations/sidepanel-manual-acceptance.md) | same filenames with `.en.md` |
| ADR | [README.md](adr/README.md) · [0002](adr/0002-runtime-auth-boundary.md)–[0008](adr/0008-directed-federation.md) | [README.en.md](adr/README.en.md) · [0002.en](adr/0002-runtime-auth-boundary.en.md)–[0008.en](adr/0008-directed-federation.en.md) |

## 2. Intentionally Chinese-only

| Area | Why |
|---|---|
| `docs/walkthrough/` (57 chapters) | Line-by-line learning material tied to Chinese comments; repo policy keeps it Chinese-only. |
| `docs/DOCUMENT_MAP.md` | Internal index for maintainers. |
| `docs/releases/` (historical notes) | Historical source drafts for GitHub Releases; newest release notes carry an English summary when relevant. |
| Internal plans / ledgers / reports | `docs/项目台账*`, `docs/产品化更新方案.md`, `docs/v025-*`, `docs/v026-*`, `docs/v028-*`, `docs/YAI项目全景分析与发展规划.md`, `docs/YAI员工薄壳-落地方案.md`, `docs/YAI灵动岛多专员-落地方案.md`, `docs/终端工艺视觉-*`, `docs/检查点A-*`, `docs/内部-主张与证据对照.md`, `docs/工具管理永久保留-落地方案.md`, `docs/宣传视频-代码生成提示词.md`, `docs/reports/` — internal process material. |
| `CHANGELOG.md` / `ROADMAP.md` | Long-running Chinese ledgers; GitHub Releases and README carry the English-facing summary. |

## 3. Remaining backlog (not yet translated)

- `CHANGELOG.en.md` and historical `docs/releases/*` (GitHub Releases carry the English summary;
  new release notes add a short English section at the top)
- `docs/DOCUMENT_MAP.md` (internal index)

## 4. Adding a translated doc

1. Read the Chinese source and write `<name>.en.md` in the same directory (full translation;
   tables/commands/versions unchanged).
2. Add the standard top notice linking back to the Chinese file (copy the first line of any existing
   `.en.md` file in this repo; it states that the Chinese version is canonical).
3. Add an `English:` link to the top of the Chinese file.
4. Add the English file to the `English:` section of `mkdocs.yml`.
5. If it is international-facing, add the pair to `REQUIRED_PAIRS` in `tests/test_bilingual_docs.py`.
6. Run `python -m pytest tests/test_bilingual_docs.py tests/test_docs_site.py`.
