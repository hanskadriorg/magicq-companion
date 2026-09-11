"""Time the analyser against real time.

Prints microseconds of CPU per audio hop and the resulting share of one
core, which is what decides whether the machine can keep up.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from magicq_companion.config import Config  # noqa: E402
from magicq_companion.dmx import build_frame  # noqa: E402
from magicq_companion.features import FeatureExtractor  # noqa: E402
from magicq_companion.simulate import generate_blocks  # noqa: E402
from magicq_companion.state_machine import SectionTracker  # noqa: E402


def run(visualizer: bool) -> None:
    cfg = Config()
    sr, hop = cfg.audio.samplerate, cfg.audio.blocksize
    extractor = FeatureExtractor(
        sr, hop, cfg.detection, cfg.build_score, cfg.bpm_rate, noise_floor_db=-55.0
    )
    extractor.visualizer = visualizer
    tracker = SectionTracker(cfg.detection, extractor.fps)
    dmx = bytearray(512)
    dt = hop / sr

    blocks = [b for _, b in generate_blocks(sr, hop)]
    start = time.process_time()
    for block in blocks:
        f = extractor.process(block)
        tracker.update(f, dt)
        build_frame(dmx, cfg.channels, f, tracker)
    cpu = time.process_time() - start

    audio_seconds = len(blocks) * dt
    per_hop_us = cpu / len(blocks) * 1e6
    print(
        f"visualizer={'on ' if visualizer else 'off'}  "
        f"{per_hop_us:6.0f} us/hop   "
        f"{cpu / audio_seconds:5.1%} of one core   "
        f"({audio_seconds:.0f} s of audio in {cpu:.1f} s CPU)"
    )


def main() -> int:
    print(f"hop budget: {512 / 44100 * 1e6:.0f} us")
    run(True)
    run(False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
