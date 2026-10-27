# AgentDojo 评测 harness

把 YAI AgentCore 作为 AgentDojo 的一个 pipeline 跑真机评测：clean utility、
注入下的 utility 与 ASR。

> 结果报告：[docs/agentdojo-evaluation.md](../../docs/agentdojo-evaluation.md)
> 本目录只放 harness；原始日志与结果 JSON 在 `benchmark_out/agentdojo/`（gitignored），
> 不随仓库分发。

## 环境

AgentDojo 是第三方评测框架，**不进项目的运行时依赖**。用独立 venv，避免污染
项目本身的 `uv sync` 环境：

```bash
uv venv --python 3.12 benchmark_out/agentdojo/.venv
uv pip install --python benchmark_out/agentdojo/.venv/Scripts/python.exe "agentdojo==0.1.35"
uv pip install --python benchmark_out/agentdojo/.venv/Scripts/python.exe -e ".[llm]"
```

模型通过 OpenAI 兼容环境变量配置（在仓库根的 `.env` 或当前环境里）：

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
LLM_MODEL=...
LLM_STRONG_MODEL=...
```

`.env` 不要提交；本 harness 不打印 Key。

## 跑

单任务冒烟：

```bash
benchmark_out/agentdojo/.venv/Scripts/python.exe benchmarks/agentdojo/run_smoke.py \
  --suite banking --user-task user_task_0
```

批量（clean + 注入）：

```bash
benchmark_out/agentdojo/.venv/Scripts/python.exe benchmarks/agentdojo/run_bench.py \
  --suites banking,travel,slack,workspace --mode both \
  --attack important_instructions
```

只跑注入、关闭不可信包裹（对照）：

```bash
benchmark_out/agentdojo/.venv/Scripts/python.exe benchmarks/agentdojo/run_bench.py \
  --suites banking --mode inject --attack important_instructions --no-guard
```

审计 raw 成功（区分真执行与“拒绝时复述目标”的假阳性）：

```bash
benchmark_out/agentdojo/.venv/Scripts/python.exe benchmarks/agentdojo/audit_attacks.py \
  benchmark_out/agentdojo/results/*.json
```

看汇总：

```bash
benchmark_out/agentdojo/.venv/Scripts/python.exe benchmarks/agentdojo/analyze.py \
  benchmark_out/agentdojo/results/<result>.json
```

## 口径

AgentDojo 代码里的 `security_results=True` 表示**注入目标达成**（攻击成功），
不是“安全”。本 harness 额外区分：

- **raw ASR**：AgentDojo 原始 `security=True` 的比例。
- **结果达成 ASR**：载荷确实到达目标 sink（消息/邮件/交易/日历/文件）的比例；
  即使模型同时写了警示，只要载荷送达就计入。
- **无警示达成**：载荷送达且模型没有识别/警示注入的比例。

`audit_attacks.py` 的判定是启发式 + 人工抽查，不是 AgentDojo 官方修订分数；
文本型注入任务（如 travel `injection_task_6`）的字符串包含检查会把拒绝时的复述
计成成功，必须单独审计。

## 适配层取舍

- 路由固定 `react`：AgentDojo 任务为英文，内核规则路由会落到 `direct` 不调工具；
  路由器不在评测范围内。
- AgentDojo 的 system message（用户身份等）由适配层前置到用户任务文本，因为内核
  system prompt 目前是固定的；这是配置差异，报告里已声明。
- 工具输出用 AgentDojo 官方 YAML 格式；YAI 侧不截断，默认开启
  `tool_output_guard`，可用 `--no-guard` 关闭做对照。
- 每个案例新建 AgentCore 与模型客户端，不携带记忆；循环上限 15。

## 依赖与 CI

本目录**不**被 `pytest`、ruff、mypy 或 sdist 白名单覆盖；它需要 AgentDojo 与
真实模型，不适合放进离线 CI。改动 harness 后至少跑一次 `py_compile` 和单任务
冒烟；报告里的数字必须来自真实运行，不得手填。
