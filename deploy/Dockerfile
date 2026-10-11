FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy TZ=Asia/Shanghai PATH=/app/.venv/bin:$PATH
RUN sed -i "s|http://deb.debian.org/debian-security|https://mirrors.cloud.tencent.com/debian-security|g; s|http://deb.debian.org/debian|https://mirrors.cloud.tencent.com/debian|g" /etc/apt/sources.list.d/debian.sources
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update && apt-get install -y --no-install-recommends chromium curl nodejs libgl1 libglib2.0-0 fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --index-url https://mirrors.cloud.tencent.com/pypi/simple uv
RUN useradd --uid 10001 --create-home app
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/deploy/requirements.txt ./requirements.txt
RUN uv venv --python /usr/local/bin/python .venv && uv pip install --python .venv/bin/python --index-url https://mirrors.cloud.tencent.com/pypi/simple -r requirements.txt
COPY crawler /opt/MediaCrawler
WORKDIR /opt/MediaCrawler
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
RUN uv venv --python /usr/local/bin/python .venv && uv pip install --python .venv/bin/python --index-url https://mirrors.cloud.tencent.com/pypi/simple -r requirements-deploy.txt
COPY backend/deploy/patch_crawler.py /tmp/patch_crawler.py
RUN python /tmp/patch_crawler.py
WORKDIR /app
COPY backend/app ./app
COPY backend/scripts ./scripts
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY backend/deploy/chromium-wrapper /usr/local/bin/aigc-chromium
RUN chmod 755 /usr/local/bin/aigc-chromium && mkdir -p /app/crawl_media /opt/MediaCrawler/.chrome_debug_profile /opt/MediaCrawler/data && chown -R app:app /app /opt/MediaCrawler
RUN apt-get update && apt-get install -y --no-install-recommends xvfb && rm -rf /var/lib/apt/lists/*
COPY backend/deploy/start-app.sh /usr/local/bin/aigc-start
RUN chmod 755 /usr/local/bin/aigc-start
ENV DISPLAY=:99
USER app
ENTRYPOINT ["/usr/local/bin/aigc-start"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
