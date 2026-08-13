"""Map analysis results onto the outgoing DMX frame."""

from __future__ import annotations

import math

from .config import ChannelMap
from .features import Features
from .state_machine import Section, SectionTracker

_BEAT_DECAY_SECONDS = 0.08
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

    four_four_level = 255 * math.exp(
        -features.seconds_since_four_four / _BEAT_DECAY_SECONDS
    )
    set_ch(channels.four_four, four_four_level)

    # energy_norm 1.0 (= recent average loudness) maps to DMX 128.
    set_ch(channels.energy, features.energy_norm * 128)
    set_ch(channels.kick, features.bass_norm * 128)

    drop_hit = (
        tracker.state is Section.DROP and tracker.t_in_state < _DROP_HIT_SECONDS
    )
    set_ch(channels.drop_hit, 255 if drop_hit else 0)
    set_ch(channels.melody, features.melody_score * 255)
    set_ch(channels.centroid, features.spectral_centroid_norm * 255)
    set_ch(channels.tilt, features.tilt_norm * 255)
    set_ch(channels.bpm_rate, features.bpm_rate)
