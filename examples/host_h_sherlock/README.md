# host_h —— 嵌入真实开源项目 sherlock

> 这是 YAI "零改造嵌入真实软件" 的最强证据：**sherlock**（92.5k stars, MIT）
> 是一个独立的、真实在用的 OSINT 工具，我们没有改它一行源码，
> 只在它外面包了一个函数，Core 就用自然语言把它调起来了。

## 这个宿主证明什么

- sherlock 原本是个命令行工具，内部函数 `sherlock()` 要传 `site_data`、
  `query_notify` 这类 CLI 风格对象，不适合直接交给 LLM。
- 我们在 `capabilities.py` 里只做了一件事：加载站点清单 → 收集回调结果 →
  过滤"已注册（Claimed）"，暴露 `lookup_username(username, limit, timeout)`
  三个语义清晰的参数。
- **不改 sherlock 源码**，这层 wrapper 就是"宿主只声明能力"的全部工作。

## 运行

```bash
# 终端版（离线脚本模型，无需 API Key）
uv run python examples/host_h_sherlock/run.py
uv run python examples/host_h_sherlock/run.py "查 gi-tuu 的社交账号"

# 网页版（左侧原生 CLI，右侧 Core 对话，命中站点渲染成链接卡片）
uv run python examples/host_h_sherlock/web_app.py
# 浏览器自动打开 http://127.0.0.1:8202
```

配置 `OPENAI_API_KEY` 后会自动换成真实模型，同一条链路同样成立。

## 安装

sherlock 是可选依赖：

```bash
uv pip install "yai-agent-core[sherlock]"
# 或在本仓库开发时：
uv pip install sherlock-project
```

## 注意

- 真实查询会联网访问各站点首页，演示时 `limit` 建议 10~20，否则较慢。
- sherlock 是**可选依赖**：未安装时本宿主不可用，但不影响 Core 与其他宿主。
