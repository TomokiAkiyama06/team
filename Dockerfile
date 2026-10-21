# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# OCR（スキャンPDF用・ローカル Tesseract）を使う場合のみ true にする
ARG INSTALL_OCR=false

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    TZ=Asia/Tokyo

RUN if [ "$INSTALL_OCR" = "true" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-jpn \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

# 依存のみ先にインストール（レイヤーキャッシュ用）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev

RUN groupadd --system --gid 10001 classbot \
    && useradd --system --uid 10001 --gid classbot --home-dir /app --shell /usr/sbin/nologin classbot \
    && mkdir -p /app/materials /app/data \
    && chown -R classbot:classbot /app/materials /app/data

USER classbot

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["classbot", "healthcheck"]

CMD ["classbot", "run"]
