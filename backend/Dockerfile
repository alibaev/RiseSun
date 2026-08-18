FROM python:3.11-slim

WORKDIR /app

COPY gateway/pyproject.toml gateway/pyproject.toml
COPY gateway/src gateway/src
RUN pip install --no-cache-dir -e gateway

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/app backend/app
COPY backend/alembic backend/alembic
COPY backend/alembic.ini backend/alembic.ini

WORKDIR /app/backend

EXPOSE 8000

CMD ["sh", "-c", "python -m alembic upgrade head && python -m app.bootstrap; uvicorn app.main:app --host 0.0.0.0 --port 8000"]
