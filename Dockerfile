FROM python:3.13-slim

WORKDIR /app

# 安装 uv（依赖管理工具）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# 先只拷依赖声明文件，利用 Docker 层缓存：代码没变但依赖没变时不用重新装依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 再拷代码
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
