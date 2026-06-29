# ── Этап 1: установка зависимостей ──────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Ставим системные зависимости для сборки (если нужны C-расширения)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Этап 2: финальный образ ───────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Копируем установленные пакеты из builder
COPY --from=builder /install /usr/local

# Копируем только исходный код (без venv, build, dist, *.exe, *.db)
COPY config_reader.py .
COPY main.py .
COPY handlers/ ./handlers/
COPY keyboards/ ./keyboards/

# Директория для базы данных — монтируется через volume
VOLUME ["/app/data"]

# Путь к БД читается из переменной окружения (по умолчанию /app/data/tuppy_vpn.db)
ENV DB_PATH=/app/data/tuppy_vpn.db

# Бот не слушает порты — только polling
CMD ["python", "main.py"]
