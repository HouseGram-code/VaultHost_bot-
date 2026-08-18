FROM python:3.11-slim

# Системные зависимости (минимальный набор)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости Python сначала (для кэша слоёв)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходный код
COPY bot.py database.py docker_manager.py status_image.py ./

# Папка для БД
RUN mkdir -p data

# Запуск через tini (правильная обработка сигналов)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "bot.py"]
