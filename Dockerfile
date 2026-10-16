# One image for both services: the app (default command) and the ETL.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/backend \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project && rm /usr/local/bin/uv

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data
USER app
# Install the DuckDB full-text search extension now, so the app does not
# download it at run time.
RUN python -c "import duckdb; duckdb.connect().install_extension('fts')"

COPY --chown=app:app backend ./backend
COPY --chown=app:app frontend ./frontend
COPY --chown=app:app etl ./etl

# Inside the container the app listens on all interfaces. docker-compose.yml
# publishes the port on 127.0.0.1 only, unless BIND_ADDRESS says otherwise.
ENV APP_HOST=0.0.0.0 APP_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["python", "-m", "macro_translator"]
