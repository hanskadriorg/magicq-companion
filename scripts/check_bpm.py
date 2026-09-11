"""Measure how fast the tempo estimate follows a BPM change.

Renders a kick-only track that steps 128 -> 150 -> 120 -> 128 BPM and
reports how long the estimate takes to settle within 3% after each step.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from magicq_companion.config import Config  # noqa: E402
from magicq_companion.features import FeatureExtractor  # noqa: E402

STEPS = [(128.0, 20.0), (150.0, 25.0), (120.0, 25.0), (128.0, 25.0)]
SR = 44100
HOP = 512


def render(bpm: float, seconds: float, phase: float, rng) -> tuple[np.ndarray, float]:
    n = int(seconds * SR)
    t = np.arange(n) / SR + phase
    period = 60.0 / bpm
    kick_phase = t % period
    env = np.exp(-kick_phase / 0.06) * (kick_phase < 0.25)
    hat_phase = t % (period / 2)
    hats = rng.standard_normal(n) * np.exp(-hat_phase / 0.015) * (hat_phase < 0.05)
    out = 0.6 * np.sin(2 * np.pi * 55.0 * t) * env + 0.05 * hats
    return out, phase + n / SR


def main() -> int:
    cfg = Config()
    extractor = FeatureExtractor(
        SR, HOP, cfg.detection, cfg.build_score, cfg.bpm_rate, noise_floor_db=-55.0
    )
    rng = np.random.default_rng(3)
    dt = HOP / SR

    t = 0.0
    phase = 0.0
    print(f"{'target':>7} {'settled after':>14} {'final bpm':>10}")
    for bpm, seconds in STEPS:
        audio, phase = render(bpm, seconds, phase, rng)
        start = t
        settled: float | None = None
        estimate = 0.0
        for i in range(0, len(audio) - HOP, HOP):
            t += dt
            f = extractor.process(audio[i:i + HOP].astype(np.float64))
            estimate = f.bpm
            if settled is None and estimate > 0 and abs(estimate - bpm) <= 0.03 * bpm:
                settled = t - start
        label = f"{settled:.1f} s" if settled is not None else "never"
        print(f"{bpm:7.0f} {label:>14} {estimate:10.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
