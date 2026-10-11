"""端到端评测的 golden task（人工标注的期望行为）。

一个 golden task 描述"给定用户输入，一个**正确的 Agent** 应该怎么做"：
该调用哪些工具、绝不能碰哪些工具、最终要交付哪些可核验的事实凭证。
它不包含模型的具体回复（那是脚本模型 / 真实模型的事），只包含**判分标准**。

设计原则：
- 全部字段是简单的字符串 / 字符串列表，可 JSON 序列化，便于从文件加载或在代码里声明；
- 判分口径尽量保守、可辩护——我们只检查能从事件流客观验证的东西，
  不试图"猜"模型内心是否产生了幻觉。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class GoldenTask:
    """一条带标注的端到端任务。

    各字段含义：

    - ``id``：任务唯一标识（如 ``react_customer_lookup``），报告里用它定位；
    - ``task``：用户的原始输入（会原样送进 ``AgentCore.astream``）；
    - ``expected_tools``：**合理调用集合**。一次正确执行实际调用的工具应落在这个集合内，
      用来算工具调用精确率（多调了集合外的工具会扣分）；
    - ``must_call``：**必须调用子集**。这些工具少调任何一个都算未完成，用来算召回率；
    - ``forbidden_tools``：**禁调工具**（如删除、对外发送等高危 / 越界操作），
      一旦调用即记一次越权（overreach），不论任务是否完成；
    - ``expected_evidence``：最终答复应交付、且应由工具结果支撑的**事实凭证**
      （如客户名、订单号、金额）。凭证既没出现在最终答复里（漏交付），
      或只出现在答复里却没有任何成功工具结果包含它（无支撑断言），都会被标记；
    - ``should_complete``：该任务是否应当成功完成。默认 ``True``；
      留给"信息不足应澄清 / 高危应被权限拦截"这类**期望它不硬完成**的负面任务；
    - ``note``：可选备注，给读评测集的人看，不参与判分。
    """

    id: str
    task: str
    expected_tools: list[str] = field(default_factory=list)
    must_call: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    expected_evidence: list[str] = field(default_factory=list)
    should_complete: bool = True
    note: str = ""

    def to_dict(self) -> dict:
        """转成可 JSON 序列化的字典。"""

        return asdict(self)
