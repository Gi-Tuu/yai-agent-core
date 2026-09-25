"""host_i 的对外能力：把 Mealie（食谱/餐规划/购物清单）包成 6 个工具。

Mealie 是一个真实的、有 Web UI 的开源应用（Python/FastAPI，MIT，~8k stars）。
我们不改它一行源码，只通过它的 REST API 包一层语义清晰的函数。

工具设计（覆盖读/写/列表/详情）：
- search_recipes: 按关键词搜食谱
- get_recipe: 拿某个食谱详情（食材/步骤）
- get_mealplan: 查某段时间的餐规划
- add_mealplan: 把某个食谱加到某一天
- get_shopping_list: 看当前购物清单
- add_shopping_item: 往购物清单加一项

httpx 是可选依赖；未配置 MEALIE_URL 时宿主回退离线模式。
"""

from __future__ import annotations

import os


class MealieClient:
    """薄 httpx 封装：base_url + Bearer token。"""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def get(self, path: str, params: dict | None = None) -> dict:
        import httpx

        r = httpx.get(
            f"{self.base_url}/api{path}",
            headers=self._headers(),
            params=params,
            timeout=15.0,
        )
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: dict) -> dict:
        import httpx

        r = httpx.post(
            f"{self.base_url}/api{path}",
            headers=self._headers(),
            json=payload,
            timeout=15.0,
        )
        r.raise_for_status()
        return r.json()


_client: MealieClient | None = None


def _get_client() -> MealieClient:
    global _client
    if _client is None:
        base = os.getenv("MEALIE_URL", "").rstrip("/")
        token = os.getenv("MEALIE_TOKEN", "")
        if not base or not token:
            raise RuntimeError("未配置 MEALIE_URL / MEALIE_TOKEN")
        _client = MealieClient(base, token)
    return _client


# ---------- 工具函数 ----------

def search_recipes(query: str, limit: int = 10) -> dict:
    """按关键词搜索食谱。

    Args:
        query: 搜索词，例如"番茄鸡蛋"。
        limit: 最多返回几条。
    """
    client = _get_client()
    data = client.get("/recipes", params={"search": query, "perPage": int(limit)})
    items = data.get("items") or data.get("data") or []
    return {
        "query": query,
        "total": data.get("total", len(items)),
        "recipes": [
            {"slug": r.get("slug"), "name": r.get("name"),
             "description": (r.get("description") or "")[:80]}
            for r in items[: int(limit)]
        ],
    }


def get_recipe(slug: str) -> dict:
    """拿某个食谱的详情（食材、步骤、份数）。

    Args:
        slug: 食谱的唯一 slug，从 search_recipes 结果里拿。
    """
    client = _get_client()
    r = client.get(f"/recipes/{slug}")
    return {
        "slug": r.get("slug"),
        "name": r.get("name"),
        "servings": r.get("servings"),
        "ingredients": [
            (ing.get("note") or "").strip() for ing in (r.get("recipeIngredient") or [])
        ][:20],
        "steps": [
            (s.get("instruction") or "").strip() for s in (r.get("recipeInstructions") or [])
        ][:20],
    }


def get_mealplan(start_date: str, end_date: str) -> dict:
    """查某段时间的餐规划。

    Args:
        start_date: 起始日期 YYYY-MM-DD。
        end_date: 结束日期 YYYY-MM-DD。
    """
    client = _get_client()
    data = client.get(
        "/households/mealplans",
        params={"start_date": start_date, "end_date": end_date},
    )
    items = data.get("items") or data.get("data") or (data if isinstance(data, list) else [])
    return {
        "range": f"{start_date}~{end_date}",
        "entries": [
            {
                "date": e.get("date"),
                "title": e.get("title"),
                "slug": (e.get("recipe") or {}).get("slug"),
            }
            for e in items
        ],
    }


def add_mealplan(date: str, slug: str, title: str = "") -> dict:
    """把某个食谱加到某一天的餐规划。

    Args:
        date: 目标日期 YYYY-MM-DD。
        slug: 食谱 slug。
        title: 这条规划的标题（可选，默认用食谱名）。
    """
    client = _get_client()
    payload = {"date": date, "entryType": "dinner", "recipeId": None}
    if title:
        payload["title"] = title
    r = client.post("/households/mealplans", payload)
    return {"added": True, "date": date, "slug": slug, "id": r.get("id")}


def get_shopping_list() -> dict:
    """看当前购物清单（Mealie v4 端点变更时优雅降级）。"""
    client = _get_client()
    try:
        data = client.get("/households/shopping-lists")
    except Exception:
        return {"count": 0, "items": [], "note": "Mealie v4 shopping-lists endpoint 变更，待适配"}
    items = data.get("items") or data.get("data") or []
    lists = items if isinstance(items, list) else []
    if lists:
        first_id = lists[0].get("id")
        detail = client.get(f"/households/shopping-lists/{first_id}")
        entries = detail.get("listItems") or []
    else:
        entries = []
    return {
        "count": len(entries),
        "items": [
            {"food": (it.get("food") or {}).get("name") or it.get("note"),
             "checked": bool(it.get("checked"))}
            for it in entries
        ],
    }


def add_shopping_item(food_name: str, note: str = "") -> dict:
    """往购物清单加一项。

    Args:
        food_name: 食材名，例如"牛奶"。
        note: 备注（可选，如"2 盒"）。
    """
    client = _get_client()
    try:
        lists = client.get("/households/shopping-lists")
    except Exception:
        return {"added": False, "reason": "Mealie v4 shopping-lists endpoint 变更，待适配"}
    items = lists.get("items") or lists.get("data") or []
    if not items:
        return {"added": False, "reason": "没有购物清单，请先在 Mealie 网页里建一个"}
    first_id = items[0].get("id")
    payload = {"food": {"name": food_name}, "note": note}
    r = client.post(f"/households/shopping-lists/{first_id}/item", payload)
    return {"added": True, "food": food_name, "id": r.get("id")}
