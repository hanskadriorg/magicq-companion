"""Resolve resource and config paths for source runs and frozen Windows builds."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Directory with packaged resources (PyInstaller extract dir or package)."""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).parent


def install_dir() -> Path:
    """Directory containing the executable (or project cwd when developing)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def static_dir() -> Path:
    return bundle_dir() / "static"


def default_config_path() -> Path:
    """Writable config.toml location.

    Frozen Windows builds use %APPDATA%\\magicq-audio-bridge so the
    Program Files install stays read-only. Dev runs use ./config.toml.
    """
    if is_frozen():
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        cfg_dir = Path(appdata) / "magicq-audio-bridge"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = cfg_dir / "config.toml"
        if not cfg.exists():
            for candidate in (
                install_dir() / "config.toml",
                bundle_dir() / "config.toml",
            ):
                if candidate.exists():
                    shutil.copy2(candidate, cfg)
                    break
        return cfg
    return install_dir() / "config.toml"


def default_layout_path(config_path: str | Path | None = None) -> Path:
    """Writable UI layout JSON, kept next to config.toml when possible."""
    if config_path is not None:
        return Path(config_path).resolve().parent / "ui_layout.json"
    if is_frozen():
        return default_config_path().parent / "ui_layout.json"
    return install_dir() / "ui_layout.json"
