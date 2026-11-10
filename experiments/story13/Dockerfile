FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.14.8-slim AS build
COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PYTHONNOUSERSITE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY main.py ./

FROM python:3.14.8-slim AS baseline
WORKDIR /app
ENV PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8088
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/main.py /app/main.py
EXPOSE 8088
CMD ["/app/.venv/bin/python", "main.py", "--host", "0.0.0.0"]

FROM baseline AS optimized
RUN PYTHONDONTWRITEBYTECODE= /app/.venv/bin/python -m compileall -q /usr/local/lib/python3.14 /app/.venv || true
RUN PYTHONDONTWRITEBYTECODE= /app/.venv/bin/python -m compileall -q /app/main.py
