"""Console status display: section transitions, genre, pace, and live meters."""

from __future__ import annotations

import sys

from .features import Features
from .state_machine import Section, SectionTracker

_STATUS_INTERVAL = 0.1
_RATE_LABELS = {32: "1/8", 64: "1/4", 128: "1/2", 255: "1/1"}


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
                    f"{tracker.state.value.upper()} ({features.archetype_label})\n"
                )
            self._last_section = tracker.state

        if t - self._last_status_t >= _STATUS_INTERVAL:
            self._last_status_t = t
            bpm = f"{features.bpm:5.1f}" if features.bpm else "  ---"
            rate = _RATE_LABELS.get(int(round(features.bpm_rate)), str(int(features.bpm_rate)))
            line = (
                f"[{_fmt_time(t)}] {tracker.state.value.upper():<9}"
                f" bpm {bpm}"
                f" | div {rate:<3}"
                f" | pace {_bar(features.pace_score, 8)}"
                f" | build {_bar(features.build_score, 8)}"
                f" | {features.archetype:<13}"
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
