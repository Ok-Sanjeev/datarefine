# AI Data Cleaner - Hardened Production Dockerfile
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user and group
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -s /bin/bash -m appuser

WORKDIR /app

# Copy dependency definition and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Create required operational directories and grant ownership to non-root appuser
RUN mkdir -p data runs uploads data/uploads && \
    chown -R appuser:appuser /app

# Switch to non-root execution
USER appuser

# Expose default HTTP port
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/health || exit 1

# Start FastAPI server with dynamic PORT support for cloud runtimes (Render, Railway, Cloud Run)
CMD ["sh", "-c", "python server.py --host 0.0.0.0 --port ${PORT:-8000}"]
