# Multi-stage Docker build for Favonius Energy EV Fleet Depot Optimization Platform
# Per PRD_v2.md: Python 3.12, Pyomo + Gurobi (primary) with HiGHS fallback
FROM python:3.12-slim as builder

# Set build arguments
ARG DEBIAN_FRONTEND=noninteractive

# Install system dependencies for building
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    g++ \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create and set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim as runtime

# Set build arguments
ARG DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies
# Gurobi requires specific system libraries (per PRD Section 8.2)
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r appuser && useradd -r -g appuser appuser

# Set working directory
WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY src/ ./src/
COPY README.md ./

# Create directories for logs and data
RUN mkdir -p /app/logs /app/data && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Set Python path
ENV PYTHONPATH=/app/src

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Default environment values
ENV WEBSOCKET_PORT=9000
ENV METRICS_PORT=8080
ENV HEALTH_CHECK_PORT=8081
ENV LOG_LEVEL=INFO
ENV ENVIRONMENT=production

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${HEALTH_CHECK_PORT}/liveness || exit 1

# Expose ports
EXPOSE ${WEBSOCKET_PORT} ${METRICS_PORT} ${HEALTH_CHECK_PORT}

# Run the application
# Per PRD_v2.md Section 7.1: FastAPI REST API server
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
