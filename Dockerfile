# Favonius Energy EV Fleet Depot Optimization Platform
# Multi-stage Docker build
#
# Reference: PRD_v2.md Section 8.2 (Gurobi + HiGHS), Development Plan Phase 7
#
# Build: docker build -t favonius-api .
# Run:   docker run -p 8000:8000 -p 9000:9000 favonius-api

# ============ Builder Stage ============
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

# Copy dependency files
COPY pyproject.toml ./

# Create virtual environment and install dependencies using uv
# Per PRD Section 8.2: Gurobi primary, HiGHS fallback
RUN uv venv /app/.venv && \
    . /app/.venv/bin/activate && \
    uv pip install --no-cache -r pyproject.toml

# ============ Runtime Stage ============
FROM python:3.12-slim AS runtime

# Set build arguments
ARG DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies
# Per PRD Section 8.2: Gurobi requires specific system libraries
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
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

# Switch to non-root user
USER appuser

# Health check (use PORT at runtime when set, e.g. Railway)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["sh", "-c", "curl -f http://localhost:${PORT:-8000}/health || exit 1"]

# Expose ports
# Per PRD Section 7.1: REST API, OCPP WebSocket (same port when OCPP_USE_SAME_PORT=true)
EXPOSE 8000 9000

# Run the application. PORT is set by Railway at runtime (e.g. 8080); default 8000 for local.
CMD ["sh", "-c", "exec uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
