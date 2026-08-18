# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libpq is required by psycopg[binary] at runtime on slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM deps AS app
COPY . .

RUN useradd --create-home --uid 1000 pipeline \
    && mkdir -p raw_data raw_data/_ingested \
    && chown -R pipeline:pipeline /app
USER pipeline

# No long-running server here — this image runs one-shot pipeline commands.
# Override the command per-run, e.g.:
#   docker run --env-file .env real-estate-pipeline python data_pipeline.py
#   docker run --env-file .env real-estate-pipeline alembic upgrade head
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "from src.config import get_settings; get_settings(); print('ok')" || exit 1

ENTRYPOINT []
CMD ["python", "data_pipeline.py"]
