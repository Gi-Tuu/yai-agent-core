"""YAI AgentCore × AgentDojo adapter (local benchmark harness, not shipped).

AgentDojo 的 pipeline 是同步的；这里把每次 query 交给 AgentCore 完整跑完，
再把工具轨迹还原成 AgentDojo 的 ChatMessage，供它的 utility/security 检查使用。

设计取舍（写进结果时也要带上）：
- 路由固定 react：AgentDojo 的任务是英文，内核规则路由会落到 direct（不调工具），
  那样测的就不是工具循环。路由本身不在本次评测范围内。
- 工具契约：AgentDojo 的 Function.parameters 是 pydantic 模型，转 JSON Schema 后
  注册进 YAI 工具总线；handler 直接调 runtime.run_function，副作用落在 AgentDojo 的 env。
- 工具输出用 AgentDojo 自己的 tool_result_to_str（YAML），保持与参考基线一致的文本形态。
- 默认开启 YAI 的 tool_output_guard（不可信数据包裹），这是被测防御的一部分；
  可用 --no-guard 跑对照。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.tool_execution import tool_result_to_str
from agentdojo.functions_runtime import FunctionCall, FunctionsRuntime
from agentdojo.logging import Logger
from agentdojo.types import ChatMessage, text_content_block_from_string

from yai_core import __version__ as yai_version
from yai_core.core import AgentCore
from yai_core.kernel.router import RouteDecision
from yai_core.llm.openai_compat import OpenAICompatProvider
from yai_core.types import Strategy, ToolSpec


class PinnedReactRouter:
    """把路由钉死在 react：本次评测的是工具循环与注入防线，不是路由器。"""

    def __init__(self) -> None:
        self.on_diagnostic = None

    def classify(self, task: str, registry: Any) -> Strategy:
        return Strategy.REACT

    async def aclassify(
        self, task: str, registry: Any, *, discoverable: Any = None
    ) -> RouteDecision:
        return RouteDecision(
            Strategy.REACT, "benchmark", "pinned react for AgentDojo", "standard"
        )


@dataclass
class RecordedCall:
    name: str
    arguments: dict[str, Any]
    output: str = ""
    error: str | None = None


@dataclass
class YAIPipeline(BasePipelineElement):
    """把 AgentCore 包成 AgentDojo 的一个 pipeline 元素。"""

    api_key: str
    base_url: str | None
    model: str
    strong_model: str | None = None
    system_context: str = ""
    tool_output_guard: bool = True
    max_iters: int = 15
    model_timeout: float = 120.0
    max_retries: int = 2
    tool_result_char_limit: int | None = None
    name: str = field(init=False)
    last_result: Any = field(default=None, init=False)
    last_recorded: list[RecordedCall] = field(default_factory=list, init=False)
    error_count: int = field(default=0, init=False)
    last_error: str = field(default="", init=False)

    def __post_init__(self) -> None:
        mode = "spotlight" if self.tool_output_guard else "raw"
        self.name = (
            f"yai-agent-core-{yai_version}-react-{mode}-{self.model.split('/')[-1]}"
        )

    def _provider(self) -> OpenAICompatProvider:
        return OpenAICompatProvider(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            strong_model=self.strong_model,
            timeout=self.model_timeout,
            max_retries=self.max_retries,
        )

    def _specs(
        self,
        runtime: FunctionsRuntime,
        env: Any,
        recorded: list[RecordedCall],
    ) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for name, function in runtime.functions.items():
            schema = function.parameters.model_json_schema()

            def handler(_name: str = name, **kwargs: Any) -> str:
                result, error = runtime.run_function(env, _name, dict(kwargs))
                if error:
                    recorded.append(
                        RecordedCall(_name, dict(kwargs), error=error)
                    )
                    raise RuntimeError(error)
                text = tool_result_to_str(result)
                recorded.append(RecordedCall(_name, dict(kwargs), output=text))
                return text

            specs.append(
                ToolSpec(
                    name=name,
                    description=function.description,
                    input_schema=schema,
                    handler=handler,
                )
            )
        return specs

    def _run(self, prompt: str, runtime: FunctionsRuntime, env: Any):
        recorded: list[RecordedCall] = []
        specs = self._specs(runtime, env, recorded)
        task_text = (
            f"{self.system_context}\n\nTask: {prompt}"
            if self.system_context.strip()
            else prompt
        )

        async def _inner():
            model = self._provider()
            core = AgentCore(
                model=model,
                router=PinnedReactRouter(),
                max_iters=self.max_iters,
                model_timeout=self.model_timeout,
                tool_result_char_limit=self.tool_result_char_limit,
                tool_output_guard=self.tool_output_guard,
            )
            core.register_tools(specs)
            try:
                return await core.run(task_text)
            finally:
                await core.aclose()
                await model.client.close()

        return asyncio.run(_inner()), recorded

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Any = None,
        messages: Sequence[ChatMessage] = (),
        extra_args: dict | None = None,
    ):
        try:
            result, recorded = self._run(query, runtime, env)
        except Exception as exc:  # noqa: BLE001 - 单例失败不能中断整轮基准
            self.error_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[pipeline-error] {self.last_error}", flush=True)
            result = type(
                "PipelineFailure",
                (),
                {
                    "final_text": f"[pipeline error] {self.last_error}",
                    "events": [],
                    "strategy": "error",
                },
            )()
            recorded = []
        self.last_result = result
        self.last_recorded = recorded

        conversation: list[ChatMessage] = []
        for index, call in enumerate(recorded, start=1):
            call_id = f"yai_call_{index}"
            ref = FunctionCall(
                function=call.name,
                args=call.arguments,
                id=call_id,
            )
            conversation.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [ref],
                }
            )
            conversation.append(
                {
                    "role": "tool",
                    "content": [text_content_block_from_string(call.output)],
                    "tool_call_id": call_id,
                    "tool_call": ref,
                    "error": call.error,
                }
            )
        conversation.append(
            {
                "role": "assistant",
                "content": [text_content_block_from_string(result.final_text or "")],
                "tool_calls": None,
            }
        )
        # 自定义 pipeline 不经过 AgentDojo 的 ToolsExecutionLoop，需要显式把消息交给
        # TraceLogger，否则落盘日志里 messages 为空，失败无法复盘。
        Logger().get().log(conversation)
        return query, runtime, env, conversation, extra_args or {}
