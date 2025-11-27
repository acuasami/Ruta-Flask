FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias de sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libspatialindex-dev \
    libgeos-dev \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Configuración mejorada de Gunicorn
CMD gunicorn \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers 2 \
    --threads 4 \
    --worker-class gthread \
    --timeout 120 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --graceful-timeout 30 \
    app:app
