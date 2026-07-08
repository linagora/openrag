# Build from repo root: docker build -f infra/docker/api.Dockerfile .
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

# Keep uv's managed Python and cache on stable, root-owned paths outside any
# user $HOME, and put the project venv under /app so it can be made
# group-writable for the arbitrary UID OpenShift assigns (see below). HOME is a
# dedicated writable subdir (not /app) so libraries that fall back to $HOME
# never need /app itself writable. UV_FROZEN keeps `uv run` from rewriting
# uv.lock at runtime, so the project root can stay read-only.
# USER/LOGNAME are set because the arbitrary UID OpenShift assigns has no
# /etc/passwd entry: getpass.getuser() reads these env vars first and so
# resolves without a passwd lookup (the same approach used for the vllm
# service in docker-compose.yaml). This avoids making /etc/passwd writable.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_CACHE_DIR=/opt/uv/cache \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_FROZEN=1 \
    HOME=/app/home \
    USER=openrag \
    LOGNAME=openrag

# Install uv & setup venv
COPY pyproject.toml uv.lock ./
RUN pip3 install uv && \
    uv python install 3.12.7 && \
    uv python pin 3.12.7
    # && \ uv sync --no-dev
COPY infra/scripts/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
# Set workdir for source code
WORKDIR /app/openrag

# Copy source code
COPY openrag/ .

# Copy assets and config (prompt templates ship inside the package under openrag/prompts/)
COPY scripts/ /app/scripts/
COPY conf/ /app/conf/
ENV PYTHONPATH=/app/openrag/
ENV APP_iPORT=${APP_iPORT:-8080}

# --- Run as an unprivileged, OpenShift-compatible user ---------------------
# OpenShift runs containers as an arbitrary, unpredictable UID that is always a
# member of the root group (GID 0). For the app to start under that policy,
# every path it writes at runtime must be group-owned by GID 0 and
# group-writable (chmod g=u). We strip group-write from the whole tree first
# (chmod -R g-w) and then grant it back ONLY on those exact paths: the venv,
# the editable install's egg-info, $HOME, data, db, logs, the HF model cache,
# and uv's cache. So /app and /opt/uv themselves, plus the copied code/config
# and uv's managed Python (/app/openrag, /app/conf, /app/scripts,
# /opt/uv/python), stay group-readable but NOT group-writable regardless of
# their source mode — the arbitrary UID cannot create, rename, or replace
# entries in them.
# The two exceptions under the otherwise read-only /app/openrag are Chainlit's
# runtime-writable dirs: it creates ./.files at import time and writes
# ./.chainlit/config.toml + translations on startup (WORKDIR is /app/openrag),
# so both are pre-created and granted group-write below or the app crashes with
# PermissionError: '/app/openrag/.files'.
# We also bake a fixed non-root UID for plain Docker/Kubernetes, where the
# arbitrary-UID remap does not happen; APP_UID is a build arg so a compose
# build can match the host user that owns the bind-mounted volumes. The user's
# primary group is 0 so it shares the same group access on either platform.
ARG APP_UID=10001
RUN useradd --uid ${APP_UID} --gid 0 --no-log-init --no-create-home \
        --home-dir /app/home --shell /sbin/nologin openrag \
    && mkdir -p /app/home /app/data /app/db /app/logs /app/model_weights/hub \
        /app/.venv /app/openrag.egg-info /opt/uv/cache \
        /app/openrag/.files /app/openrag/.chainlit \
    && chgrp -R 0 /app /opt/uv \
    && chmod -R g-w /app /opt/uv \
    && chmod -R g=u /app/home /app/data /app/db /app/logs /app/model_weights \
        /app/.venv /app/openrag.egg-info /opt/uv/cache \
        /app/openrag/.files /app/openrag/.chainlit
# Expose APP_UID at runtime so entrypoint.sh can drop back to this user after
# fixing bind-mount permissions (when the container is started as root, e.g.
# compose `user: "0:0"`). USER below keeps the image's default non-root.
ENV APP_UID=${APP_UID}
USER ${APP_UID}

ENTRYPOINT ../entrypoint.sh
