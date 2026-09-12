FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libfreetype6 \
        libjpeg62-turbo \
        zlib1g \
        libcairo2 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /tmp/generated /tmp/asset_cache
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /tmp/generated /tmp/asset_cache /app
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]