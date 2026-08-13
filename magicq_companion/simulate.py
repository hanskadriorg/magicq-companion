"""Synthetic techno track generator for testing the pipeline end to end.

Produces mono audio blocks that follow a scripted section sequence
(groove -> breakdown -> build-up -> drop -> ...) so the detector and
the MagicQ patch can be tested without a DJ or an audio interface.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

BPM = 128.0

# (section name, duration in seconds)
DEFAULT_SCRIPT: list[tuple[str, float]] = [
    ("groove", 25),
    ("melodic", 18),
    ("breakdown", 12),
    ("buildup", 12),
    ("drop", 20),
    ("groove", 20),
    ("buildup", 14),
    ("drop", 20),
    ("groove", 15),
]


def generate_blocks(
    samplerate: int, blocksize: int, script: list[tuple[str, float]] | None = None
) -> Iterator[tuple[str, np.ndarray]]:
    """Yield (current scripted section, audio block) pairs."""
    script = script or DEFAULT_SCRIPT
    rng = np.random.default_rng(7)
    beat_period = 60.0 / BPM

    t_global = 0.0
    for section, duration in script:
        n_blocks = int(duration * samplerate / blocksize)
        section_start = t_global
        for _ in range(n_blocks):
            t = np.arange(blocksize) / samplerate + t_global
            t_section = t_global - section_start
            block = _render(section, t, t_section, duration, beat_period, rng)
            yield section, block.astype(np.float64)
            t_global += blocksize / samplerate


def _render(
    section: str,
    t: np.ndarray,
    t_section: float,
    duration: float,
    beat_period: float,
    rng: np.random.Generator,
) -> np.ndarray:
    out = np.zeros_like(t)

    if section == "groove":
        out += _kick(t, beat_period)
        out += 0.10 * _hats(t, beat_period / 2, rng)
        out += 0.05 * _pad(t)
        out *= 0.8

    elif section == "melodic":
        # Kick keeps going, but pads + a lead take over the mid/high.
        out += _kick(t, beat_period)
        out += 0.08 * _hats(t, beat_period / 2, rng)
        out += 0.45 * _pad(t)
        out += 0.35 * _lead(t)
        out *= 0.75

    elif section == "breakdown":
        # No kick, no hats - just atmosphere.
        out += 0.18 * _pad(t)

    elif section == "buildup":
        progress = min(1.0, t_section / duration)
        # Kick keeps pumping; hats double up half-way; riser sweeps up.
        out += _kick(t, beat_period)
        hat_period = beat_period / (2 if progress < 0.5 else 4)
        out += (0.08 + 0.20 * progress) * _hats(t, hat_period, rng)
        out += (0.05 + 0.30 * progress) * _riser(t, progress)
        out *= 0.55 + 0.25 * progress
        # The classic gap: brief near-silence right before the drop.
        if t_section > duration - 0.4:
            out *= 0.05

    elif section == "drop":
        out += 1.4 * _kick(t, beat_period)
        out += 0.30 * _hats(t, beat_period / 4, rng)
        out += 0.20 * _bassline(t, beat_period)
        out += 0.10 * _pad(t)

    return out


def _kick(t: np.ndarray, period: float) -> np.ndarray:
    """60 Hz sine burst with fast decay on every beat."""
    phase = t % period
    envelope = np.exp(-phase / 0.06) * (phase < 0.25)
    return np.sin(2 * np.pi * 55.0 * t) * envelope


def _hats(t: np.ndarray, period: float, rng: np.random.Generator) -> np.ndarray:
    """Short noise bursts (high-frequency content) on a grid."""
    phase = t % period
    envelope = np.exp(-phase / 0.015) * (phase < 0.05)
    noise = rng.standard_normal(len(t))
    return noise * envelope


def _pad(t: np.ndarray) -> np.ndarray:
    """Sustained mid-frequency chord."""
    return (
        np.sin(2 * np.pi * 220.0 * t)
        + 0.7 * np.sin(2 * np.pi * 277.2 * t)
        + 0.5 * np.sin(2 * np.pi * 329.6 * t)
    ) / 3.0


def _lead(t: np.ndarray) -> np.ndarray:
    """Simple mid/high lead line above the kick."""
    # Slow arpeggio-ish motion around 500–900 Hz.
    freq = 520.0 + 180.0 * np.sin(2 * np.pi * 0.5 * t)
    return 0.6 * np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * 2 * freq * t)


def _riser(t: np.ndarray, progress: float) -> np.ndarray:
    """Noise sweep whose brightness rises with build progress."""
    noise = np.sin(2 * np.pi * (1000 + 6000 * progress) * t)
    return noise * (0.5 + 0.5 * progress)


def _bassline(t: np.ndarray, period: float) -> np.ndarray:
    phase = t % (period / 2)
    envelope = np.exp(-phase / 0.12)
    return np.sin(2 * np.pi * 82.0 * t) * envelope
