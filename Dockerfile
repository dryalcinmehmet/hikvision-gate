# Hikvision LAN agent — Rabbit consumer + SADP (no HTTP server)
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# netifaces often builds from source on slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /root/.cache /var/lib/apt/lists/*

COPY main.py rabbit_consumer.py amqp_tls.py sadp_core.py gateway_isapi.py ./

# Runtime: HIKVISION_GATEWAY_AMQP_URL and HIKVISION_GATEWAY_ORG_ID (see .env.example)
CMD ["python", "main.py"]
