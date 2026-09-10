FROM ghcr.io/astral-sh/uv:0.12.10 AS uv

FROM python:3.14-alpine AS build

# Install uv & setup install dir
COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# Load dependencies into a virtualenv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY ./pyproject.toml /app/pyproject.toml
COPY ./uv.lock /app/uv.lock

RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH"

FROM build AS dist

# Load source
COPY ./src ./

# Entrypoint
STOPSIGNAL SIGINT
CMD [ "python", "main.py", "--config", "env" ]
