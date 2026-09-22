"""自校准路由离线 benchmark 任务集（60 条，全部人工标注 ground-truth）。

每条 ``BenchTask`` 标注该任务"理想路由"应选的策略：
- direct：闲聊 / 写作 / 解释 / 翻译，不需要宿主工具；
- react：单步查询或一次工具调用即可完成；
- plan：明确多步骤，需要先规划再执行；
- clarify：信息不全，应先反问澄清。

任务集刻意混入约 10 条"规则路由会判错"的长尾样本（英文 / 隐性工具意图、
含行动词的创作请求、对象不清的伪指令），为自校准学习制造可纠正的空间。
这是 benchmark 的学习信号来源，不是产品缺陷的掩饰——规则在典型中文任务上
本来就很准，学习的价值集中在长尾。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchTask:
    """一条带 ground-truth 的 benchmark 任务。"""

    task: str
    gt: str  # direct / react / plan / clarify
    note: str = ""


# 每个策略 15 条，共 60 条。note 标注"反例"的是规则路由预期判错的长尾。
TASKS: list[BenchTask] = [
    # ------------------------------------------------------------- direct (15)
    BenchTask("你好", "direct", "闲聊"),
    BenchTask("用一句话介绍你自己", "direct", "自我介绍"),
    BenchTask("写一段欢迎新用户的开场白", "direct", "写作"),
    BenchTask("给我讲个轻松的笑话", "direct", "闲聊创作"),
    BenchTask("用大白话解释什么是人工智能", "direct", "解释"),
    BenchTask("帮我润色这句话，让它更礼貌一些", "direct", "写作"),
    BenchTask("写一首关于春天的短诗", "direct", "创作"),
    BenchTask("把这段话翻译成英文", "direct", "翻译"),
    BenchTask("帮我想三个产品命名的方向", "direct", "创作"),
    BenchTask("给我一点面试自我介绍的建议", "direct", "咨询"),
    BenchTask("晚安", "direct", "闲聊"),
    BenchTask("谢谢你的帮助", "direct", "闲聊"),
    BenchTask("解释一下番茄工作法是怎么回事", "direct", "解释"),
    BenchTask("帮我写一条周末爬山的朋友圈文案", "direct", "创作"),
    BenchTask("讲一个查案推理的小故事", "direct", "反例：含'查'字但实为创作"),
    # -------------------------------------------------------------- react (15)
    BenchTask("查一下我上周的日记", "react", "单步查询"),
    BenchTask("搜索笔记里关于 Q3 的内容", "react", "单步查询"),
    BenchTask("列出我所有待办事项", "react", "单步查询"),
    BenchTask("统计华东区订单数量", "react", "单步统计"),
    BenchTask("查一下今天的天气", "react", "单步查询"),
    BenchTask("获取客户王总的联系方式", "react", "单步查询"),
    BenchTask("读取最近一份会议纪要", "react", "单步读取"),
    BenchTask("记录一下今天的心情是平静的", "react", "单步写入"),
    BenchTask("查询订单 A001 的状态", "react", "单步查询"),
    BenchTask("帮我查查本月销售额是多少", "react", "单步查询"),
    BenchTask("Search my notes for Q3", "react", "反例：英文隐性工具意图"),
    BenchTask("List my pending orders", "react", "反例：英文，无中文行动词"),
    BenchTask("Show me today's weather", "react", "反例：英文工具意图"),
    BenchTask("帮我看看上周的日记里写了什么", "react", "反例：'看看'不在行动词表"),
    BenchTask("我那份还没完成的待办现在都有啥", "react", "反例：无显性行动词"),
    # --------------------------------------------------------------- plan (15)
    BenchTask("先查客户，再统计订单，最后整理成报表", "plan", "多步"),
    BenchTask("筛选华东区订单并按区域汇总销售额", "plan", "多步"),
    BenchTask("统计各区域销售额并整理成一份报表", "plan", "多步"),
    BenchTask("先搜索客户资料，然后新建一条跟进记录", "plan", "多步"),
    BenchTask("查一下本月订单，计算总额，再导出成清单", "plan", "多步"),
    BenchTask("列出待办，按优先级排序，然后给我一份今日计划", "plan", "多步"),
    BenchTask("先读取会议纪要，再提炼行动项，最后保存到笔记", "plan", "多步"),
    BenchTask("查询各区域客户数量，对比分析，生成简报", "plan", "多步"),
    BenchTask("汇总本周销售数据并整理成周报", "plan", "多步"),
    BenchTask("把客户按行业分组，统计每组数量，再导出", "plan", "多步"),
    BenchTask("先查库存，再下单补货，最后通知仓管", "plan", "多步"),
    BenchTask("检索上个月的所有订单，分类统计，形成报表", "plan", "多步"),
    BenchTask("先看客户资料，再查他的订单，然后给出跟进建议", "plan", "多步"),
    BenchTask("收集各部门反馈，归类问题，整理成改进清单", "plan", "多步"),
    BenchTask("查一下季度业绩，对比上个季度，写一段总结", "plan", "多步"),
    # ------------------------------------------------------------ clarify (15)
    BenchTask("随便", "clarify", "模糊"),
    BenchTask("你看着办", "clarify", "模糊"),
    BenchTask("什么都行", "clarify", "模糊"),
    BenchTask("帮我弄一下", "clarify", "模糊"),
    BenchTask("那个东西帮我处理下", "clarify", "信息不全"),
    BenchTask("帮我查一下那个", "clarify", "反例：含'查'但对象不清"),
    BenchTask("把相关的内容整理一下", "clarify", "反例：对象不清"),
    BenchTask("那个事情你帮我跟进下", "clarify", "信息不全"),
    BenchTask("随便弄点什么", "clarify", "模糊"),
    BenchTask("帮我搞一下之前那个", "clarify", "信息不全"),
    BenchTask("那个事情你帮我看看吧", "clarify", "信息不全"),
    BenchTask("那个东西你帮我弄一下", "clarify", "信息不全"),
    BenchTask("就之前说的那个，弄一下", "clarify", "信息不全"),
    BenchTask("随便帮我安排安排", "clarify", "模糊"),
    BenchTask("帮我把那边的东西弄好", "clarify", "信息不全"),
]

assert len(TASKS) == 60, f"任务集应为 60 条，当前 {len(TASKS)}"
assert all(item.gt in {"direct", "react", "plan", "clarify"} for item in TASKS)
