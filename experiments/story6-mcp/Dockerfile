FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.14.8-slim

COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/backend/app ./src/backend/app
COPY scripts ./scripts
COPY LICENSE ./
RUN uv sync --frozen --no-dev --no-editable \
    && .venv/bin/python -c "import app.story6_mcp"

EXPOSE 8092
CMD ["/app/.venv/bin/python", "-m", "app.story6_mcp"]
