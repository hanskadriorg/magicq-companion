"""Per-block audio feature extraction.

Everything works on small hops (default 512 samples @ 44.1 kHz, ~12 ms)
so the pipeline stays real-time. Features are normalized against slow
rolling baselines so the system adapts when the DJ changes gain or EQ.

Absolute level matters too: a signal below the configured noise floor is
treated as no music at all rather than being normalized up into looking
like a track.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

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

# Level headroom above the noise floor that counts as "full" programme
# level for the music gate.
_LEVEL_SPAN_DB = 24.0
# Samples at or above this are treated as clipped (0 dBFS with rounding).
_CLIP_THRESHOLD = 0.994

# Kick detection: minimum bass-band modulation depth, and the shortest
# interval between two kicks (0.26 s ≈ 230 BPM).
_KICK_MIN_DEPTH = 0.22
_KICK_REFRACTORY = 0.26
# Bass-envelope ceiling decay. Short enough that the modulation range
# collapses within a beat or two of the kick dropping out.
_KICK_PEAK_DECAY = 1.5
# Beat-to-beat intervals longer than this are never a quarter note.
_KICK_MAX_INTERVAL = 2.6

# BPM-rate DMX steps: eighth, quarter, half, full tempo.
_BPM_RATE_LEVELS = tuple(BPM_RATE_NAME_TO_LEVEL[n] for n in BPM_RATE_NAMES)
_BPM_RATE_NAME_TO_INDEX = {n: i for i, n in enumerate(BPM_RATE_NAMES)}
# Snap between divider steps — MagicQ should see 32/64/128/255 only.
_BPM_RATE_FADE_SECONDS = 0.0
# Adaptive thresholds from a rolling energy window (seconds / sample rate).
# 90 s is long enough to ignore a breakdown, short enough that a new
# loud section can climb back to 1/1 without waiting out a 5-minute average.
_BPM_RATE_HISTORY_SECONDS = 90.0
_BPM_RATE_HISTORY_HZ = 2.0
# Need this much history before trusting percentiles (cold-start).
_BPM_RATE_MIN_HISTORY_SECONDS = 12.0
# Smooth loudness over half a second so kick gaps don't yank the rate,
# but a real energy jump still lands within a beat.
_BPM_RATE_ENERGY_SMOOTH_SECONDS = 0.55

# The dashboard only redraws ~20 times a second, so the spectrum and the
# spectral-flatness term are recomputed at a fraction of the hop rate.
_SPECTRUM_HZ = 25.0
_FLATNESS_HZ = 20.0

_SPECTRUM_LO_HZ = 40.0
_SPECTRUM_HI_HZ = 8000.0
_SPECTRUM_BANDS = 48


def to_db(value: float) -> float:
    """Amplitude (0..1 full scale) to dBFS, floored at -99 dB."""
    return float(20.0 * np.log10(max(float(value), 1.1e-5)))


@dataclass
class Features:
    """One frame (hop) of analysis results."""

    rms: float = 0.0
    peak: float = 0.0
    # Absolute input level, for the dashboard input meter and the music gate.
    rms_db: float = -99.0
    peak_db: float = -99.0
    # 0..1: how far the input sits above the configured noise floor.
    level: float = 0.0
    above_floor: bool = False
    clipping: bool = False
    bass: float = 0.0
    mid: float = 0.0
    treble: float = 0.0
    flux: float = 0.0
    onset_density: float = 0.0  # onsets per second, recent window
    bass_norm: float = 0.0      # bass relative to rolling baseline (1.0 = avg)
    energy_norm: float = 0.0    # loudness relative to rolling baseline
    # 0..1: how much the kick envelope pulses within the bass band.
    kick_depth: float = 0.0
    # 0..1: how even recent kick intervals are (1 = solid 4/4).
    kick_regularity: float = 0.0
    # 0..1: structured music vs hiss/silence (absolute level + tonality + pulse).
    music_score: float = 0.0
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
    # Hard 0/255: 255 while a 4/4 kick grid is locked, 0 when it isn't.
    four_four_gate: float = 0.0
    four_four_locked: bool = False
    # 0..3 — which quarter of the bar the grid thinks we're on.
    beat_in_bar: int = 0
    # Chase-rate hint: fades between 32/64/128/255 (1/8 … full BPM) by energy.
    bpm_rate: float = 32.0
    # Log-frequency band levels in dBFS, 40 Hz–8 kHz (for the dashboard).
    # Absolute, so the visualiser can show real loudness instead of an
    # autoranged shape.
    spectrum_db: tuple[int, ...] = field(default_factory=tuple)


class FeatureExtractor:
    def __init__(
        self,
        samplerate: int,
        hop: int,
        detection: DetectionConfig,
        build_cfg: BuildScoreConfig,
        bpm_rate_cfg: BpmRateConfig | None = None,
        win: int = 2048,
        noise_floor_db: float = -55.0,
    ) -> None:
        self.sr = samplerate
        self.hop = hop
        self.win = win
        self.fps = samplerate / hop
        self.dt = hop / samplerate
        self.detection = detection
        self.build_cfg = build_cfg
        self.bpm_rate_cfg = bpm_rate_cfg or BpmRateConfig()
        self.noise_floor_db = float(noise_floor_db)

        self._buffer = np.zeros(win, dtype=np.float64)
        self._window = np.hanning(win)
        self._prev_mag: np.ndarray | None = None

        freqs = np.fft.rfftfreq(win, 1.0 / samplerate)
        self._freqs = freqs
        self._bass_bins = (freqs >= 40) & (freqs <= 120)
        # Synths, pads, leads, chords — where "melodic" focus lives.
        self._mid_bins = (freqs >= 250) & (freqs <= 2000)
        self._treble_bins = (freqs >= 2000) & (freqs <= 8000)

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
        self._tilt_smooth = 0.5
        self._kick_depth_smooth = 0.0
        self._music_smooth = 0.0
        self._flatness = 1.0
        self._flatness_every = max(1, int(round(self.fps / _FLATNESS_HZ)))

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

        # Beat (kick) detection: envelope plus the ceiling/floor pair that
        # describes how deeply the bass band is actually modulated.
        self._bass_env = 0.0
        self._bass_peak = 0.0
        self._bass_trough = 0.0
        self._bass_pulsing = False
        self._bass_above = False
        self._last_beat_t = -10.0
        self._beat_pulse_on = False
        self._beat_intervals: deque[float] = deque(maxlen=10)
        self._tempo_candidates: deque[float] = deque(maxlen=3)

        # 4/4 quarter-note grid (phase-locked loop steered by kicks).
        self._grid_phase = 0.0          # 0..1 within the current quarter note
        self._grid_bpm = 128.0
        self._grid_locked = False
        self._last_four_four_t = -10.0
        self._beat_in_bar = 0
        self._four_four_lock_t = -1.0
        self._four_four_unlock_t = -1.0
        self._four_four_out = False

        # Downsampled histories for trend slopes / lifts.
        trend_len = int(_TREND_WINDOW_SECONDS * _TREND_RATE)
        self._trend_every = max(1, int(round(self.fps / _TREND_RATE)))
        self._rms_trend: deque[float] = deque(maxlen=trend_len)
        self._treble_trend: deque[float] = deque(maxlen=trend_len)
        self._onset_trend: deque[float] = deque(maxlen=trend_len)
        self._bass_trend: deque[float] = deque(maxlen=trend_len)
        self._flux_trend: deque[float] = deque(maxlen=trend_len)
        self._flux_base = 0.0
        self._build_score = 0.0

        self._t = 0.0
        self._spec_bins: np.ndarray | None = None
        self._spec_mask: np.ndarray | None = None
        self._spec_onesided: np.ndarray | None = None
        self._spec_cache: tuple[int, ...] = ()
        self._spec_every = max(1, int(round(self.fps / _SPECTRUM_HZ)))
        # Hann RMS so band energy matches the unwindowed input-meter dBFS.
        self._win_rms = float(np.sqrt(np.mean(self._window**2))) or 1.0
        self.visualizer = True

    # ------------------------------------------------------------------

    def process(self, block: np.ndarray) -> Features:
        """Consume one hop of mono samples and return the current features."""
        self._t += self.dt
        self._frames_seen += 1

        # Some PipeWire/USB devices emit a non-finite startup block while
        # their graph is being linked. Treat those samples as silence so one
        # bad block cannot poison the rolling FFT state and kill the analyzer.
        if not np.isfinite(block).all():
            block = np.nan_to_num(block, nan=0.0, posinf=0.0, neginf=0.0)

        self._buffer = np.roll(self._buffer, -len(block))
        self._buffer[-len(block):] = block

        mag = np.abs(np.fft.rfft(self._buffer * self._window)) / self.win

        rms = float(np.sqrt(np.mean(block**2)))
        peak = float(np.max(np.abs(block))) if len(block) else 0.0
        rms_db = to_db(rms)
        peak_db = to_db(peak)
        floor_db = self.noise_floor_db
        above_floor = rms_db > floor_db
        level = _clip01((rms_db - floor_db) / _LEVEL_SPAN_DB)

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
        # Hold the bass average up when the kick drops out so bass_norm
        # (and kick-absent) stay visible instead of the baseline chasing down.
        self._bass_base = _asymmetric_base(
            self._bass_base, bass, alpha, hold_high=True
        )
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

        beat = self._detect_beat(bass, above_floor)
        if beat:
            self._beat_pulse_on = not self._beat_pulse_on
        bpm = self._estimate_bpm()
        kick_depth = self._update_kick_depth(above_floor)
        four_four_beat = self._update_four_four_grid(
            beat, bpm, above_floor, kick_depth
        )

        build_score = self._update_trends_and_build_score(
            energy_norm, treble_norm, onset_density, flux, bass_norm
        )
        melody_score = self._melody_score(
            bass_norm, mid_norm, treble_norm, tilt, build_score, onset_density
        )
        music_score = self._update_music_score(level, mag, kick_depth)
        bpm_rate = self._update_bpm_rate(energy_norm)
        spectrum_db = self._spectrum_db(mag) if self.visualizer else ()

        return Features(
            rms=rms,
            peak=peak,
            rms_db=rms_db,
            peak_db=peak_db,
            level=level,
            above_floor=above_floor,
            clipping=peak >= _CLIP_THRESHOLD,
            bass=bass,
            mid=mid,
            treble=treble,
            flux=flux,
            onset_density=onset_density,
            bass_norm=bass_norm,
            energy_norm=energy_norm,
            kick_depth=kick_depth,
            kick_regularity=self._kick_regularity(),
            music_score=music_score,
            build_score=build_score,
            melody_score=melody_score,
            beat=beat,
            beat_pulse=255.0 if self._beat_pulse_on else 0.0,
            bpm=bpm,
            seconds_since_beat=self._t - self._last_beat_t,
            four_four_beat=four_four_beat,
            seconds_since_four_four=self._t - self._last_four_four_t,
            four_four_gate=self._four_four_gate(above_floor),
            four_four_locked=self._grid_locked,
            beat_in_bar=self._beat_in_bar,
            bpm_rate=bpm_rate,
            spectrum_db=spectrum_db,
        )

    def _spectrum_db(self, mag: np.ndarray) -> tuple[int, ...]:
        """Per-band RMS in dBFS, same scale as the input meter.

        ``mag`` is |rFFT| / N. Summing one-sided power (Parseval) gives the
        RMS that band contributes; averaging bin magnitudes under-reads
        music by tens of dB and made the visualizer look empty.
        """
        if self._frames_seen % self._spec_every and self._spec_cache:
            return self._spec_cache
        if self._spec_bins is None:
            edges = np.geomspace(_SPECTRUM_LO_HZ, _SPECTRUM_HI_HZ, _SPECTRUM_BANDS + 1)
            idx = np.digitize(self._freqs, edges) - 1
            mask = (idx >= 0) & (idx < _SPECTRUM_BANDS)
            self._spec_mask = mask
            self._spec_bins = idx[mask]
            onesided = np.full(mag.shape[0], 2.0)
            onesided[0] = 1.0
            if mag.shape[0] > 1:
                onesided[-1] = 1.0
            self._spec_onesided = onesided[mask]
        power = np.bincount(
            self._spec_bins,
            weights=(mag[self._spec_mask] ** 2) * self._spec_onesided,
            minlength=_SPECTRUM_BANDS,
        )
        band_rms = np.sqrt(np.maximum(power, 0.0)) / self._win_rms
        db = 20.0 * np.log10(np.maximum(band_rms, 1.1e-5))
        # np.clip does not remove NaN; one non-finite bin used to kill the
        # analyzer thread with ValueError in int(round(...)).
        db = np.nan_to_num(db, nan=-99.0, posinf=6.0, neginf=-99.0)
        self._spec_cache = tuple(int(round(x)) for x in np.clip(db, -99.0, 6.0))
        return self._spec_cache

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

    def _detect_beat(self, bass: float, above_floor: bool) -> bool:
        """Kick detection from the depth of the bass-band modulation.

        A floor/ceiling pair brackets the bass envelope over the last few
        seconds. A kick is the envelope crossing 55% of that range, released
        again below 30% (Schmitt trigger). Because the trigger points are
        derived from the range rather than from the average, a shallow but
        real pulse on a compressed feed still fires, while a flat band —
        hiss, a sub drone, or digital silence — produces nothing at all.
        """
        self._bass_env += 0.35 * (bass - self._bass_env)
        env = self._bass_env
        if not above_floor:
            self._bass_peak = env
            self._bass_trough = env
            self._bass_pulsing = False
            self._bass_above = False
            return False

        self._bass_peak = max(self._bass_peak * (1.0 - self.dt / _KICK_PEAK_DECAY), env)
        rise = max(self.dt / 6.0, 1.0 / max(self._frames_seen, 1))
        if env < self._bass_trough:
            self._bass_trough = env
        else:
            self._bass_trough += rise * (env - self._bass_trough)

        span = self._bass_peak - self._bass_trough
        self._bass_pulsing = span > _KICK_MIN_DEPTH * (self._bass_peak + _EPS)
        high = self._bass_trough + 0.55 * span
        low = self._bass_trough + 0.30 * span

        beat = False
        if self._bass_above:
            if env < low:
                self._bass_above = False
        elif env > high:
            self._bass_above = True
            if self._bass_pulsing and (self._t - self._last_beat_t) > _KICK_REFRACTORY:
                beat = True
                if self._last_beat_t > 0:
                    self._register_interval(self._t - self._last_beat_t)
                self._last_beat_t = self._t
        return beat

    def _register_interval(self, interval: float) -> None:
        """Fold one kick interval into the tempo history.

        Missed kicks arrive as near-multiples of the beat and are divided
        back down; extra hits between kicks are discarded. An interval that
        fits neither, but which repeats consistently, is a real tempo change
        and replaces the history outright — that is what lets the BPM climb
        again instead of only ever drifting down.
        """
        if interval <= 0.0 or interval > _KICK_MAX_INTERVAL:
            return
        hist = self._beat_intervals
        if len(hist) < 4:
            hist.append(interval)
            self._tempo_candidates.clear()
            return

        reference = float(np.median(hist))
        for divisor in (1, 2, 3, 4):
            folded = interval / divisor
            if abs(folded - reference) <= 0.13 * reference:
                hist.append(folded)
                self._tempo_candidates.clear()
                return

        if interval < 0.62 * reference:
            return  # a bassline note or ghost hit between kicks

        candidates = self._tempo_candidates
        if candidates and abs(interval - candidates[-1]) > 0.10 * candidates[-1]:
            candidates.clear()
        candidates.append(interval)
        if len(candidates) >= candidates.maxlen:
            hist.clear()
            hist.extend(candidates)
            candidates.clear()

    def _estimate_bpm(self) -> float:
        if len(self._beat_intervals) < 2:
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

    def _update_four_four_grid(
        self, kick: bool, bpm: float, above_floor: bool, kick_depth: float
    ) -> bool:
        """Advance a 4/4 quarter-note grid; return True when a beat fires.

        Kicks steer the phase (PLL). Channel 11 is a lock flag (255 while a
        4/4 formation is held), not a metronome pulse.
        """
        need, miss_beats, reg_need, depth_need = self._four_four_params()
        if bpm > 0:
            # Snap on a real tempo change, glide on small corrections.
            if abs(bpm - self._grid_bpm) > 0.08 * self._grid_bpm:
                self._grid_bpm = bpm
            else:
                alpha = 0.25 if self._grid_locked else 0.5
                self._grid_bpm += alpha * (bpm - self._grid_bpm)

        period = 60.0 / max(self._grid_bpm, 1.0)
        kick_alive = (self._t - self._last_beat_t) <= miss_beats * period
        formed = (
            above_floor
            and bpm > 0
            and len(self._beat_intervals) >= need
            and self._kick_regularity() >= reg_need
            and kick_alive
            and kick_depth >= depth_need
        )
        if self._grid_locked and not kick_alive:
            self._beat_intervals.clear()
        self._grid_locked = formed
        fired = False

        if kick:
            if formed:
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

    def _four_four_params(self) -> tuple[int, float, float, float]:
        """Kicks needed, miss-window, regularity floor, kick-depth floor."""
        inten = _clip01(float(getattr(self.detection, "four_four_intensity", 0.55)))
        need = max(3, min(8, int(round(7.0 - 4.0 * inten))))
        miss_beats = 1.0 + 2.0 * inten
        reg_need = 0.28 + 0.42 * (1.0 - inten)
        depth_need = 0.10 + 0.16 * (1.0 - inten)
        return need, miss_beats, reg_need, depth_need

    def _four_four_gate(self, above_floor: bool) -> float:
        """255 while a 4/4 formation is held, after the smoothing delay."""
        raw = self._grid_locked
        try:
            smooth = max(0.0, float(self.detection.four_four_smooth_seconds))
        except (TypeError, ValueError, AttributeError):
            smooth = 0.4
        if not raw:
            if self._four_four_unlock_t < 0:
                self._four_four_unlock_t = self._t
            self._four_four_lock_t = -1.0
            # Drop immediately in silence; otherwise wait out the smoother.
            if (
                not above_floor
                or smooth <= 0
                or (self._t - self._four_four_unlock_t) >= smooth
            ):
                self._four_four_out = False
            return 255.0 if self._four_four_out else 0.0
        if self._four_four_lock_t < 0:
            self._four_four_lock_t = self._t
        self._four_four_unlock_t = -1.0
        if (self._t - self._four_four_lock_t) >= smooth:
            self._four_four_out = True
        return 255.0 if self._four_four_out else 0.0

    def _kick_regularity(self) -> float:
        if len(self._beat_intervals) < 2:
            return 0.0
        iv = np.asarray(self._beat_intervals, dtype=np.float64)
        med = float(np.median(iv))
        if med <= 0:
            return 0.0
        return _clip01(1.0 - float(np.std(iv) / med) / 0.35)

    def _update_kick_depth(self, above_floor: bool) -> float:
        """0 = no kick right now; ~1 = kick pulsing hard in the bass band.

        The modulation range alone is not enough: right after the kick stops
        the ceiling is still high while the floor has already collapsed, which
        reads as maximum depth exactly when a breakdown starts. So the range
        is gated on a kick having actually landed within the last two beats.
        """
        if not above_floor:
            raw = 0.0
        else:
            span = max(self._bass_peak - self._bass_trough, 0.0)
            depth = _clip01(span / (self._bass_peak + _EPS) / 0.65)
            period = 60.0 / max(self._grid_bpm, 90.0)
            since = self._t - self._last_beat_t
            recent = _clip01((2.0 * period - since) / period)
            raw = depth * recent
        alpha = 1.0 - float(np.exp(-self.dt / 0.35))
        self._kick_depth_smooth += alpha * (raw - self._kick_depth_smooth)
        return _clip01(self._kick_depth_smooth)

    def _update_music_score(
        self, level: float, mag: np.ndarray, kick_depth: float
    ) -> float:
        """Structured music vs hiss / digital silence.

        Level is measured against the configured noise floor, so a quiet or
        disconnected input can never look like a track no matter how long
        the baselines have had to adapt.
        """
        if self._frames_seen % self._flatness_every == 0:
            pos = mag[mag > 1e-12]
            if pos.size:
                self._flatness = float(
                    np.exp(np.mean(np.log(pos))) / (np.mean(pos) + _EPS)
                )
            else:
                self._flatness = 1.0
        flat = self._flatness

        flux_arr = np.asarray(self._flux_hist, dtype=np.float64)
        if flux_arr.size >= 8:
            flux_mod = float(np.std(flux_arr) / (np.mean(flux_arr) + _EPS))
        else:
            flux_mod = 0.0

        regular = self._kick_regularity()
        tonal = _clip01((0.72 - flat) / 0.55)
        motion = _clip01(flux_mod / 0.85)
        raw = level * (
            0.34 * tonal + 0.28 * motion + 0.22 * regular + 0.16 * kick_depth
        )
        if kick_depth > 0.32 and level > 0.30:
            raw = max(raw, 0.62)
        if level <= 0.0:
            raw = 0.0
        alpha = 1.0 - float(np.exp(-self.dt / 0.8))
        self._music_smooth += alpha * (raw - self._music_smooth)
        return _clip01(self._music_smooth)

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
        """Pick full/half/quarter/eighth BPM from recent energy.

        Thresholds mix absolute energy_norm (1.0 = recent average) with
        percentiles of the last 90 s. Jumps up immediately; decay_seconds
        holds before each step down. DMX values snap with no fade.
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
        if _BPM_RATE_FADE_SECONDS <= 0.0 or self._bpm_rate_fade_t >= _BPM_RATE_FADE_SECONDS:
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
        """Map energy → rate index. Jump up as far as energy justifies.

        0=eighth(32), 1=quarter(64), 2=half(128), 3=full(255)
        """
        if energy >= 1.28:
            abs_idx = 3
        elif energy >= 1.10:
            abs_idx = 2
        elif energy >= 0.88:
            abs_idx = 1
        else:
            abs_idx = 0

        hist = self._bpm_rate_history
        min_samples = int(_BPM_RATE_MIN_HISTORY_SECONDS * _BPM_RATE_HISTORY_HZ)
        if len(hist) < max(8, min_samples // 4):
            return abs_idx

        arr = np.asarray(hist, dtype=np.float64)
        q25, q50, q75 = (float(x) for x in np.percentile(arr, (25, 50, 75)))
        span = max(q75 - q25, 0.05)
        q25 = min(q25, q50 - 0.15 * span)
        q75 = max(q75, q50 + 0.15 * span)

        if energy >= q75:
            perc_idx = 3
        elif energy >= q50:
            perc_idx = 2
        elif energy >= q25:
            perc_idx = 1
        else:
            perc_idx = 0

        i = self._bpm_rate_index
        up_idx = max(abs_idx, perc_idx)
        down_idx = min(abs_idx, perc_idx)
        if up_idx > i:
            return up_idx
        if down_idx < i:
            return down_idx
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
        bass_norm: float,
    ) -> float:
        """Composite build tension from slopes, level lifts, and filter opens.

        The score is only recomputed when the (10 Hz) trend histories grow,
        which is roughly every eighth hop — fitting seven least-squares
        slopes at the full hop rate was the single most expensive thing the
        analyser did.
        """
        alpha_flux = max(self._alpha_slow, 1.0 / max(self._frames_seen, 1))
        self._flux_base += alpha_flux * (flux - self._flux_base)

        if self._frames_seen % self._trend_every:
            return self._build_score

        self._rms_trend.append(energy_norm)
        self._treble_trend.append(treble_norm)
        self._onset_trend.append(onset_density)
        self._bass_trend.append(bass_norm)
        self._flux_trend.append(flux / (self._flux_base + _EPS))

        if self._t < WARMUP_SECONDS:
            self._build_score = 0.0
        elif len(self._rms_trend) < self._rms_trend.maxlen // 2:
            self._build_score = 0.0
        else:
            self._build_score = self._compute_build_score()
        return self._build_score

    def _compute_build_score(self) -> float:
        cfg = self.build_cfg
        energy_slope = _relative_slope(self._rms_trend)
        treble_slope = _relative_slope(self._treble_trend)
        onset_slope = _absolute_slope(self._onset_trend)
        bass_slope = _relative_slope(self._bass_trend)

        # Level lift: second half of the window vs first half (slow builds).
        lift = max(_half_lift(self._rms_trend), _half_lift(self._treble_trend))

        # Filter-open: highs rising while the kick is flat or falling.
        treble_rise = _clip01(treble_slope / (cfg.full_scale_treble_slope + _EPS))
        bass_not_rising = _clip01(
            1.0 - bass_slope / (cfg.full_scale_energy_slope + _EPS)
        )
        divergence = treble_rise * bass_not_rising

        # Activity: busy hats / spectral flux above baseline.
        onset_level = float(np.mean(self._onset_trend))
        flux_level = float(np.mean(self._flux_trend)) if self._flux_trend else 0.0
        activity = _clip01(
            max(onset_level - 2.0, 0.0) / (cfg.full_scale_activity + _EPS)
            + _clip01((flux_level - 1.15) / 0.6) * 0.5
        )

        parts = (
            (cfg.weight_energy, _clip01(energy_slope / cfg.full_scale_energy_slope)),
            (cfg.weight_treble, treble_rise),
            (cfg.weight_onsets, _clip01(onset_slope / cfg.full_scale_onset_slope)),
            (cfg.weight_lift, _clip01(lift / (cfg.full_scale_lift + _EPS))),
            (cfg.weight_divergence, divergence),
            (cfg.weight_activity, activity),
        )
        total_w = sum(w for w, _ in parts) or 1.0
        return _clip01(sum(w * c for w, c in parts) / total_w)


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


def _asymmetric_base(
    base: float, value: float, alpha: float, *, hold_high: bool = False
) -> float:
    """Skewed EMA. Default: follow drops faster than rises (mid/treble).

    hold_high: follow rises faster and drops slowly (kick baseline).
    """
    if hold_high:
        rate = alpha * (1.25 if value > base else 0.22)
    else:
        rate = alpha * (0.25 if value > base else 1.5)
    return base + rate * (value - base)
