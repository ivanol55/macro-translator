# Macro Translator — developer targets.
# The app itself runs with Docker Compose (see README.md). These targets are for
# local development and for the quality benchmark in bench/.

.PHONY: test lint bench bench-quick bench-report

# Tests with fixtures and a fake LLM (no model needed).
test:
	uv run pytest

# Format and lint checks.
lint:
	uv run ruff check .
	uv run ruff format --check .

# Full benchmark against the models in bench/models.yaml, then the report.
bench:
	uv run python bench/run.py
	uv run python bench/report.py

# Quick pass: dev cases only, one run each.
bench-quick:
	uv run python bench/run.py --split dev --runs 1
	uv run python bench/report.py

# Regenerate bench/REPORT.md from the results already in bench/results/.
bench-report:
	uv run python bench/report.py
