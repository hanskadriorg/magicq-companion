"""Shuffle cue order with no immediate repeat."""

from __future__ import annotations

import random
from typing import Sequence


class CueShuffleBag:
    """Exhaust a shuffled bag, then reshuffle without repeating the previous cue."""

    def __init__(self, cue_ids: Sequence[str]) -> None:
        self._pool = [c for c in cue_ids if c]
        self._bag: list[str] = []
        self._last: str | None = None

    def next(self, rng: random.Random | None = None) -> str | None:
        if not self._pool:
            return None
        if len(self._pool) == 1:
            self._last = self._pool[0]
            return self._last
        gen = rng or random.Random()
        if not self._bag:
            self._bag = list(self._pool)
            gen.shuffle(self._bag)
            if self._last is not None and self._bag[0] == self._last:
                self._bag.append(self._bag.pop(0))
        pick = self._bag.pop(0)
        self._last = pick
        return pick


def pick_cue_id(
    cue_ids: Sequence[str],
    last_id: str | None,
    rng: random.Random | None = None,
    bag: CueShuffleBag | None = None,
) -> str | None:
    """Pick next cue from a shuffle bag (preferred) or one-off without repeat."""
    if bag is not None:
        return bag.next(rng)
    pool = [c for c in cue_ids if c]
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    gen = rng or random.Random()
    if last_id is None or last_id not in pool:
        return gen.choice(pool)
    choices = [c for c in pool if c != last_id]
    return gen.choice(choices)
