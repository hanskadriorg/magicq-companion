Matter.js sidecar for virtual On/Off and RGB lights.

This process does **not** send Art-Net or sACN itself. It reports DMX channel
levels to MagicQ Companion (`PYTHON_DMX_URL`), which sends real Art-Net/sACN.

Started automatically by `python -m magicq_companion --ui`.
