<!-- 一个 PR 只解决一件事；标题遵循 Conventional Commits：feat / fix / docs / test / refactor / chore -->

## 改了什么

-

## 为什么

-

## 怎么验证的

```bash
# 粘贴你实际跑过的命令与结果，例如：
uv run ruff check src tests examples scripts
uv run pytest
uv run python scripts/smoke_test.py
```

## 清单

- [ ] `ruff check` 通过
- [ ] `pytest` 全绿（新行为有对应测试，且测试不依赖网络/API Key）
- [ ] 没有改动 `pyproject.toml` 的 `dependencies`（新第三方依赖只进可选 extra 且模块内懒加载）
- [ ] 改了行为的地方已同步 `docs/walkthrough/` 对应篇目
- [ ] 新增的自适应决策有对应 `AgentEvent`，不存在静默决策
- [ ] 没有提交 `.env`、API Key、临时脚本或临时服务文件
- [ ] 公开材料中未出现真实姓名等个人隐私（对外署名只用 GitHub: Gi-Tuu / 队名 YAI）

## 是否改动公开 API / 架构红线

- [ ] 否
- [ ] 是（请说明，并在描述中 @ 维护者确认）：
