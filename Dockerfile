FROM python:3.10-slim

WORKDIR /app

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем пакеты
# Флаг --no-cache-dir экономит место и ускоряет установку
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY bot.py .

# Команда запуска
CMD ["python", "bot.py"]
