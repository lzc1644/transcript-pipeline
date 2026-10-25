# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ARG CADDY_IMAGE=caddy:2-alpine@sha256:24ec7b05bc532c7bb57f727030c5056e257ff7138b4b50460306819ff79362bd

FROM ${NODE_IMAGE} AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
WORKDIR /extensions/dictation
COPY extensions/dictation/package.json extensions/dictation/package-lock.json ./
RUN npm ci
COPY extensions/dictation/ ./
WORKDIR /frontend
COPY frontend/ ./
COPY shared/ /shared/
RUN npm --prefix /extensions/dictation run package:dev
RUN npm run build

FROM ${CADDY_IMAGE} AS https-binary

FROM ${PYTHON_IMAGE} AS app
ARG APP_UID=1000
ARG APP_GID=1000

ENV HOME=/home/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        ffmpeg \
        libgomp1 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir -p /home/app/.cache/transcript-pipeline \
    && chown -R app:app /home/app

COPY requirements.txt ./requirements.txt
COPY docker/constraints.txt ./constraints.txt
RUN python -m venv /app/.venv \
    && /app/.venv/bin/python -m pip install --no-cache-dir \
        --constraint /app/constraints.txt \
        --requirement /app/requirements.txt \
        nvidia-cublas-cu12 \
        nvidia-cudnn-cu12 \
    && /app/.venv/bin/python -m pip check

# Optional SDKs live in a separate interpreter: the Web/Whisper environment
# keeps its existing Pydantic/Hugging Face/CUDA runtime versions.
ARG ASR_BACKENDS=whisper
COPY requirements-asr-*.txt ./
RUN case "${ASR_BACKENDS}" in \
      whisper) ;; \
      qwen|funasr|all) \
        /app/.venv/bin/python -m venv /app/.venv/asr-py312 \
        && case "${ASR_BACKENDS}" in \
             qwen) ASR_REQUIREMENTS="-r requirements-asr-qwen.txt" ;; \
             funasr) ASR_REQUIREMENTS="-r requirements-asr-funasr.txt" ;; \
             all) ASR_REQUIREMENTS="-r requirements-asr-qwen.txt -r requirements-asr-funasr.txt" ;; \
           esac \
        && /app/.venv/asr-py312/bin/python -m pip install --no-cache-dir --retries 10 --timeout 60 pip==26.2.1 \
        && /app/.venv/asr-py312/bin/python -m pip install --no-cache-dir --retries 10 --timeout 60 --constraint requirements-asr-constraints.txt setuptools wheel \
        && /app/.venv/asr-py312/bin/python -m pip install --no-cache-dir --retries 10 --timeout 60 --no-build-isolation --resume-retries 10 ${ASR_REQUIREMENTS} \
        && /app/.venv/asr-py312/bin/python -m pip check ;; \
      *) echo "ASR_BACKENDS must be whisper, qwen, funasr or all" >&2; exit 2 ;; \
    esac

# faster-whisper 1.2.1 calls av.open(metadata_errors=...). PyAV 19 removed
# that API; keep the verified base decoder binding independently of worker av.
RUN /app/.venv/bin/python -m pip install --no-cache-dir av==16.1.0 \
    && /app/.venv/bin/python -m pip check

# Dictation adds CPU-only dependencies in its own interpreter. Model weights
# are prepared explicitly into the persistent data mount, never on mic start.
ARG DICTATION=0
COPY --chown=app:app requirements-dictation.txt ./requirements-dictation.txt
RUN if [ "${DICTATION}" = "1" ]; then \
      /app/.venv/bin/python -m venv /app/.venv/dictation-py312 \
      && /app/.venv/dictation-py312/bin/python -m pip install --no-cache-dir -r requirements-dictation.txt \
      && /app/.venv/dictation-py312/bin/python -m pip check; \
    elif [ "${DICTATION}" != "0" ]; then echo "DICTATION must be 0 or 1" >&2; exit 2; fi

# Reuse Caddy's static binary, with Supervisor owning both process lifecycles.
# No second runtime image/container or host Caddy configuration is required.
COPY --from=https-binary /usr/bin/caddy /usr/local/bin/caddy
COPY docker/requirements-runtime.txt /app/requirements-runtime.txt
RUN /app/.venv/bin/python -m pip install --no-cache-dir -r /app/requirements-runtime.txt \
    && /app/.venv/bin/python -m pip check \
    && mkdir -p /app/data /data/caddy /config/caddy \
    && chown -R app:app /app/data /data /config \
    # Changing ownership clears Caddy's upstream file capability. Port 8443
    # needs no capability, so the final container can retain cap_drop: ALL.
    && chown app:app /usr/local/bin/caddy
COPY --chown=app:app docker/ /app/docker/
ENV HTTPS_ENABLED=true \
    HTTPS_ADDRESSES=https://localhost:8443 \
    HTTPS_DEFAULT_SNI=localhost

# COPY preserves restrictive host modes (new files may be 0600/0700).
# Give the non-root runtime ownership instead of relying on world-readable sources.
COPY --chown=app:app api_server.py ./api_server.py
COPY --chown=app:app config/settings.yaml ./config/settings.yaml
COPY --chown=app:app config/prompts/ ./config/prompts/
COPY --chown=app:app config/glossaries/ ./config/glossaries/
COPY --chown=app:app scripts/ ./scripts/
COPY --chown=app:app src/ ./src/
COPY --from=frontend-builder --chown=app:app /frontend/dist/ /app/frontend/dist/

USER app
EXPOSE 8000 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/bin/sh", "/app/docker/healthcheck.sh"]

CMD ["/app/.venv/bin/supervisord", "-c", "/app/docker/supervisord.conf"]
