"""Console status display: section transitions and a live status line."""

from __future__ import annotations

import sys

from .features import Features
from .state_machine import Section, SectionTracker

_STATUS_INTERVAL = 0.1


class Monitor:
    def __init__(self) -> None:
        self._last_section: Section | None = None
        self._last_status_t = 0.0

    def update(self, t: float, features: Features, tracker: SectionTracker) -> None:
        if tracker.state is not self._last_section:
            if self._last_section is not None:
                came_from = self._last_section.value.upper()
                sys.stdout.write(
                    f"\r\033[K[{_fmt_time(t)}] {came_from} -> "
                    f"{tracker.state.value.upper()}\n"
                )
            self._last_section = tracker.state

        if t - self._last_status_t >= _STATUS_INTERVAL:
            self._last_status_t = t
            bpm = f"{features.bpm:5.1f}" if features.bpm else "  ---"
            line = (
                f"[{_fmt_time(t)}] {tracker.state.value.upper():<9}"
                f" bpm {bpm}"
                f" | energy {_bar(features.energy_norm / 2)}"
                f" | bass {_bar(features.bass_norm / 2)}"
                f" | build {_bar(features.build_score)}"
                f" | ramp {int(tracker.build_progress * 255):3d}"
            )
            sys.stdout.write(f"\r\033[K{line}")
            sys.stdout.flush()

    def close(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()


def _bar(value: float, width: int = 10) -> str:
    filled = max(0, min(width, int(value * width)))
    return "#" * filled + "." * (width - filled)


def _fmt_time(t: float) -> str:
    minutes, seconds = divmod(int(t), 60)
    return f"{minutes:02d}:{seconds:02d}"
