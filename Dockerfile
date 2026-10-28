# Production Dockerfile for STUDY-BUDDY
FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (hash-pinned)
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy app
COPY . .

# Non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:5000/healthz || exit 1

# Worker/timeout settings live ONLY in gunicorn.conf.py (audit fix,
# 2026-10-28) — same single source of truth as render.yaml. The conf
# file binds 0.0.0.0:$PORT (default 5000), matching EXPOSE + healthcheck.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "unified_app:app"]
