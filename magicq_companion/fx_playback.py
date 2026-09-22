"""Merge and fade FX cue frames onto an output buffer."""

from __future__ import annotations

from .fx_dmx import position_pairs, release_target, scale_pair_value
from .fx_store import FxCue, FxRecording
from .fx_tags import FxTagKind, merge_value, tag_for_channel


def _parse_rec_tags(recording: FxRecording) -> dict[int, FxTagKind]:
    out: dict[int, FxTagKind] = {}
    for k, v in recording.tags.items():
        try:
            out[int(k)] = FxTagKind(str(v).lower())
        except ValueError:
            out[int(k)] = FxTagKind.INTENSITY
    return out


def apply_cue_frame(
    out: bytearray,
    base: bytes,
    recording: FxRecording,
    cue: FxCue,
    frame_index: int,
    *,
    fade_t: float,
    cue_level: float,
    tags: dict[int, FxTagKind],
) -> bool:
    """Write one playback frame into `out`. Returns True if frame has colour content."""
    frames = recording.frames
    if not frames:
        return False
    start = max(0, cue.trim_start)
    end = cue.trim_end if cue.trim_end > start else len(frames)
    end = min(end, len(frames))
    if end <= start:
        return False
    rel = frame_index - start
    if rel < 0:
        return False
    if cue.loop:
        idx = start + (rel % max(1, end - start))
    else:
        idx = start + rel
        if idx >= end:
            return False
    src = frames[idx]
    rec_tags = _parse_rec_tags(recording)
    merged_tags = {**rec_tags, **tags}
    pairs = position_pairs(merged_tags)
    paired_fine = {fine: coarse for coarse, fine in pairs}
    has_colour = False
    seen_coarse: set[int] = set()

    for ch in range(1, min(len(out), len(src), len(base)) + 1):
        if ch in paired_fine:
            continue
        kind = tag_for_channel(ch, merged_tags)
        if kind is FxTagKind.COLOUR and int(src[ch - 1]) > 0:
            has_colour = True
        coarse = ch
        fine_ch = coarse + 1
        if (coarse, fine_ch) in pairs and fine_ch <= len(src):
            seen_coarse.add(coarse)
            c0, f0 = int(base[coarse - 1]), int(base[fine_ch - 1])
            c1, f1 = int(src[coarse - 1]), int(src[fine_ch - 1])
            sc, sf = scale_pair_value(c1, f1, cue_level)
            blended_c = merge_value(kind, c0, sc, fade_t, 1.0)
            blended_f = merge_value(kind, f0, sf, fade_t, 1.0)
            out[coarse - 1] = release_target(kind, c0, c1, blended_c, cue_level)
            out[fine_ch - 1] = release_target(kind, f0, f1, blended_f, cue_level)
            continue
        if coarse in seen_coarse:
            continue
        current = int(base[ch - 1])
        candidate = int(src[ch - 1])
        blended = merge_value(kind, current, candidate, fade_t, cue_level)
        out[ch - 1] = release_target(kind, current, candidate, blended, cue_level)
    return has_colour


def cue_fade_level(
    t_in_cue: float,
    cue_duration: float,
    fade_in_s: float,
    fade_out_s: float,
    *,
    released: bool = False,
    release_at: float | None = None,
    now: float | None = None,
) -> float:
    if t_in_cue < 0:
        return 0.0
    effective_t = t_in_cue
    if released and release_at is not None and now is not None:
        hold = max(fade_in_s, release_at)
        if now < hold:
            effective_t = now - (hold - fade_in_s) if fade_in_s > 0 else t_in_cue
        else:
            out_t = now - hold
            if fade_out_s > 0:
                return max(0.0, 1.0 - out_t / fade_out_s)
            return 0.0
    if fade_in_s > 0 and effective_t < fade_in_s:
        return effective_t / fade_in_s
    if cue_duration > 0 and fade_out_s > 0 and not released:
        remaining = cue_duration - effective_t
        if remaining < fade_out_s:
            return max(0.0, remaining / fade_out_s)
    return 1.0
