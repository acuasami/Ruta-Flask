# Usa una imagen base de Python con librerías de compilación necesarias
FROM python:3.9-slim-buster

# 1. Instalar librerías de sistema (APT) necesarias para GDAL/GEOS
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libgdal-dev \
    libgeos-dev \
    gdal-bin \
    proj-bin && \
    rm -rf /var/lib/apt/lists/*

# 2. Configurar el entorno de Python (para encontrar GDAL/GEOS)
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

# 3. Copiar y exponer el código
WORKDIR /app
COPY requirements.txt .

# 4. Instalar dependencias de Python (utilizando las librerías de sistema instaladas)
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copiar el resto de la aplicación
COPY . /app

# 6. Definir el comando de inicio para Gunicorn (servidor estable)
# El puerto se obtiene automáticamente de Railway
CMD gunicorn --bind 0.0.0.0:$PORT app:app --workers 1