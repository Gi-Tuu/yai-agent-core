"""一个可独立运行的销售 CRM 小软件（业务本体）。

本文件不导入、也不知道 YAI Agent Core 的存在：
- standalone_cli.py 是它自带的菜单界面，没有 AI 也完整可用；
- run_agent.py 把同一个 SalesCrm 对象交给 AgentCore.auto()，零改造得到数字员工。

数据为虚构种子数据；默认持久化到包目录 crm_data.json，可 _reset() 重置。
"""

import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

_DATA_FILE = Path(__file__).parent / "crm_data.json"


def _seed(today: date) -> dict:
    """生成一套自洽的虚构演示数据（日期相对 today，保证超期跟进演示稳定）。"""
    customers = [
        {"name": "王敏", "company": "华东智造集团", "level": "重点",
         "owner": "本人", "last_followup": (today - timedelta(days=7)).isoformat()},
        {"name": "李强", "company": "南方软件有限公司", "level": "重点",
         "owner": "本人", "last_followup": (today - timedelta(days=5)).isoformat()},
        {"name": "赵雷", "company": "华北云服务公司", "level": "普通",
         "owner": "本人", "last_followup": (today - timedelta(days=2)).isoformat()},
        {"name": "陈静", "company": "华东硬件渠道商", "level": "普通",
         "owner": "本人", "last_followup": (today - timedelta(days=1)).isoformat()},
        {"name": "周涛", "company": "华南服务外包园", "level": "普通",
         "owner": "本人", "last_followup": (today - timedelta(days=9)).isoformat()},
    ]
    orders = [
        {"order": "S001", "customer": "华东智造集团", "category": "硬件",
         "amount": 12000, "region": "华东"},
        {"order": "S002", "customer": "南方软件有限公司", "category": "软件",
         "amount": 8000, "region": "华南"},
        {"order": "S003", "customer": "华北云服务公司", "category": "服务",
         "amount": 6000, "region": "华北"},
        {"order": "S004", "customer": "华东硬件渠道商", "category": "硬件",
         "amount": 4500, "region": "华东"},
        {"order": "S005", "customer": "华南服务外包园", "category": "服务",
         "amount": 3200, "region": "华南"},
        {"order": "S006", "customer": "华东智造集团", "category": "软件",
         "amount": 5000, "region": "华东"},
    ]
    followups = [
        {"customer": "王敏", "content": "初次拜访，确认有产线改造预算",
         "created_at": (today - timedelta(days=7)).isoformat()},
        {"customer": "李强", "content": "方案已发送，等待客户内部评审",
         "created_at": (today - timedelta(days=5)).isoformat()},
        {"customer": "赵雷", "content": "电话回访，对软件套餐感兴趣",
         "created_at": (today - timedelta(days=2)).isoformat()},
    ]
    todos = [
        {"title": "整理报价单模板", "due": "", "done": True},
        {"title": "给南方软件补发产品资料", "due": today.isoformat(), "done": False},
        {"title": "周五前回访华东智造王敏",
         "due": (today + timedelta(days=2)).isoformat(), "done": False},
        {"title": "核对华北云服务合同条款", "due": "", "done": False},
    ]
    return {"customers": customers, "orders": orders,
            "followups": followups, "todos": todos}


class SalesCrm:
    """销售 CRM 业务对象：客户 / 订单 / 跟进 / 待办 / 日报。

    公开方法即业务能力，可被菜单调用，也可被 YAI 内省为 Agent 工具。
    """

    def __init__(
        self,
        data_path: str | Path | None = _DATA_FILE,
        *,
        clock: Callable[[], date] = date.today,
        persist: bool = True,
    ) -> None:
        self._clock = clock
        self._path = Path(data_path) if data_path else None
        self._persist = persist and self._path is not None
        self._data = self._load()

    # ---------- 存储 ----------

    def _load(self) -> dict:
        if self._path and self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        data = _seed(self._clock())
        self._save(data)
        return data

    def _save(self, data: dict | None = None) -> None:
        if not self._persist:
            return
        data = data or self._data
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _reset(self) -> dict:
        """恢复演示种子数据（菜单使用；下划线开头不注册为 Agent 工具）。"""
        self._data = _seed(self._clock())
        self._save()
        return {"ok": True, "message": "已恢复演示数据",
                "customers": len(self._data["customers"])}

    def _find_customer(self, name: str) -> dict | None:
        """先精确匹配，再按包含匹配。"""
        for c in self._data["customers"]:
            if c["name"] == name:
                return c
        for c in self._data["customers"]:
            if name in c["name"] or c["name"] in name:
                return c
        return None

    # ---------- 读：客户 / 订单 ----------

    def list_customers(self, level: str = "") -> list[dict]:
        """列出全部客户，可按客户等级（重点/普通）筛选。"""
        if level:
            return [c for c in self._data["customers"] if c["level"] == level]
        return list(self._data["customers"])

    def get_customer(self, name: str) -> dict:
        """按姓名查询单个客户档案，包含其全部跟进记录。"""
        customer = self._find_customer(name)
        if customer is None:
            return {"ok": False, "error": f"客户 {name!r} 不存在"}
        records = [f for f in self._data["followups"] if f["customer"] == customer["name"]]
        return {**customer, "followups": records}

    def list_orders(self, category: str = "", region: str = "") -> list[dict]:
        """列出全部订单，可按品类（硬件/软件/服务）和区域筛选，参数为空表示不筛。"""
        rows = self._data["orders"]
        if category:
            rows = [r for r in rows if r["category"] == category]
        if region:
            rows = [r for r in rows if r["region"] == region]
        return rows

    def sum_amount(self, category: str = "", region: str = "") -> int:
        """统计销售额合计，可按品类和区域筛选，参数为空表示全部。"""
        return sum(r["amount"] for r in self.list_orders(category, region))

    # ---------- 读：跟进 / 待办 / 日报 ----------

    def list_followups(self, customer: str = "") -> list[dict]:
        """列出跟进记录，可按客户姓名筛选，参数为空表示全部。"""
        if customer:
            matched = self._find_customer(customer)
            if matched is None:
                return [{"ok": False, "error": f"客户 {customer!r} 不存在"}]
            customer = matched["name"]
            return [f for f in self._data["followups"] if f["customer"] == customer]
        return list(self._data["followups"])

    def customers_due_followup(self, days: int = 3) -> list[dict]:
        """列出超过指定天数未跟进的客户，按逾期天数从多到少排序。"""
        today = self._clock()
        due = []
        for c in self._data["customers"]:
            last = date.fromisoformat(c["last_followup"])
            overdue = (today - last).days
            if overdue >= days:
                due.append({"name": c["name"], "company": c["company"],
                            "level": c["level"], "last_followup": c["last_followup"],
                            "days_overdue": overdue})
        due.sort(key=lambda x: x["days_overdue"], reverse=True)
        return due

    def list_todos(self, status: str = "open") -> list[dict]:
        """列出待办，status 取 open（未完成，默认）/ done（已完成）/ all（全部）。"""
        if status not in ("open", "done", "all"):
            raise ValueError("status 只能是 open / done / all")
        if status == "all":
            return list(self._data["todos"])
        want_done = status == "done"
        return [t for t in self._data["todos"] if t["done"] == want_done]

    def daily_brief(self) -> str:
        """生成今日销售日报：销售总额与分品类汇总、超期未跟进客户、未完成待办。"""
        today = self._clock().isoformat()
        total = self.sum_amount()
        lines = [f"销售日报（{today}）", ""]
        lines.append(f"一、销售总额：¥{total:,}（共 {len(self._data['orders'])} 笔订单）")
        for category in ("硬件", "软件", "服务"):
            subtotal = self.sum_amount(category=category)
            lines.append(f"  - {category}：¥{subtotal:,}")
        lines.append("")
        due = self.customers_due_followup(3)
        lines.append(f"二、超过 3 天未跟进客户（{len(due)} 位）：")
        if due:
            for d in due:
                lines.append(f"  - {d['name']}（{d['company']}，{d['level']}）"
                             f"已 {d['days_overdue']} 天未跟进")
        else:
            lines.append("  - 无")
        lines.append("")
        open_todos = self.list_todos("open")
        lines.append(f"三、未完成待办（{len(open_todos)} 项）：")
        for t in open_todos:
            due_text = f"，截止 {t['due']}" if t["due"] else ""
            lines.append(f"  - {t['title']}{due_text}")
        return "\n".join(lines)

    # ---------- 写：客户 / 跟进 / 待办（嵌入 Core 后触发权限确认） ----------

    def add_customer(self, name: str, company: str, level: str = "普通") -> dict:
        """新增客户档案，level 默认为普通（可填重点）。"""
        if not name.strip():
            return {"ok": False, "error": "客户姓名不能为空"}
        if self._find_customer(name) is not None:
            return {"ok": False, "error": f"客户 {name!r} 已存在"}
        customer = {"name": name.strip(), "company": company, "level": level or "普通",
                    "owner": "本人", "last_followup": self._clock().isoformat()}
        self._data["customers"].append(customer)
        self._save()
        return {"ok": True, "customer": customer}

    def add_followup(self, customer: str, content: str) -> dict:
        """为指定客户新增一条跟进记录，并刷新该客户的最近跟进日期。"""
        matched = self._find_customer(customer)
        if matched is None:
            return {"ok": False, "error": f"客户 {customer!r} 不存在"}
        if not content.strip():
            return {"ok": False, "error": "跟进内容不能为空"}
        today = self._clock().isoformat()
        record = {"customer": matched["name"], "content": content, "created_at": today}
        self._data["followups"].append(record)
        matched["last_followup"] = today
        self._save()
        return {"ok": True, "record": record}

    def create_todo(self, title: str, due: str = "") -> dict:
        """新建待办事项，due 为可选截止日期（YYYY-MM-DD），不传表示无截止日。"""
        if not title.strip():
            return {"ok": False, "error": "待办标题不能为空"}
        todo = {"title": title.strip(), "due": due.strip(), "done": False}
        self._data["todos"].append(todo)
        self._save()
        return {"ok": True, "todo": todo}

    def complete_todo(self, title: str) -> dict:
        """按标题模糊匹配一条未完成待办并标记完成。"""
        for todo in self._data["todos"]:
            if not todo["done"] and title in todo["title"]:
                todo["done"] = True
                self._save()
                return {"ok": True, "todo": todo}
        return {"ok": False, "error": f"没有找到标题包含 {title!r} 的未完成待办"}
