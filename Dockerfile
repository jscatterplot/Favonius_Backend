# Favonius Energy EV Fleet Depot Optimization Platform
# Multi-stage Docker build
#
# Reference: PRD_v2_7_Building_Integration.md Section 8.2 (Gurobi + HiGHS),
#            favonius_development_plan_v3.md (Phase-aligned production deployment)
#
# Build: docker build -t favonius-api .
# Run:   docker run -p 8000:8000 -p 9000:9000 favonius-api

# ============ Builder Stage ============
# TODO: Pin by SHA256 digest for supply-chain safety (run: docker pull python:3.12-slim && docker inspect --format='{{.RepoDigests}}' python:3.12-slim)
FROM python:3.12-slim AS builder

# Set build arguments
ARG DEBIAN_FRONTEND=noninteractive

# Install system dependencies for building
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    g++ \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package installer)
RUN pip install --no-cache-dir uv

# Create and set working directory
WORKDIR /app

# Copy dependency manifest
COPY pyproject.toml ./

# Create virtual environment and install runtime dependencies from pyproject
# This keeps Docker installs aligned with the project dependency source of truth.
RUN uv venv /app/.venv && \
    . /app/.venv/bin/activate && \
    python -c "import tomllib, pathlib; print('\n'.join(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']))" > /tmp/requirements.runtime.txt && \
    uv pip install --no-cache-dir -r /tmp/requirements.runtime.txt

# ============ Runtime Stage ============
# Pin base image for supply chain security (NIS2 Article 21)
# Update this digest when upgrading the base image
# TODO: Pin by SHA256 digest for supply-chain safety (run: docker pull python:3.12-slim && docker inspect --format='{{.RepoDigests}}' python:3.12-slim)
FROM python:3.12-slim AS runtime

# Set build arguments
ARG DEBIAN_FRONTEND=noninteractive
# Security TODO: Use BuildKit --mount=type=secret instead of ARG to prevent
# license key from appearing in docker image history.
# Build with: docker build --secret id=maxmind_key,src=maxmind.key .
# Then: RUN --mount=type=secret,id=maxmind_key curl ... $(cat /run/secrets/maxmind_key)
ARG MAXMIND_LICENSE_KEY
ARG MAXMIND_ACCOUNT_ID

# Install runtime dependencies
# Per PRD Section 8.2: Gurobi requires specific system libraries
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    file \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r appuser && useradd -r -g appuser appuser

# Set working directory
WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code and migrations (for pre-deploy runner)
COPY src/ ./src/
COPY config/ ./config/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY README.md ./
COPY schemas/ ./schemas/

# Download MaxMind GeoLite2-Country database for Article 73-3 geo-blocking.
# The database is loaded into memory at startup (~5MB) for sub-microsecond lookups.
# Rebuild the image monthly to refresh the database.
#
# Authenticates via HTTP Basic Auth (account ID + license key) per MaxMind's
# 2024 policy change. Both MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY must be
# set as build args; without them the build skips the download and the runtime
# fallback in geo_block.py picks up the same credentials at startup.
RUN mkdir -p /app/data && \
    if [ -n "${MAXMIND_LICENSE_KEY:-}" ] && [ -n "${MAXMIND_ACCOUNT_ID:-}" ]; then \
        if curl -sSL --fail --retry 3 --retry-delay 5 --retry-all-errors \
            -u "${MAXMIND_ACCOUNT_ID}:${MAXMIND_LICENSE_KEY}" \
            -o /tmp/geoip.tar.gz \
            "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download?suffix=tar.gz"; then \
            if file /tmp/geoip.tar.gz | grep -q gzip; then \
                tar -xzf /tmp/geoip.tar.gz --strip-components=1 -C /app/data --wildcards '*/GeoLite2-Country.mmdb' && \
                echo "GeoLite2-Country.mmdb downloaded successfully"; \
            else \
                echo "WARNING: MaxMind download did not return a gzip archive — runtime fallback will retry"; \
            fi; \
        else \
            echo "WARNING: MaxMind download failed after retries — runtime fallback will retry"; \
        fi; \
        rm -f /tmp/geoip.tar.gz; \
    elif [ -n "${MAXMIND_LICENSE_KEY:-}" ]; then \
        echo "WARNING: MAXMIND_LICENSE_KEY set but MAXMIND_ACCOUNT_ID missing — MaxMind requires both since 2024. Skipping build-time download."; \
    else \
        echo "MAXMIND_ACCOUNT_ID/MAXMIND_LICENSE_KEY not set — GeoIP DB must be mounted at /app/data/GeoLite2-Country.mmdb"; \
    fi

# Create directories for logs, data, and ensure proper permissions
RUN mkdir -p /app/logs /app/data /opt/gurobi && \
    chown -R appuser:appuser /app /opt/gurobi

# Set Python environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Default environment variables
ENV API_PORT=8000
ENV OCPP_SERVER_PORT=9000
ENV LOG_LEVEL=INFO
ENV ENVIRONMENT=production
ENV GEOIP_DB_PATH=/app/data/GeoLite2-Country.mmdb

# Switch to non-root user
USER appuser

# Health check (use PORT at runtime when set, e.g. Railway)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["sh", "-c", "curl -f http://localhost:${PORT:-8000}/health || exit 1"]

# Expose ports
# Per PRD Section 7.1: REST API, OCPP WebSocket (same port when OCPP_USE_SAME_PORT=true)
EXPOSE 8000 9000

# Run migrations then start the application.
# Migrations are idempotent (skip already-applied files) so running them on every
# container start is safe and ensures schema is always up-to-date before the API
# tries to query tables like "depots".
# PORT is set by Railway at runtime (e.g. 8080); default 8000 for local.
CMD ["sh", "-c", "python scripts/run_migrations.py && exec uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
