# ATLAS backend — FastAPI execution middleware (spec §1, §6)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build deps for asyncpg / hiredis wheels, removed in the same layer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && apt-get purge -y --auto-remove build-essential

COPY app ./app
COPY pytest.ini ./
COPY tests ./tests

# Run unprivileged — the execution bridge holds broker credentials.
RUN useradd --create-home --uid 10001 atlas \
 && chown -R atlas:atlas /app
USER atlas

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
