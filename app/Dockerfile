FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    fonts-noto-core \
    fonts-indic \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /var/sewasetu/uploads /var/log/sewasetu \
    && crontab /app/deploy/crontab

ENV PORT=8000
CMD ["sh", "-c", "python -m scripts.init_db; gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 4 --timeout 30 app:app"]
