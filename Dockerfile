# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.11.32 AS uv

FROM python:3.12.13-slim-bookworm AS runtime

ARG UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    WBB_HOST=0.0.0.0 \
    WBB_PORT=8000 \
    WBB_DATA_DIR=/data \
    WBB_DATABASE_PATH=/data/wechat-bridge.db \
    WBB_CREDENTIALS_PATH=/data/wechat/credentials.json \
    WEB_CONCURRENCY=1

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

# 先复制依赖元数据，使依赖安装层可以复用构建缓存。
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin app \
    && mkdir -p /data/wechat \
    && chown -R app:app /data

USER app:app

VOLUME ["/data"]
EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port = os.getenv('WBB_PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health/live', timeout=3).read()"]

# 保持单进程：iLink 长轮询只能有一个明确所有者；扫码登录由常驻服务的
# Basic Auth 管理页面驱动。
ENTRYPOINT ["wechat-bot-bridge"]
CMD ["serve"]
