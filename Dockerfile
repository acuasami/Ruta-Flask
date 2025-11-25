FROM python:3.11-slim

WORKDIR /app

# 🔹 MODIFICACIÓN: Instalar dependencias del sistema para OSMnx y Geopandas
# libspatialindex-dev es vital para Rtree/OSMnx
# libgeos-dev y gdal-bin ayudan a Geopandas/Shapely
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

CMD gunicorn --bind 0.0.0.0:$PORT app:app