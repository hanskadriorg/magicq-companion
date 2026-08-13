"""Section state machine: GROOVE / BREAKDOWN / BUILDUP / DROP.

Turns the continuous features into one discrete "which part of the
track are we in" decision, with hysteresis so lighting looks don't
flicker at section boundaries.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from enum import Enum
from itertools import islice

from .config import DetectionConfig
from .features import WARMUP_SECONDS, Features


class Section(Enum):
    GROOVE = "groove"
    BREAKDOWN = "breakdown"
    BUILDUP = "buildup"
    DROP = "drop"


class SectionTracker:
    def __init__(self, cfg: DetectionConfig, frame_rate: float) -> None:
        self.cfg = cfg
        self.state = Section.GROOVE
        self.t_in_state = 0.0
        self.build_progress = 0.0

        # Evidence accumulators (seconds of consistent condition).
        self._ev_breakdown = 0.0
        self._ev_build = 0.0
        self._ev_groove = 0.0
        self._ev_fizzle = 0.0

        # Smoothed signals for the drop trigger. Raw bass/energy pulse to
        # near zero between kicks, so everything here is smoothed over
        # slightly more than one kick period before being compared.
        n = int(1.5 * frame_rate)
        # Longer window for local contrast (floor of recent energy).
        n_long = int(4.0 * frame_rate)
        self._bass_hist: deque[float] = deque(maxlen=n)
        self._energy_hist: deque[float] = deque(maxlen=n)
        self._energy_long: deque[float] = deque(maxlen=n_long)
        self._flux_hist: deque[float] = deque(maxlen=n)
        self._frame_rate = frame_rate
        self._t_total = 0.0
        self._bass_smooth = 0.0
        self._energy_smooth = 0.0
        self._flux_smooth = 0.0

    # ------------------------------------------------------------------

    @property
    def intensity(self) -> float:
        return max(0.0, min(1.0, float(self.cfg.intensity)))

    def _build_threshold(self) -> float:
        """Higher intensity → lower build_score needed to enter BUILDUP."""
        # intensity 0 → 1.35×, 0.55 → ~0.91×, 1 → 0.55×
        return self.cfg.build_score_on * (1.35 - 0.80 * self.intensity)

    def _evidence_need(self) -> float:
        """Higher intensity → less seconds of evidence before switching."""
        # intensity 0 → 1.5×, 1 → 0.5×
        return self.cfg.evidence_seconds * (1.5 - 1.0 * self.intensity)

    def _min_dwell(self) -> float:
        return self.cfg.min_dwell_seconds * (1.25 - 0.5 * self.intensity)

    def _drop_jump_ratio(self) -> float:
        # intensity 0 → 1.25×, 1 → 0.75× of configured jump
        return self.cfg.drop_energy_jump * (1.25 - 0.5 * self.intensity)

    # ------------------------------------------------------------------

    def update(self, f: Features, dt: float) -> Section:
        cfg = self.cfg
        self.t_in_state += dt
        self._t_total += dt

        alpha_fast = 1.0 - math.exp(-dt / 0.6)
        self._bass_smooth += alpha_fast * (f.bass_norm - self._bass_smooth)
        self._energy_smooth += alpha_fast * (f.energy_norm - self._energy_smooth)
        self._flux_smooth += alpha_fast * (f.flux - self._flux_smooth)

        self._bass_hist.append(self._bass_smooth)
        self._energy_hist.append(self._energy_smooth)
        self._energy_long.append(self._energy_smooth)
        self._flux_hist.append(self._flux_smooth)

        kick_absent = f.bass_norm < cfg.kick_absent_below
        kick_present = f.bass_norm > cfg.kick_present_above
        building = f.build_score > self._build_threshold()

        self._ev_breakdown = _accumulate(self._ev_breakdown, kick_absent, dt)
        self._ev_build = _accumulate(self._ev_build, building, dt)
        self._ev_groove = _accumulate(self._ev_groove, kick_present and not building, dt)
        self._ev_fizzle = _accumulate(self._ev_fizzle, not building, dt)

        self._update_build_progress(f, dt)

        previous = self.state
        self._transition(f)
        if self.state is not previous:
            self.t_in_state = 0.0
            self._ev_breakdown = self._ev_build = 0.0
            self._ev_groove = self._ev_fizzle = 0.0
            if self.state is Section.DROP:
                self.build_progress = 1.0
        return self.state

    # ------------------------------------------------------------------

    def _transition(self, f: Features) -> None:
        state = self.state
        dwell_ok = self.t_in_state >= self._min_dwell()
        need = self._evidence_need()

        # The drop can interrupt a build-up or breakdown at any moment.
        if state in (Section.BUILDUP, Section.BREAKDOWN) and self._drop_triggered(f):
            self.state = Section.DROP
            return

        if state is Section.DROP:
            if self.t_in_state >= self.cfg.drop_hold_seconds:
                self.state = Section.GROOVE
            return

        if state is Section.GROOVE:
            if dwell_ok and self._ev_build >= need:
                self.state = Section.BUILDUP
            elif dwell_ok and self._ev_breakdown >= need * 0.75:
                self.state = Section.BREAKDOWN
            return

        if state is Section.BREAKDOWN:
            if self._ev_build >= need * 0.75:
                self.state = Section.BUILDUP
            elif dwell_ok and self._ev_groove >= need:
                self.state = Section.GROOVE
            return

        if state is Section.BUILDUP:
            timed_out = self.t_in_state >= self.cfg.build_timeout_seconds
            fizzled = self._ev_fizzle >= need * 2.0 and dwell_ok
            if timed_out or fizzled:
                self.state = Section.GROOVE
            return

    def _drop_triggered(self, f: Features) -> bool:
        """Drop = bass slam, energy jump, or local contrast after a build.

        Inspired by EDM switch-point / novelty research: energy contrast
        vs a recent floor, bass re-entry, and spectral-flux spike — not
        only a single ratio against ~1 s ago.
        """
        if self._t_total < WARMUP_SECONDS:
            return False
        if len(self._bass_hist) < self._bass_hist.maxlen:
            return False

        bass_ago = _window_median(self._bass_hist, self._frame_rate)
        energy_ago = _window_median(self._energy_hist, self._frame_rate)
        jump = self._drop_jump_ratio()

        # Classic drop out of a breakdown/kick-gap.
        bass_slam = self._bass_smooth > (1.35 - 0.25 * self.intensity) and bass_ago < 0.40

        # Drop out of a kick-driven build: sudden loudness jump with bass.
        energy_jump = (
            self._energy_smooth > energy_ago * jump
            and self._bass_smooth > 0.75
            and self._energy_smooth > (1.05 - 0.15 * self.intensity)
            and self.t_in_state > 1.5
        )

        # Local contrast: energy now vs the quietest recent moment
        # (catches drops after a snare fill / duck that slope-ratio misses).
        local_contrast = False
        if len(self._energy_long) >= self._energy_long.maxlen // 2:
            floor = min(self._energy_long)
            local_contrast = (
                floor > 0.15
                and self._energy_smooth > floor * (1.55 - 0.35 * self.intensity)
                and self._bass_smooth > 0.85
                and self.t_in_state > 1.5
            )

        # Spectral novelty spike with bass present (transient "hit").
        flux_spike = False
        if len(self._flux_hist) >= 8:
            flux_med = statistics.median(self._flux_hist)
            flux_spike = (
                flux_med > 0
                and self._flux_smooth > flux_med * (2.2 - 0.6 * self.intensity)
                and self._bass_smooth > 0.9
                and self._energy_smooth > energy_ago * 1.1
                and self.t_in_state > 1.5
            )

        # Late in a strong build, accept a softer energy jump.
        soft_payoff = (
            self.state is Section.BUILDUP
            and self.build_progress > 0.55
            and self._energy_smooth > energy_ago * max(1.12, jump * 0.85)
            and self._bass_smooth > 0.85
            and self.t_in_state > 2.0
        )

        return bass_slam or energy_jump or local_contrast or flux_spike or soft_payoff

    def _update_build_progress(self, f: Features, dt: float) -> None:
        if self.state is Section.BUILDUP:
            rate = (0.5 + f.build_score) / self.cfg.expected_build_seconds
            self.build_progress = min(1.0, self.build_progress + rate * dt)
        elif self.state is Section.DROP:
            # Hold high for the first beats of the drop, then release.
            if self.t_in_state > 2.0:
                self.build_progress *= math.exp(-dt * 1.5)
        else:
            self.build_progress *= math.exp(-dt * 3.0)


def _window_median(hist: deque[float], frame_rate: float) -> float:
    """Median of the samples 0.7-1.3 seconds ago."""
    n = len(hist)
    start = n - int(1.3 * frame_rate)
    stop = n - int(0.7 * frame_rate)
    return statistics.median(islice(hist, max(start, 0), stop))


def _accumulate(value: float, condition: bool, dt: float) -> float:
    """Rise while the condition holds, decay twice as fast when it doesn't."""
    if condition:
        return min(value + dt, 30.0)
    return max(value - 2.0 * dt, 0.0)
