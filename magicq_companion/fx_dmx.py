"""DMX merge helpers: 16-bit pairs, fade releases, blackout."""

from __future__ import annotations

from .fx_tags import FxTagKind

# Channels that were legacy-retired and must not be driven to "off" as zero pan/tilt.
_LEGACY_RETIRED = frozenset({7, 8, 12, 13})


def position_pairs(tags: dict[int, FxTagKind]) -> set[tuple[int, int]]:
    """Coarse/fine pairs for tagged position channels (coarse, fine=coarse+1)."""
    pairs: set[tuple[int, int]] = set()
    for ch, kind in tags.items():
        if kind is FxTagKind.POSITION and 1 <= ch < 512:
            pairs.add((int(ch), int(ch) + 1))
    return pairs


def expand_pair_channels(pairs: set[tuple[int, int]]) -> dict[int, int]:
    """Map fine channel → coarse channel for paired fades."""
    fine_to_coarse: dict[int, int] = {}
    for coarse, fine in pairs:
        fine_to_coarse[fine] = coarse
    return fine_to_coarse


def scale_pair_value(
    coarse: int,
    fine: int,
    level: float,
) -> tuple[int, int]:
    """Scale a 16-bit coarse/fine pair by `level` without splitting independently."""
    if level <= 0.0:
        return coarse, fine
    if level >= 1.0:
        return coarse, fine
    value16 = (int(coarse) << 8) | int(fine)
    scaled = int(round(value16 * level))
    scaled = max(0, min(0xFFFF, scaled))
    return (scaled >> 8) & 0xFF, scaled & 0xFF


def release_target(
    kind: FxTagKind,
    current: int,
    candidate: int,
    blended: int,
    cue_level: float,
) -> int:
    """On fade-out, do not slam position/colour to 0 — hold until blend completes."""
    if cue_level > 0.0:
        return blended
    if kind is FxTagKind.POSITION:
        return current
    if kind is FxTagKind.COLOUR and candidate == 0 and current != 0:
        return current
    return blended


def is_blackout_frame(frame: bytes) -> bool:
    """True only when every channel is zero (real blackout)."""
    return not any(frame)
