FROM python:3.11-slim

WORKDIR /app

COPY gateway/pyproject.toml gateway/pyproject.toml
COPY gateway/src gateway/src
RUN pip install --no-cache-dir -e gateway

EXPOSE 50051

CMD ["python", "-m", "mmws_gateway.grpc_server"]
