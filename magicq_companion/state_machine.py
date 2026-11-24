"""Section state machine: GROOVE / BREAKDOWN / BUILDUP / DROP.

Turns the continuous features into one discrete "which part of the
track are we in" decision, with hysteresis so lighting looks don't
flicker at section boundaries, and continuous parameter blending from
the offline Genre Agent across 4/4 and syncopated genres.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from enum import Enum
from itertools import islice

from .config import STYLE_PROFILES, DetectionConfig
from .features import WARMUP_SECONDS, Features
from .genre_agent import GenreControlVector
from .genre_profiles import GenrePolicy


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

        # Raw + lightly-smoothed histories for a fast energy-rise drop.
        n = max(8, int(2.5 * frame_rate))
        n_long = int(4.0 * frame_rate)
        self._bass_hist: deque[float] = deque(maxlen=n)
        self._energy_hist: deque[float] = deque(maxlen=n)
        self._energy_raw: deque[float] = deque(maxlen=n)
        self._energy_long: deque[float] = deque(maxlen=n_long)
        self._flux_hist: deque[float] = deque(maxlen=n)
        self._frame_rate = frame_rate
        self._t_total = 0.0
        self._bass_smooth = 0.0
        self._energy_smooth = 0.0
        self._energy_beat = 0.0
        self._flux_smooth = 0.0
        self.style_resolved = "club"
        self._style_guess = "club"
        self._style_hold = 0.0
        self._genre: GenreControlVector | None = None
        self._policy: GenrePolicy | None = None
        self._last_solid_kick_t = -100.0
        self._heard_kick = False
        self._kick_gone_s = 0.0
        self._kick_need_s = 2.8
        self._breakdown_cue = False

    # ------------------------------------------------------------------

    def _clamp01(self, name: str, default: float = 0.55) -> float:
        return max(0.0, min(1.0, float(getattr(self.cfg, name, default))))

    def _using_genre_vector(self) -> bool:
        mode = str(getattr(self.cfg, "style", "auto") or "auto").lower()
        return mode == "auto" and self._genre is not None and self._genre.backend != "off"

    def _using_policy(self) -> bool:
        mode = str(getattr(self.cfg, "style", "auto") or "auto").lower()
        return mode == "auto" and self._policy is not None

    def _profile(self) -> dict:
        return STYLE_PROFILES.get(self.style_resolved, STYLE_PROFILES["club"])

    def _param(self, name: str, default: float = 1.0) -> float:
        if self._using_genre_vector() and hasattr(self._genre, name):
            return float(getattr(self._genre, name))
        return float(self._profile().get(name, default))

    @property
    def build_intensity(self) -> float:
        if self._using_policy():
            return max(0.0, min(1.0, float(self._policy.build_intensity)))  # type: ignore[union-attr]
        return self._clamp01("build_intensity")

    @property
    def drop_intensity(self) -> float:
        if self._using_policy():
            return max(0.0, min(1.0, float(self._policy.drop_intensity)))  # type: ignore[union-attr]
        extra = self._param("drop_intensity_add", 0.0)
        return max(0.0, min(1.0, self._clamp01("drop_intensity") + extra))

    def _syncopation_tolerance(self) -> float:
        return max(0.0, min(1.0, self._param("syncopation_tolerance", 0.15)))

    def _build_threshold(self) -> float:
        """Higher build intensity -> lower build_score needed to enter BUILDUP."""
        raw = self.cfg.build_score_on * (1.35 - 0.80 * self.build_intensity)
        return raw * self._param("build_threshold_mul", 1.0)

    def _evidence_need(self) -> float:
        """Higher build intensity -> fewer seconds of evidence before switching."""
        need = self.cfg.evidence_seconds * (1.5 - 1.0 * self.build_intensity)
        return max(0.3, need * self._param("evidence_mul", 1.0))

    def _min_dwell(self) -> float:
        dwell = self.cfg.min_dwell_seconds * (1.25 - 0.5 * self.build_intensity)
        return max(0.5, dwell * self._param("dwell_mul", 1.0))

    def _home_section(self) -> Section:
        if self._using_genre_vector():
            if float(self._genre.fizzle_to_breakdown) > 0.5:  # type: ignore[union-attr]
                return Section.BREAKDOWN
            return Section.GROOVE
        name = str(self._profile().get("fizzle_to", "groove")).lower()
        if name == "breakdown":
            return Section.BREAKDOWN
        return Section.GROOVE

    def _breakdown_need(self, need: float) -> float:
        return need * self._param("breakdown_need_mul", 0.45)

    def _groove_return_need(self, need: float) -> float:
        return need * self._param("groove_return_mul", 1.0)

    def _breakdown_intensity(self) -> float:
        if self._using_policy():
            return max(0.0, min(1.0, float(self._policy.breakdown_intensity)))  # type: ignore[union-attr]
        try:
            return max(0.0, min(1.0, float(self.cfg.breakdown_intensity)))
        except (TypeError, ValueError, AttributeError):
            return 0.55

    def _breakdown_dwell(self) -> float:
        """Groove -> breakdown should not wait a full phrase."""
        inten = self._breakdown_intensity()
        dwell = self._min_dwell() * self._param("breakdown_dwell_mul", 0.45)
        dwell *= 1.25 - 0.70 * inten
        return max(0.35, dwell)

    def _drop_hold(self) -> float:
        if self._using_policy():
            return max(1.0, float(self._policy.drop_hold_seconds))  # type: ignore[union-attr]
        return max(1.0, self.cfg.drop_hold_seconds * self._param("drop_hold_mul", 1.0))

    def _expected_build(self) -> float:
        return max(2.0, self.cfg.expected_build_seconds * self._param("expected_build_mul", 1.0))

    def _kick_absent_below(self) -> float:
        inten = self._breakdown_intensity()
        return self.cfg.kick_absent_below * (0.75 + 0.50 * inten) * self._param(
            "kick_absent_mul", 1.0
        )

    def _build_decay(self) -> float:
        """Seconds of 'not building' before BUILDUP falls back to GROOVE."""
        if self._using_policy():
            return max(0.25, float(self._policy.build_decay_seconds))  # type: ignore[union-attr]
        base = max(0.25, float(getattr(self.cfg, "build_decay_seconds", 8.0)))
        return base * self._param("build_decay_mul", 1.0)

    def _drop_jump_ratio(self) -> float:
        # Keep fast drop-sensing parameters on Layer 2 while honoring Layer 1 drop_intensity.
        jump = self.cfg.drop_energy_jump * (1.25 - 0.5 * self.drop_intensity)
        if self._using_policy():
            return max(1.02, jump)
        return jump * self._param("drop_jump_mul", 1.0)

    def _drop_rise_needed(self) -> float:
        rise = float(getattr(self.cfg, "drop_rise", 0.40))
        if self._using_policy():
            return max(0.05, rise * (1.20 - 0.40 * self.drop_intensity))
        return max(0.05, rise * self._param("drop_rise_mul", 1.0))

    def reset_on_silence(self) -> None:
        """Clear active section/ramp state once audio has been absent."""
        self.state = Section.GROOVE
        self.t_in_state = 0.0
        self.build_progress = 0.0
        self._ev_breakdown = 0.0
        self._ev_build = 0.0
        self._ev_groove = 0.0
        self._ev_fizzle = 0.0
        self._heard_kick = False
        self._kick_gone_s = 0.0
        self._breakdown_cue = False

    def evidence(self) -> dict:
        g = self._genre
        return {
            "ev_build": round(self._ev_build, 2),
            "ev_breakdown": round(self._ev_breakdown, 2),
            "ev_fizzle": round(self._ev_fizzle, 2),
            "ev_need": round(self._evidence_need(), 2),
            "build_decay": round(self._build_decay(), 2),
            "kick_gone": round(self._kick_gone_s, 2),
            "kick_need": round(self._kick_need_s, 2),
            "style": self.style_resolved,
            "style_mode": getattr(self.cfg, "style", "auto"),
            "archetype": g.archetype if g is not None else self.style_resolved,
            "archetype_label": g.archetype_label if g is not None else self.style_resolved,
            "genre_tags": g.formatted_tags() if g is not None else "",
            "genre_backend": g.backend if g is not None else "dsp",
        }

    # ------------------------------------------------------------------

    def update(
        self,
        f: Features,
        dt: float,
        *,
        genre: GenreControlVector | None = None,
        policy: GenrePolicy | None = None,
    ) -> Section:
        cfg = self.cfg
        self._genre = genre
        self._policy = policy
        self.t_in_state += dt
        self._t_total += dt

        smooth_s = max(0.02, float(getattr(cfg, "drop_smooth_seconds", 0.08)))
        alpha_fast = 1.0 - math.exp(-dt / smooth_s)
        beat_smooth_s = max(0.85, 1.5 * (60.0 / f.bpm) if f.bpm >= 60.0 else 0.85)
        alpha_beat = 1.0 - math.exp(-dt / beat_smooth_s)
        self._bass_smooth += alpha_fast * (f.bass_norm - self._bass_smooth)
        self._energy_smooth += alpha_fast * (f.energy_norm - self._energy_smooth)
        self._energy_beat += alpha_beat * (f.energy_norm - self._energy_beat)
        self._flux_smooth += alpha_fast * (f.flux - self._flux_smooth)

        self._bass_hist.append(self._bass_smooth)
        self._energy_hist.append(self._energy_smooth)
        # Beat-smoothed energy for fast-rise prevents individual kick crests in a
        # steady groove from looking like section-level loudness jumps.
        self._energy_raw.append(self._energy_beat)
        self._energy_long.append(self._energy_beat)
        self._flux_hist.append(self._flux_smooth)

        self._resolve_style(f, dt)
        self._update_kick_absence(f, dt)

        sync_tol = self._syncopation_tolerance()
        sync_groove_alive = (
            sync_tol >= 0.45
            and f.bass_norm > 0.65
            and f.onset_density >= 2.0
        )
        kick_present = (
            f.above_floor
            and (
                f.bass_norm > cfg.kick_present_above
                or f.kick_depth > 0.35
                or sync_groove_alive
            )
        )
        building = f.above_floor and (f.build_score > self._build_threshold())

        self._ev_breakdown = _accumulate(self._ev_breakdown, self._breakdown_cue, dt)
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

        can_drop = state in (Section.BUILDUP, Section.BREAKDOWN) or (
            state is Section.GROOVE and bool(getattr(self.cfg, "drop_from_groove", True))
        )
        if can_drop and self._drop_triggered(f):
            self.state = Section.DROP
            return

        if state is Section.DROP:
            if self.t_in_state >= 2.5 and self._ev_breakdown >= self._breakdown_need(need):
                self.state = Section.BREAKDOWN
            elif self.t_in_state >= self._drop_hold():
                self.state = Section.GROOVE
            return

        if state is Section.GROOVE:
            if dwell_ok and self._ev_build >= need:
                self.state = Section.BUILDUP
            elif (
                self.t_in_state >= self._breakdown_dwell()
                and self._ev_breakdown >= self._breakdown_need(need)
            ):
                self.state = Section.BREAKDOWN
            return

        if state is Section.BREAKDOWN:
            if self._ev_build >= need * 0.75:
                self.state = Section.BUILDUP
            elif dwell_ok and self._ev_groove >= self._groove_return_need(need):
                self.state = Section.GROOVE
            return

        if state is Section.BUILDUP:
            timed_out = self.t_in_state >= self.cfg.build_timeout_seconds
            fizzled = self._ev_fizzle >= self._build_decay() and dwell_ok
            if timed_out or fizzled:
                self.state = self._home_section()
            return

    def _resolve_style(self, f: Features, dt: float) -> None:
        mode = str(getattr(self.cfg, "style", "auto") or "auto").lower()
        if mode != "auto" and mode in STYLE_PROFILES:
            self.style_resolved = mode
            return
        if self._genre is not None and self._genre.backend != "off":
            self.style_resolved = self._genre.archetype
            return
        guess = self._infer_style(f)
        if guess == self._style_guess:
            self._style_hold += dt
        else:
            self._style_guess = guess
            self._style_hold = 0.0
        if self._style_hold >= 20.0 or self.style_resolved not in STYLE_PROFILES:
            self.style_resolved = guess

    def _infer_style(self, f: Features) -> str:
        bpm = float(f.bpm or 0.0)
        regular = float(getattr(f, "kick_regularity", 0.0))
        if 0 < bpm < 98 and f.onset_density < 2.5:
            return "breakdown"
        if regular < 0.45 and (bpm >= 156 or f.onset_density > 5.5):
            return "punchy"
        if regular >= 0.55 and 136 < bpm <= 154:
            return "long_build"
        if regular >= 0.50 and 116 <= bpm <= 136:
            return "club"
        if bpm >= 156:
            return "punchy"
        if bpm > 136:
            return "long_build"
        return "club"

    def _update_kick_absence(self, f: Features, dt: float) -> None:
        """Breakdown cue: beat/bass dropped out relative to the genre's groove."""
        period = 60.0 / f.bpm if f.bpm >= 60.0 else 0.50
        inten = self._breakdown_intensity()
        sync_tol = self._syncopation_tolerance()
        if self._using_policy():
            base_beats = max(1.0, min(16.0, float(self._policy.breakdown_beats)))  # type: ignore[union-attr]
            beats = base_beats * (1.50 - 0.85 * inten)
        else:
            try:
                user_beats = max(1.0, min(16.0, float(self.cfg.breakdown_beats)))
            except (TypeError, ValueError, AttributeError):
                user_beats = 4.0
            style_beats = self._param("breakdown_beats", 4.0)
            beats = user_beats * (style_beats / 4.0) * (1.50 - 0.85 * inten)
        # Syncopated genres (hip-hop, trap, DnB, breakbeat) naturally leave gaps
        # between sub-kicks while snares/hats carry the rhythm.
        beats *= 1.0 + 0.90 * sync_tol
        self._kick_need_s = max(0.4, beats * period)

        if f.beat:
            self._last_solid_kick_t = self._t_total
            self._heard_kick = True
            self._kick_gone_s = 0.0
        else:
            self._kick_gone_s += dt

        if float(getattr(f, "music_score", 1.0)) < 0.12:
            self._breakdown_cue = False
            return

        gone_long = self._heard_kick and self._kick_gone_s >= self._kick_need_s
        if sync_tol >= 0.45:
            # In breakbeat/hip-hop/rock, an active snare/hat pattern with solid
            # bassline energy is still a groove even if 4/4 kick pulses paused.
            percussion_lull = f.onset_density < 1.8 or f.bass_norm < self._kick_absent_below() * 1.30
            gone_long = gone_long and percussion_lull

        bass_out = f.bass_norm < self._kick_absent_below() and f.kick_depth < 0.22
        if sync_tol >= 0.45:
            bass_out = bass_out and f.onset_density < 2.2
        self._breakdown_cue = gone_long or bass_out

    def _drop_triggered(self, f: Features) -> bool:
        """Drop = a short-window energy rise, plus slam/onset cues."""
        if self._t_total < WARMUP_SECONDS:
            return False
        min_state = max(0.0, float(getattr(self.cfg, "drop_min_state_seconds", 0.15)))
        if self.t_in_state < min_state:
            return False
        if len(self._energy_raw) < max(8, int(0.2 * self._frame_rate)):
            return False

        if self._fast_rise(f):
            return True

        bass_ago = _window_median(self._bass_hist, self._frame_rate)
        energy_ago = _window_median(self._energy_hist, self._frame_rate)
        jump = self._drop_jump_ratio()
        min_bass = float(getattr(self.cfg, "drop_min_bass", 0.70))

        bass_slam = self._bass_smooth > (1.35 - 0.25 * self.drop_intensity) and bass_ago < 0.40

        energy_jump = (
            self._energy_smooth > energy_ago * jump
            and self._bass_smooth > min_bass
            and self._energy_smooth > (1.05 - 0.15 * self.drop_intensity)
        )

        local_contrast = False
        if len(self._energy_long) >= self._energy_long.maxlen // 2:
            floor = min(self._energy_long)
            local_contrast = (
                floor > 0.15
                and self._energy_smooth > floor * (1.55 - 0.35 * self.drop_intensity)
                and self._bass_smooth > min_bass
            )

        flux_spike = False
        if bool(getattr(self.cfg, "drop_use_onset", True)) and len(self._flux_hist) >= 8:
            flux_med = statistics.median(self._flux_hist)
            flux_spike = (
                flux_med > 0
                and self._flux_smooth > flux_med * (2.2 - 0.6 * self.drop_intensity)
                and self._bass_smooth > max(0.9, min_bass)
                and self._energy_smooth > energy_ago * 1.1
            )

        soft_payoff = (
            self.state is Section.BUILDUP
            and self.build_progress > 0.55
            and self._energy_smooth > energy_ago * max(1.12, jump * 0.85)
            and self._bass_smooth > min_bass
        )

        hit = bass_slam or energy_jump or local_contrast or flux_spike or soft_payoff
        if hit and self.state is Section.GROOVE:
            return self._fast_rise(f)
        return hit

    def _fast_rise(self, f: Features) -> bool:
        """Energy rose by `drop_rise` (or jump ratio) inside `drop_rise_window`."""
        window = max(0.12, float(getattr(self.cfg, "drop_rise_window", 0.5)))
        need = max(4, int(window * self._frame_rate))
        if len(self._energy_raw) < need:
            return False
        ago = self._energy_raw[-need]
        now = self._energy_beat
        min_bass = float(getattr(self.cfg, "drop_min_bass", 0.70))
        if f.bass_norm < min_bass and self._bass_smooth < min_bass:
            return False
        rise = now - ago
        needed = self._drop_rise_needed()
        abs_ok = rise >= needed
        ratio_ok = ago > 0.20 and now > ago * self._drop_jump_ratio()
        if self.state is Section.GROOVE:
            return abs_ok and (ratio_ok or rise >= needed * 1.15)
        return abs_ok or ratio_ok

    def _update_build_progress(self, f: Features, dt: float) -> None:
        if self.state is Section.BUILDUP:
            rate = (0.5 + f.build_score) / self._expected_build()
            self.build_progress = min(1.0, self.build_progress + rate * dt)
        elif self.state is Section.DROP:
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
