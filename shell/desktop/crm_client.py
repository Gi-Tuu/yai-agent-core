"""销售专员的跨进程宿主客户端：灵动岛经本地 HTTP 操控 host_e 销售 CRM。

桌面灵动岛与 host_e 网页工作台是两个独立进程。灵动岛里的"销售专员"不复制
任何 CRM 业务逻辑，而是持有一个 :class:`HostECrmClient`：它把方法调用转成
``POST /api/agent/tool``，由 host_e 进程内的 ``ToolService`` 忠实执行真实业务。

- 工具名、参数、返回结构、docstring 与 ``SalesCrm`` 业务方法**逐一对齐**，
  因此 Core 自省到的工具描述与进程内嵌入完全一致；
- 只用标准库（urllib，同步），Core 会在工作线程里执行同步工具，不阻塞事件循环；
- 只连 ``127.0.0.1``；权限三档决策在灵动岛侧（AllowlistPolicy），
  宿主只负责执行，连接不上时给出可操作的中文提示；
- 每个请求带 ``Authorization: Bearer <token>``：令牌取自 ``YAI_TOOL_TOKEN``，
  否则读宿主启动时落盘的本地令牌文件。服务端无令牌一律 401——否则同机任意
  进程都能绕过灵动岛侧的审批直接改业务数据。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class HostECrmClient:
    """host_e 销售 CRM 的本地 HTTP 代理（方法与 ``SalesCrm`` 同名同参）。"""

    #: 与 host_e 服务端约定的令牌文件位置（两侧各一份常量，由测试断言一致）。
    TOKEN_FILE = Path(__file__).resolve().parents[2] / "data" / "host_e" / "tool_token"
    TOKEN_ENV = "YAI_TOOL_TOKEN"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = 20.0,
        token: str | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("YAI_HOST_E_URL") or "http://127.0.0.1:8200"
        ).rstrip("/")
        self.timeout = timeout
        self._tool_url = f"{self.base_url}/api/agent/tool"
        self._preview_url = f"{self.base_url}/api/agent/preview"
        self._token = token or self._resolve_token()

    @classmethod
    def _resolve_token(cls) -> str:
        """环境变量优先，其次读宿主启动时落盘的本地令牌文件。"""
        env = os.getenv(cls.TOKEN_ENV, "").strip()
        if env:
            return env
        try:
            return cls.TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def ping(self) -> bool:
        """探测宿主是否在线（工具清单端点），不抛异常。"""
        try:
            request = urllib.request.Request(
                f"{self.base_url}/api/agent/tools",
                headers=self._auth_headers(),
            )
            with urllib.request.urlopen(request, timeout=3.0) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    # ---- 读：客户 ----
    def list_customers(self, level: str = "") -> list[dict]:
        """列出全部客户，可按客户等级（重点/普通）筛选。"""
        return self._call("list_customers", level=level)

    def search_customers(
        self, keyword: str = "", level: str = "", status: str = ""
    ) -> list[dict]:
        """按关键词搜索客户（同时匹配姓名或公司名），可叠加等级（重点/普通）、
        状态（潜在/合作中/流失）筛选，参数为空表示不筛。"""
        return self._call(
            "search_customers", keyword=keyword, level=level, status=status
        )

    def get_customer(self, name: str) -> dict:
        """按姓名查询单个客户档案，聚合其跟进、订单、商机与金额汇总。"""
        return self._call("get_customer", name=name)

    def list_orders(self, category: str = "", region: str = "") -> list[dict]:
        """列出全部订单，可按品类（硬件/软件/服务）、区域（华东/华南/华北，
        "华东区/华东地区"等写法会自动归一）筛选，参数为空表示不筛。"""
        return self._call("list_orders", category=category, region=region)

    def sum_amount(self, category: str = "", region: str = "") -> int:
        """统计销售额合计，可按品类和区域筛选，参数为空表示全部。"""
        return self._call("sum_amount", category=category, region=region)

    def list_opportunities(self, stage: str = "") -> list[dict]:
        """列出商机管道，可按阶段筛选（初步接触/需求确认/方案报价/谈判/赢单/输单）；
        stage 传 open 只看在途（未赢单/输单），传空表示全部。"""
        return self._call("list_opportunities", stage=stage)

    def list_followups(self, customer: str = "") -> list[dict]:
        """列出跟进记录，可按客户姓名筛选，参数为空表示全部。"""
        return self._call("list_followups", customer=customer)

    def customers_due_followup(self, days: int = 3) -> list[dict]:
        """列出超过指定天数未跟进的客户，按逾期天数从多到少排序。"""
        return self._call("customers_due_followup", days=days)

    def list_todos(self, status: str = "open") -> list[dict]:
        """列出待办，status 取 open（未完成，默认）/ done（已完成）/ all（全部）。"""
        return self._call("list_todos", status=status)

    def daily_brief(self) -> str:
        """生成今日销售日报：销售总额与分品类汇总、超期未跟进客户、在途商机、未完成待办。"""
        return self._call("daily_brief")

    # ---- 写：客户 / 跟进 / 订单 / 商机 / 待办（灵动岛侧权限闸会先确认）----
    def add_customer(self, name: str, company: str, level: str = "普通") -> dict:
        """新增客户档案，level 默认为普通（可填重点），新客户状态默认为潜在。"""
        return self._call("add_customer", name=name, company=company, level=level)

    def update_customer(
        self, name: str, company: str = "", level: str = "",
        phone: str = "", status: str = "",
    ) -> dict:
        """编辑已有客户资料，只更新传入的非空字段（公司/等级/电话/状态）。"""
        return self._call(
            "update_customer", name=name, company=company, level=level,
            phone=phone, status=status,
        )

    def add_followup(self, customer: str, content: str) -> dict:
        """为指定客户新增一条跟进记录，并刷新该客户的最近跟进日期。"""
        return self._call("add_followup", customer=customer, content=content)

    def create_order(
        self, customer: str, category: str, amount: Any,
        region: str = "", product: str = "",
    ) -> dict:
        """录入一笔订单：客户可填姓名或公司名，品类为硬件/软件/服务，金额为正数，
        区域与产品可选；自动生成下一个订单号。"""
        return self._call(
            "create_order", customer=customer, category=category, amount=amount,
            region=region, product=product,
        )

    def create_opportunity(
        self, title: str, customer: str, amount: Any, stage: str = "初步接触"
    ) -> dict:
        """新建商机：名称、客户（姓名或公司名）、预期金额，阶段默认初步接触
        （可选初步接触/需求确认/方案报价/谈判/赢单/输单）。"""
        return self._call(
            "create_opportunity", title=title, customer=customer,
            amount=amount, stage=stage,
        )

    def update_opportunity_stage(self, title: str, stage: str) -> dict:
        """推进或更新商机阶段（初步接触/需求确认/方案报价/谈判/赢单/输单），按标题模糊匹配。"""
        return self._call("update_opportunity_stage", title=title, stage=stage)

    def create_todo(self, title: str, due: str = "") -> dict:
        """新建待办事项，due 为可选截止日期（YYYY-MM-DD），不传表示无截止日。"""
        return self._call("create_todo", title=title, due=due)

    def complete_todo(self, title: str) -> dict:
        """按标题模糊匹配一条未完成待办并标记完成。"""
        return self._call("complete_todo", title=title)

    def delete_todo(self, title: str) -> dict:
        """按标题删除一条待办（完成与否均可），模糊匹配。"""
        return self._call("delete_todo", title=title)

    def delete_order(self, order_no: str) -> dict:
        """按订单号删除一笔订单。"""
        return self._call("delete_order", order_no=order_no)

    def delete_opportunity(self, title: str) -> dict:
        """按标题删除一个商机，模糊匹配。"""
        return self._call("delete_opportunity", title=title)

    def delete_customer(self, name: str) -> dict:
        """删除客户并级联清理其跟进、订单、商机（订单/商机按公司名关联）。"""
        return self._call("delete_customer", name=name)

    # ---- 按需能力（命中能力缺口才被发现；与进程内 catalog 同源）----
    def get_visit_weather(self, city: str) -> dict:
        """查询客户所在城市的天气，并给出外勤拜访建议（是否带伞、是否适宜上门）。"""
        return self._call("get_visit_weather", city=city)

    def calc_quote_with_tax(self, amount: Any, rate: float = 13.0) -> dict:
        """按增值税率计算含税报价：不含税金额、税额、价税合计（金额单位：元）。"""
        return self._call("calc_quote_with_tax", amount=amount, rate=rate)

    # ---- HTTP 传输 ----
    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _preview(self, tool_name: str, arguments: dict) -> dict:
        """向 host_e 要写操作的真实预览；只读，不触发业务写入。

        刻意用下划线：`AgentCore.auto()` 会内省所有公开方法，`preview` 是
        审批链路内部能力，不能作为模型可调用的业务工具出现。
        """
        data = json.dumps(
            {"name": tool_name, "arguments": arguments or {}},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self._preview_url,
            data=data,
            headers=self._auth_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail") or detail
            except (ValueError, TypeError):
                pass
            raise RuntimeError(f"生成 {tool_name} 预览失败（HTTP {exc.code}）：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"无法连接销售 CRM 工作台（{self.base_url}），无法生成审批预览。"
            ) from exc
        if not body.get("ok") or not isinstance(body.get("preview"), dict):
            raise RuntimeError(f"生成 {tool_name} 预览失败：{body.get('error', '响应格式不合法')}")
        return body["preview"]

    def _call(self, tool_name: str, **arguments: Any) -> Any:
        # 形参刻意不叫 ``name``：add_customer / get_customer / update_customer 等
        # 业务工具本身就带名为 name 的关键字参数，同名会与本方法首参撞车报
        # "got multiple values for argument 'name'"。
        data = json.dumps({"name": tool_name, "arguments": arguments}).encode("utf-8")
        request = urllib.request.Request(
            self._tool_url,
            data=data,
            headers=self._auth_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail") or detail
            except (ValueError, TypeError):
                pass
            if exc.code == 404:
                raise RuntimeError(f"销售 CRM 不支持工具 {tool_name!r}：{detail}") from exc
            if exc.code == 400:
                raise RuntimeError(f"调用 {tool_name} 时参数有误：{detail}") from exc
            raise RuntimeError(
                f"调用 {tool_name} 失败（HTTP {exc.code}）：{detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"无法连接销售 CRM 工作台（{self.base_url}）。请先在另一个终端启动它："
                "uv run python examples/host_e_sales_crm/web_app.py，"
                "再对销售专员下达任务。"
            ) from exc
        if not body.get("ok"):
            raise RuntimeError(
                f"调用 {tool_name} 失败：{body.get('error', '未知错误')}"
            )
        return body.get("result")


def build_crm_preview(client: HostECrmClient, tool_name: str):
    """把 host_e 的预览端点包装成 ToolGovernance.preview 需要的闭包。"""

    def preview(arguments: dict, subject) -> dict:
        return client._preview(tool_name, arguments)

    return preview


def build_crm_catalog(client: HostECrmClient):
    """构造销售专员的按需能力目录（天气 / 含税报价），关键词与进程内 catalog 同源。

    这里把候选函数换成 client 的 HTTP 绑定方法，但关键词 / 描述复用 host_e
    ``catalog_capabilities.CANDIDATES``，保证进程内与跨进程的发现行为一致。
    """
    from host_e_sales_crm import catalog_capabilities as cap
    from yai_core import DiscoveredCandidate, StaticCatalog

    # 顺序与 cap.CANDIDATES 逐项对应（天气、含税）；strict=True 保证 catalog 增改
    # 能力而这里漏挂绑定方法时立即显性报错，而不是静默错配。
    bindings = (client.get_visit_weather, client.calc_quote_with_tax)
    candidates = [
        DiscoveredCandidate(bind, original.keywords, description=original.description)
        for bind, original in zip(bindings, cap.CANDIDATES, strict=True)
    ]
    return StaticCatalog(candidates)
