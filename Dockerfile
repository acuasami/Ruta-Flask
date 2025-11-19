# USAR ESTA IMAGEN (Bookworm es más reciente y funciona)
FROM python:3.11-slim-bookworm

# 1. Instalar librerías de sistema
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

# 2. Variables de entorno para GDAL
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

# 3. Directorio de trabajo
WORKDIR /app

# 4. Instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copiar el código
COPY . /app

# 6. Comando de arranque
CMD gunicorn --bind 0.0.0.0:$PORT app:app --workers 1
