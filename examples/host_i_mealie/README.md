# host_i —— 嵌入真实开源食谱应用 Mealie

> 这是 YAI "零改造嵌入真实 Web 应用" 的最强证据：
> **Mealie**（Python/FastAPI，MIT，~8k stars）是一个有完整 Web UI 的
> 个人食谱与餐规划应用，我们没改它一行源码，
> 只通过它的 REST API 包了一层，Core 就用自然语言把它调起来了。

## 这个宿主证明什么

- Mealie 原本是个完整 Web 应用：你得在浏览器里点搜索、点食谱、点餐规划、点购物清单。
- 我们在 `capabilities.py` 里包了 6 个工具：
  - `search_recipes` / `get_recipe`（读：搜菜、看详情）
  - `get_mealplan` / `add_mealplan`（读写：查/加餐规划）
  - `get_shopping_list` / `add_shopping_item`（读写：看/加购物项）
- **不改 Mealie 源码**，这层 wrapper 就是"宿主只声明能力"的全部工作。

## 运行

```bash
# 离线演示（脚本模型，无需 Mealie、无需 API Key）
uv run python examples/host_i_mealie/run.py
uv run python examples/host_i_mealie/run.py "今晚做番茄炒蛋，把缺的食材加进购物清单"
```

配置 `MEALIE_URL` 和 `MEALIE_TOKEN`（在 Mealie 网页 `/user/profile/api-tokens` 生成）后，
会自动换成真实 Mealie，同一条链路同样成立。

## 对比感

左边是 Mealie 原本的卡片式网页，右边是 Core 对话：
- 原生：得自己点搜索框、输入"番茄"、翻食谱、看食材、再切到购物清单手动加
- 嵌入 Core：说一句"今晚做番茄炒蛋，缺的食材加清单"，Core 自己串起搜菜→看食材→加购物项三步
