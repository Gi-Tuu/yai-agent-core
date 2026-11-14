"""AgentLoop 的协议辅助：伪工具调用识别、模型阶段标记、工具输出包裹。

从 ``kernel/loop.py`` 拆出；``loop`` 继续 re-export，旧导入路径保持可用。
"""

from __future__ import annotations

_RAW_TOOL_CALL_NOTICE = (
    "模型返回了未识别的工具调用文本，本次没有执行任何工具。"
    "请重试，或换用支持结构化 function calling 的模型。"
)


def _looks_like_raw_tool_call(text: str) -> bool:
    """识别被模型写成正文的 XML/文本工具调用。

    仅认足够像工具调用的标记组合，避免把普通讲解（只提到 ``<tool_call>``）
    误判；一旦命中就失败关闭，绝不把这段文本当最终答案或尝试解析执行。
    """
    lowered = text.lower()
    if "<arg_key>" in lowered or "<arg_value>" in lowered:
        return True
    if "<function_call" in lowered:
        return True
    return "<tool_call" in lowered and "</tool_call>" in lowered


#: 私有异常标记：这个异常来自"模型调用"（而不是工具/宿主 SPI/内核）。
#: 上层据此给出 ``ERROR.stage = "model"`` 与对应诊断种类，避免把工具治理
#: 故障错报成模型故障（审计 F5）；不改变异常类型，旧调用方照旧 except。
_MODEL_STAGE_ATTR = "_yai_model_stage"


def _mark_model_stage(exc: BaseException) -> None:
    """给模型调用抛出的异常打私有标记；标记失败不影响异常继续传播。"""
    try:
        setattr(exc, _MODEL_STAGE_ATTR, True)
    except Exception:  # noqa: BLE001 - 极少数异常实例不允许写属性
        pass


def _is_model_stage(exc: BaseException) -> bool:
    """该异常是否来自模型调用（见 :func:`_mark_model_stage`）。"""
    return bool(getattr(exc, _MODEL_STAGE_ATTR, False))


def _set_outcome(
    outcome: dict[str, object] | None, status: str, error: str = ""
) -> None:
    """把执行结局写进调用方传入的字典（``_react_cycle`` 的失败出口）。"""
    if outcome is None:
        return
    outcome["status"] = status
    if error:
        outcome["error"] = error


#: 工具输出在上下文里的包裹标记：显式把工具返回值标成"数据"而不是指令。
#: 这是 Spotlighting 类防御的最小实现——不改变工具语义，只改变它进 prompt 的形态。
_UNTRUSTED_TOOL_OPEN = '<tool_output untrusted="true">'
_UNTRUSTED_TOOL_CLOSE = "</tool_output>"


def wrap_tool_output(text: str) -> str:
    """把工具返回值包成不可信数据块，降低间接提示注入的成功率。"""
    return _UNTRUSTED_TOOL_OPEN + "\n" + text + "\n" + _UNTRUSTED_TOOL_CLOSE
