# NaviSan — Telegram-бот для обработки FLAC-библиотеки
#
# Сборка:  docker build -t navisan .
# Запуск:  docker compose up -d navisan

# Python 3.12 slim — стабильный production-образ.
# Код не использует возможностей специфичных для 3.14,
# поэтому используем последний стабильный релиз LTS.
FROM python:3.12-slim

# Метаданные
LABEL maintainer="navisan" \
      description="FLAC music library processing bot"

# Не буферизуем stdout/stderr — логи сразу видны в docker compose logs
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Создаём пользователя с UID/GID 1000 — совпадает с user: "1000:1000"
# в docker-compose.yml на сервере, чтобы права на тома были одинаковые
RUN groupadd --gid 1000 navisan \
 && useradd --uid 1000 --gid navisan --shell /bin/bash --create-home navisan

WORKDIR /app

# 1. Системные пакеты flac (ReplayGain) и git (для самообновления)
RUN apt-get update && apt-get install -y --no-install-recommends \
    flac \
    git \
 && rm -rf /var/lib/apt/lists/*

# 2. Python-зависимости (кэшируются, пока requirements.txt не меняется)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY *.py ./

# Переключаемся на непривилегированного пользователя
USER navisan

# Настройка безопасной директории git для работы с монтируемым репозиторием
RUN git config --global --add safe.directory /app \
 && git config --global --add safe.directory '*' \
 && git config --global url."https://github.com/".insteadOf "git@github.com:"

# .env монтируется снаружи как volume — не копируем его в образ
# Точки монтирования описаны в docker-compose.yml

CMD ["python", "bot.py"]
