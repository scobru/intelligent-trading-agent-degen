FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=3000

# Prophet va compilato: servono i build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    dos2unix \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN dos2unix ./start.sh && chmod +x ./start.sh

# Database SQLite persistente
RUN mkdir -p /app/data

EXPOSE 3000

CMD ["/bin/bash", "./start.sh"]
