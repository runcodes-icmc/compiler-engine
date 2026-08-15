FROM ghcr.io/astral-sh/uv:0.11.29 AS uv

FROM python:3.14-slim-trixie AS runtime

# Install dependencies
RUN apt-get update &&\
    apt-get install --no-install-recommends -y curl iptables libdevmapper-dev libpq-dev python3-dev &&\
    apt-get clean &&\
    rm -rf /var/lib/apt/lists/*

FROM runtime AS build

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
