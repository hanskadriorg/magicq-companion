FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 alsa-utils libgomp1 \
    && rm -rf /var/lib/var/lists/* /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/fetch_models.py scripts/fetch_models.py
RUN python scripts/fetch_models.py --dest /app/models

COPY magicq_companion/ magicq_companion/
COPY config.toml .

EXPOSE 8765

CMD ["python", "-m", "magicq_companion", "--ui"]
