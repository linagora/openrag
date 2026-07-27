# Build from repo root: docker build -f infra/docker/ray.Dockerfile .
FROM python:3.12-slim

# Installer curl
RUN apt-get update && apt-get install -y curl && apt-get clean
RUN apt-get update && apt-get install -y git && apt-get clean
RUN apt-get update && apt-get install -y iputils-ping
RUN apt-get update && apt-get install -y \
    build-essential \
    g++ \
    gcc \
    cmake \
    make \
    ssh \ 
    rsync \
    wget \
    libpq-dev python3-dev \
    && rm -rf /var/lib/apt/lists/*

# install ffmpeg
RUN apt update && \
    apt install -y ffmpeg

# Node + promptfoo back the admin evaluation page: EvalRunner shells out to
# `promptfoo eval`. Pinned rather than run through `npx promptfoo@latest` so a
# run never depends on npm reachability — or on the CLI's behaviour changing
# under a deployment that was not rebuilt.
ARG PROMPTFOO_VERSION=0.121.19
# Debian's nodejs package is 20.19.x, below promptfoo's floor
# (^20.20.0 || >=22.22.0), so Node comes from NodeSource instead.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g promptfoo@${PROMPTFOO_VERSION} \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*
ENV PROMPTFOO_DISABLE_TELEMETRY=1 \
    PROMPTFOO_DISABLE_UPDATE=1


# Set environment variables for Hugging Face cache location
ENV XDG_CACHE_HOME=${XDG_CACHE_HOME:-/app/model_weights}
ENV HF_HOME=${HF_HOME:-/app/model_weights}
ENV HF_HOME=/app/model_weights
ENV HF_HUB_CACHE=${HF_HUB_CACHE:-/app/model_weights/hub}


# Set workdir for uv
WORKDIR /app

# Set HOME before installing so uv's Python and cache land under /app (owned by
# the non-root user below), not /root.
ENV HOME=/app

# Install uv & setup venv
COPY pyproject.toml uv.lock ./
RUN pip3 install uv && \
    uv python install 3.12.7 && \
    uv python pin 3.12.7 
# && \uv sync --no-dev
COPY infra/scripts/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Set workdir for source code
WORKDIR /app/openrag

# Copy source code
COPY openrag/ .

# Copy assets and config (prompt templates ship inside the package under openrag/prompts/)
COPY scripts/ /app/scripts/
COPY conf/ /app/conf/

RUN ln -s /app/.venv/bin/ray /usr/local/bin/ray

ENV PYTHONPATH=/app/openrag/

# Run as non-root. The app writes under /app (venv, data, logs, model_weights),
# so the user owns /app.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home app \
    && mkdir -p /app/data /app/logs /app/model_weights \
    && chown -R 10001:10001 /app
USER 10001:10001
