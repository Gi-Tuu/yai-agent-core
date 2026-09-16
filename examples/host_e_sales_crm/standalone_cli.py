"""销售 CRM 的自带菜单界面：不依赖 YAI Agent Core，python 直接运行。

运行：.venv/Scripts/python.exe examples/host_e_sales_crm/standalone_cli.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from host_e_sales_crm.crm_app import SalesCrm  # noqa: E402

MENU = """
========== 销售 CRM（独立软件，未接入 AI）==========
 1. 客户列表　　　　　 2. 查询客户档案
 3. 订单查询　　　　　 4. 销售额汇总
 5. 超期未跟进客户　　 6. 待办列表
 7. 今日销售日报
 8. 写跟进　　　　　　 9. 新建待办
10. 完成待办　　　　　11. 新增客户
12. 重置演示数据　　　 0. 退出
====================================================
"""


def _print(obj: object, out) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2), file=out)


def main(argv=None, *, inputs=None, out=None, crm: SalesCrm | None = None) -> SalesCrm:
    """菜单主循环。inputs 注入可离线测试（字符串序列），out 可重定向输出。"""
    out = out or sys.stdout
    crm = crm or SalesCrm()
    iterator = iter(inputs) if inputs is not None else None

    def read(prompt: str) -> str:
        if iterator is not None:
            value = next(iterator, "0")
            print(f"{prompt}{value}", file=out)
            return value
        return input(prompt)

    while True:
        print(MENU, file=out)
        choice = read("请选择 > ").strip()
        if choice == "0":
            print("再见。", file=out)
            return crm
        elif choice == "1":
            _print(crm.list_customers(read("等级（可空）> ")), out)
        elif choice == "2":
            _print(crm.get_customer(read("客户姓名 > ")), out)
        elif choice == "3":
            _print(crm.list_orders(read("品类（可空）> "), read("区域（可空）> ")), out)
        elif choice == "4":
            _print({"total": crm.sum_amount(
                read("品类（可空）> "), read("区域（可空）> "))}, out)
        elif choice == "5":
            raw = read("超期天数（默认 3）> ").strip() or "3"
            try:
                days = int(raw)
            except ValueError:
                print(f"无法识别的天数 {raw!r}，按 3 天处理。", file=out)
                days = 3
            _print(crm.customers_due_followup(days), out)
        elif choice == "6":
            status = read("状态 open/done/all（默认 open）> ").strip() or "open"
            if status not in ("open", "done", "all"):
                print(f"无法识别的状态 {status!r}，按 open 处理。", file=out)
                status = "open"
            _print(crm.list_todos(status), out)
        elif choice == "7":
            print(crm.daily_brief(), file=out)
        elif choice == "8":
            _print(crm.add_followup(read("客户姓名 > "), read("跟进内容 > ")), out)
        elif choice == "9":
            _print(crm.create_todo(read("待办标题 > "), read("截止日期（可空）> ")), out)
        elif choice == "10":
            _print(crm.complete_todo(read("待办标题关键词 > ")), out)
        elif choice == "11":
            _print(crm.add_customer(read("客户姓名 > "), read("公司 > "),
                                    read("等级（默认普通）> ") or "普通"), out)
        elif choice == "12":
            _print(crm._reset(), out)
        else:
            print("无效选项，请重新输入。", file=out)


if __name__ == "__main__":
    main()
