FROM python:3.11.7-slim-bookworm

WORKDIR /app

# Reemplazar sources.list para usar solo repositorios de Bookworm
RUN rm -rf /etc/apt/sources.list.d/* && \
    echo "deb http://deb.debian.org/debian bookworm main" > /etc/apt/sources.list && \
    echo "deb http://deb.debian.org/debian bookworm-updates main" >> /etc/apt/sources.list && \
    echo "deb http://security.debian.org/debian-security bookworm-security main" >> /etc/apt/sources.list

# Actualizar e instalar solo lo esencial
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copiar requirements
COPY requirements.txt .

# Instalar dependencias de Python
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar aplicación
COPY . .

# --- SOLO UN CMD ---
# Usa la variable PORT (Render la establece en 10000) o 5000 por defecto
CMD gunicorn --bind 0.0.0.0:${PORT:-5000} --timeout 120 app:app
