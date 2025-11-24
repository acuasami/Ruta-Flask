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

# Copiar aplicación (Asegúrate de que hay un espacio entre los puntos)
COPY . .

# Render ignora EXPOSE, pero puedes dejarlo documentado o comentado
# EXPOSE 5000

# --- CAMBIO CRÍTICO PARA RENDER ---
# Usamos 'sh -c' para poder leer la variable de entorno $PORT.
# Si $PORT no está definida (localmente), usa 5000 por defecto.
CMD sh -c "gunicorn --bind 0.0.0.0:${PORT:-5000} app:app"

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
