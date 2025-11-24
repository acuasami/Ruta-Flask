FROM python:3.11-slim

WORKDIR /app

# Solo dependencias esenciales
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# FORMA CORRECTA - Elige UNA de estas opciones:

# Opción 1: Con puerto fijo (10000)
CMD gunicorn --bind 0.0.0.0:10000 --workers 1 --threads 2 --timeout 120 --preload app:app

# Opción 2: Con variable PORT (recomendado para Railway/Render)
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120 --preload app:app
