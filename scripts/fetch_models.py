#!/usr/bin/env python3
"""Download and verify Essentia's offline Discogs-EffNet ONNX model + class labels.

Run once during setup on T14 / T15 (or build packaging):
    python scripts/fetch_models.py

After fetching, MagicQ Venue Companion runs 100% offline with zero network calls.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

MODEL_BASENAME = "discogs-effnet-bsdynamic-1"
FILES: tuple[tuple[str, str, str | None], ...] = (
    (
        f"{MODEL_BASENAME}.onnx",
        f"https://essentia.upf.edu/models/feature-extractors/discogs-effnet/{MODEL_BASENAME}.onnx",
        None,
    ),
    (
        f"{MODEL_BASENAME}.json",
        f"https://essentia.upf.edu/models/feature-extractors/discogs-effnet/{MODEL_BASENAME}.json",
        None,
    ),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path, expected_sha256: str | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "magicq-venue-companion/3.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out)
    if tmp.stat().st_size < 512:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded file looks truncated: {url}")
    actual_sha = sha256_file(tmp)
    if expected_sha256 and actual_sha.lower() != expected_sha256.lower():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"SHA-256 mismatch for {dest.name}: expected {expected_sha256}, got {actual_sha}"
        )
    tmp.replace(dest)
    print(f"Saved {dest} ({dest.stat().st_size / (1024 * 1024):.2f} MiB, sha256={actual_sha[:12]}...)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch offline ONNX genre models.")
    parser.add_argument(
        "--dest",
        default=str(Path(__file__).resolve().parents[1] / "models"),
        help="Target directory for model files (default: ./models)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if files already exist.",
    )
    args = parser.parse_args(argv)
    dest_dir = Path(args.dest).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    for filename, url, expected_sha in FILES:
        target = dest_dir / filename
        if target.is_file() and target.stat().st_size >= 512 and not args.force:
            print(f"Already present: {target} (sha256={sha256_file(target)[:12]}...)")
            continue
        print(f"Downloading {url} ...")
        try:
            download(url, target, expected_sha)
        except Exception as exc:
            print(f"ERROR downloading {filename}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
