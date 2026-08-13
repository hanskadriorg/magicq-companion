Matter.js sidecar for virtual On/Off and RGB lights.

This process does **not** send Art-Net or sACN. It reports DMX channel
levels to the Python app (`PYTHON_DMX_URL`), which uses the working
sender from magicq-audio-bridge.

Started automatically by `python -m magicq_audio_bridge --ui`.
