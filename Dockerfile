ARG PYTHON_IMAGE=python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83

FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv
FROM ${PYTHON_IMAGE}

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY scripts/provision_database_roles.py ./scripts/provision_database_roles.py
COPY scripts/provision_mfa_secret_volume.py ./scripts/provision_mfa_secret_volume.py
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
    && groupadd --gid 10001 trading-bot \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin trading-bot

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "trading_bot.app:app", "--host", "0.0.0.0", "--port", "8000"]
