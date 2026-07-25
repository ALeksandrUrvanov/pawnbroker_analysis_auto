FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY app/ ./app/
COPY prompts/ ./prompts/
COPY camera_mapping.json ./

# Рабочие директории
RUN mkdir -p /app/audio_cache /app/reports

# Планировщик
CMD ["python", "-m", "app.cli"]
