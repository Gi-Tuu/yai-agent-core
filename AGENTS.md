# AGENTS.md — 给 AI 协作者的项目约定

## 环境与命令纪律

- **Shell 一律使用 PowerShell 7（pwsh）**，不要用 Windows PowerShell 5.1。命令行中调用时写
  `pwsh -NoProfile -Command '...'`（外层用单引号，避免 `$` 变量被外层 shell 提前展开）。
- pwsh 继承当前工作目录（即项目根），不要在 `-Command` 里再 `Set-Location`（嵌套引号会被吃掉）；
  确需切目录时用 `Set-Location -LiteralPath '路径'`。
- 项目根目录含空格：`D:\YAI Agent Core`，在 pwsh 命令内引用路径一律加单引号。
- Python 解释器固定用项目 venv：`.\.venv\Scripts\python.exe`；包管理用 `uv`，不要用 pip 直接装。
- 常用命令：
  - 全量检查：`uv run ruff check src tests examples scripts`
  - 测试：`uv run pytest`（测试不允许依赖网络或 API Key）
  - 三宿主冒烟：`.\.venv\Scripts\python.exe scripts\smoke_test.py`
  - 本地 API：`uv run uvicorn scripts.serve_example:app --host 127.0.0.1 --port 8000`（用完即停）
- **临时测试脚本/临时服务用完立刻清理**（文件删除、后台进程停止、端口释放），不留在仓库里。
- `.env` 含真实 Key，永远不提交、不在输出中回显；模板是 `.env.example`。

## 架构红线（详见 CONTRIBUTING.md）

1. 内核本体（`src/yai_core/`）零第三方硬依赖，`pyproject.toml` 的 `dependencies` 保持为空；
   openai / fastapi 等只进可选依赖并在模块内懒加载。
2. 外部能力一律走 `spi/` 四个契约（model / channel / memory / policy）。
3. 事件流唯一出口：执行方收集事件 → `AgentLoop` 统一 yield → `AgentCore.astream` 唯一 emit。
4. 每个自适应决策必须发 `AgentEvent`，禁止静默决策。
5. v0.1 不做：MCP Server、Multi-Agent、自进化写工具、向量记忆、内置 UI、coding agent。

## 工作流约定

- 提交信息用 Conventional Commits（feat/fix/docs/test/refactor/chore）。
- 改行为必须同步加/改测试，并同步 `docs/walkthrough/` 对应篇目。
- 讲义构建：`.\.venv\Scripts\python.exe scripts\build_walkthrough_docx.py`（产物在 gitignore 的 `docs/exports/`）。
