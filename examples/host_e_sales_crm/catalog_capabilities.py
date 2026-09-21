"""host_e 按需能力目录：默认不注册，命中"能力缺口"时才被发现。

这些函数刻意**不**放在 ``SalesCrm`` 业务对象上，因此 ``AgentCore.auto(crm)``
启动时不会内省注册它们；它们只登记进 :class:`StaticCatalog`。

闭环：数字员工感知到现有工具不足以完成任务（capability_missing）→
内核用缺口描述 + 原始任务匹配目录关键词 → 命中才把候选构造成 ToolSpec 注册
（tool_discovered）→ 本轮即可调用；执行时仍走统一权限策略，
**能被发现不等于被授权执行**。

目录里没有的能力永远不会被发现——宿主把哪些函数放进目录，本身就是一层授权。
"""

from __future__ import annotations

from yai_core import DiscoveredCandidate, StaticCatalog

# 固定城市天气，保证演示稳定；未列出的城市用确定性映射兜底（全程不联网）。
_WEATHER_TABLE: dict[str, dict] = {
    "上海": {"weather": "多云", "temp_c": 24, "rain": False},
    "湛江": {"weather": "晴", "temp_c": 27, "rain": False},
    "北京": {"weather": "晴", "temp_c": 22, "rain": False},
    "广州": {"weather": "阵雨", "temp_c": 26, "rain": True},
    "深圳": {"weather": "雷阵雨", "temp_c": 27, "rain": True},
    "杭州": {"weather": "阴", "temp_c": 23, "rain": False},
    "南京": {"weather": "多云", "temp_c": 22, "rain": False},
    "成都": {"weather": "小雨", "temp_c": 21, "rain": True},
}

# 未在表内的城市按名称散列到固定天气，保证同一城市每次结果一致。
_FALLBACK_WEATHER = (("晴", False), ("多云", False), ("阴", False),
                     ("小雨", True), ("阵雨", True))


def get_visit_weather(city: str) -> dict:
    """查询客户所在城市的天气，并给出外勤拜访建议（是否带伞、是否适宜上门）。"""
    city = (city or "").strip() or "本地"
    info = _WEATHER_TABLE.get(city)
    if info is None:
        seed = sum(ord(ch) for ch in city)
        weather, rain = _FALLBACK_WEATHER[seed % len(_FALLBACK_WEATHER)]
        info = {"weather": weather, "temp_c": 20 + seed % 8, "rain": rain}
    if info["rain"]:
        advice = "有降雨，建议带伞，并提前和客户确认行程是否改期"
    else:
        advice = "天气适宜，适合上门拜访"
    return {
        "city": city,
        "weather": info["weather"],
        "temp_c": info["temp_c"],
        "rain": info["rain"],
        "advice": advice,
    }


def calc_quote_with_tax(amount: float, rate: float = 13.0) -> dict:
    """按增值税率计算含税报价：不含税金额、税额、价税合计（金额单位：元）。"""
    amount = round(float(amount), 2)
    rate = float(rate)
    if amount < 0:
        return {"ok": False, "error": "金额不能为负"}
    tax = round(amount * rate / 100, 2)
    return {
        "amount_excl_tax": amount,
        "tax_rate_pct": rate,
        "tax": tax,
        "amount_incl_tax": round(amount + tax, 2),
    }


# 候选只登记一次；目录是只读的，可在多次会话间共享。
CANDIDATES = (
    DiscoveredCandidate(
        get_visit_weather,
        ("天气", "气温", "下雨", "带伞", "阵雨", "雷阵雨", "小雨", "外勤", "weather"),
        description="查询客户所在城市天气并给出外勤拜访建议（按需启用）",
    ),
    DiscoveredCandidate(
        calc_quote_with_tax,
        ("含税", "税率", "税点", "增值税", "价税合计", "不含税", "发票", "tax"),
        description="按税率计算含税报价（不含税金额/税额/价税合计，按需启用）",
    ),
)


def build_catalog() -> StaticCatalog:
    """构造 host_e 的进程内静态能力目录。"""
    return StaticCatalog(CANDIDATES)


def catalog_summary() -> list[dict]:
    """供前端展示"按需能力"：名称 + 一句话说明（不暴露完整 schema）。"""
    return [
        {
            "name": cand.spec_name(),
            "description": cand.description or (cand.fn.__doc__ or "").strip().splitlines()[0],
        }
        for cand in CANDIDATES
    ]
