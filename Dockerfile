FROM python:3.11-slim

WORKDIR /app

# INSTALAR DEPENDENCIAS DE SISTEMA (CRÍTICO PARA OSMNX)
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

# Aumentamos el timeout de Gunicorn a 120s para evitar cortes en el arranque
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 300 app:app