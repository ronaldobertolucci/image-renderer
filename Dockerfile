FROM python:3.12-slim

# Dependências de sistema para o Pillow:
# libfreetype6  → renderização de fontes TrueType
# libjpeg-dev   → suporte a JPEG nas imagens de entrada
# zlib1g-dev    → suporte a PNG
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libfreetype6 \
        libjpeg-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia o requirements primeiro para aproveitar o cache de camada do Docker.
# Só reinstala dependências quando o requirements.txt mudar.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY . .

# Garante que os diretórios de saída existam na imagem
RUN mkdir -p /tmp/generated /tmp/asset_cache

# Usuário não-root para reduzir superfície de ataque
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /tmp/generated /tmp/asset_cache /app
USER appuser

# O CMD é sobrescrito por cada serviço no docker-compose
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
