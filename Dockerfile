FROM python:3.12-slim

# Unbuffered output so `docker logs` shows the console monitor live.
ENV PYTHONUNBUFFERED=1

# PortAudio for sounddevice, alsa-utils for the arecord fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 alsa-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY magicq_audio_bridge/ magicq_audio_bridge/
COPY config.toml .

EXPOSE 8765

CMD ["python", "-m", "magicq_audio_bridge", "--ui"]
