# syntax=docker/dockerfile:1.7
# AEVAF backend image - shared by the API, Celery worker, Celery beat and the directory watcher.
ARG PYTHON_VERSION=3.10

# ---------------------------------------------------------------- stage 1: build dependencies
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Compiler + libpq headers are only needed here, in case a wheel has to be built from source.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml ./
# Install third-party dependencies only, so this layer is cached until pyproject.toml changes.
# (A stub package satisfies the build backend; it is uninstalled again straight away.)
RUN mkdir app && touch app/__init__.py \
 && pip install . \
 && pip uninstall -y aevaf-backend

# ------------------------------------------------------------------ stage 2: runtime image
FROM python:${PYTHON_VERSION}-slim AS runtime

# ffmpeg is installed because the project spec asks for it; nothing in the code uses it yet.
# Build with --build-arg INSTALL_FFMPEG=false to save ~100 MB.
ARG INSTALL_FFMPEG=true

# libpq5 is the runtime half of libpq-dev (the headers stay behind in the builder stage).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && if [ "$INSTALL_FFMPEG" = "true" ]; then apt-get install -y --no-install-recommends ffmpeg; fi \
 && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    MEDIA_ROOT=/media

# Unprivileged user; /media is where the shared media volume is mounted.
RUN groupadd --system --gid 10001 aevaf \
 && useradd --system --uid 10001 --gid aevaf --home-dir /app --shell /usr/sbin/nologin aevaf \
 && mkdir -p /app /media \
 && chown aevaf:aevaf /app /media

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=aevaf:aevaf . /app
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod 0755 /app/docker-entrypoint.sh

USER aevaf
EXPOSE 8000

# Healthchecks are defined per service in docker-compose.yml (each service runs a different command).
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# One uvicorn worker on purpose: WebSocket job progress is held in this process's memory.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
