# 竞品源码核验：OpenMuse-Win（2026-11-03，只读）

> 仓库：<https://github.com/wojiadexiaoming-copy/openmuse-win>
> 核验 commit：`2385664a1c754b1e9a8a15a0f4a88d64b04ea4b8`（2026-10-29，没有更新的 main）
> 核验方式：直接读取 GitHub raw 源码与仓库树（未克隆、未执行、未修改竞品仓库）。
> 结论边界：**源码级核验**；没有在竞品环境里运行任何测试，"真实环境验证"按其测试
> 标记与依赖清单判断，标注为「源码证据」而非「我方实测」。

## 0. 许可与事实

| 项 | 值 |
|---|---|
| License | **AGPL-3.0**（`license.spdx_id`）→ 与 MIT 的 YAI 不兼容，**禁止复制任何代码** |
| 默认分支 / 最新提交 | `main` / `2385664`（2026-10-29） |
| 规模 | 仓库约 918 KB；`tools/browser/worker.py` 4 710 行、`agent/orchestrator.py` 1 534 行、`tools/desktop.py` 1 411 行、`agent/llm_router.py` 838 行、`agent/approval.py` 607 行、`agent/verifier.py` 444 行 |
| 测试 | **88 个测试文件**（浏览器相关 18+ 个、`test_completion_verifier.py` 346 行、`test_desktop.py` 243 行） |
| CI | **没有 `.github/` 目录，无任何 CI workflow**（仓库树实测 0 个 `.github/*` 文件） |
| 第三方依赖（`requirements.txt`） | openai、playwright、pillow、apscheduler、sqlalchemy、qrcode、pywebview、pyautogui、pywinauto、httpx、pyyaml、pytest |

> "零依赖"口径澄清：其 `pyproject.toml` 只列 pyautogui / pywinauto / qrcode 三项，
> 完整 `requirements.txt` 才是真相——**没有 Agent 框架依赖（LangChain/CrewAI/AutoGen），
> 但完整系统大量依赖第三方库**。YAI 的 `dependencies = []` 是更严格的内核约束，
> 不代表"整个产品零依赖"。

## 1. 逐文件核验（源码证据）

| 文件 | 体量 | 真实实现 | 模拟 / 未验证证据 | 值得注意 |
|---|---|---|---|---|
| `tools/browser/worker.py` | 4 710 行 | Playwright 页面观察、DOM/可访问性提取、点击/输入/等待/标签页 | `tests/test_browser.py` 里 `@pytest.mark.skipif(True, reason="需 Chromium 真机…")`；真实浏览器用例默认跳过（`无 Chromium → pytest.skip`） | 浏览器能力靠**单体 worker**实现，未与内核治理契约分离 |
| `tools/desktop.py` | 1 411 行 | 窗口枚举 / 截图 / 热键 / UIA 操作（pywinauto + pyautogui + Pillow） | `tests/test_desktop.py` 用 `MagicMock` + `fake_gui()` 模拟桌面，**不是真机 UIA** | Windows 耦合进工具层 |
| `tools/desktop_accessibility.py` | 666 行 | UIA 控件 Pattern 查询（`mock_patterns` 只是变量名，不是假实现） | 无真机测试标记 | 语义化 UIA 思路与 YAI 的 Desktop Provider 同向 |
| `agent/llm_router.py` | 838 行 | 多 Provider 路由、按角色失败恢复、能力探测（`supports_images`）、credential tag、序列化 | 无对应"未授权 provider 零调用"测试 | 有路由治理雏形，但无宿主声明式授权集合 |
| `agent/approval.py` | 607 行 | **L1/L2/L3 三级审批**（源码中 L1×7 / L2×8 / L3×14）、审批摘要 humanize | 纯单元测试；无真实审批链路证据 | 分级理念可借鉴，但写死在应用层 |
| `agent/risk.py` | 124 行 | 命令/路径风险判定（shlex 解析） | 单元测试 | 轻量规则，非可插拔策略 |
| `agent/verifier.py` | 444 行 | **确定性产物/证据检查**：`capture_artifacts`、`extract_criteria`、`check_criterion`、`verify_outcome`；没有 LLM/截图关键词 | 有 `test_completion_verifier.py`（346 行），偏文件/产物断言 | 与 YAI Outcome Verification 契约同方向；YAI 额外区分执行收据/任务验收并支持宿主 verifier |
| `agent/result_observer.py` | 174 行 | 截图/DOM 变化的**观察辅助**（`_evidence`、`observe`、`build_result_observer`） | 未见真机证据 | 视觉只做辅助，未作为唯一验收依据（与 YAI 立场一致） |
| `agent/orchestrator.py` | 1 534 行 | ReAct 编排（openai 调用、上下文、工具循环） | 单测 + 无 CI | 单体编排器，与 Windows/UI 强耦合 |
| `agent/subagent.py` | 347 行 | 子 Agent 委派（工具/上下文收窄） | 单测 | 与 YAI `shell/employee` 思路相近，但耦合应用层 |

## 2. 六问回答

1. **哪些功能真实可用？** 源码层面：浏览器 worker、桌面工具、多 Provider 路由、
   三级审批、确定性产物验收、编排器均有实质实现（不是空壳）。
2. **哪些只有模拟测试？** 桌面 UI 自动化——`test_desktop.py` 全程 `MagicMock`/`fake_gui`；
   浏览器真实操作测试默认 `skipif(True)`（需 Chromium 真机），CI 环境不跑。
3. **哪些有真实环境验证证据？** 仓库内**没有** CI 配置；测试中有"需真机/无 Chromium 则 skip"
   的分支，说明作者在本地做过真机验证，但**仓库没有可审计的自动化证据**。
4. **值得借鉴的架构思想：** ① L1/L2/L3 分级审批；② 确定性产物验收（文件/产物检查）
   而非模型自评；③ UIA 语义 Pattern 优先、几何点击兜底；④ 多 Provider 能力探测与
   按角色失败恢复；⑤ 结果观察者作为"证据采集"旁路。
5. **不适合 YAI 的实现：** AGPL 代码不可复制；Windows/UI 强耦合的编排器与工具层；
   把浏览器 worker 做成单体而非可插拔执行适配器；无 CI 的工程链路。
6. **YAI 的等效机制 vs 真缺口：**

| 竞品能力 | YAI 现状 | 结论 |
|---|---|---|
| ReAct 编排 / 规划 / 子 Agent | `AgentLoop` + `AdaptiveRouter` + `shell/employee` | **已有等效** |
| 多 Provider 路由 / 故障转移 | v0.26 `ProviderRoutingPolicy` + `allow_fallback` | **已有等效且更严格**（宿主授权集合、未授权零调用、子任务收窄） |
| L1/L2/L3 审批 | 原生参数级审批 + `PermissionPolicy` + 收据 | **已有等效**（声明驱动，非写死） |
| 浏览器自动化 | v0.26 Browser Provider（治理/快照引用/收据）+ 可选 Playwright | **已有等效**；真实浏览器未真机验收（竞品同样缺自动化证据） |
| Windows 桌面自动化 | v0.26 Desktop Provider（UIA、焦点保护、高风险拒绝）+ 本机真机冒烟 | **已有等效**；覆盖面窄于竞品（竞品工具更多） |
| 确定性产物验收 | v0.26 `VerificationResult` + 宿主 verifier + 浏览器/桌面证据 | **已有等效契约**；具体领域 verifier 需宿主实现 |
| 截图 / 视觉定位 | 仅契约位（后端 `screenshot()`，默认返回 None） | **真缺口**（不作为必需能力，按需再补） |
| 完整 Web UI / 安装包 / 定时任务 | 无（明确不做内核内置 UI/调度） | **有意不做**（属宿主层） |

## 3. 可被客观测试证明的差异化（YAI 侧）

1. **未授权 Provider 零调用**：`tests/test_provider_routing.py` 用调用计数证明
   "A 未授权时 A 失败也不会调用 B"，且取消 / 超时 / 重试不改变授权范围。
2. **治理链路唯一**：浏览器/桌面工具都注册进 `ToolRegistry`，经 `ToolExecutor`
   产生收据与事件；不存在第二套执行/权限系统。
3. **记忆损坏不静默丢档**：`tests/test_memory_vault_corruption.py` 证明 quarantine /
   read_only / raise 三种处置与原档字节保留（竞品无对应证据）。
4. **任务验收与执行收据分离**：`RunResult.verification` + `DONE` 事件携带证据；
   `tests/test_outcome_verification.py` 证明验收失败不改文本、不重试。
5. **可嵌入性**：`dependencies = []` 由 AST 守门测试保证，浏览器/桌面/扩展均为可选模块。

## 4. 结论

OpenMuse-Win 是一个**有真实实现、测试数量多、但无 CI 且真实环境证据只在作者本地**的
AGPL Windows 应用。YAI 不应在功能数量上跟跑；差异化应落在
**可嵌入（MIT、零硬依赖）+ 可治理（唯一执行链路 + 宿主授权）+ 可验证（收据/验收分离）
+ 可移植（可选 Provider）**，并用可复现测试而不是截图/视频证明。

> 未核实项：竞品在真实 Windows 机器上的桌面自动化成功率、浏览器任务完成率、
> 安装包/产品形态——本轮只做源码与测试结构核验，不做运行对比。
