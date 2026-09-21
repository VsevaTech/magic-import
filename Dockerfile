# Magic Import — single-container image (FastAPI + SQLite)
# BASE_IMAGE is only overridden for offline/local builds; CI uses the official image.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

# install dependencies first for better layer caching
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

COPY demo-data ./demo-data

RUN useradd -r -u 10001 magic && mkdir -p /data && chown -R magic:magic /data /app
USER magic
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
