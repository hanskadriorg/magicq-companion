"""Manual FX channel tags and merge rules (HTP / LTP)."""

from __future__ import annotations

from enum import Enum


class FxTagKind(str, Enum):
    INTENSITY = "intensity"
    POSITION = "position"
    COLOUR = "colour"


class FadeMerge(str, Enum):
    HTP = "htp"
    LTP = "ltp"


TAG_DEFAULT_MERGE: dict[FxTagKind, FadeMerge] = {
    FxTagKind.INTENSITY: FadeMerge.HTP,
    FxTagKind.POSITION: FadeMerge.LTP,
    FxTagKind.COLOUR: FadeMerge.LTP,
}


def merge_value(
    kind: FxTagKind,
    current: int,
    candidate: int,
    fade_t: float,
    cue_level: float,
) -> int:
    """Blend `candidate` into `current` at fade position fade_t in [0,1]."""
    merge = TAG_DEFAULT_MERGE[kind]
    scaled = int(round(candidate * cue_level))
    if fade_t <= 0.0:
        return current
    if fade_t >= 1.0:
        target = scaled
    else:
        target = int(round(current + (scaled - current) * fade_t))
    if merge is FadeMerge.HTP:
        return max(current, target)
    return target


def tag_for_channel(channel: int, tags: dict[int, FxTagKind]) -> FxTagKind:
    """Channel 0 in tags is the recording-wide default tag kind."""
    if 0 in tags:
        return tags[0]
    return tags.get(channel, FxTagKind.INTENSITY)
