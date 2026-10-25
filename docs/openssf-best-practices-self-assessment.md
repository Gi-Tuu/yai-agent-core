# OpenSSF Best Practices Badge（原 CII）自评清单 —— YAI Agent Core

> 对照 OpenSSF Best Practices **Passing** 级官方标准（https://www.bestpractices.dev/en/criteria/passing ）
> 逐条评估本仓库当前 main（**v0.22.0**，2026-10-25 复核）。状态：**Met 已满足 / Unmet 不满足（附理由）/ N/A 不适用（附理由）**。
> 复核要点见文末「八、复核记录（2026-10-25）」；本次只更新**仓库内**自证文本，
> **未在 bestpractices.dev 平台上重新提交**（平台提交需本人账号 OAuth，属外部写入）。
>
> **如何申请（网页操作，约 15 分钟）**
> 1. 打开 https://www.bestpractices.dev ，右上角用 **GitHub 账号登录（OAuth）**；
> 2. 点 **+ New Project / Get Your Badge**，填入仓库公开 URL（GitHub：`https://github.com/Gi-Tuu/yai-agent-core`）；
> 3. 系统按 77 条逐条列出，**按下表勾选 Met / Unmet / N/A**；标 `{Met URL}` 的条目需粘贴对应文件的 GitHub 网页 URL；
> 4. 全部 MUST 为 Met（或 N/A）、SHOULD 为 Met 或 Unmet+理由、SUGGESTED 为 Met 或 Unmet，即可提交获得 **Passing Badge**，把徽章链接放进 README。
>
> **结果：已于 2026-10-10 通过自评并获得 Passing（过境）徽章**（项目 [#15360](https://www.bestpractices.dev/projects/15360)），Basics/Change Control/Reporting/Quality/Security/Analysis 六区全部满足；英文门面已补齐，`english` 条改为 Met；仅 2 条 SUGGESTED（专用动态分析 fuzzer/DAST）为 Unmet，不阻塞 Passing。
>
> **2026-10-25 复核**：徽章状态不变（Passing）。仓库侧的证据已更新——测试规模从当时的 ~1 584 项增至
> **1 881 项（1 876 通过 + 5 跳过）**、行覆盖率 **94.37%**；原先 Unmet 的两条动态分析
> SUGGESTED **已具备可用工具**（atheris fuzzing 4 个 target + CI `fuzz` job，见第五节的
> `dynamic_analysis*` 行），本清单据此改判为 Met，**但平台上的自评仍以 2026-10-10 那次提交为准**，
> 下次自评时再正式勾选。

状态图例：✅ Met · ⚠️ Unmet（需填理由） · ➖ N/A（需填理由）

---

## 一、Basics 基础

| ID | 要求（级别） | 状态 | 依据 / 填写内容 |
|---|---|---|---|
| description_good | 官网须简述软件做什么、解决什么问题（MUST） | ✅ | README 首句："进程内嵌入式自适应 Agent 内核"。URL：`.../blob/main/README.md` |
| interact | 官网说明如何获取、反馈、贡献（MUST） | ✅ | README（安装/Quickstart）+ CONTRIBUTING.md + Issues 入口 |
| contribution | 说明贡献流程（是否用 PR）（MUST，Met URL） | ✅ | `CONTRIBUTING.md`：fork → 分支 → PR，使用 Conventional Commits |
| contribution_requirements | 可接受贡献的要求/编码标准（SHOULD，Met URL） | ✅ | CONTRIBUTING：须过 `ruff`、`pytest`，同步加测试与讲义 |
| floss_license | 成果以 FLOSS 发布（MUST） | ✅ | MIT（`pyproject.toml` PEP 639 SPDX） |
| floss_license_osi | 许可证经 OSI 认可（SUGGESTED） | ✅ | MIT 为 OSI 认可许可证 |
| license_location | 许可证在仓库标准位置（MUST，Met URL） | ✅ | 根目录 `LICENSE` |
| documentation_basics | 提供基础文档（MUST，可 N/A） | ✅ | README + `docs/` + `docs/walkthrough/00–55`（55 篇逐行讲义 + 00-index） |
| documentation_interface | 描述外部接口的输入/输出（MUST，可 N/A） | ✅ | `docs/architecture.md`、SPI 契约、FastAPI 端点、walkthrough |
| sites_https | 站点（网站/仓库/下载）支持 HTTPS/TLS（MUST） | ✅ | GitHub / Gitee / PyPI 均 HTTPS |
| discussion | 可搜索、URL 可定位、新人可参与、无需专有软件的讨论机制（MUST） | ✅ | GitHub Issues / PR（Gitee 同步） |
| english | 英文文档、接受英文反馈（SHOULD） | ✅ | **Met**：已提供 README.en.md、CONTRIBUTING.en.md、SECURITY.md、docs/architecture.en.md、docs/comparison.en.md，并接受英文 issue / code review；2026-10-10 随 Passing 自评（#15360）勾选 Met。 |
| maintained | 项目处于维护状态（MUST） | ✅ | 持续提交（建库 2026-09-09，403 次提交，2026-10-25 仍有合并），ROADMAP 活跃；GitHub 开放 issue/PR 计数为 0 |

## 二、Change Control 变更控制

| ID | 要求（级别） | 状态 | 依据 / 填写内容 |
|---|---|---|---|
| repo_public | 公开可读、带 URL 的版本库（MUST） | ✅ | GitHub + Gitee 公开仓库 |
| repo_track | 记录改动内容/人/时间（MUST） | ✅ | git 历史 |
| repo_interim | 含版本之间的中间版本，非仅最终发布（MUST） | ✅ | 主干持续 commit |
| repo_distributed | 使用分布式 VCS（SUGGESTED） | ✅ | git |
| version_unique | 每个面向用户的发布有唯一版本标识（MUST） | ✅ | 0.7.0 → 0.22.0 唯一（17 个发布 tag） |
| version_semver | 使用 SemVer / CalVer（SUGGESTED） | ✅ | SemVer（当前 `0.22.0`） |
| version_tags | 在 VCS 中标记发布（SUGGESTED） | ✅ | tags `v0.7.0` … `v0.22.0`（含 `v0.8.1`），另有里程碑 tag `v0.5.0-hangzhou` |
| release_notes | 每版有人可读发布说明，非 git log（MUST，Met URL） | ✅ | `CHANGELOG.md` |
| release_notes_vulns | 列出本版修复的、已分配 CVE 的公开运行时漏洞（MUST，可 N/A） | ➖ | **N/A 理由**："项目成果自身无已分配 CVE 的公开漏洞；故无此类条目。" |
| report_process | 提供提交 bug 的流程（MUST，Met URL） | ✅ | GitHub Issues + `.github/ISSUE_TEMPLATE/` |
| report_tracker | 使用 issue tracker 跟踪单个问题（SHOULD） | ✅ | GitHub Issues |
| report_responses | 近 2–12 月回应多数 bug（MUST） | ✅ | 无未回应/积压的 bug（开放 issue 为 0） |
| enhancement_responses | 近 2–12 月回应多数增强请求（SHOULD） | ✅ | 无积压增强请求 |
| report_archive | 报告与回应有公开可搜索归档（MUST，Met URL） | ✅ | GitHub Issues 公开归档 |
| vulnerability_report_process | 在站点发布漏洞提交流程（MUST，Met URL） | ✅ | `SECURITY.md` |
| vulnerability_report_private | 支持私密漏洞报告及保密渠道（MUST，可 N/A，Met URL） | ✅ | SECURITY.md：GitHub Private Security Advisories / 邮箱 |
| vulnerability_report_response | 近 6 月漏洞首次响应 ≤14 天（MUST，可 N/A） | ➖ | **N/A 理由**："该时间段内未收到漏洞报告。" |

## 三、Quality 质量

| ID | 要求（级别） | 状态 | 依据 / 填写内容 |
|---|---|---|---|
| build | 若需构建，提供可自动重建的工作构建系统（MUST，可 N/A） | ✅ | hatchling 构建后端，`uv build` / pip 安装 |
| build_common_tools | 使用常见构建工具（SUGGESTED，可 N/A） | ✅ | 标准 Python 打包（hatchling + uv） |
| build_floss_tools | 仅用 FLOSS 工具即可构建（SHOULD，可 N/A） | ✅ | 工具链全 FLOSS |
| test | 至少一个 FLOSS 自动测试套件，并说明如何运行（MUST） | ✅ | pytest 套件，**1 881 项（1 876 通过 + 5 跳过，全部离线、不依赖网络与密钥）**；CI 与 README/CONTRIBUTING 说明 `uv run pytest` |
| test_invocation | 测试套件可以该语言的标准方式调用（SHOULD） | ✅ | `pytest` / `uv run pytest` |
| test_most | 套件覆盖多数分支/输入/功能（SUGGESTED） | ✅ | 行覆盖率 **94.37%**（2026-10-25 本地全量实测：8 017 语句 / 451 未覆盖；CI 门禁 90） |
| test_continuous_integration | 实施持续集成（SUGGESTED） | ✅ | GitHub Actions（多版本矩阵） |
| test_policy | 有"新功能须加测试"的策略（MUST） | ✅ | CONTRIBUTING / AGENTS 纪律："改行为必须同步加/改测试" |
| tests_are_added | 最近重大改动有遵守加测试的证据（MUST） | ✅ | 近期 compaction / 沙箱 / 动态发现源均带对应测试 |
| tests_documented_added | 加测试策略写进变更提案说明（SUGGESTED） | ✅ | CONTRIBUTING.md |
| warnings | 启用警告标志/安全模式/linter（MUST，可 N/A） | ✅ | ruff + mypy |
| warnings_fixed | 处理警告（MUST，可 N/A） | ✅ | ruff 全范围干净、mypy 零错误 |
| warnings_strict | 尽可能严格（SUGGESTED，可 N/A） | ✅ | ruff 全范围 + mypy + 覆盖率门禁 + CodeQL |

## 四、Security 安全

| ID | 要求（级别） | 状态 | 依据 / 填写内容 |
|---|---|---|---|
| know_secure_design | 至少一名主开发者懂安全软件设计（MUST） | ✅ | 自评声明：开发者设计并实现了权限三档、沙箱隔离、密钥不入库、参数化 SQL、`hmac.compare_digest`、SECURITY 策略（即安全设计落地证据） |
| know_common_errors | 主开发者懂本类软件常见漏洞及至少一种缓解（MUST） | ✅ | 项目对注入（参数化查询）、越权（权限闸/白名单）、凭据泄露（.env 不入库/密钥扫描）、代码执行（沙箱 SPI）均有缓解 |
| crypto_published | 默认仅用公开评审过的加密协议/算法（MUST，可 N/A） | ➖ | **N/A**：本软件不实现或协商加密协议；仅经标准库做 HMAC 校验 |
| crypto_call | 调用专门加密库、不自实现加密（SHOULD，可 N/A） | ➖ | **N/A**：核心用途非加密；需要处调用 Python 标准库 `hmac`/`secrets` |
| crypto_floss | 依赖加密的功能可用 FLOSS 实现（MUST，可 N/A） | ➖ | **N/A**：不自带加密功能；Python 标准库本身为 FLOSS |
| crypto_keylength | 默认密钥长度满足 NIST 至 2030（MUST，可 N/A） | ➖ | **N/A**：无密钥长度协商/自管密钥长度场景 |
| crypto_working | 默认不依赖破损算法/不当模式（MUST，可 N/A） | ➖ | **N/A**：不使用加密算法；HMAC 比较不依赖破损哈希 |
| crypto_weaknesses | 不依赖已知严重弱点算法/模式（SHOULD，可 N/A） | ➖ | **N/A**：不使用 SHA-1/CRC 等 |
| crypto_pfs | 密钥协商实现前向保密（SHOULD，可 N/A） | ➖ | **N/A**：无密钥协商 |
| crypto_password_storage | 若为外部用户存密码，须加盐迭代哈希（MUST，可 N/A） | ➖ | **N/A**：不存储用户登录密码；API Key 以 HMAC 方式比对、非密码哈希落库 |
| crypto_random | 用 CSPRNG 生成密钥/nonce（MUST，可 N/A） | ✅ | 随机标识经 `uuid.uuid4`（底层 `os.urandom`，CSPRNG） |
| delivery_mitm | 交付机制抗 MITM（MUST） | ✅ | PyPI 经 HTTPS + OIDC Trusted Publishing；仓库 HTTPS |
| delivery_unsigned | 不得经 HTTP 取哈希且不验签（MUST NOT） | ✅ | 无此做法 |
| vulnerabilities_fixed_60_days | 无中高危公开漏洞超 60 天未修（MUST） | ✅ | CodeQL 0 alerts、依赖审计无未修中高危 |
| vulnerabilities_critical_fixed | 快速修复严重漏洞（SHOULD） | ✅ | 无已知严重漏洞 |
| no_leaked_credentials | 公开仓库不泄露有效私钥/密码（MUST） | ✅ | 仓库密钥扫描仅见测试假 key（`test-key`）；`.env` 不入库 |

## 五、Analysis 分析

| ID | 要求（级别） | 状态 | 依据 / 填写内容 |
|---|---|---|---|
| static_analysis | 重大发布前应用静态分析工具（超出编译器/linter）（MUST，可 N/A/Met 理由） | ✅ | CodeQL（SAST）+ mypy；在发布流程中 |
| static_analysis_common_vulnerabilities | 工具含查常见漏洞的规则（SUGGESTED，可 N/A） | ✅ | CodeQL 安全规则集 |
| static_analysis_fixed | 静态分析发现的中高危漏洞及时修（MUST，可 N/A） | ✅ | CodeQL 0 alerts，无待修项 |
| static_analysis_often | 每次提交或每日做静态分析（SUGGESTED，可 N/A） | ✅ | CodeQL 在 push/PR 触发 + 每周一定时 |
| dynamic_analysis | 重大发布前用动态分析工具（SUGGESTED） | ✅ | **Met**：`fuzzing/` 下 4 个 atheris（libFuzzer）target——`fuzz_features.py`、`fuzz_schema.py`、`fuzz_compaction.py`、`fuzz_sandbox_gate.py`；CI `fuzz` job（Ubuntu / py3.12）每次运行，每个 target 限时 45 秒，覆盖内核里直接面对不可信输入的纯函数与随包发布的沙箱 AST 闸门。 |
| dynamic_analysis_unsafe | 内存不安全语言用动态工具查内存问题（SUGGESTED，可 N/A） | ➖ | **N/A**：Python 为内存安全语言，无 C/C++ 产物 |
| dynamic_analysis_enable_assertions | 动态分析配置启用大量断言（SUGGESTED） | ✅ | **Met**：每个 target 同时断言一组不变量（分桶值合法、`key` 固定 9 元组、根节点类型形状保持、区间满足 `0 <= start < end <= n` 等），违反即报错退出。 |
| dynamic_analysis_fixed | 动态分析发现的中高危及时修（MUST，可 N/A） | ✅ | **Met**：fuzz 实测撞出过深嵌套属性链让 `RecursionError` 穿透代码工具沙箱闸门，现已在闸门内转为拒绝并固化为用例（`tests/test_sandbox_static_gate.py`）；v0.21.0 还修掉了 fuzz 撞出的预言机 NaN 恒等陷阱。 |

---

## 六、Silver / Gold 差距（暂不申请，仅备忘）

- **Silver** 在 Passing 基础上新增：必须有**第二人**审查重要变更（`silver 级 code_review`：至少一个非作者审核者）、加密相关更严格、必须用能查常见漏洞的静态分析等。单人项目结构性卡在"多人审查"，**不建议为拿 Silver 找挂名审查**。
- **Gold** 进一步要求：双人审查且至少一人外部、安全加固、完整重放/回滚、更严格测试覆盖等。
- 建议：**Passing 已于 2026-10-10 获得**；Silver/Gold 待真正出现外部贡献者后自然达成，不做流程表演。

## 七、申请后动作

1. 获得 Passing Badge 后，把官方提供的徽章 Markdown/URL 放到 `README.md` 顶部徽章区；
2. 在 `CHANGELOG.md` 记录"获得 OpenSSF Best Practices Passing Badge"；
3. 此条同时改进 OpenSSF Scorecard 的 **CII-Best-Practices** 计分项：获得 Passing 徽章后，2026-10-10 的 Scorecard 跑分（commit f052359）已由 0 分转为 **5 分**（Passing 档计 5 分，Silver/Gold 才对应更高分，并非满分）；同次跑分总分由 6.1 升至 **6.5**，**CI-Tests** 项也随已合并 PR 的 CI 全绿转为 10 分。

---

## 八、复核记录（2026-10-25）

| 项 | 复核前（2026-10-10 自评时） | 复核后（2026-10-25 本地实测） |
|---|---|---|
| 自评基线 | main v0.8.0 | main v0.22.0（commit a5dacd6） |
| 测试规模 | ~1 584 项 | **1 881 项**（1 876 通过 + 5 跳过），75.54s，exit 0 |
| 行覆盖率 | 约 92.6% | **94.37%**（8 017 语句 / 451 未覆盖；CI 门禁 90） |
| 讲义规模 | `docs/walkthrough/00–27` | `00–55`（55 篇逐行讲义 + 00-index） |
| 发布 tag | v0.7.0、v0.8.0 | 17 个发布 tag：v0.7.0 … v0.22.0（含 v0.8.1） |
| 动态分析（SUGGESTED） | ⚠ Unmet：未部署 fuzzer/DAST | ✅ Met：atheris fuzzing 4 个 target + CI `fuzz` job（Ubuntu/py3.12，每个 target 限时 45 秒）；已有 fuzz 撞出缺陷并修复的实例 |
| 提交活跃度 | 建库 2026-09-09 | 403 次提交，2026-10-25 仍有合并；GitHub 开放 issue/PR 计数 0 |

未变的判定与边界：

- **徽章状态不变**：Passing（#15360，2026-10-10 获得）。本次只把**仓库内**自证文本校准到现状，
  **未在 bestpractices.dev 重新提交**——平台提交需本人 OAuth 账号，属外部写入，本轮未执行。
- **Silver / Gold 仍卡在多人审查**：单人项目缺第二审查者，不为拿徽章找挂名审查。
- **Discussions 仍未开启**：Issue + PR 已满足 `discussion` 条；是否开 Discussions 属维护决策，
  本轮明确不做。
- **本文件不构成新的对外承诺**：第三方评分以平台复扫结果为准；自评结论不替代复扫。
