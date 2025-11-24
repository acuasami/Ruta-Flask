FROM python:3.11-slim-bullseye

WORKDIR /app

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

# Copiar requirements primero para cachear las dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto de la aplicación
COPY . .

# Exponer el puerto
EXPOSE 5000

# Comando para ejecutar la aplicación
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
