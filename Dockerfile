# CAMBIO IMPORTANTE: Usamos "slim-bookworm" (Debian 12) que sí tiene repositorios activos
FROM python:3.11-slim-bookworm

# 1. Instalar librerías de sistema necesarias para GDAL/GEOS y compilación
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libgdal-dev \
    libgeos-dev \
    gdal-bin \
    proj-bin \
    pkg-config && \
    rm -rf /var/lib/apt/lists/*

# 2. Configurar variables para que Python encuentre GDAL/GEOS al compilar
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

# 3. Preparar el directorio de trabajo
WORKDIR /app

# 4. Copiar dependencias e instalar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copiar el resto del código
COPY . /app

# 6. Comando de arranque
CMD gunicorn --bind 0.0.0.0:$PORT app:app --workers 1
