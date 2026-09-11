"""Run the synthetic track through the analyser and print the timeline.

Usage:  python scripts/check_sections.py [style]

Prints one line per detected section change, plus the scripted section that
was playing at the time, so detector changes can be compared at a glance.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from magicq_companion.config import Config  # noqa: E402
from magicq_companion.features import FeatureExtractor  # noqa: E402
from magicq_companion.simulate import generate_blocks  # noqa: E402
from magicq_companion.state_machine import SectionTracker  # noqa: E402


def main() -> int:
    style = sys.argv[1] if len(sys.argv) > 1 else "club"
    cfg = Config()
    cfg.detection.style = style
    cfg.detection.drop_from_groove = False
    cfg.audio.noise_floor_db = -55.0

    sr, hop = cfg.audio.samplerate, cfg.audio.blocksize
    extractor = FeatureExtractor(
        sr, hop, cfg.detection, cfg.build_score, cfg.bpm_rate,
        noise_floor_db=cfg.audio.noise_floor_db,
    )
    tracker = SectionTracker(cfg.detection, extractor.fps)
    dt = hop / sr

    t = 0.0
    previous = tracker.state
    scripted = ""
    print(f"style={style}")
    print(f"{'time':>7}  {'detected':<10} {'scripted':<10} {'bpm':>6} {'music':>6} {'level':>7}")
    for scripted, block in generate_blocks(sr, hop):
        t += dt
        f = extractor.process(block)
        tracker.update(f, dt)
        if tracker.state is not previous:
            print(
                f"{t:7.1f}  {tracker.state.value:<10} {scripted:<10} "
                f"{f.bpm:6.1f} {f.music_score:6.2f} {f.rms_db:6.1f}dB"
            )
            previous = tracker.state
    print(f"{t:7.1f}  (end)      {scripted:<10} bpm={extractor._estimate_bpm():.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
