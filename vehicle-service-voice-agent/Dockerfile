# SpeedCare Voice Agent - Docker Configuration
# Multi-stage build for FastAPI backend + LiveKit voice agent worker

FROM python:3.11-slim AS base

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster package management
RUN pip install --no-cache-dir uv

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies using uv
RUN uv pip install --no-cache --system -r requirements.txt

# Production stage for API server
FROM base AS api

COPY . .

# Create non-root user for security
RUN useradd -m -u 1000 speedcare && chown -R speedcare:speedcare /app
USER speedcare

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

EXPOSE 8000

# Default command for API server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

# Worker stage for LiveKit voice agent
FROM base AS worker

COPY . .

# Create non-root user
RUN useradd -m -u 1000 speedcare && chown -R speedcare:speedcare /app
USER speedcare

# Health check for worker (ensures Python process is running)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD pgrep -f "python.*simple_agent.py" > /dev/null || exit 1

# Default command for worker
CMD ["python", "simple_agent.py", "dev"]

# Combined stage (for simpler deployments - runs both API and worker)
FROM base AS combined

COPY . .
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Create non-root user
RUN useradd -m -u 1000 speedcare && chown -R speedcare:speedcare /app
USER speedcare

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
