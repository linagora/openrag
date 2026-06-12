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
    libpq-dev python3-dev \
    # Cairo libraries for SVG support (cairosvg)
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libffi-dev libgdk-pixbuf-xlib-2.0-0 shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# install ffmpeg
RUN apt update && \
    apt install -y ffmpeg 

# Set environment variables for Hugging Face cache location
ENV XDG_CACHE_HOME=${XDG_CACHE_HOME:-/app/model_weights}
ENV HF_HOME=${HF_HOME:-/app/model_weights}
ENV HF_HUB_CACHE=${HF_HUB_CACHE:-/app/model_weights/hub}

# Set workdir for uv
WORKDIR /app

# Keep uv's managed Python, cache and project venv outside $HOME so the
# artifacts produced during the (root) build stay reachable for the
# unprivileged user that runs the container.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_CACHE_DIR=/opt/uv/cache \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Install uv & setup venv
COPY pyproject.toml uv.lock ./
RUN pip3 install uv && \
    uv python install 3.12.7 && \
    uv python pin 3.12.7
    # && \ uv sync --no-dev
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
# Set workdir for source code
WORKDIR /app/openrag

# Copy source code
COPY openrag/ .

# Copy assets and config
COPY prompts/ /app/prompts/
COPY conf/ /app/conf/
ENV PYTHONPATH=/app/openrag/
ENV APP_iPORT=${APP_iPORT:-8080}

# --- Run as an unprivileged user -------------------------------------------
# openrag only ever writes under /app (data, logs, db, the HF model cache and
# the uv-created venv) plus uv's cache, and binds non-privileged ports, so it
# has no need for root. Create a dedicated user and give it ownership of *only*
# those runtime-write paths, plus the /app dir node itself so the runtime
# `uv run` can create the venv and the editable install's openrag.egg-info at
# the project root. The application code, config files and uv's managed Python
# install stay root-owned/read-only (least privilege). UID/GID are build
# args so a deployment can match the host user owning the bind-mounted volumes
# (./data, ./logs, ~/.cache/huggingface); the 1000 default fits a typical
# single-user host.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid ${APP_GID} openrag \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --no-log-init --create-home --shell /bin/bash openrag \
    && mkdir -p /app/data /app/logs /app/db /app/model_weights/hub /app/.venv /opt/uv/cache \
    && chown openrag:openrag /app \
    && chown -R openrag:openrag \
       /app/data /app/logs /app/db /app/model_weights /app/.venv /opt/uv/cache
USER openrag

ENTRYPOINT ../entrypoint.sh
