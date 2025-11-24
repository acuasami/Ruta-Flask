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
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 1 --timeout 60 app:app
