# v0.26 审计整改计划（2026-11-03）

> 基准：`feat/v026-runtime-expansion` @ `2acfca5`。审计报告（本会话）发现的 P1/P2/P3 问题，
> 按"先定契约、再改实现、最后统一门禁"的顺序分 5 批修复；刻意**不做**逐条散修，避免
> 改 A 坏 B、或同一根因在多处重复修。

## 0. 修复原则与跨模块契约

1. **事件契约**：不新增 `EventType`（仍 21 类）。失败仍用既有 `ERROR` / `DONE` / 收据表达。
2. **终止保证**：除 `CancelledError` 外，任何被内核捕获的失败都必须让事件流以 `ERROR + DONE`
   收尾；`DONE.verification` 仍在 `DONE` 前计算。
3. **公开 API 仅加性**：`RunResult` 新增 `status` / `error`（默认 `"ok"` / `None`）；
   其它签名默认值不变。
4. **数据隔离 fail-closed**：`owner` 已声明而实例未声明 → 只读降级；保存绝不把已有 owner
   写回 `null`。
5. **迁移幂等**：SQLite 迁移先做列对账再 DDL，DDL 与 `user_version` 同事务；失败分类报错。
6. **收据链按 run**：`ToolExecutor.begin_run()` 在每个任务开始时重置链根；单 run 收据
   `verify_receipt_chain` 必须为 `True`。
7. **配置写入原子化**：Bridge 与 CLI 共用同一原子写（temp + `os.replace` + fsync + `.bak`），
   禁止半截 JSON / 丢更新。
8. **供应链**：Playwright MCP 固定到 `@0.0.84`，不再使用 `@latest`。
9. **行为兼容**：不改磁盘格式、事件名、SPI 形状、sidecar 协议；迁移只做"补齐缺列"。
10. **文档同步**：改行为同步 `docs/walkthrough/`；中英门面（architecture/README 等）同步；
    白皮书数字标注为 v0.25.0 快照。

## 1. 批次与写作用域

| 批次 | 主题 | 主要文件 | 负责人 |
|---|---|---|---|
| Batch 0 | 可信终态与内核契约 | `verification.py`、`kernel/loop.py`、`kernel/context.py`、`kernel/compaction.py`、`core.py`、`config.py`、`types.py` | audit-execution-core |
| Batch 1 | 数据完整性与隔离 | `memory/sqlite_store.py`、`memory/vault.py`、`memory/__init__.py` | audit-data-memory |
| Batch 2 | 执行器治理与收据链 | `tools/executor.py`、`tools/execution_audit.py`、`policy/authorize.py` | Lead 接手（原负责人失败） |
| Batch 3 | 扩展/Bridge/脚本 | `sidecar/native_bridge.py`、`extension/*`、三个 scripts | audit-v026-ext |
| Batch 4 | 发布/CI/文档/守卫 | `pyproject.toml`、`.github/workflows/*`、`tests/test_kernel_dependencies.py`、`tests/test_spi_surface.py`、docs | Lead |

## 2. 问题 → 修复映射

| 审计 ID | 严重度 | 修复动作 | 批次 | 状态 |
|---|---|---|---|---|
| DB-1 | P1 | v1→v2/v0 列对账 + 显式事务 | 1 | 已验证（memory 225 passed） |
| VER-1 | P1 | 异常/非法分支转 `not_verifiable`，全成功才 success | 0 | 已验证（Lead 复现 + 回归） |
| RELEASE-1 | P1 | sdist include `/extension` `/benchmarks` + 缺失文档；CI sdist job；publish 前置 pytest/lint/mypy/lock | 4 | 已验证（本地包内全量 exit 0） |
| CORE-1 | P2 | `as_kwargs` 浅拷贝 | 0 | 已验证（from_config 引用） |
| CORE-2 | P2 | SPI 失败兜底 + `core.astream` 终止保证 | 0 | 已验证（16 条终态回归） |
| CORE-3 | P2 | gate/policy 异常失败关闭为 TOOL_RESULT | 2 | 已验证（runtime contract） |
| CORE-4 | P2 | 空 final_text 不写 assistant；RunResult status/error | 0 | 已验证 |
| DATA-1 | P2 | `tool_calls` 损坏降级 None | 1 | 已验证 |
| DATA-2 | P2 | 读路径不因 touch 落盘失败而失败 | 1 | 已验证 |
| DATA-3 | P2 | 保存前检测外部修改并合并 | 1 | 已验证（双实例回归） |
| DATA-4 | P2 | owner fail-closed + 不抹除 | 1 | 已验证 |
| NB-1/NB-2 | P2 | 终态先于 run_started、崩溃清 run_id、SW 重连 | 3 | 已验证（静态+假写者回归） |
| EXT-1 | P2 | 面板 status 同步、可取消 | 3 | 已验证（静态契约） |
| CFG-1 | P2 | base_url 变更必须带新 Key；限制 scheme | 3 | 已验证 |
| CFG-2 | P2 | 最后面板关闭断 native port；文案改为重启生效 | 3 | 已验证（静态契约） |
| CFG-3 | P2 | 原子配置写 + 备份轮转 | 3 | 已验证（原子写冒烟） |
| SCR-1 | P2 | dry-run 脱敏 | 3 | 已验证 |
| SCR-2 | P2 | CDP pid 校验后再 stop | 3 | 已验证（假 pid 回归） |
| VER-2 | P3 | 证据条数/字节上限；Bridge 截断保留 type/run_id | 0/3 | 部分（Bridge 侧已改；证据上限见遗留） |
| NB-3 | P3 | `_rpc` 超时 + 孤儿响应丢弃 | 3 | 已验证 |
| F5/F6/F9/F10/F11/F12 | P3 | stage 归因、限尾兜底、召回超时、上下文计费、收据链、字段口径 | 0/2 | 已验证（F12 成本口径见遗留） |
| 文档漂移 | P3 | decrypt 语义、Browser 口径、白皮书快照标注、死测试引用 | 4 | 已改 |
| 零依赖/事件守门 | P3 | AST 模块作用域扫描 + fastapi allowlist；事件真实构造点 + 冻结集合 | 4 | 已改，已测 |
| 发布门禁 | P1 | publish 前置门禁、CI sdist job | 4 | 已改，待 CI |

## 3. 暂缓项（已记录，不在本次硬修）

- **god module 拆分**（`loop.py` / `executor.py` / `types.py`）：高风险重构，单独立项，先补行为
  契约测试再拆。
- **浏览器/桌面真实后端自动化**（假 SDK 驱动）：需要较大的假对象工程，作为下一步专项；
  本次只修正文案与元数据过滤等低风险项。
- **浏览器 tabs / 桌面窗口元数据按策略过滤**：涉及产品语义（是否允许模型看到被禁窗口标题），
  需要宿主确认后再改默认行为。
- **跨进程文件锁**：SQLite/Vault 多进程写协议需产品定义（单写者 vs 多写合并），本次只做
  单进程多实例合并 + 冲突检测。
- **压测**（P95/P99、长稳、内存）：需要独立环境与时间，未在本次范围。

## 4. 验收门禁

1. `uv run pytest` 全量通过（含新增回归），覆盖率 ≥ 90%。
2. `ruff check src tests examples scripts fuzzing shell`、`mypy src/yai_core`、`uv lock --check`。
3. `tests/test_kernel_dependencies.py` 能抓到 `try: import httpx`；`tests/test_spi_surface.py`
   校验 21 个事件都有真实 `AgentEvent(EventType.X)` 发出点。
4. `uv build --sdist` → 解包 → 包内 `pytest -q` exit 0。
5. 安全回归：dry-run 无明文 Key；configure_llm 改 base_url 无新 Key 被拒；
   `verify_receipt_chain(单 run)` 为 True；半迁移库可打开。
6. 文档：中英 architecture 的 decrypt 语义一致；白皮书数字标注 v0.25.0 快照；
   无死测试文件引用。


## 5. Follow-up（2026-11-03，PR 合并后收尾）

| 问题 | 修复 | 验证 |
|---|---|---|
| F12 成本口径 | `before_model_call` 计尝试、`record_model_call` 只累计 token；loop 计数收敛到 `_model_chat`（含 replan）；所有 DONE 固定成本键 | `test_budget.py` / `test_loop_runtime_contract.py`；全量通过 |
| VER-2 证据上限 | 50 条 / detail 4k / 总 64KB，超限折叠 + `truncated` 标注 | `test_outcome_verification.py` |
| VER-2 Bridge 截断 | 截断帧保留 `params.type/run_id/status/error` 与顶层 `event_type/run_id`；扩展识别并收尾 | `test_native_bridge.py` / 扩展静态契约 |
| 多进程 vault 丢更新 | `<storage>.lock` 跨进程 + 进程内锁；`_save` 临界区覆盖检测/合并/原子写；拿不到锁降级并诊断 | `test_memory_vault.py`（spawn 双进程 10 条全保留） |
| 适配器元数据泄漏 | tabs/windows 按策略过滤，只回 `hidden_count` | `test_adapter_metadata_filter.py` |
| 真后端无回归 | 假 playwright / pywinauto SDK 覆盖真实后端代码路径 | `test_playwright_backend_fake_sdk.py` / `test_pywinauto_backend_fake_sdk.py` |
| 真机验收 | 面板重开取消、强杀 sidecar 重连、关面板重启 Bridge | 真实 Edge 实测记录见 `docs/guides/browser-extension-bridge.md` |

残留：宿主 verifier 自返回的 evidence 未套上限（避免改验收结论）；advisory 文件锁
不约束绕过 MemoryVault 的写入；真实鼠标点工具栏图标与安装包分发仍需人工。
