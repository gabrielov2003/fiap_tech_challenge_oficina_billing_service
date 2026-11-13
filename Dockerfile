FROM python:3.11-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim

RUN useradd -m appuser
WORKDIR /app

COPY --from=builder /install /usr/local
COPY src ./src

USER appuser

EXPOSE 5000

CMD ["ddtrace-run", "gunicorn", "--chdir", "src", "--workers", "2", "--bind", "0.0.0.0:5000", "app:create_app()"]
