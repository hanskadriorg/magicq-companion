"""Map analysis results onto the outgoing DMX frame."""

from __future__ import annotations

from .config import ChannelMap, DetectionConfig
from .features import Features
from .state_machine import Section, SectionTracker


def drop_hit_level(t_in_state: float, hold: float, fade: float) -> float:
    """255 for `hold` seconds, then a linear fade to 0 over `fade` seconds."""
    if t_in_state < 0:
        return 0.0
    hold_s = max(0.0, float(hold))
    fade_s = max(0.0, float(fade))
    if t_in_state < hold_s:
        return 255.0
    if fade_s <= 0:
        return 0.0
    u = (t_in_state - hold_s) / fade_s
    if u >= 1.0:
        return 0.0
    return 255.0 * (1.0 - u)


def build_frame(
    dmx: bytearray,
    channels: ChannelMap,
    features: Features,
    tracker: SectionTracker,
    detection: DetectionConfig | None = None,
) -> None:
    """Write current values into the 512-byte DMX frame (in place)."""

    def set_ch(address: int, value: float) -> None:
        if value != value:  # NaN from silent / missing audio
            value = 0.0
        dmx[address - 1] = max(0, min(255, int(value)))

    for section, address in (
        (Section.GROOVE, channels.groove),
        (Section.BREAKDOWN, channels.breakdown),
        (Section.BUILDUP, channels.buildup),
        (Section.DROP, channels.drop),
    ):
        set_ch(address, 255 if tracker.state is section else 0)

    set_ch(channels.build_progress, tracker.build_progress * 255)

    # Square BPM clock — no intermediate levels (MagicQ dislikes fades here).
    set_ch(channels.beat_pulse, features.beat_pulse)

    # Square 4/4 lock: 255 for the whole time a kick grid is held.
    set_ch(channels.four_four, features.four_four_gate)

    if tracker.state is Section.DROP:
        try:
            hold = float(getattr(detection, "drop_hit_seconds", 0.15))
        except (TypeError, ValueError, AttributeError):
            hold = 0.15
        try:
            fade = float(getattr(detection, "drop_hit_fade_seconds", 0.0))
        except (TypeError, ValueError, AttributeError):
            fade = 0.0
        set_ch(channels.drop_hit, drop_hit_level(tracker.t_in_state, hold, fade))
    else:
        set_ch(channels.drop_hit, 0)
    set_ch(channels.bpm_rate, features.bpm_rate)
    set_ch(channels.drop_momentary, 0)
