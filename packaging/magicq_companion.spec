# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — build on Windows:
#   pyinstaller packaging/magicq_companion.spec

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent.resolve()
PKG = ROOT / "magicq_companion"

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
        "magicq_companion",
        "magicq_companion.__main__",
        "magicq_companion.audio",
        "magicq_companion.artnet",
        "magicq_companion.config",
        "magicq_companion.dmx",
        "magicq_companion.features",
        "magicq_companion.monitor",
        "magicq_companion.paths",
        "magicq_companion.pipeline",
        "magicq_companion.simulate",
        "magicq_companion.state_machine",
        "magicq_companion.ui",
        "magicq_companion.netif",
        "magicq_companion.output",
        "magicq_companion.sacn",
        "magicq_companion.matter_runtime",
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
    name="MagicQCompanion",
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
    name="MagicQCompanion",
)
