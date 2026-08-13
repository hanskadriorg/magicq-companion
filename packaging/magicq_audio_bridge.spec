# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — build on Windows:
#   pyinstaller packaging/magicq_audio_bridge.spec

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent.resolve()
PKG = ROOT / "magicq_audio_bridge"

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(PKG / "static"), "static"),
        (str(ROOT / "config.toml"), "."),
    ],
    hiddenimports=[
        "magicq_audio_bridge",
        "magicq_audio_bridge.__main__",
        "magicq_audio_bridge.audio",
        "magicq_audio_bridge.artnet",
        "magicq_audio_bridge.config",
        "magicq_audio_bridge.dmx",
        "magicq_audio_bridge.features",
        "magicq_audio_bridge.monitor",
        "magicq_audio_bridge.paths",
        "magicq_audio_bridge.pipeline",
        "magicq_audio_bridge.simulate",
        "magicq_audio_bridge.state_machine",
        "magicq_audio_bridge.ui",
        "numpy",
        "sounddevice",
        "aiohttp",
        "cffi",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "scipy",
        "tkinter",
        "matplotlib",
        "pytest",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MagicQAudioBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # keep console for logs / --list-devices
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MagicQAudioBridge",
)
