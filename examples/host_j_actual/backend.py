"""Actual Budget 数据后端：统一的领域原语 + 两种实现。

YAI 是进程内 Python 内核，不挑宿主后端的形态。Actual Budget 官方提供的是
Node 包 ``@actual-app/cli``（``actual <command> <subcommand>``，默认
``--format json``，金额一律整数分），并不是一套朴素 REST；因此本宿主把官方
CLI 包成一组领域工具，正好演示"内核不绑定任何特定后端协议"。

两种实现：

- :class:`FakeActualBackend`：内存假账，离线默认，CI/录屏完全确定、不联网。
- :class:`CliActualBackend`：通过官方 CLI 子进程对接真实 sync server。命令、
  环境变量、整数分约定均来自官方文档
  https://actualbudget.org/docs/api/cli/ 。跨账户的汇总（对账概览/预算对比）
  按官方 Tips "拉一段原始交易后在本地聚合" 实现，不臆造 ActualQL 端点。

内核/示例不引入 Node 或第三方 HTTP 依赖：CLI 后端只在被真正实例化时才使用
标准库 :mod:`subprocess`，且 ``runner`` 可注入，便于离线锁定 argv 而不真的
拉起进程。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 领域约定
# ---------------------------------------------------------------------------
#
# 所有金额在后端层一律使用整数分（cents），与 Actual CLI 的 JSON 输出一致；
# 转换成"元"只发生在 capabilities 工具层，避免浮点误差。
#
# 交易 dict 字段：
#   id, account_id, date(YYYY-MM-DD), amount_cents(支出为负),
#   payee, category_id(None 表示未分类), notes
# 分类 dict 字段：id, name, group, is_income
# 账户 dict 字段：id, name, type, offbudget, balance_cents

DEMO_MONTH = "2026-09"


@runtime_checkable
class ActualBackend(Protocol):
    """宿主记账后端的最小领域契约。Fake 与 CLI 两种实现都满足它。"""

    offline: bool

    def accounts(self) -> list[dict[str, Any]]:
        """返回全部账户（含余额，单位分）。"""
        ...

    def categories(self) -> list[dict[str, Any]]:
        """返回全部分类。"""
        ...

    def transactions(
        self,
        account_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回交易，可按账户与日期区间（YYYY-MM-DD）过滤。"""
        ...

    def add_transaction(
        self,
        account_id: str,
        date: str,
        amount_cents: int,
        payee: str,
        category_id: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """新增一笔交易，返回落库后的交易 dict。"""
        ...

    def set_transaction_category(self, tx_id: str, category_id: str) -> dict[str, Any]:
        """补/改一笔交易的分类（对应官方 ``transactions update``）。"""
        ...

    def budget_month(self, month: str) -> dict[str, Any]:
        """返回某月预算设置：{category_id: budget_cents}（无设置则缺省）。"""
        ...


# ---------------------------------------------------------------------------
# 离线内存后端
# ---------------------------------------------------------------------------


class FakeActualBackend:
    """内存假账：数据贴近真实个人账，离线即可完整演示对账五拍。"""

    offline = True

    def __init__(self) -> None:
        # 账户：一个活期（资产）、一张信用卡（负债）。
        self._accounts: list[dict[str, Any]] = [
            {
                "id": "a-checking",
                "name": "活期账户",
                "type": "checking",
                "offbudget": False,
                "balance_cents": 128530,
            },
            {
                "id": "a-credit",
                "name": "信用卡",
                "type": "credit",
                "offbudget": False,
                "balance_cents": -43210,
            },
        ]
        self._categories: list[dict[str, Any]] = [
            {"id": "c-food", "name": "餐饮", "group": "日常", "is_income": False},
            {"id": "c-grocery", "name": "食材杂货", "group": "日常", "is_income": False},
            {"id": "c-transit", "name": "交通", "group": "日常", "is_income": False},
            {"id": "c-housing", "name": "居住", "group": "固定", "is_income": False},
            {"id": "c-fun", "name": "娱乐", "group": "弹性", "is_income": False},
            {"id": "c-income", "name": "工资", "group": "收入", "is_income": True},
        ]
        # 9 月账：刻意留 3 笔未分类（category_id=None），构成对账长尾任务。
        self._transactions: list[dict[str, Any]] = [
            {"id": "t1", "account_id": "a-checking", "date": "2026-09-01",
             "amount_cents": 1580000, "payee": "公司工资", "category_id": "c-income",
             "notes": "9月工资"},
            {"id": "t2", "account_id": "a-checking", "date": "2026-09-02",
             "amount_cents": -320000, "payee": "房东王姐", "category_id": "c-housing",
             "notes": "房租"},
            {"id": "t3", "account_id": "a-credit", "date": "2026-09-05",
             "amount_cents": -3800, "payee": "星巴克", "category_id": None,
             "notes": ""},
            {"id": "t4", "account_id": "a-credit", "date": "2026-09-08",
             "amount_cents": -2600, "payee": "地铁出行", "category_id": "c-transit",
             "notes": ""},
            {"id": "t5", "account_id": "a-checking", "date": "2026-09-12",
             "amount_cents": -18650, "payee": "盒马鲜生", "category_id": None,
             "notes": "周末采购"},
            {"id": "t6", "account_id": "a-credit", "date": "2026-09-15",
             "amount_cents": -3240, "payee": "滴滴出行", "category_id": None,
             "notes": "加班打车"},
            {"id": "t7", "account_id": "a-credit", "date": "2026-09-18",
             "amount_cents": -9600, "payee": "海底捞", "category_id": "c-food",
             "notes": "聚餐"},
            {"id": "t8", "account_id": "a-checking", "date": "2026-09-22",
             "amount_cents": -12800, "payee": "电影院", "category_id": "c-fun",
             "notes": ""},
        ]
        self._budgets: dict[str, dict[str, int]] = {
            DEMO_MONTH: {
                "c-food": 150000,
                "c-grocery": 80000,
                "c-transit": 20000,
                "c-fun": 10000,
            }
        }
        self._seq = 100

    # -- 只读 --------------------------------------------------------------
    def accounts(self) -> list[dict[str, Any]]:
        return [dict(a) for a in self._accounts]

    def categories(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._categories]

    def transactions(
        self,
        account_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._transactions
        if account_id is not None:
            rows = [t for t in rows if t["account_id"] == account_id]
        if start is not None:
            rows = [t for t in rows if t["date"] >= start]
        if end is not None:
            rows = [t for t in rows if t["date"] <= end]
        return [dict(t) for t in rows]

    def budget_month(self, month: str) -> dict[str, Any]:
        return dict(self._budgets.get(month, {}))

    # -- 写 ----------------------------------------------------------------
    def add_transaction(
        self,
        account_id: str,
        date: str,
        amount_cents: int,
        payee: str,
        category_id: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        if not any(a["id"] == account_id for a in self._accounts):
            raise ValueError(f"未知账户：{account_id}")
        self._seq += 1
        tx = {
            "id": f"t{self._seq}",
            "account_id": account_id,
            "date": date,
            "amount_cents": int(amount_cents),
            "payee": payee,
            "category_id": category_id,
            "notes": notes,
        }
        self._transactions.append(tx)
        return dict(tx)

    def set_transaction_category(self, tx_id: str, category_id: str) -> dict[str, Any]:
        if not any(c["id"] == category_id for c in self._categories):
            raise ValueError(f"未知分类：{category_id}")
        for t in self._transactions:
            if t["id"] == tx_id:
                t["category_id"] = category_id
                return dict(t)
        raise ValueError(f"未知交易：{tx_id}")


# ---------------------------------------------------------------------------
# 官方 CLI 后端（真机）
# ---------------------------------------------------------------------------


class CliActualError(RuntimeError):
    """官方 CLI 调用失败（非零退出 / 无法解析 JSON）。"""


class CliActualBackend:
    """通过官方 ``@actual-app/cli`` 对接真实 Actual sync server。

    配置全部走环境变量（与官方文档一致，且避免把口令放进进程参数）：

    - ``ACTUAL_SERVER_URL``  sync server 地址
    - ``ACTUAL_SYNC_ID``     Budget Sync ID
    - ``ACTUAL_PASSWORD`` 或 ``ACTUAL_SESSION_TOKEN``  鉴权

    ``cli`` 可执行文件名默认 ``actual``；``runner`` 用于注入测试替身，签名为
    ``runner(argv: list[str], env: dict) -> subprocess.CompletedProcess``。
    """

    offline = False

    def __init__(
        self,
        *,
        cli: str = "actual",
        runner: Any | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.cli = cli
        self.env = env if env is not None else dict(os.environ)
        self._runner = runner or self._default_runner
        missing = [
            k
            for k in ("ACTUAL_SERVER_URL", "ACTUAL_SYNC_ID")
            if not self.env.get(k)
        ]
        secret = self.env.get("ACTUAL_PASSWORD") or self.env.get("ACTUAL_SESSION_TOKEN")
        if not secret:
            missing.append("ACTUAL_PASSWORD/ACTUAL_SESSION_TOKEN")
        if missing and runner is None:
            # 注入 runner 的离线单测不需要真实环境；真机实例化则应尽早报错。
            raise CliActualError(
                "缺少 Actual CLI 环境变量：" + ", ".join(sorted(set(missing)))
            )

    @staticmethod
    def _default_runner(argv: list[str], env: dict[str, str]) -> Any:
        if shutil.which(argv[0]) is None:
            raise CliActualError(
                f"找不到官方 CLI `{argv[0]}`，请先 npm i -g @actual-app/cli（Node 22+）"
            )
        return subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def _json(self, *parts: str) -> Any:
        argv = [self.cli, *parts, "--format", "json"]
        proc = self._runner(argv, self.env)
        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
        code = getattr(proc, "returncode", 0)
        if code != 0:
            raise CliActualError(f"`{' '.join(argv)}` 退出码 {code}：{stderr.strip()}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise CliActualError(
                f"`{' '.join(argv)}` 输出不是合法 JSON：{stdout[:200]}"
            ) from exc

    # -- 只读：命令严格对应官方 CLI 文档 -----------------------------------
    def accounts(self) -> list[dict[str, Any]]:
        return list(self._json("accounts", "list"))

    def categories(self) -> list[dict[str, Any]]:
        return list(self._json("categories", "list"))

    def transactions(
        self,
        account_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        if account_id is None:
            # 跨账户：逐账户拉取后在本地聚合（官方 Tips 建议本地聚合，且账户数很少；
            # 不臆造 ActualQL 的日期过滤写法）。
            rows: list[dict[str, Any]] = []
            for acct in self.accounts():
                rows.extend(self.transactions(acct["id"], start, end))
            return rows
        parts = ["transactions", "list", "--account", account_id]
        if start:
            parts += ["--start", start]
        if end:
            parts += ["--end", end]
        return list(self._json(*parts))

    def budget_month(self, month: str) -> dict[str, Any]:
        # 官方 `budgets month <month>` 返回该月预算表；归一化成 {category_id: cents}。
        raw = self._json("budgets", "month", month)
        budgets: dict[str, Any] = {}
        if isinstance(raw, dict):
            raw = raw.get("month", raw)
        for item in raw if isinstance(raw, list) else []:
            cid = item.get("category_id") or item.get("category") or item.get("id")
            amount = item.get("budgeted") or item.get("amount") or item.get("budget")
            if cid is not None and amount is not None:
                budgets[cid] = int(amount)
        return budgets

    # -- 写 ----------------------------------------------------------------
    def add_transaction(
        self,
        account_id: str,
        date: str,
        amount_cents: int,
        payee: str,
        category_id: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "date": date,
            "amount": int(amount_cents),  # 官方约定：整数分
            "payee_name": payee,
        }
        if category_id:
            record["category"] = category_id
        if notes:
            record["notes"] = notes
        out = self._json(
            "transactions", "add", "--account", account_id,
            "--data", json.dumps([record], ensure_ascii=False),
        )
        return out[0] if isinstance(out, list) and out else {"added": record}

    def set_transaction_category(self, tx_id: str, category_id: str) -> dict[str, Any]:
        # 官方 `transactions update <id> --data '{...}'`；分类字段与
        # @actual-app/api 的 updateTransaction 一致，使用 category 外键 id。
        out = self._json(
            "transactions", "update", tx_id,
            "--data", json.dumps({"category": category_id}, ensure_ascii=False),
        )
        return out if isinstance(out, dict) else {"id": tx_id, "category": category_id}


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def get_backend(
    *,
    force_fake: bool = False,
    cli: str = "actual",
    runner: Any | None = None,
) -> ActualBackend:
    """按环境选择后端：配齐官方 CLI 环境变量走真机，否则回退离线 Fake。

    ``force_fake`` 用于显式指定离线回放；``runner`` 仅测试注入。
    """
    configured = bool(
        os.environ.get("ACTUAL_SERVER_URL")
        and os.environ.get("ACTUAL_SYNC_ID")
        and (os.environ.get("ACTUAL_PASSWORD") or os.environ.get("ACTUAL_SESSION_TOKEN"))
    )
    if configured and not force_fake:
        return CliActualBackend(cli=cli, runner=runner)  # type: ignore[return-value]
    return FakeActualBackend()
