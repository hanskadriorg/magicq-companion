"""BAAS_26 show/set file helpers (no .shw parser — raw file serve/upload only)."""

from __future__ import annotations

import shutil
from pathlib import Path

BAAS_SHOW_NAME = "BAAS_26.shw"
BAAS_SET_NAME = "BAAS_26.set"


def bundled_baas_dir(bundle_root: Path) -> Path:
    return bundle_root / "data"


def repo_baas_paths(bundle_root: Path) -> dict[str, Path]:
    data = bundled_baas_dir(bundle_root)
    return {
        BAAS_SHOW_NAME: data / BAAS_SHOW_NAME,
        BAAS_SET_NAME: data / BAAS_SET_NAME,
    }


def user_baas_dir(config_path: Path | None) -> Path:
    if config_path is not None:
        return Path(config_path).resolve().parent / "baas"
    return Path.cwd() / "baas"


def baas_status(bundle_root: Path, config_path: Path | None) -> dict:
    repo = repo_baas_paths(bundle_root)
    user_dir = user_baas_dir(config_path)
    out = {}
    for name in (BAAS_SHOW_NAME, BAAS_SET_NAME):
        repo_path = repo[name]
        user_path = user_dir / name
        out[name] = {
            "in_repo": repo_path.is_file(),
            "in_user_store": user_path.is_file(),
            "repo_path": str(repo_path),
            "user_path": str(user_path),
        }
    return out


def resolve_baas_download(
    name: str,
    bundle_root: Path,
    config_path: Path | None,
) -> Path | None:
    if name not in (BAAS_SHOW_NAME, BAAS_SET_NAME):
        return None
    user_path = user_baas_dir(config_path) / name
    if user_path.is_file():
        return user_path
    repo_path = repo_baas_paths(bundle_root)[name]
    if repo_path.is_file():
        return repo_path
    return None


def store_baas_upload(
    name: str,
    src: Path,
    config_path: Path | None,
) -> Path:
    if name not in (BAAS_SHOW_NAME, BAAS_SET_NAME):
        raise ValueError("invalid baas file name")
    dest_dir = user_baas_dir(config_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    shutil.copy2(src, dest)
    return dest
