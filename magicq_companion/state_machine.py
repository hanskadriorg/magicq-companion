"""Section state machine: GROOVE / BREAKDOWN / BUILDUP / DROP.

Turns the continuous features into one discrete "which part of the
track are we in" decision, with hysteresis so lighting looks don't
flicker at section boundaries.
"""

from __future__ import annotations

import math
from collections import deque
from enum import Enum

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
        # near zero between kicks, so everything here is smoothed before
        # being compared. Time constant comes from cfg.drop_smooth_seconds.
        n = int(8.0 * frame_rate)
        self._bass_hist: deque[float] = deque(maxlen=n)
        self._energy_hist: deque[float] = deque(maxlen=n)
        self._energy_long: deque[float] = deque(maxlen=n)
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

        tau = max(0.04, float(cfg.drop_smooth_seconds))
        alpha_fast = 1.0 - math.exp(-dt / tau)
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
        cfg = self.cfg

        # The drop can interrupt a build-up or breakdown at any moment,
        # and optionally a groove if the slam is obvious (missed buildup).
        drop_states = (Section.BUILDUP, Section.BREAKDOWN)
        if cfg.drop_from_groove:
            drop_states = (Section.BUILDUP, Section.BREAKDOWN, Section.GROOVE)
        if state in drop_states and self._drop_triggered(f):
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
        """Drop = energy now louder than the recent peak by a set amount.

        That is a rise-of-N in ~drop_rise_window_seconds, but compared to
        the peak in that window (not a single trough 0.5 s ago) so a
        groove kick does not count. Breakdown bass-slam remains a backup.
        """
        cfg = self.cfg
        if self._t_total < WARMUP_SECONDS:
            return False
        window = max(0.08, float(cfg.drop_rise_window_seconds))
        need = max(8, int(window * self._frame_rate) + 2)
        if len(self._energy_hist) < need or len(self._bass_hist) < need:
            return False

        min_in = max(0.0, float(cfg.drop_min_in_state))
        if self.t_in_state < min_in:
            return False

        jump = self._drop_jump_ratio()
        e_now = self._energy_smooth
        b_now = self._bass_smooth
        b_ago = _sample_ago(self._bass_hist, self._frame_rate, window)
        # Peak in the lookback window, excluding the last ~80 ms (this slam).
        e_peak = _range_max(self._energy_hist, self._frame_rate, 0.08, window)
        b_peak = _range_max(self._bass_hist, self._frame_rate, 0.08, window)
        bass_in = b_now > cfg.kick_present_above
        loud_now = e_now > (1.05 - 0.15 * self.intensity)
        amount = float(cfg.drop_rise_amount)
        rose = (e_now - e_peak) >= amount or (b_now - b_peak) >= amount
        ratio = e_peak > 0.05 and e_now >= e_peak * jump
        from_quieter = e_peak < float(cfg.drop_quiet_below)
        fast_rise = bass_in and loud_now and from_quieter and (rose or ratio)

        bass_slam = b_now > (1.35 - 0.25 * self.intensity) and b_ago < 0.40

        soft_payoff = (
            self.state is Section.BUILDUP
            and self.build_progress > 0.55
            and e_now > e_peak * max(1.12, jump * 0.85)
            and b_now > 0.85
        )

        if self.state is Section.GROOVE:
            return fast_rise

        return fast_rise or bass_slam or soft_payoff

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


def _sample_ago(hist: deque[float], frame_rate: float, seconds: float) -> float:
    """Value from about `seconds` ago (last sample if the buffer is short)."""
    n = len(hist)
    if n == 0:
        return 0.0
    back = max(0, min(n - 1, int(round(seconds * frame_rate))))
    return hist[n - 1 - back]


def _range_max(hist: deque[float], frame_rate: float, lo_s: float, hi_s: float) -> float:
    """Max of samples from lo_s–hi_s ago (excludes the newest tail)."""
    n = len(hist)
    if n == 0:
        return 0.0
    start = n - 1 - max(0, int(round(hi_s * frame_rate)))
    stop = n - 1 - max(0, int(round(lo_s * frame_rate)))
    start = max(0, min(n - 1, start))
    stop = max(start + 1, min(n, stop + 1))
    return max(list(hist)[start:stop])


def _accumulate(value: float, condition: bool, dt: float) -> float:
    """Rise while the condition holds, decay twice as fast when it doesn't."""
    if condition:
        return min(value + dt, 30.0)
    return max(value - 2.0 * dt, 0.0)
