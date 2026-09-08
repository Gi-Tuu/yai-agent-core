# 逐行讲解 09 · `batteries/fastapi_server/app.py`：把 Core 变成在线 API

> Battery = 可选外围。内核不依赖 FastAPI；只有要部署成 HTTP 服务时才用它。
> 知识点：可选依赖的导入守卫、Pydantic 请求模型、闭包捕获、路由装饰器、JSON 安全序列化。
> 配套启动示例：`scripts/serve_example.py`。

## 块 1 · 依赖守卫（L15-L21）
```python
try:
    from fastapi import FastAPI
    from pydantic import BaseModel
except ImportError as exc:
    raise ImportError(
        "FastAPI Battery 需要可选依赖：uv pip install -e '.[server]'"
    ) from exc
```
- 模块被 import 时就尝试引入 fastapi/pydantic；没装就给出**可操作的安装提示**，而不是让用户面对一堆难懂的 traceback。
- 注意：这个文件属于 Battery，允许依赖第三方；`yai_core` 内核文件（types/kernel/spi...）依然零硬依赖。边界要清楚。

## 块 2 · 请求体模型（L24-L25）
```python
class RunRequest(BaseModel):
    task: str
```
- Pydantic 模型：声明 HTTP 请求体长什么样。客户端 POST `{"task": "..."}`，FastAPI 自动校验：缺字段/类型错会直接返回 422，并把 JSON 转成 `req.task`。
- **踩过的坑（真实记录）**：这个类最初写在 `create_app` 函数内部，FastAPI 无法把闭包里的模型识别成请求体，报 422 query 参数缺失。提到模块级后正常——所以框架的"魔法"也有边界，遇到怪错先怀疑作用域。

## 块 3 · create_app：工厂函数（L28-L31）
```python
def create_app(core: Any) -> FastAPI:
    app = FastAPI(title="YAI Agent Core API", version="0.1.0")
    commit = os.getenv("YAI_GIT_COMMIT", "dev")
    slug = os.getenv("YAI_PROJECT_SLUG", "yai-agent-core")
```
- **应用工厂**：传入一个已组装好的 core，返回一个 HTTP app。这样同一份代码可以挂不同宿主的 core。
- `core: Any`：Battery 不反向依赖内核具体类型，避免循环依赖，也方便测试时传假 core。
- commit/slug 从环境变量读，部署 X-Agent 时注入真实 pinned commit；本地缺省 "dev"。

## 块 4 · 四个路由（L33-L54）

### 4.1 健康检查（X-Agent 硬门槛）
```python
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "commit": commit}
```
- `@app.get(...)`：路由装饰器，把 URL 和函数绑定。
- 返回 dict，FastAPI 自动转 JSON。X-Agent 审核机就靠这个端点确认服务在线且 commit 与提交一致。

### 4.2 验证文件（X-Agent 硬门槛）
```python
@app.get("/.well-known/xagent-verification.json")
async def verification() -> dict:
    return {"schemaVersion": 1, "slug": slug, "commit": commit}
```
- 固定路径、固定字段（官方契约）。`/.well-known/` 是业界放站点元信息的惯例目录。

### 4.3 工具清单
```python
@app.get("/v1/tools")
async def list_tools() -> list[dict]:
    return core.list_tools()
```
- 直接复用门面方法，把"自动发现了哪些能力"暴露出去，评委 curl 一下就能看到宿主能力集。

### 4.4 执行任务
```python
@app.post("/v1/agent/run")
async def run_agent(req: RunRequest) -> dict:
    result = await core.run(req.task)
    return {
        "strategy": result.strategy.value,
        "final_text": result.final_text,
        "events": [
            {"type": e.type.value, "data": _jsonable(e.data)} for e in result.events
        ],
    }
```
- 请求体由 Pydantic 自动解析成 `req`。
- `await core.run(...)`：异步框架配异步内核，一个进程就能并发处理多个请求。
- 响应三部分：用了什么策略、最终结果、**全过程事件**（这是 YAI 的差异化：别的黑盒 API 只给结果，我们给可审计过程）。
- 列表推导式逐个事件转成可 JSON 化的字典。

## 块 5 · JSON 安全兜底（L59-L63）
```python
def _jsonable(data: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in data.items():
        out[k] = v if isinstance(v, str | int | float | bool | list | dict | None) else str(v)
```
- 事件 data 里可能混进枚举、异常对象等不能直接 json.dumps 的东西。
- `isinstance(v, 白名单类型)`：是 JSON 原生类型就原样保留，否则 `str(v)` 转字符串，保证响应永远不会因为序列化失败而 500。
- 这是"边界层做防御"的典型：内部可以灵活，对外输出必须规整。

## 块 6 · 怎么启动（对照 `scripts/serve_example.py`）
```python
core = AgentCore.auto(capabilities, _model)   # 组装内核（有 Key 用真模型，否则离线模型）
app = create_app(core)                        # 包成 HTTP 应用
```
命令行：
```powershell
$env:YAI_GIT_COMMIT="你的40位commit"; $env:YAI_PROJECT_SLUG="gi-tuu-yai"
.\.venv\Scripts\python.exe -m uvicorn scripts.serve_example:app --host 0.0.0.0 --port 8000
```
然后：
```powershell
curl http://localhost:8000/health
curl -X POST http://localhost:8000/v1/agent/run -H "Content-Type: application/json" -d '{"task":"列出全部笔记"}'
```

## 一次 HTTP 请求的完整链路
```
客户端 POST /v1/agent/run
  → Pydantic 校验请求体
  → core.run(task)
      → Router 选策略 → Loop 跑 ReAct → Executor 调宿主函数 → 事件流
  → 结果经 _jsonable 规整
  → JSON 响应回客户端
```

## 自检

1. 为什么 Pydantic 模型必须放模块级而不能放在 create_app 里？
2. `/health` 里的 commit 为什么要从环境变量读，而不是写死？
3. 为什么响应里要带 events？这对比赛评审意味着什么？
4. `_jsonable` 防的是什么问题？删掉它、在事件 data 里放一个枚举会怎样？
5. 把 host_c_companion 也包成一个 app（改 serve_example 即可），用 curl 跑通。
