"""Resolve resource, model, and config paths for source runs and frozen builds."""

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


def repo_dir() -> Path:
    """Repository root when running from source, or install dir when frozen."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def install_dir() -> Path:
    """Directory containing the executable (or project cwd when developing)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def static_dir() -> Path:
    return bundle_dir() / "static"


def models_dir() -> Path:
    """Directory holding offline ONNX genre models."""
    for candidate in (
        install_dir() / "models",
        repo_dir() / "models",
        bundle_dir() / "models",
    ):
        if candidate.is_dir():
            return candidate
    return repo_dir() / "models"


def default_model_path(filename: str = "discogs-effnet-bsdynamic-1.onnx") -> Path:
    """Resolve the default offline ONNX model file."""
    for directory in (
        install_dir() / "models",
        repo_dir() / "models",
        bundle_dir() / "models",
    ):
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return models_dir() / filename


def default_config_path() -> Path:
    """Writable config.toml location.

    Frozen Windows builds use %APPDATA%\\magicq-companion so the
    Program Files install stays read-only. Dev runs use ./config.toml.
    """
    if is_frozen():
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        cfg_dir = Path(appdata) / "magicq-companion"
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


def data_dir(config_path: str | Path | None = None) -> Path:
    if config_path is not None:
        return Path(config_path).resolve().parent
    if is_frozen():
        return default_config_path().parent
    return install_dir()


def default_layout_path(config_path: str | Path | None = None) -> Path:
    """Writable UI layout JSON, kept next to config.toml when possible."""
    return data_dir(config_path) / "ui_layout.json"


def default_genre_profiles_path(config_path: str | Path | None = None) -> Path:
    """Writable learned per-genre profiles JSON, kept next to config.toml."""
    return data_dir(config_path) / "genre_profiles.json"
