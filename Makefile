.PHONY: help install install-dev test lint format type-check clean docker-up docker-down docker-logs

# Default target
help:
	@echo "Favonius Energy - Development Makefile"
	@echo ""
	@echo "Available commands:"
	@echo "  make install       - Install production dependencies"
	@echo "  make install-dev   - Install development dependencies"
	@echo "  make test          - Run tests"
	@echo "  make lint          - Run linting and formatting checks"
	@echo "  make format        - Format code with black and isort"
	@echo "  make type-check    - Run type checking with mypy"
	@echo "  make docker-up     - Start docker-compose services"
	@echo "  make docker-down   - Stop docker-compose services"
	@echo "  make docker-logs   - View docker-compose logs"
	@echo "  make clean         - Clean build artifacts and caches"

# Installation
install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"
	pre-commit install

# Testing
test:
	pytest

test-unit:
	pytest tests/unit -v

test-integration:
	pytest tests/integration -v

test-e2e:
	pytest tests/e2e -v

test-coverage:
	pytest --cov=src --cov-report=html --cov-report=term

# Code quality
lint:
	ruff check src tests
	black --check src tests
	isort --check-only src tests
	mypy src

format:
	black src tests
	isort src tests

type-check:
	mypy src

# Docker operations
docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

docker-restart:
	docker-compose restart

# Cleanup
clean:
	find . -type d -name "__pycache__" -exec rm -r {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name "*.egg-info" -exec rm -r {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -r {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -r {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -r {} + 2>/dev/null || true
	rm -rf build/ dist/ .coverage htmlcov/ 2>/dev/null || true

# Development workflow
dev-setup: install-dev
	@echo "Development environment setup complete!"
	@echo "Run 'pre-commit install' if not already done"

# Quick checks before commit
pre-commit-checks: format lint type-check
	@echo "All pre-commit checks passed!"

