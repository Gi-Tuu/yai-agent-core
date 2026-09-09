# YAI Agent Core —— 在线 API 示例镜像（X-Agent 部署形态）
# 内核零硬依赖；镜像只装 server + llm 两组可选依赖。
# 构建：docker build -t yai-agent-core .
# 运行：docker run --rm -p 8000:8000 --env-file .env yai-agent-core

FROM python:3.13-slim

# 复用官方 uv 二进制
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

WORKDIR /app

# 直接装进系统环境，省去激活 venv
ENV UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# 构建时固化 pinned commit（X-Agent 硬门槛：部署版本与提交哈希一致）
# docker build --build-arg YAI_GIT_COMMIT=$(git rev-parse HEAD) -t yai-agent-core .
ARG YAI_GIT_COMMIT=dev
ENV YAI_GIT_COMMIT=${YAI_GIT_COMMIT}

# 先拷依赖清单以利用层缓存；README 是 pyproject 的 readme 字段所需
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# 在线示例依赖 examples（宿主能力）与 scripts（启动模块）
COPY examples ./examples
COPY scripts ./scripts

RUN uv pip install --system -e ".[server,llm]"

EXPOSE 8000

# 容器级健康检查，直接打 X-Agent 要求的 /health 端点
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "scripts.serve_example:app", "--host", "0.0.0.0", "--port", "8000"]
