# Contexto de build: raiz do repositório (precisa de database/migrations).
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MIGRATIONS_DIR=/app/migrations
WORKDIR /app
RUN useradd --system --uid 10001 --no-create-home app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/src ./src
COPY backend/gunicorn.conf.py .
COPY database/migrations ./migrations

# Imagem de testes: docker compose run --rm test
FROM base AS test
COPY backend/requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY backend/pytest.ini .
COPY backend/tests ./tests
USER app
CMD ["pytest", "-v"]

# Imagem de execução (padrão): Gunicorn, usuário sem privilégios.
FROM base AS runtime
USER app
EXPOSE 8000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "src.adapters.web.app:create_app()"]
