"""host_j 暴露给 Core 的记账能力（工具）。

工具按"默认只读 / 需授权写入 / 按需发现"三层组织，正好对应权限三档与能力
发现：

- **默认只读工具**：宿主原生就有，列入权限白名单，自动放行。
- **写入工具**：``categorize_transactions`` / ``add_expense``，会改账，部分
  审批档下必须经用户确认。
- **按需发现工具**：``suggest_category``（交易自动分类建议）默认**不注册**，
  只放进发现目录；模型识别到能力缺口后才在本轮被发现、授权、使用。

所有后端交互都走 :mod:`backend` 的领域原语，金额在工具层换算成"元"，后端
内部仍保持 Actual 的整数分约定。
"""

from __future__ import annotations

import calendar
from types import SimpleNamespace
from typing import Any

from yai_core import DiscoveredCandidate, StaticCatalog, build_spec

from .backend import DEMO_MONTH, ActualBackend, FakeActualBackend

#: 默认只读工具名（部分审批档下自动放行）。
READ_TOOL_NAMES = [
    "list_accounts",
    "list_recent_transactions",
    "list_categories",
    "get_month_budget",
    "reconcile_overview",
]
#: 写入工具名（会改账，部分审批档下需确认）。
WRITE_TOOL_NAMES = ["categorize_transactions", "add_expense"]
#: 按需发现、默认不注册的工具名。
DISCOVERABLE_TOOL_NAMES = ["suggest_category"]


def _yuan(cents: int) -> float:
    """整数分 → 元（两位小数，仅用于展示给模型/人）。"""
    return round(cents / 100.0, 2)


def _month_bounds(month: str) -> tuple[str, str]:
    """``YYYY-MM`` → 该月首尾日期（``YYYY-MM-DD``）。"""
    year, mon = (int(x) for x in month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


#: 收款人/备注关键词 → 分类（宿主侧的确定性小规则，作为"可选插件"存在）。
_KEYWORD_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("c-food", "餐饮", ("星巴克", "咖啡", "奶茶", "海底捞", "外卖", "美团", "饿了么",
                        "餐厅", "饭店", "烧烤", "肯德基", "麦当劳")),
    ("c-grocery", "食材杂货", ("盒马", "鲜生", "超市", "买菜", "菜场", "沃尔玛",
                            "永辉", "家乐福", "杂货", "生鲜")),
    ("c-transit", "交通", ("滴滴", "打车", "地铁", "公交", "出行", "高铁", "12306",
                          "加油", "停车", "出租")),
    ("c-housing", "居住", ("房租", "房东", "物业", "水费", "电费", "燃气", "宽带")),
    ("c-fun", "娱乐", ("电影", "影院", "游戏", "演出", "ktv", "KTV", "剧本杀", "网易云")),
]


def make_tools(backend: ActualBackend | None = None) -> SimpleNamespace:
    """构造一组绑定了具体后端的工具函数（CLI / 网页 / 测试共用）。"""
    backend = backend or FakeActualBackend()

    def _category_name(cid: str | None) -> str | None:
        if cid is None:
            return None
        for c in backend.categories():
            if c["id"] == cid:
                return c["name"]
        return None

    # -- 只读 --------------------------------------------------------------
    def list_accounts() -> dict[str, Any]:
        """列出全部账户及其当前余额（人民币元），区分资产与负债。"""
        accounts = backend.accounts()
        return {
            "count": len(accounts),
            "accounts": [
                {
                    "id": a["id"],
                    "name": a["name"],
                    "type": a["type"],
                    "balance_yuan": _yuan(a["balance_cents"]),
                }
                for a in accounts
            ],
        }

    def list_recent_transactions(month: str = DEMO_MONTH) -> dict[str, Any]:
        """列出某月全部交易（人民币元，支出为负），并标出未分类项。"""
        start, end = _month_bounds(month)
        rows = backend.transactions(start=start, end=end)
        items = [
            {
                "id": t["id"],
                "date": t["date"],
                "account_id": t["account_id"],
                "payee": t["payee"],
                "amount_yuan": _yuan(t["amount_cents"]),
                "category_id": t["category_id"],
                "category": _category_name(t["category_id"]),
                "uncategorized": t["category_id"] is None,
                "notes": t.get("notes", ""),
            }
            for t in sorted(rows, key=lambda x: x["date"])
        ]
        uncategorized = [i["id"] for i in items if i["uncategorized"]]
        return {
            "month": month,
            "total": len(items),
            "uncategorized_ids": uncategorized,
            "uncategorized_count": len(uncategorized),
            "transactions": items,
        }

    def list_categories() -> dict[str, Any]:
        """列出全部收支分类及其分组。"""
        cats = backend.categories()
        return {
            "count": len(cats),
            "categories": [
                {"id": c["id"], "name": c["name"], "group": c["group"],
                 "is_income": c["is_income"]}
                for c in cats
            ],
        }

    def get_month_budget(month: str = DEMO_MONTH) -> dict[str, Any]:
        """对比某月各分类预算与实际支出，标记超支分类（本地聚合）。"""
        start, end = _month_bounds(month)
        rows = [
            t for t in backend.transactions(start=start, end=end)
            if t["amount_cents"] < 0 and t["category_id"] is not None
        ]
        spent: dict[str, int] = {}
        for t in rows:
            spent[t["category_id"]] = spent.get(t["category_id"], 0) + abs(
                t["amount_cents"]
            )
        budgets = backend.budget_month(month)
        lines = []
        over = []
        for cid, budget_cents in budgets.items():
            used = spent.get(cid, 0)
            line = {
                "category_id": cid,
                "category": _category_name(cid),
                "budget_yuan": _yuan(budget_cents),
                "spent_yuan": _yuan(used),
                "remaining_yuan": _yuan(budget_cents - used),
                "over_budget": used > budget_cents,
            }
            lines.append(line)
            if line["over_budget"]:
                over.append(line["category"])
        return {"month": month, "lines": lines, "over_budget_categories": over}

    def reconcile_overview(month: str = DEMO_MONTH) -> dict[str, Any]:
        """无参式对账概览：账户数、未分类笔数、本月支出、超支分类数（供子员工）。"""
        recent = list_recent_transactions(month)
        budget = get_month_budget(month)
        spend_total = round(
            sum(
                abs(i["amount_yuan"])
                for i in recent["transactions"]
                if i["amount_yuan"] < 0
            ),
            2,
        )
        return {
            "month": month,
            "account_count": list_accounts()["count"],
            "uncategorized_count": recent["uncategorized_count"],
            "total_spending_yuan": spend_total,
            "over_budget_categories": budget["over_budget_categories"],
        }

    # -- 写入（需授权）-----------------------------------------------------
    def categorize_transactions(assignments: list[dict[str, Any]]) -> dict[str, Any]:
        """批量为未分类交易补分类：assignments 为 [{tx_id, category_id}, ...]。"""
        updated = []
        for item in assignments:
            tx = backend.set_transaction_category(item["tx_id"], item["category_id"])
            updated.append(
                {"tx_id": tx["id"], "category_id": tx["category_id"],
                 "category": _category_name(tx["category_id"])}
            )
        return {"updated_count": len(updated), "updated": updated}

    def add_expense(
        account_id: str,
        date: str,
        amount_yuan: float,
        payee: str,
        category_id: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """记一笔支出（金额单位元，正数），返回落库交易。"""
        cents = -int(round(abs(float(amount_yuan)) * 100))
        tx = backend.add_transaction(
            account_id, date, cents, payee, category_id=category_id, notes=notes
        )
        return {"id": tx["id"], "amount_yuan": _yuan(tx["amount_cents"]), "payee": payee}

    # -- 按需发现（默认不注册）--------------------------------------------
    def suggest_category(payee: str, note: str = "") -> dict[str, Any]:
        """根据收款人与备注关键词，为一笔未分类交易建议收支分类。"""
        text = f"{payee} {note}".lower()
        for cid, cname, keywords in _KEYWORD_RULES:
            for kw in keywords:
                if kw.lower() in text:
                    return {
                        "category_id": cid,
                        "category": cname,
                        "confidence": 0.9,
                        "matched_keyword": kw,
                        "reason": f"收款人/备注命中关键词「{kw}」，建议归入{cname}",
                    }
        return {
            "category_id": None,
            "category": None,
            "confidence": 0.0,
            "matched_keyword": None,
            "reason": "未命中任何分类关键词，需要人工指定",
        }

    return SimpleNamespace(
        list_accounts=list_accounts,
        list_recent_transactions=list_recent_transactions,
        list_categories=list_categories,
        get_month_budget=get_month_budget,
        reconcile_overview=reconcile_overview,
        categorize_transactions=categorize_transactions,
        add_expense=add_expense,
        suggest_category=suggest_category,
    )


def default_tool_specs(tools: SimpleNamespace) -> list:
    """默认注册到 Core 的工具：全部只读 + 写入（写入受权限档约束）。"""
    return [
        build_spec(tools.list_accounts),
        build_spec(tools.list_recent_transactions),
        build_spec(tools.list_categories),
        build_spec(tools.get_month_budget),
        build_spec(tools.reconcile_overview),
        build_spec(tools.categorize_transactions),
        build_spec(tools.add_expense),
    ]


def build_catalog(tools: SimpleNamespace) -> StaticCatalog:
    """按需能力目录：自动分类建议默认不注册，命中缺口时才被发现。"""
    return StaticCatalog(
        [
            DiscoveredCandidate(
                tools.suggest_category,
                ("分类", "归类", "未分类", "对账", "category", "这笔算什么", "什么类"),
                description="根据收款人/备注为未分类交易自动建议收支分类（按需启用）",
            )
        ]
    )
