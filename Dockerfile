FROM python:3.12-slim

# Install system dependencies (pg_dump for backups)
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 appuser \
    && mkdir -p /tmp/backups /app/logs \
    && chown -R appuser:appuser /app /tmp/backups /app/logs

USER appuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV LOG_JSON=true

# Health check: verify DB connectivity
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import asyncio, asyncpg, os; asyncio.run(asyncpg.connect(os.environ['DATABASE_URL']))" || exit 1

CMD ["python", "start.py"]
