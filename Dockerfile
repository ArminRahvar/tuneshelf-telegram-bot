FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 bot.py database.py musicbrainz.py archive.py \
    telegram_delivery.py postgres_database.py ./

USER 10001:10001
CMD ["python", "bot.py"]