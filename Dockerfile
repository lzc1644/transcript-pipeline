# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

FROM ${NODE_IMAGE} AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

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

COPY api_server.py ./api_server.py
COPY config/settings.yaml ./config/settings.yaml
COPY config/prompts/ ./config/prompts/
COPY config/glossaries/ ./config/glossaries/
COPY scripts/ ./scripts/
COPY src/ ./src/
COPY --from=frontend-builder /frontend/dist/ /app/frontend/dist/

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8000/api/health >/dev/null || exit 1

CMD ["/app/.venv/bin/python", "-m", "uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
