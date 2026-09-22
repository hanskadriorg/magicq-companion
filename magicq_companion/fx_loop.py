"""Loop detection and snap-to-loop helpers for FX recordings."""

from __future__ import annotations


def frame_distance(a: bytes, b: bytes, skip_channels: set[int] | None = None) -> float:
    """Mean absolute DMX difference on 1-based channels (0..255 scale)."""
    skip = skip_channels or set()
    if len(a) != len(b):
        n = min(len(a), len(b))
        a, b = a[:n], b[:n]
    total = 0.0
    count = 0
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        ch = i + 1
        if ch in skip:
            continue
        total += abs(int(x) - int(y))
        count += 1
    return total / count if count else 0.0


def detect_loop_window(
    frames: list[bytes],
    *,
    window: int = 8,
    threshold: float = 6.0,
    skip_channels: set[int] | None = None,
) -> int | None:
    """Return frame index where the tail matches the head (loop closure)."""
    if len(frames) < window * 2 + 2:
        return None
    head = frames[:window]
    best_i: int | None = None
    best_score = threshold
    for i in range(window, len(frames) - window):
        tail = frames[i : i + window]
        score = sum(
            frame_distance(head[j], tail[j], skip_channels) for j in range(window)
        ) / window
        if score < best_score:
            best_score = score
            best_i = i
    return best_i


def snap_trim_to_loop(
    frames: list[bytes],
    trim_start: int,
    trim_end: int,
    *,
    search_radius: int = 6,
    match_threshold: float = 6.0,
    skip_channels: set[int] | None = None,
) -> tuple[int, int, bool]:
    """Nudge trim handles within a small window; False if loop seam not found."""
    if not frames or trim_end <= trim_start:
        return trim_start, trim_end, False
    n = len(frames)
    start = max(0, min(trim_start, n - 1))
    end = max(start + 1, min(trim_end, n))

    def local_score(i: int, j: int) -> float:
        if j >= n or i >= j:
            return 1e9
        return frame_distance(frames[i], frames[j], skip_channels)

    best_start = start
    best_s = local_score(start, min(start + 1, n - 1))
    for di in range(-search_radius, search_radius + 1):
        cand = start + di
        if 0 <= cand < end:
            s = local_score(cand, min(cand + 1, n - 1))
            if s < best_s:
                best_s = s
                best_start = cand

    best_end = end
    loop_ref = frames[best_start]
    best_e = local_score(best_start, end - 1)
    for di in range(-search_radius, search_radius + 1):
        cand = end + di
        if best_start < cand <= n:
            s = frame_distance(loop_ref, frames[cand - 1], skip_channels)
            if s < best_e:
                best_e = s
                best_end = cand
    best_end = max(best_start + 1, min(best_end, n))
    seam = frame_distance(frames[best_start], frames[best_end - 1], skip_channels)
    if seam > match_threshold:
        return trim_start, trim_end, False
    if best_start == trim_start and best_end == trim_end:
        return trim_start, trim_end, True
    return best_start, best_end, True
