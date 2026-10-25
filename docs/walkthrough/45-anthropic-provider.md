# 逐行讲解 45 · AnthropicProvider：换一个模型接口要翻译掉哪四件事

> 第 08 篇讲过 `llm/openai_compat.py`：一个真实模型后端只要实现 `achat(messages, tools, *, tier)`。
> 这一篇是**同一个契约的第二个实现**——`llm/anthropic.py`。宿主侧仍然只是换一个 provider 对象，
> 内核一行不改；所有差别都在这一个适配器里消化掉。

## 0. 为什么不"再兼容一层 OpenAI 形状"

Claude 的 Messages API 与 OpenAI 的 chat/completions 不是同一套线格式，硬套会写出半吊子适配器。
四个必须落地的差异，每一个都能单独把请求打回 400：

| 差异 | OpenAI 侧 | Anthropic 侧 | 落在哪个函数 |
|---|---|---|---|
| 系统提示 | `messages` 里一条 `role:"system"` | **顶层 `system` 参数**，不在 messages 里 | `split_system` |
| 输出上限 | `max_tokens` 可选 | **必填** | `AnthropicProvider.max_tokens`（显式参数 > 环境变量 > 4096） |
| 工具调用 | assistant 的 `tool_calls` 字段 | assistant 内容里的 **`tool_use` 块** | `_tool_use_blocks` |
| 工具结果 | `role:"tool"` 的独立消息 | **`user` 消息里的 `tool_result` 块** | `to_anthropic_messages` |

还有一条形状差异：内核喂给模型的工具目录是 `{"type":"function","function":{...}}`，
Anthropic 要 `{"name","description","input_schema"}` —— 见 `to_anthropic_tools`。

## 1. 上下文翻译：`to_anthropic_messages`

内核的 `Context.llm_messages()` 产出的是 OpenAI 线格式，适配器按角色分派：

```
system    → 抽走，交给顶层参数（多条合并成一段，空条目丢掉）
user      → [{"type":"text","text": ...}]
assistant → 文本块（非空才留） + 每个 tool_call 一个 tool_use 块
tool      → 转成 user 消息里的一个 tool_result 块（带原 tool_use_id）
```

两个容易漏的细节，都有测试钉着：

- **相邻同角色必须合并**。一次模型调用里出两个工具，就会有两条 `role:"tool"` 结果；
  它们必须合成**一条 user 消息、两个 tool_result 块**，而不是两条 user 消息。
- **不生成空文本块**。assistant 只调工具时 `content` 是空串，塞一个空 text 块进去会被上游拒绝；
  所以空文本直接跳过，只留 `tool_use`。

这里翻译的是已有上下文；历史工具参数解析失败时仍按 `{}` 序列化，不转发坏 JSON。
这不代表允许执行非法模型响应：新响应在下面的 `parse_response` 边界严格校验。

## 2. 响应解析：`parse_response`

响应内容是一串块，本模块只认两种：`text` 拼接成 `content`，`tool_use` 变
`ToolCallRequest(id, name, input)`。**其它块类型（例如思考块）忽略但保留在 `raw` 里**——
不在这里猜测语义，也不丢原始响应。

`tool_use.input` 必须是字典；缺失、`null`、数组和标量会抛出不含原始参数的 `ValueError`，
不再替换成 `{}` 执行默认值。显式空对象合法；整批响应解析成功后才交给内核执行。

token 用量映射成 `TokenUsage(input_tokens → prompt, output_tokens → completion, total=两者相加)`；
后端没给 usage 时 `usage=None`，**内核不臆造数字**，此时任务级 token 预算不生效、
自动退回按次数兜底（与 OpenAI 适配器同一口径）。

## 3. 能力标记与档位

```python
class AnthropicProvider:
    yai_live_router = True          # 供 llm_router="auto" 识别：这是真联网后端
```

档位沿用内核的 standard/strong 两档：`model` / `strong_model`（后者缺省等于前者），
`tier="strong"` 时换强模型——路由与成本档位这套机制不关心对面是谁家接口。

## 4. 依赖边界与测试口径

- SDK 只在构造函数里懒加载；没装时抛**带安装指引**的 `ImportError`
  （`uv sync --extra anthropic`），而不是裸 ImportError。
- 内核 `dependencies` 仍为 `[]`：`anthropic` 只进可选 extra，与 `llm`（OpenAI 兼容）并列。
- 测试全离线：`tests/test_anthropic_provider.py` 用假 `anthropic` 模块（`sys.modules` 注入）
  与假客户端，断言的是**发出去的请求长什么样**——system 在顶层、max_tokens 必填、
  工具结果挂在 user 消息上、tools 是 `name/input_schema` 形状；另有一条端到端用例
  跑完"模型调工具 → 宿主函数执行 → 结果回灌 → 二次请求带 tool_result"。
- API Key 只从参数或环境变量读（`ANTHROPIC_API_KEY`），不落盘、不进日志。

## 5. 现在还不做什么（边界）

1. **不做流式**：`achat` 一次性返回，内核照旧在拿到完整响应后发 `MODEL_MESSAGE`；
   接流式要先决定"增量事件怎么进唯一出口"，那是内核级改动，不塞在适配器里。
2. **不做多模态输入**：`content` 目前按纯文本处理，图片/PDF 块要等消息层的 `parts`（任务 M）。
3. **不做提示缓存 / `cache_control`、不做 thinking 参数**：这些是厂商专有扩展，
   等内核有"能力旗标"级别的表达需求再谈，不在通用契约里塞私有字段。
4. **不自动重试**：重试交给 SDK 或外层 `FallbackModelProvider`（后者可传 `max_retries=0` 快速降级）。

## 6. 自检（读完本篇应能回答）

1. 为什么 `system` 不能留在 messages 里？少了这一步会怎样？
2. 一次调用产生两个工具结果时，翻译后的消息数组长什么样？
3. 已有历史的参数翻译与新响应的参数校验有什么区别？为什么不能把非法新响应替换成空对象再执行？
4. 这个 provider 与 OpenAI 那个，在内核眼里有哪几处不同？（答案应该是：没有。）
