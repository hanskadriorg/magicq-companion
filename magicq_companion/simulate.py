"""Synthetic multi-genre and techno track generators for end-to-end testing.

`generate_blocks` preserves the classic 128 BPM techno script used by
benchmark/section scripts. `generate_venue_blocks` cycles continuously
through warm deep house (120 BPM), slow hip-hop/R&B (82 BPM), peak-time
club techno (138 BPM), and Drum & Bass (174 BPM) to exercise the Genre Agent
and Intrinsic Pace Score without hardware.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

BPM = 128.0

# (section name, duration in seconds) — kept intact for scripts/check_sections.py
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

# Multi-genre venue showcase: (genre_style, bpm, section, duration_s)
VENUE_SHOWCASE: list[tuple[str, float, str, float]] = [
    ("deep_house", 120.0, "groove", 18.0),
    ("deep_house", 120.0, "breakdown", 8.0),
    ("deep_house", 120.0, "buildup", 10.0),
    ("deep_house", 120.0, "drop", 14.0),
    ("hiphop_slow", 82.0, "groove", 20.0),
    ("hiphop_slow", 82.0, "breakdown", 8.0),
    ("club_techno", 138.0, "groove", 16.0),
    ("club_techno", 138.0, "buildup", 10.0),
    ("club_techno", 138.0, "drop", 16.0),
    ("dnb_fast", 174.0, "groove", 16.0),
    ("dnb_fast", 174.0, "drop", 14.0),
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


def generate_venue_blocks(
    samplerate: int,
    blocksize: int,
    *,
    loop: bool = True,
) -> Iterator[tuple[str, np.ndarray]]:
    """Yield `(label, block)` across multiple venue genres and tempos."""
    rng = np.random.default_rng(11)
    t_global = 0.0
    while True:
        for style, bpm, section, duration in VENUE_SHOWCASE:
            beat_period = 60.0 / bpm
            n_blocks = int(duration * samplerate / blocksize)
            section_start = t_global
            for _ in range(n_blocks):
                t = np.arange(blocksize) / samplerate + t_global
                t_section = t_global - section_start
                block = _render_venue(
                    style, section, t, t_section, duration, beat_period, rng
                )
                yield f"{style}:{section}", block.astype(np.float64)
                t_global += blocksize / samplerate
        if not loop:
            break


def _render_venue(
    style: str,
    section: str,
    t: np.ndarray,
    t_section: float,
    duration: float,
    beat_period: float,
    rng: np.random.Generator,
) -> np.ndarray:
    if section == "breakdown":
        return 0.22 * _pad(t)

    if style == "hiphop_slow":
        # Slow 82 BPM warm R&B / hip-hop groove with sparse highs & warm chords.
        out = 0.55 * _kick(t, beat_period)
        out += 0.32 * _pad(t)
        out += 0.03 * _hats(t, beat_period, rng)
        return np.clip(out * 0.75, -0.95, 0.95)

    if style == "deep_house":
        if section == "buildup":
            p = min(1.0, t_section / max(duration, 1.0))
            out = 0.65 * _kick(t, beat_period)
            out += (0.06 + 0.14 * p) * _hats(t, beat_period / 2, rng)
            out += 0.30 * _pad(t) + (0.05 + 0.25 * p) * _riser(t, p)
            if t_section > duration - 0.35:
                out *= 0.08
            return out * 0.72
        if section == "drop":
            out = 1.15 * _kick(t, beat_period)
            out += 0.18 * _hats(t, beat_period / 4, rng)
            out += 0.28 * _pad(t) + 0.18 * _bassline(t, beat_period)
            return np.clip(out * 0.85, -0.95, 0.95)
        out = 0.75 * _kick(t, beat_period)
        out += 0.08 * _hats(t, beat_period / 2, rng)
        out += 0.35 * _pad(t)
        return out * 0.75

    if style == "dnb_fast":
        # Syncopated fast breakbeat + sub-bass at 174 BPM.
        bar = beat_period * 4.0
        in_bar = t % bar
        # 2-step kick on beat 1 and the 'and' of beat 2 (syncopated).
        k1 = np.exp(-(in_bar) / 0.055) * (in_bar < 0.18)
        off = (in_bar - 2.5 * beat_period) % bar
        k2 = np.exp(-off / 0.055) * (off < 0.18)
        kicks = np.sin(2 * np.pi * 58.0 * t) * (k1 + k2)
        snare_phase = (in_bar - beat_period) % (2.0 * beat_period)
        snare_env = np.exp(-snare_phase / 0.03) * (snare_phase < 0.09)
        snare = rng.standard_normal(len(t)) * snare_env * 0.42
        hats = _hats(t, beat_period / 2, rng) * (0.28 if section == "drop" else 0.18)
        sub = 0.28 * np.sin(2 * np.pi * 52.0 * t)
        gain = 1.15 if section == "drop" else 0.82
        return np.clip((kicks + snare + hats + sub) * gain, -0.95, 0.95)

    # Default: peak-time club techno (138 BPM) with dense 16th-note percussion.
    out = _render(section, t, t_section, duration, beat_period, rng)
    if section == "groove":
        out += 0.18 * _hats(t, beat_period / 4, rng)
    return np.clip(out, -0.95, 0.95)


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
        out += _kick(t, beat_period)
        out += 0.08 * _hats(t, beat_period / 2, rng)
        out += 0.45 * _pad(t)
        out += 0.35 * _lead(t)
        out *= 0.75

    elif section == "breakdown":
        out += 0.18 * _pad(t)

    elif section == "buildup":
        progress = min(1.0, t_section / duration)
        out += _kick(t, beat_period)
        hat_period = beat_period / (2 if progress < 0.5 else 4)
        out += (0.08 + 0.20 * progress) * _hats(t, hat_period, rng)
        out += (0.05 + 0.30 * progress) * _riser(t, progress)
        out *= 0.55 + 0.25 * progress
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
