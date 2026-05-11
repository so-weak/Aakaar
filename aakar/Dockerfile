# syntax=docker/dockerfile:1.7
# ---- Aakaar backend image ----------------------------------------------------
# Uses Microsoft's Playwright base which already ships Chromium and the
# system libraries Playwright requires — saving ~500 MB of apt installs.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AAKAR_DATA_DIR=/data \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    TOKENIZERS_PARALLELISM=false \
    LOKY_MAX_CPU_COUNT=1

WORKDIR /app

# Install Python deps first so the dep layer caches across code edits.
COPY pyproject.toml ./
RUN python -m pip install --upgrade pip \
 && pip install --no-cache-dir \
        "pydantic>=2.6,<3" "sqlalchemy>=2.0,<3" "alembic>=1.13" \
        "numpy>=1.26" "faiss-cpu>=1.8" "openai>=1.40" \
        "sentence-transformers>=2.7" "fastapi>=0.115" "uvicorn[standard]>=0.30" \
        "bcrypt>=4.1" "PyJWT>=2.9" "python-multipart>=0.0.9" \
        "httpx>=0.27" "python-dotenv>=1.0" "playwright>=1.47" \
 && playwright install --with-deps chromium

# Copy source.
COPY aakar ./aakar
COPY alembic.ini ./alembic.ini
COPY interpreter ./interpreter
COPY tests ./tests

# Non-root runtime user.
RUN groupadd --system aakar \
 && useradd  --system --gid aakar --home /app aakar \
 && mkdir -p /data \
 && chown -R aakar:aakar /app /data
USER aakar

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Run migrations then boot uvicorn. Single worker is intentional — the
# in-process FAISS index and per-run browser pool are not fork-safe.
CMD alembic upgrade head && \
    exec uvicorn aakar.api.main:app \
      --host 0.0.0.0 --port 8000 --workers 1
