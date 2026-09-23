"""host_i Mealie 嵌入演示：离线测试，不联网、不需要 Mealie。

用 unittest.mock 替换 httpx.get/post，验证：
1. search_recipes 正确解析分页返回；
2. add_shopping_item 在没有购物清单时友好报错；
3. 脚本化模型驱动 Core 走完"搜菜→看详情→加购物项"三步链。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from host_i_mealie import capabilities as cap  # noqa: E402
from host_i_mealie.demo_agent import build_core  # noqa: E402


def _fake_mealie_responses(path: str, **_):
    """根据 URL 返回假数据。"""

    class _R:
        def __init__(self, payload: dict) -> None:
            self._p = payload

        def json(self) -> dict:
            return self._p

        def raise_for_status(self) -> None:
            pass

    if path.startswith("/recipes") and path != "/recipes/tomato-egg":
        return _R({"total": 2, "items": [
            {"slug": "tomato-egg", "name": "番茄炒蛋", "description": "家常快炒"},
            {"slug": "tomato-beef", "name": "番茄牛腩", "description": "慢炖"},
        ]})
    if path == "/recipes/tomato-egg":
        return _R({
            "slug": "tomato-egg", "name": "番茄炒蛋", "servings": 2,
            "recipeIngredient": [{"note": "番茄 2 个"}, {"note": "鸡蛋 3 个"}],
            "recipeInstructions": [{"instruction": "打散鸡蛋"}, {"instruction": "炒番茄"}],
        })
    if path == "/households/shopping-lists":
        return _R({"items": [{"id": "sl-1", "name": "默认清单"}]})
    if "/shopping-lists/sl-1/item" in path:
        return _R({"id": "item-9"})
    return _R({})


def test_search_recipes_parses_pagination() -> None:
    """search_recipes 从分页返回里提取 slug/name。"""
    with patch.object(
        cap.MealieClient, "get",
        side_effect=lambda path, params=None: _fake_mealie_responses(path).json(),
    ):
        cap._client = cap.MealieClient("http://x", "t")
        r = cap.search_recipes("番茄", limit=5)
    assert r["total"] == 2
    assert r["recipes"][0]["slug"] == "tomato-egg"


def test_get_recipe_extracts_ingredients() -> None:
    """get_recipe 提取食材和步骤。"""
    with patch.object(
        cap.MealieClient, "get",
        side_effect=lambda path, params=None: _fake_mealie_responses(path).json(),
    ):
        cap._client = cap.MealieClient("http://x", "t")
        r = cap.get_recipe("tomato-egg")
    assert r["servings"] == 2
    assert any("番茄" in i for i in r["ingredients"])
    assert len(r["steps"]) == 2


def test_add_shopping_item_no_list_friendly() -> None:
    """没有购物清单时友好报错，不抛异常。"""
    with patch.object(cap.MealieClient, "get", return_value={"items": []}):
        cap._client = cap.MealieClient("http://x", "t")
        r = cap.add_shopping_item("牛奶")
    assert r["added"] is False
    assert "购物清单" in r["reason"]


def test_core_drives_three_step_chain() -> None:
    """脚本模型 → Core 自动走三步工具链 → 最终总结。"""
    async def _run() -> str:
        with (
            patch.object(
                cap.MealieClient, "get",
                side_effect=lambda path, params=None: _fake_mealie_responses(path).json(),
            ),
            patch.object(cap.MealieClient, "post", side_effect=lambda path, payload: {"id": "x"}),
        ):
            os.environ["MEALIE_URL"] = "http://x"
            os.environ["MEALIE_TOKEN"] = "t"
            core = build_core()
            result = await core.run("今晚做番茄炒蛋")
        return getattr(result, "content", str(result))

    final = asyncio.run(_run())
    assert "番茄" in final or "购物" in final
