# Fuzzing（atheris）

对内核里**直接面对不可信输入的纯函数**做持续模糊测试。这些 target 不是 pytest
用例（不放在 `tests/`），而是基于 [atheris](https://github.com/google/atheris)
（Python 的 libFuzzer 封装），由 CI 的 `fuzz` job 限时运行。

## Targets

| 文件 | 被测函数 | 为什么 fuzz |
|---|---|---|
| `fuzz_features.py` | `TaskFeatures.from_task` / `.key` | 任意、可能含畸形 Unicode 的自然语言；正则、`lower`、`strip`、子串命中 |
| `fuzz_schema.py` | `sanitize_schema` | MCP/OpenAPI 喂入的任意深度、任意键型的 JSON Schema；递归清洗 |
| `fuzz_compaction.py` | `user_round_starts` / `select_compaction_range` / `render_for_summary` | 随机消息序列下的轮次边界、recent 窗口对齐与渲染 |
| `fuzz_sandbox_gate.py` | 代码工具沙箱的 AST 静态闸门（`examples/host_g_sandbox/_worker.py` + `shell/desktop/_sandbox_worker.py`） | 输入完全由模型（及上游 prompt injection）控制的代码文本；拒绝规则、递归深度、两份实现漂移 |

> 前三条测的是内核里的纯函数，最后一条测的是**随仓库发布的宿主/产品层沙箱闸门**——
> 内核不内置代码执行器（`ToolSandbox` 只定义契约），但这两份 worker 是真会被人拿去跑
> 模型生成的代码，所以按不可信输入对待。同一个 target 会比对两份实现，漂移即失败。
> 深嵌套属性链曾让 `RecursionError` 穿透闸门（现已在闸门内转成拒绝），就是这条 target
> 的用例化证据；Windows 上跑不了 atheris，那条case固定在 `tests/test_sandbox_static_gate.py`。

每个 target 同时断言了一组**不变量**（分桶值合法、`key` 固定 9 元组、根节点类型
形状保持、区间满足 `0 <= start < end <= n` 等），fuzz 一旦发现违反即报错。

## 平台限制（重要）

`atheris` 在 PyPI 只发布 **Linux x86_64 + CPython 3.12 / 3.13 / 3.14** 的 wheel；
**没有 Windows、macOS、cp311 的预编译包**，本机 Windows 无法 `pip install atheris`。
因此：

- fuzz 只在 CI 的 **Ubuntu / py3.12** 上跑；
- 依赖通过 `fuzz` optional extra 安装，并用环境 marker 限定，其它平台 `uv sync`
  不会尝试安装 atheris；
- 这些 target 不被 pytest 收集（放在 `fuzzing/`，`testpaths = tests`）。

## 运行

CI（`fuzz` job，每个 target 限时 45 秒）：

```bash
uv sync --extra fuzz
for t in fuzzing/fuzz_*.py; do uv run python "$t" -max_total_time=45; done
```

在受支持的 Linux 环境本地长跑：

```bash
uv sync --extra fuzz
uv run python fuzzing/fuzz_features.py -max_total_time=600 -print_final_stats=1
# 可加语料目录、-jobs=N 并行等 libFuzzer 参数
```

## 与 OpenSSF Scorecard 的关系

Scorecard 的 `Fuzzing` 检查对 Python 项目会静态扫描仓库中的 `import atheris`；
本目录让该检查有真实、可运行的 target 支撑，而不是只放一个空 import 来"刷分"。
后续若需要更强的持续 fuzz，可再接入 ClusterFuzzLite / OSS-Fuzz（同一批 target）。
