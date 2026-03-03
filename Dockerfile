FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PORT=8000

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
RUN uv sync --no-dev --no-install-project

COPY . /app

EXPOSE 8000

CMD ["sh", "-c", "uv run --no-sync uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
