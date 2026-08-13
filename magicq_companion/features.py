"""Per-block audio feature extraction.

Everything works on small hops (default 512 samples @ 44.1 kHz, ~12 ms)
so the pipeline stays real-time. Features are normalized against slow
rolling baselines so the system adapts when the DJ changes gain or EQ.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .config import (
    BPM_RATE_NAME_TO_LEVEL,
    BPM_RATE_NAMES,
    BpmRateConfig,
    BuildScoreConfig,
    DetectionConfig,
)

_EPS = 1e-9

# Trend histories are downsampled to roughly this rate (Hz) before
# fitting a slope, which keeps polyfit cheap and smooths jitter.
_TREND_RATE = 10.0
_TREND_WINDOW_SECONDS = 8.0

# Suppress build/drop logic until baselines have settled after startup.
WARMUP_SECONDS = 10.0

# Map spectral centroid (Hz) onto 0..1 for DMX. Club music usually sits
# well inside this log range; outside it we just clamp.
_CENTROID_HZ_LO = 80.0
_CENTROID_HZ_HI = 8000.0

# BPM-rate DMX steps: eighth, quarter, half, full tempo.
_BPM_RATE_LEVELS = tuple(BPM_RATE_NAME_TO_LEVEL[n] for n in BPM_RATE_NAMES)
_BPM_RATE_NAME_TO_INDEX = {n: i for i, n in enumerate(BPM_RATE_NAMES)}
_BPM_RATE_FADE_SECONDS = 0.25
# Adaptive thresholds from a rolling energy window (seconds / sample rate).
_BPM_RATE_HISTORY_SECONDS = 300.0
_BPM_RATE_HISTORY_HZ = 2.0
# Need this much history before trusting percentiles (cold-start).
_BPM_RATE_MIN_HISTORY_SECONDS = 30.0
# Smooth loudness over ~2 s so kick gaps don't yank the rate to 1/8.
_BPM_RATE_ENERGY_SMOOTH_SECONDS = 2.0


@dataclass
class Features:
    """One frame (hop) of analysis results."""

    rms: float = 0.0
    bass: float = 0.0
    mid: float = 0.0
    treble: float = 0.0
    flux: float = 0.0
    onset_density: float = 0.0  # onsets per second, recent window
    bass_norm: float = 0.0      # bass relative to rolling baseline (1.0 = avg)
    energy_norm: float = 0.0    # loudness relative to rolling baseline
    build_score: float = 0.0    # 0..1 composite "tension rising" score
    # 0..1: kick still present but focus has shifted to mid/high (melody/pads).
    melody_score: float = 0.0
    beat: bool = False          # kick detected in this frame
    # Hard 0/255 toggle on each kick — ChamSys BPM inputs dislike fades.
    beat_pulse: float = 0.0
    bpm: float = 0.0
    seconds_since_beat: float = 999.0
    # Phase-locked 4/4 quarter-note grid (pulses even if a kick is missed).
    four_four_beat: bool = False
    seconds_since_four_four: float = 999.0
    # 0..3 — which quarter of the bar the grid thinks we're on.
    beat_in_bar: int = 0
    # Brightness proxies for lighting (0..1 → DMX 0..255).
    spectral_centroid_norm: float = 0.0  # dark bass-heavy → bright/trebly
    tilt_norm: float = 0.0              # (mid+high)/bass, scaled for DMX
    # Chase-rate hint: fades between 32/64/128/255 (1/8 … full BPM) by energy.
    bpm_rate: float = 32.0


class FeatureExtractor:
    def __init__(
        self,
        samplerate: int,
        hop: int,
        detection: DetectionConfig,
        build_cfg: BuildScoreConfig,
        bpm_rate_cfg: BpmRateConfig | None = None,
        win: int = 2048,
    ) -> None:
        self.sr = samplerate
        self.hop = hop
        self.win = win
        self.fps = samplerate / hop
        self.dt = hop / samplerate
        self.detection = detection
        self.build_cfg = build_cfg
        self.bpm_rate_cfg = bpm_rate_cfg or BpmRateConfig()

        self._buffer = np.zeros(win, dtype=np.float64)
        self._window = np.hanning(win)
        self._prev_mag: np.ndarray | None = None

        freqs = np.fft.rfftfreq(win, 1.0 / samplerate)
        self._freqs = freqs
        self._bass_bins = (freqs >= 40) & (freqs <= 120)
        # Synths, pads, leads, chords — where "melodic" focus lives.
        self._mid_bins = (freqs >= 250) & (freqs <= 2000)
        self._treble_bins = (freqs >= 2000) & (freqs <= 8000)
        self._log_centroid_lo = float(np.log(_CENTROID_HZ_LO))
        self._log_centroid_span = float(np.log(_CENTROID_HZ_HI) - self._log_centroid_lo)

        # Slow baselines (EMA). Warm up fast for the first seconds.
        self._alpha_slow = 1.0 / (detection.baseline_seconds * self.fps)
        self._frames_seen = 0
        self._bass_base = 0.0
        self._mid_base = 0.0
        self._treble_base = 0.0
        self._rms_base = 0.0
        self._bass_energy_base = 0.0  # sum-of-bins baseline for tilt
        self._tilt_base = 1.0
        self._melody_smooth = 0.0
        self._centroid_smooth_hz = 500.0
        self._tilt_smooth = 0.5

        # BPM-rate fader (32/64/128/255) with 0.25 s linear crossfade.
        # Thresholds adapt to the last 5 minutes of smoothed energy.
        lo, hi = self._bpm_rate_bounds()
        start = max(lo, min(hi, 1))
        self._bpm_rate_index = start
        self._bpm_rate_current = float(_BPM_RATE_LEVELS[start])
        self._bpm_rate_from = float(_BPM_RATE_LEVELS[start])
        self._bpm_rate_target = float(_BPM_RATE_LEVELS[start])
        self._bpm_rate_fade_t = _BPM_RATE_FADE_SECONDS
        self._bpm_rate_energy = 1.0
        self._bpm_rate_down_timer = 0.0
        hist_len = max(8, int(_BPM_RATE_HISTORY_SECONDS * _BPM_RATE_HISTORY_HZ))
        self._bpm_rate_history: deque[float] = deque(maxlen=hist_len)
        self._bpm_rate_hist_every = max(1, int(round(self.fps / _BPM_RATE_HISTORY_HZ)))

        # Onset detection state.
        flux_window = int(self.fps * 1.0)
        self._flux_hist: deque[float] = deque(maxlen=flux_window)
        self._last_onset_t = -10.0
        self._onset_times: deque[float] = deque(maxlen=64)

        # Beat (kick) detection state.
        self._bass_env = 0.0
        self._bass_peak = 0.0
        self._bass_above = False
        self._last_beat_t = -10.0
        self._beat_pulse_on = False
        self._beat_intervals: deque[float] = deque(maxlen=16)

        # 4/4 quarter-note grid (phase-locked loop steered by kicks).
        self._grid_phase = 0.0          # 0..1 within the current quarter note
        self._grid_bpm = 128.0
        self._grid_locked = False
        self._last_four_four_t = -10.0
        self._beat_in_bar = 0

        # Downsampled histories for trend slopes / lifts.
        trend_len = int(_TREND_WINDOW_SECONDS * _TREND_RATE)
        self._trend_every = max(1, int(round(self.fps / _TREND_RATE)))
        self._rms_trend: deque[float] = deque(maxlen=trend_len)
        self._treble_trend: deque[float] = deque(maxlen=trend_len)
        self._onset_trend: deque[float] = deque(maxlen=trend_len)
        self._bass_trend: deque[float] = deque(maxlen=trend_len)
        self._centroid_trend: deque[float] = deque(maxlen=trend_len)
        self._tilt_trend: deque[float] = deque(maxlen=trend_len)
        self._flux_trend: deque[float] = deque(maxlen=trend_len)
        self._flux_base = 0.0

        self._t = 0.0

    # ------------------------------------------------------------------

    def process(self, block: np.ndarray) -> Features:
        """Consume one hop of mono samples and return the current features."""
        self._t += self.dt
        self._frames_seen += 1

        self._buffer = np.roll(self._buffer, -len(block))
        self._buffer[-len(block):] = block

        mag = np.abs(np.fft.rfft(self._buffer * self._window)) / self.win

        rms = float(np.sqrt(np.mean(block**2)))
        bass = float(np.mean(mag[self._bass_bins]))
        mid = float(np.mean(mag[self._mid_bins]))
        treble = float(np.mean(mag[self._treble_bins]))

        if self._prev_mag is None:
            flux = 0.0
        else:
            flux = float(np.sum(np.clip(mag - self._prev_mag, 0.0, None)))
        self._prev_mag = mag

        # Baselines: fast warmup, then slow adaptation.
        alpha = max(self._alpha_slow, 1.0 / max(self._frames_seen, 1))
        self._bass_base += alpha * (bass - self._bass_base)
        self._rms_base += alpha * (rms - self._rms_base)
        # Mid/treble/tilt baselines lag on the way up so a sustained melodic
        # stretch keeps scoring instead of the baseline "catching" it.
        self._mid_base = _asymmetric_base(self._mid_base, mid, alpha)
        self._treble_base = _asymmetric_base(self._treble_base, treble, alpha)
        # Band *sums* for tilt (means under-weight wide mid/treble bands).
        # Floor the denominator on a slow bass-energy baseline so the ratio
        # doesn't explode between kicks.
        bass_e = float(np.sum(mag[self._bass_bins]))
        mid_e = float(np.sum(mag[self._mid_bins]))
        treble_e = float(np.sum(mag[self._treble_bins]))
        self._bass_energy_base += alpha * (bass_e - self._bass_energy_base)
        tilt = (mid_e + treble_e) / (max(bass_e, self._bass_energy_base) + _EPS)
        self._tilt_base = _asymmetric_base(self._tilt_base, tilt, alpha)

        bass_norm = bass / (self._bass_base + _EPS)
        mid_norm = mid / (self._mid_base + _EPS)
        treble_norm = treble / (self._treble_base + _EPS)
        energy_norm = rms / (self._rms_base + _EPS)

        onset = self._detect_onset(flux)
        if onset:
            self._onset_times.append(self._t)
        onset_density = self._onset_density()

        beat = self._detect_beat(bass)
        if beat:
            self._beat_pulse_on = not self._beat_pulse_on
        bpm = self._estimate_bpm()
        four_four_beat = self._update_four_four_grid(beat, bpm)

        centroid_hz = self._spectral_centroid_hz(mag)
        centroid_norm = self._normalize_centroid(centroid_hz)
        tilt_norm = self._normalize_tilt(tilt)
        build_score = self._update_trends_and_build_score(
            energy_norm,
            treble_norm,
            onset_density,
            flux,
            centroid_norm,
            tilt_norm,
            bass_norm,
        )
        melody_score = self._melody_score(
            bass_norm, mid_norm, treble_norm, tilt, build_score, onset_density
        )
        bpm_rate = self._update_bpm_rate(energy_norm)

        return Features(
            rms=rms,
            bass=bass,
            mid=mid,
            treble=treble,
            flux=flux,
            onset_density=onset_density,
            bass_norm=bass_norm,
            energy_norm=energy_norm,
            build_score=build_score,
            melody_score=melody_score,
            beat=beat,
            beat_pulse=255.0 if self._beat_pulse_on else 0.0,
            bpm=bpm,
            seconds_since_beat=self._t - self._last_beat_t,
            four_four_beat=four_four_beat,
            seconds_since_four_four=self._t - self._last_four_four_t,
            beat_in_bar=self._beat_in_bar,
            spectral_centroid_norm=centroid_norm,
            tilt_norm=tilt_norm,
            bpm_rate=bpm_rate,
        )

    # ------------------------------------------------------------------

    def _detect_onset(self, flux: float) -> bool:
        """Spectral-flux onset: flux spikes above its recent median."""
        hist = self._flux_hist
        onset = False
        if len(hist) >= hist.maxlen // 2:
            threshold = 1.6 * float(np.median(hist)) + _EPS
            refractory_over = (self._t - self._last_onset_t) > 0.1
            if flux > threshold and refractory_over:
                onset = True
                self._last_onset_t = self._t
        hist.append(flux)
        return onset

    def _onset_density(self) -> float:
        window = 4.0
        count = sum(1 for t in self._onset_times if self._t - t <= window)
        return count / window

    def _detect_beat(self, bass: float) -> bool:
        """Kick detection: bass envelope crossing above its baseline.

        The threshold adapts to the actual modulation depth of the bass:
        on a clean mixer feed kicks swing far above the average, but on a
        compressed signal (e.g. a laptop mic with auto-gain) the pulses
        are shallow, so a fixed 1.4x-average threshold would never fire.
        """
        # Short envelope smoothing (~50 ms) to ignore single-frame noise.
        self._bass_env += 0.35 * (bass - self._bass_env)
        # Track recent kick peaks with a ~4 s decay.
        self._bass_peak = max(self._bass_peak * (1.0 - self.dt / 4.0), self._bass_env)
        adaptive = self._bass_base + 0.45 * (self._bass_peak - self._bass_base)
        threshold = min(1.4 * self._bass_base, adaptive) + _EPS
        beat = False
        above = self._bass_env > threshold
        if above and not self._bass_above and (self._t - self._last_beat_t) > 0.28:
            beat = True
            interval = self._t - self._last_beat_t
            if self._last_beat_t > 0 and self._interval_plausible(interval):
                self._beat_intervals.append(interval)
            self._last_beat_t = self._t
        self._bass_above = above
        return beat

    def _interval_plausible(self, interval: float) -> bool:
        """Reject beat intervals from double-hits (e.g. bassline notes
        between kicks) so they don't pollute the tempo estimate."""
        if len(self._beat_intervals) < 4:
            return True
        median = float(np.median(self._beat_intervals))
        return interval > 0.7 * median

    def _estimate_bpm(self) -> float:
        if len(self._beat_intervals) < 4:
            return 0.0
        interval = float(np.median(self._beat_intervals))
        if interval <= 0:
            return 0.0
        bpm = 60.0 / interval
        # Fold octave errors into the club-typical range.
        while bpm < 90 and bpm > 0:
            bpm *= 2
        while bpm > 180:
            bpm /= 2
        return bpm

    def _update_four_four_grid(self, kick: bool, bpm: float) -> bool:
        """Advance a 4/4 quarter-note grid; return True when a beat fires.

        Kicks steer the phase (PLL). Once locked, the grid keeps pulsing on
        every quarter note even if a kick is missed — which is what you want
        for chase sync on four-on-the-floor techno/house.
        """
        if bpm > 0:
            alpha = 0.15 if self._grid_locked else 0.5
            self._grid_bpm += alpha * (bpm - self._grid_bpm)

        period = 60.0 / max(self._grid_bpm, 1.0)
        fired = False

        if kick:
            # Align the grid to this kick and pulse with it.
            if len(self._beat_intervals) >= 4:
                self._grid_locked = True
                fired = True
            self._grid_phase = self.dt / period
        else:
            self._grid_phase += self.dt / period
            if self._grid_phase >= 1.0:
                self._grid_phase -= 1.0
                if self._grid_locked:
                    fired = True  # predicted beat — kick was missed/soft

        # Refractory: don't double-fire when a prediction and the real kick
        # land within ~40% of a beat of each other.
        if fired and (self._t - self._last_four_four_t) < 0.4 * period:
            fired = False
            if kick:
                # Still resync phase to the kick, just don't pulse twice.
                self._grid_phase = self.dt / period

        if fired:
            self._last_four_four_t = self._t
            self._beat_in_bar = (self._beat_in_bar + 1) % 4

        return fired

    def _spectral_centroid_hz(self, mag: np.ndarray) -> float:
        """Brightness of the spectrum in Hz, EMA-smoothed."""
        weights = mag.astype(np.float64)
        total = float(np.sum(weights))
        if total < _EPS:
            hz = self._centroid_smooth_hz
        else:
            hz = float(np.sum(self._freqs * weights) / total)
        alpha = 1.0 - float(np.exp(-self.dt / 0.35))
        self._centroid_smooth_hz += alpha * (hz - self._centroid_smooth_hz)
        return self._centroid_smooth_hz

    def _normalize_centroid(self, hz: float) -> float:
        """Map centroid Hz → 0..1 on a log frequency axis."""
        log_hz = float(np.log(max(hz, _CENTROID_HZ_LO)))
        return _clip01((log_hz - self._log_centroid_lo) / self._log_centroid_span)

    def _normalize_tilt(self, tilt: float) -> float:
        """Map (mid+high)/bass onto 0..1 for a lighting fader.

        ~0.5 is a typical kick-forward groove; higher = brighter / more open.
        """
        alpha = 1.0 - float(np.exp(-self.dt / 0.35))
        self._tilt_smooth += alpha * (tilt - self._tilt_smooth)
        # Absolute (mid+high)/bass on a log scale. Kick-forward grooves sit
        # mid-low; open/melodic stretches push toward 1.0.
        return _clip01(
            (float(np.log1p(self._tilt_smooth)) - float(np.log1p(0.2)))
            / (float(np.log1p(6.0)) - float(np.log1p(0.2)))
        )

    def _bpm_rate_bounds(self) -> tuple[int, int]:
        """Inclusive index range from config (swapped if min/max inverted)."""
        lo = _BPM_RATE_NAME_TO_INDEX.get(
            str(self.bpm_rate_cfg.min).lower(), 0
        )
        hi = _BPM_RATE_NAME_TO_INDEX.get(
            str(self.bpm_rate_cfg.max).lower(), 3
        )
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi

    def _update_bpm_rate(self, energy_norm: float) -> float:
        """Pick full/half/quarter/eighth BPM from recent energy; fade 0.25 s.

        Thresholds are percentiles of the last 5 minutes of smoothed
        loudness. Config clamps the allowed band and optional decay holds
        a higher rate before stepping down.
        """
        # Smooth over ~2 s — ignore between-kick dips.
        a_e = 1.0 - float(np.exp(-self.dt / _BPM_RATE_ENERGY_SMOOTH_SECONDS))
        self._bpm_rate_energy += a_e * (energy_norm - self._bpm_rate_energy)

        if self._frames_seen % self._bpm_rate_hist_every == 0:
            self._bpm_rate_history.append(self._bpm_rate_energy)

        lo, hi = self._bpm_rate_bounds()
        # Keep index inside the live-configured band.
        if self._bpm_rate_index < lo or self._bpm_rate_index > hi:
            self._bpm_rate_index = max(lo, min(hi, self._bpm_rate_index))
            self._bpm_rate_down_timer = 0.0

        desired = self._bpm_rate_index_for_energy(self._bpm_rate_energy)
        desired = max(lo, min(hi, desired))
        idx = self._apply_bpm_rate_decay(desired)

        target = float(_BPM_RATE_LEVELS[idx])
        if idx != self._bpm_rate_index:
            self._bpm_rate_index = idx
            self._bpm_rate_from = self._bpm_rate_current
            self._bpm_rate_target = target
            self._bpm_rate_fade_t = 0.0

        self._bpm_rate_fade_t += self.dt
        if self._bpm_rate_fade_t >= _BPM_RATE_FADE_SECONDS:
            self._bpm_rate_current = self._bpm_rate_target
        else:
            u = self._bpm_rate_fade_t / _BPM_RATE_FADE_SECONDS
            self._bpm_rate_current = (
                self._bpm_rate_from
                + (self._bpm_rate_target - self._bpm_rate_from) * u
            )
        return self._bpm_rate_current

    def _apply_bpm_rate_decay(self, desired: int) -> int:
        """Step up immediately; hold before each step down (decay_seconds)."""
        i = self._bpm_rate_index
        if desired >= i:
            self._bpm_rate_down_timer = 0.0
            return desired
        decay = max(0.0, float(self.bpm_rate_cfg.decay_seconds))
        if decay <= 0.0:
            self._bpm_rate_down_timer = 0.0
            return desired
        self._bpm_rate_down_timer += self.dt
        if self._bpm_rate_down_timer >= decay:
            self._bpm_rate_down_timer = 0.0
            return i - 1  # one step per decay window
        return i

    def _bpm_rate_index_for_energy(self, energy: float) -> int:
        """Map energy → rate index using 5‑minute percentiles + hysteresis.

        0=eighth(32), 1=quarter(64), 2=half(128), 3=full(255)
        """
        hist = self._bpm_rate_history
        min_samples = int(_BPM_RATE_MIN_HISTORY_SECONDS * _BPM_RATE_HISTORY_HZ)
        if len(hist) < max(8, min_samples // 4):
            # Cold start: coarse absolute gates until the window fills.
            if energy >= 1.25:
                return 3
            if energy >= 1.05:
                return 2
            if energy >= 0.90:
                return 1
            return 0

        arr = np.asarray(hist, dtype=np.float64)
        # Quartile edges of the recent night — adaptive to the room/DJ gain.
        q25, q50, q75 = (float(x) for x in np.percentile(arr, (25, 50, 75)))
        # Ensure a tiny spread so a flat history doesn't collapse.
        span = max(q75 - q25, 0.05)
        q25 = min(q25, q50 - 0.15 * span)
        q75 = max(q75, q50 + 0.15 * span)

        i = self._bpm_rate_index
        # Hysteresis: harder to leave a band than to enter it.
        up = (q25, q50, q75)
        down = (
            q25 - 0.08 * span,
            q50 - 0.08 * span,
            q75 - 0.08 * span,
        )
        if i < 3 and energy >= up[i]:
            return i + 1
        if i > 0 and energy < down[i - 1]:
            return i - 1
        return i

    def _melody_score(
        self,
        bass_norm: float,
        mid_norm: float,
        treble_norm: float,
        tilt: float,
        build_score: float,
        onset_density: float,
    ) -> float:
        """Kick still present, but mid/high content is the focus.

        Typical techno/house "melodic" stretches: four-on-the-floor keeps
        going while pads, leads, or chords take the ear. That shows up as
        elevated 250 Hz–8 kHz energy (and a brighter spectrum vs bass)
        without the kick disappearing (that would be a breakdown).
        """
        if self._t < WARMUP_SECONDS:
            self._melody_smooth = 0.0
            return 0.0

        # Gate: require the kick band near/above its average.
        kick_gate = _clip01((bass_norm - 0.55) / 0.35)

        # Prefer sustained mid (pads/leads) over pure treble (hats).
        mid_lift = _clip01((mid_norm - 0.95) / 0.35)
        treble_lift = _clip01((treble_norm - 0.95) / 0.45)

        # Spectrum tilted brighter than usual (more mid+high per unit bass).
        tilt_norm = tilt / (self._tilt_base + _EPS)
        tilt_lift = _clip01((tilt_norm - 1.05) / 0.3)

        tone = 0.55 * mid_lift + 0.15 * treble_lift + 0.30 * tilt_lift
        # Soft-mute hard builds and busy hat storms.
        build_mute = 1.0 - 0.75 * build_score
        perc_mute = _clip01(1.0 - (onset_density - 3.5) / 5.0)
        raw = _clip01(1.85 * kick_gate * tone * build_mute * perc_mute)

        # ~0.8 s EMA — readable as a lighting fader, not a twitch meter.
        alpha = 1.0 - float(np.exp(-self.dt / 0.8))
        self._melody_smooth += alpha * (raw - self._melody_smooth)
        return _clip01(self._melody_smooth)

    def _update_trends_and_build_score(
        self,
        energy_norm: float,
        treble_norm: float,
        onset_density: float,
        flux: float,
        centroid_norm: float,
        tilt_norm: float,
        bass_norm: float,
    ) -> float:
        """Composite build tension from slopes, level lifts, and filter opens.

        Pure energy-slope detection misses many techno builds (slow filter
        opens, rising hats, brightness without a loudness ramp). This mixes
        classic rising slopes with MIR-style novelty cues: spectral
        brightness, level lift vs earlier in the window, high/bass
        divergence, and percussion activity.
        """
        alpha_flux = max(self._alpha_slow, 1.0 / max(self._frames_seen, 1))
        self._flux_base += alpha_flux * (flux - self._flux_base)

        if self._frames_seen % self._trend_every == 0:
            self._rms_trend.append(energy_norm)
            self._treble_trend.append(treble_norm)
            self._onset_trend.append(onset_density)
            self._bass_trend.append(bass_norm)
            self._centroid_trend.append(centroid_norm)
            self._tilt_trend.append(tilt_norm)
            self._flux_trend.append(flux / (self._flux_base + _EPS))

        if self._t < WARMUP_SECONDS:
            return 0.0
        if len(self._rms_trend) < self._rms_trend.maxlen // 2:
            return 0.0

        cfg = self.build_cfg
        energy_slope = _relative_slope(self._rms_trend)
        treble_slope = _relative_slope(self._treble_trend)
        onset_slope = _absolute_slope(self._onset_trend)
        bass_slope = _relative_slope(self._bass_trend)
        bright_slope = max(
            _absolute_slope(self._centroid_trend),
            _absolute_slope(self._tilt_trend),
            _relative_slope(self._treble_trend),
        )

        # Level lift: second half of the window vs first half (slow builds).
        lift = max(
            _half_lift(self._rms_trend),
            _half_lift(self._treble_trend),
            _half_lift(self._centroid_trend),
            _half_lift(self._tilt_trend),
        )

        # Filter-open: highs/brightness rising while kick is flat or falling.
        bright_rise = _clip01(bright_slope / (cfg.full_scale_brightness_slope + _EPS))
        bass_not_rising = _clip01(1.0 - bass_slope / (cfg.full_scale_energy_slope + _EPS))
        divergence = bright_rise * bass_not_rising

        # Activity: busy hats / spectral flux above baseline.
        onset_level = float(np.mean(self._onset_trend))
        flux_level = float(np.mean(self._flux_trend)) if self._flux_trend else 0.0
        activity = _clip01(
            max(onset_level - 2.0, 0.0) / (cfg.full_scale_activity + _EPS)
            + _clip01((flux_level - 1.15) / 0.6) * 0.5
        )

        parts = (
            (cfg.weight_energy, _clip01(energy_slope / cfg.full_scale_energy_slope)),
            (cfg.weight_treble, _clip01(treble_slope / cfg.full_scale_treble_slope)),
            (cfg.weight_onsets, _clip01(onset_slope / cfg.full_scale_onset_slope)),
            (cfg.weight_brightness, bright_rise),
            (cfg.weight_lift, _clip01(lift / (cfg.full_scale_lift + _EPS))),
            (cfg.weight_divergence, divergence),
            (cfg.weight_activity, activity),
        )
        total_w = sum(w for w, _ in parts) or 1.0
        score = sum(w * c for w, c in parts) / total_w
        return _clip01(score)


def _absolute_slope(hist: deque[float]) -> float:
    """Least-squares slope of a history, in units per second."""
    y = np.asarray(hist, dtype=np.float64)
    x = np.arange(len(y)) / _TREND_RATE
    slope, _ = np.polyfit(x, y, 1)
    return float(slope)


def _relative_slope(hist: deque[float]) -> float:
    """Slope as a fraction of the mean level per second (scale-invariant)."""
    y = np.asarray(hist, dtype=np.float64)
    mean = float(np.mean(y))
    return _absolute_slope(hist) / (mean + _EPS)


def _half_lift(hist: deque[float]) -> float:
    """Relative rise of the recent half vs the earlier half of a history."""
    y = np.asarray(hist, dtype=np.float64)
    if len(y) < 4:
        return 0.0
    mid = len(y) // 2
    older = float(np.mean(y[:mid]))
    newer = float(np.mean(y[mid:]))
    return max(0.0, (newer - older) / (older + _EPS))


def _clip01(x: float) -> float:
    return float(np.clip(x, 0.0, 1.0))


def _asymmetric_base(base: float, value: float, alpha: float) -> float:
    """EMA baseline that follows drops faster than rises."""
    rate = alpha * (0.25 if value > base else 1.5)
    return base + rate * (value - base)
