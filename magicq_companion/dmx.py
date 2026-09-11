"""Map analysis results onto the outgoing DMX frame."""

from __future__ import annotations

from .config import ChannelMap
from .features import Features
from .state_machine import Section, SectionTracker

# How long the drop-hit channel stays at 255 after DROP starts.
# Long enough for MagicQ macros / automations to catch, short enough
# that it only fires at the impact — not for the whole drop look.
_DROP_HIT_SECONDS = 0.15


def build_frame(
    dmx: bytearray,
    channels: ChannelMap,
    features: Features,
    tracker: SectionTracker,
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

    drop_hit = (
        tracker.state is Section.DROP and tracker.t_in_state < _DROP_HIT_SECONDS
    )
    set_ch(channels.drop_hit, 255 if drop_hit else 0)
    set_ch(channels.bpm_rate, features.bpm_rate)
    # Retired channels (energy / kick / melody) — keep them dark so MagicQ
    # does not hold the last value after a config upgrade.
    for retired in (7, 8, 10):
        dmx[retired - 1] = 0
