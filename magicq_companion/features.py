"""Per-block audio feature extraction.

Everything works on small hops (default 512 samples @ 44.1 kHz, ~12 ms)
so the pipeline stays real-time. Features combine rolling-baseline relative
cues (for gain-invariant section transitions) with compression-invariant
intrinsic pace cues (transient density, hi-hat modulation, kick punch,
genre-guided true BPM, and genre arousal) so slow tracks never trigger
frantic chases and compressed fast club tracks never lose energy.
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
    FxSizeConfig,
)
from .genre_agent import GenreControlVector
from .genre_profiles import GenrePolicy

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

# True musical BPM bounds across downtempo/R&B through Drum & Bass.
_BPM_MIN = 60.0
_BPM_MAX = 190.0
_AC_RATE_HZ = 20.0
_AC_WINDOW_SECONDS = 6.0

# BPM-rate DMX steps: eighth, quarter, half, full tempo.
_BPM_RATE_LEVELS = tuple(BPM_RATE_NAME_TO_LEVEL[n] for n in BPM_RATE_NAMES)
_BPM_RATE_NAME_TO_INDEX = {n: i for i, n in enumerate(BPM_RATE_NAMES)}
# Snap between divider steps — MagicQ should see 32/64/128/255 only.
_BPM_RATE_FADE_SECONDS = 0.0
_PACE_SMOOTH_SECONDS = 0.75

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
    # 0..1: compression-invariant intrinsic musical pace / drive.
    pace_score: float = 0.0
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
    # Chase-rate DMX value: snaps between 32/64/128/255 (1/8 … full BPM).
    bpm_rate: float = 32.0
    # Dynamic FX size DMX value (0..255): genre base + fast bump on energy rises/drops.
    fx_size: float = 0.0
    # Live Genre Agent telemetry attached for dashboard & logging.
    archetype: str = "house_tech"
    archetype_label: str = "House / Tech House"
    genre_tags_text: str = ""
    genre_backend: str = "dsp"
    # Log-frequency band levels in dBFS, 40 Hz–8 kHz (for the dashboard).
    spectrum_db: tuple[int, ...] = field(default_factory=tuple)


class FeatureExtractor:
    def __init__(
        self,
        samplerate: int,
        hop: int,
        detection: DetectionConfig,
        build_cfg: BuildScoreConfig,
        bpm_rate_cfg: BpmRateConfig | None = None,
        fx_size_cfg: FxSizeConfig | None = None,
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
        self.fx_size_cfg = fx_size_cfg or FxSizeConfig()
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
        self._bass_energy_base = 0.0
        self._tilt_base = 1.0
        self._melody_smooth = 0.0
        self._tilt_smooth = 0.5
        self._kick_depth_smooth = 0.0
        self._music_smooth = 0.0
        self._flatness = 1.0
        self._flatness_every = max(1, int(round(self.fps / _FLATNESS_HZ)))

        # Hi-hat / high-band pulse detector (counts fast 8th/16th subdivisions).
        self._treble_env = 0.0
        self._prev_treble_env = 0.0
        self._hat_diff_hist: deque[float] = deque(maxlen=max(16, int(round(self.fps * 1.2))))
        self._last_hat_t = -10.0
        self._hat_times: deque[float] = deque(maxlen=96)
        self._pace_smooth = 0.35
        self._fx_size_current = 0.0
        self._size_energy_fast = 1.0
        self._size_flux_fast = 1.0

        # BPM-rate state (32/64/128/255).
        lo, hi = self._bpm_rate_bounds()
        start = max(lo, min(hi, 1))
        self._bpm_rate_index = start
        self._bpm_rate_current = float(_BPM_RATE_LEVELS[start])
        self._bpm_rate_from = float(_BPM_RATE_LEVELS[start])
        self._bpm_rate_target = float(_BPM_RATE_LEVELS[start])
        self._bpm_rate_fade_t = _BPM_RATE_FADE_SECONDS
        self._bpm_rate_down_timer = 0.0

        # Onset detection state + 20 Hz flux envelope for tempo octave disambiguation.
        flux_window = int(self.fps * 1.0)
        self._flux_hist: deque[float] = deque(maxlen=flux_window)
        self._last_onset_t = -10.0
        self._onset_times: deque[float] = deque(maxlen=64)
        self._ac_every = max(1, int(round(self.fps / _AC_RATE_HZ)))
        self._flux_ac_accum = 0.0
        self._flux_ac_hist: deque[float] = deque(
            maxlen=int(_AC_WINDOW_SECONDS * _AC_RATE_HZ)
        )
        self._resolved_bpm = 0.0

        # Beat (kick) detection state.
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
        self._grid_phase = 0.0
        self._grid_bpm = 124.0
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
        self._win_rms = float(np.sqrt(np.mean(self._window**2))) or 1.0
        self.visualizer = True
        self._last_genre: GenreControlVector | None = None

    # ------------------------------------------------------------------

    def process(
        self,
        block: np.ndarray,
        *,
        genre: GenreControlVector | None = None,
        policy: GenrePolicy | None = None,
        section: str = "groove",
        build_progress: float = 0.0,
    ) -> Features:
        """Consume one hop of mono samples and return the current features."""
        self._t += self.dt
        self._frames_seen += 1
        self._last_genre = genre

        # Treat non-finite startup blocks from PipeWire/USB devices as silence
        # so a single bad frame never poisons rolling FFT/EMA state.
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

        self._prev_treble_env = self._treble_env
        self._treble_env += 0.55 * (treble - self._treble_env)
        hat_rise = max(0.0, self._treble_env - self._prev_treble_env)
        if above_floor:
            if len(self._hat_diff_hist) >= 16:
                h_mean = float(np.mean(self._hat_diff_hist))
                h_peak = float(np.max(self._hat_diff_hist))
                h_thr = max(h_mean * 2.2, h_peak * 0.28, 1e-5)
                if hat_rise > h_thr and (self._t - self._last_hat_t) >= 0.055:
                    self._hat_times.append(self._t)
                    self._last_hat_t = self._t
            self._hat_diff_hist.append(hat_rise)
        else:
            self._hat_diff_hist.clear()
            self._hat_times.clear()

        if self._prev_mag is None:
            flux = 0.0
        else:
            flux = float(np.sum(np.clip(mag - self._prev_mag, 0.0, None)))
        self._prev_mag = mag

        self._flux_ac_accum += flux
        if self._frames_seen % self._ac_every == 0:
            self._flux_ac_hist.append(self._flux_ac_accum / self._ac_every)
            self._flux_ac_accum = 0.0

        # Baselines: fast warmup, then slow adaptation.
        alpha = max(self._alpha_slow, 1.0 / max(self._frames_seen, 1))
        self._bass_base = _asymmetric_base(
            self._bass_base, bass, alpha, hold_high=True
        )
        self._rms_base += alpha * (rms - self._rms_base)
        self._mid_base = _asymmetric_base(self._mid_base, mid, alpha)
        self._treble_base = _asymmetric_base(self._treble_base, treble, alpha)
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
        bpm = self._estimate_bpm(genre)
        kick_depth = self._update_kick_depth(above_floor)
        kick_reg = self._kick_regularity()
        four_four_beat = self._update_four_four_grid(
            beat, bpm, above_floor, kick_depth, policy=policy
        )

        build_score = self._update_trends_and_build_score(
            energy_norm, treble_norm, onset_density, flux, bass_norm
        )
        melody_score = self._melody_score(
            bass_norm, mid_norm, treble_norm, tilt, build_score, onset_density
        )
        music_score = self._update_music_score(level, mag, kick_depth)
        pace_score = self._update_pace_score(
            above_floor=above_floor,
            energy_norm=energy_norm,
            onset_density=onset_density,
            kick_depth=kick_depth,
            kick_regularity=kick_reg,
            bpm=bpm,
            bass_e=bass_e,
            mid_e=mid_e,
            treble_e=treble_e,
            genre=genre,
        )
        bpm_rate = self._update_bpm_rate(
            pace_score=pace_score,
            above_floor=above_floor,
            kick_depth=kick_depth,
            bass_norm=bass_norm,
            build_score=build_score,
            genre=genre,
            policy=policy,
            section=section,
        )
        fx_size = self._update_fx_size(
            above_floor=above_floor,
            energy_norm=energy_norm,
            flux=flux,
            pace_score=pace_score,
            kick_depth=kick_depth,
            bass_norm=bass_norm,
            build_score=build_score,
            build_progress=build_progress,
            section=section,
            policy=policy,
        )
        spectrum_db = self._spectrum_db(mag) if self.visualizer else ()

        arch = genre.archetype if genre is not None else "house_tech"
        arch_label = genre.archetype_label if genre is not None else "House / Tech House"
        tags_text = genre.formatted_tags() if genre is not None else ""
        backend = genre.backend if genre is not None else "dsp"

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
            kick_regularity=kick_reg,
            music_score=music_score,
            build_score=build_score,
            melody_score=melody_score,
            pace_score=pace_score,
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
            fx_size=fx_size,
            archetype=arch,
            archetype_label=arch_label,
            genre_tags_text=tags_text,
            genre_backend=backend,
            spectrum_db=spectrum_db,
        )

    def _spectrum_db(self, mag: np.ndarray) -> tuple[int, ...]:
        """Per-band RMS in dBFS, same scale as the input meter."""
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

    def _hat_density(self) -> float:
        window = 3.0
        count = sum(1 for t in self._hat_times if self._t - t <= window)
        return count / window

    def _detect_beat(self, bass: float, above_floor: bool) -> bool:
        """Kick detection from the depth of the bass-band modulation."""
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
        """Fold one kick interval into the tempo history."""
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
            return

        candidates = self._tempo_candidates
        if candidates and abs(interval - candidates[-1]) > 0.10 * candidates[-1]:
            candidates.clear()
        candidates.append(interval)
        if len(candidates) >= candidates.maxlen:
            hist.clear()
            hist.extend(candidates)
            candidates.clear()

    def _estimate_bpm(self, genre: GenreControlVector | None = None) -> float:
        """Estimate true BPM in 60..190 using kick intervals + genre/onset octave check."""
        if len(self._beat_intervals) < 2:
            return 0.0
        interval = float(np.median(self._beat_intervals))
        if interval <= 0:
            return 0.0
        raw_bpm = 60.0 / interval
        genre_vec = genre if genre is not None else self._last_genre

        # Bring extreme values into the coarse musical window first.
        base = raw_bpm
        while base < _BPM_MIN and base > 0:
            base *= 2.0
        while base > _BPM_MAX:
            base /= 2.0

        candidates: list[tuple[float, float]] = []
        for mul in (0.5, 1.0, 2.0):
            cand = base * mul
            if _BPM_MIN <= cand <= _BPM_MAX:
                candidates.append((cand, mul))
        if not candidates:
            return base

        onset_rate = self._onset_density()
        hat_rate = self._hat_density()
        best_bpm = base
        best_score = -1e9
        for cand, mul in candidates:
            score = 0.0
            # Strong preference for the directly measured kick interval unless
            # genre + rhythmic subdivision evidence strongly disagrees.
            if abs(cand - raw_bpm) <= 0.5:
                score += 0.40
            elif mul == 1.0:
                score += 0.24

            score += 0.28 * self._flux_autocorr_support(cand)

            # Fast breakbeat/DnB 2-step kicks measure ~85-92 BPM in the sub band
            # while hats/snares rattle at >= 5 Hz (160-180 BPM subdivisions).
            if cand >= 152.0 and 76.0 <= raw_bpm <= 96.0 and hat_rate >= 5.0:
                score += 0.36
            elif cand > 152.0 and hat_rate < 4.0 and raw_bpm < 96.0:
                score -= 0.40

            if genre_vec is not None and genre_vec.backend != "off":
                lo = genre_vec.natural_bpm_lo
                hi = genre_vec.natural_bpm_hi
                if lo <= cand <= hi:
                    prior = 1.0
                elif cand < lo:
                    prior = float(np.exp(-((lo - cand) / 18.0) ** 2))
                else:
                    prior = float(np.exp(-((cand - hi) / 20.0) ** 2))
                weight = 0.38 if genre_vec.backend in ("onnx", "manual") else 0.28
                score += weight * prior
            elif 70.0 <= cand <= 155.0:
                score += 0.12

            # Continuity hysteresis prevents flipping octaves mid-phrase.
            if self._resolved_bpm > 0 and abs(cand - self._resolved_bpm) <= 0.08 * self._resolved_bpm:
                score += 0.06

            if score > best_score:
                best_score = score
                best_bpm = cand

        self._resolved_bpm = best_bpm
        return best_bpm

    def _flux_autocorr_support(self, bpm: float) -> float:
        """Normalized autocorrelation support at beat period (and half-beat) in flux."""
        if len(self._flux_ac_hist) < int(2.5 * _AC_RATE_HZ) or bpm <= 0:
            return 0.0
        arr = np.asarray(self._flux_ac_hist, dtype=np.float64)
        centered = arr - float(np.mean(arr))
        denom = float(np.dot(centered, centered))
        if denom <= _EPS:
            return 0.0

        def _corr_at(period_s: float) -> float:
            lag = int(round(period_s * _AC_RATE_HZ))
            if lag <= 1 or lag >= len(centered) - 2:
                return 0.0
            vals = []
            for d in (-1, 0, 1):
                k = lag + d
                if 1 <= k < len(centered):
                    vals.append(float(np.dot(centered[:-k], centered[k:])) / denom)
            return max(0.0, max(vals) if vals else 0.0)

        beat_p = 60.0 / bpm
        c_beat = _corr_at(beat_p)
        c_sub = _corr_at(beat_p * 0.5)
        return _clip01(0.70 * c_beat + 0.30 * c_sub)

    def _update_four_four_grid(
        self,
        kick: bool,
        bpm: float,
        above_floor: bool,
        kick_depth: float,
        *,
        policy: GenrePolicy | None = None,
    ) -> bool:
        """Advance a 4/4 quarter-note grid; return True when a beat fires."""
        need, miss_beats, reg_need, depth_need = self._four_four_params(policy)
        if bpm > 0:
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
                    fired = True

        if fired and (self._t - self._last_four_four_t) < 0.4 * period:
            fired = False
            if kick:
                self._grid_phase = self.dt / period

        if fired:
            self._last_four_four_t = self._t
            self._beat_in_bar = (self._beat_in_bar + 1) % 4

        return fired

    def _four_four_params(
        self, policy: GenrePolicy | None = None
    ) -> tuple[int, float, float, float]:
        """Kicks needed, miss-window, regularity floor, kick-depth floor."""
        if policy is not None:
            inten = _clip01(float(policy.four_four_intensity))
        else:
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
        """0 = no kick right now; ~1 = kick pulsing hard in the bass band."""
        if not above_floor:
            raw = 0.0
        else:
            span = max(self._bass_peak - self._bass_trough, 0.0)
            depth = _clip01(span / (self._bass_peak + _EPS) / 0.65)
            period = 60.0 / max(self._grid_bpm, 65.0)
            since = self._t - self._last_beat_t
            recent = _clip01((2.0 * period - since) / period)
            raw = depth * recent
        alpha = 1.0 - float(np.exp(-self.dt / 0.35))
        self._kick_depth_smooth += alpha * (raw - self._kick_depth_smooth)
        return _clip01(self._kick_depth_smooth)

    def _update_music_score(
        self, level: float, mag: np.ndarray, kick_depth: float
    ) -> float:
        """Structured music vs hiss / digital silence."""
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

    # ------------------------------------------------------------------
    # Intrinsic Pace Score + Genre-Guided Chase Rate (bpm_rate)
    # ------------------------------------------------------------------

    def _update_pace_score(
        self,
        *,
        above_floor: bool,
        energy_norm: float,
        onset_density: float,
        kick_depth: float,
        kick_regularity: float,
        bpm: float,
        bass_e: float,
        mid_e: float,
        treble_e: float,
        genre: GenreControlVector | None,
    ) -> float:
        """Compute compression-invariant musical pace/drive in 0..1."""
        if not above_floor:
            alpha_off = 1.0 - float(np.exp(-self.dt / 0.6))
            self._pace_smooth += alpha_off * (0.0 - self._pace_smooth)
            return _clip01(self._pace_smooth)

        # 1. Percussive transient density (onsets/s, ~1.2 slow -> ~5.5 peak).
        transient_drive = _clip01((onset_density - 1.4) / 4.2)

        # 2. High-band subdivision rate (~1.5 Hz quarter-note -> ~8.5 Hz 16th-note hats)
        #    plus high-frequency spectral brightness.
        hat_hz = self._hat_density()
        total_e = bass_e + mid_e + treble_e + _EPS
        high_share = treble_e / total_e
        hat_drive = _clip01(
            0.72 * _clip01((hat_hz - 1.8) / 6.5)
            + 0.28 * _clip01((high_share - 0.04) / 0.22)
        )

        # 4. True tempo factor (80 BPM -> 0.0, 144 BPM -> 1.0).
        tempo_drive = _clip01((bpm - 80.0) / 64.0) if bpm > 0 else 0.25

        # 3. Kick punch & pulse drive, scaled by tempo so slow 75-90 BPM kicks
        #    do not inflate chase pace like 135 BPM club kicks.
        kick_drive = _clip01(
            kick_depth * (0.60 + 0.40 * kick_regularity) * (0.45 + 0.55 * tempo_drive)
        )

        # 5. Genre Agent arousal prior.
        genre_arousal = (
            _clip01(genre.arousal_prior)
            if (genre is not None and genre.backend != "off")
            else 0.50
        )

        # 6. Modest within-track relative loudness lift (never dominates flat masters).
        dynamic_lift = _clip01((energy_norm - 0.80) / 0.55)

        raw = (
            0.22 * transient_drive
            + 0.24 * hat_drive
            + 0.16 * kick_drive
            + 0.18 * tempo_drive
            + 0.12 * genre_arousal
            + 0.08 * dynamic_lift
        )

        alpha = 1.0 - float(np.exp(-self.dt / _PACE_SMOOTH_SECONDS))
        self._pace_smooth += alpha * (raw - self._pace_smooth)
        return _clip01(self._pace_smooth)

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

    def _update_bpm_rate(
        self,
        *,
        pace_score: float,
        above_floor: bool,
        kick_depth: float,
        bass_norm: float,
        build_score: float,
        genre: GenreControlVector | None,
        policy: GenrePolicy | None,
        section: str,
    ) -> float:
        """Map intrinsic pace + section + GenrePolicy envelope onto 32/64/128/255."""
        cfg_lo, cfg_hi = self._bpm_rate_bounds()
        if not above_floor:
            self._bpm_rate_index = cfg_lo
            self._bpm_rate_current = float(_BPM_RATE_LEVELS[cfg_lo])
            self._bpm_rate_target = self._bpm_rate_current
            self._bpm_rate_down_timer = 0.0
            return self._bpm_rate_current

        sec = (section or "groove").lower()

        cur = self._bpm_rate_index
        up_thresholds = (0.31, 0.53, 0.72)
        down_thresholds = (0.25, 0.46, 0.65)

        desired = cur
        while desired < 3 and pace_score >= up_thresholds[desired]:
            desired += 1
        while desired > 0 and pace_score < down_thresholds[desired - 1]:
            desired -= 1

        # Layer 1 (GenrePolicy / GenreControlVector) sets macro floor & ceiling;
        # Layer 2 (section & fast track energy) modulates immediately within it.
        genre_ceiling = 3
        decay_override: float | None = None
        if policy is not None:
            g_min = int(round(_clip01(policy.bpm_rate_min_idx / 3.0) * 3.0))
            g_max = int(round(_clip01(policy.bpm_rate_max_idx / 3.0) * 3.0))
            d_max = int(round(_clip01(policy.bpm_rate_drop_max_idx / 3.0) * 3.0))
            decay_override = max(0.0, float(policy.bpm_rate_decay))
            if g_min > g_max:
                g_min, g_max = g_max, g_min
            if d_max < g_max:
                d_max = g_max
            genre_ceiling = d_max if sec == "drop" else g_max

            if sec == "breakdown" or (kick_depth < 0.14 and bass_norm < 0.52):
                desired = min(desired, min(1, g_max))
            elif sec == "drop":
                desired = min(d_max, max(desired + 1, g_max))
            elif sec == "buildup":
                if build_score > 0.65:
                    desired = min(d_max, max(desired, g_max))
                else:
                    desired = max(g_min, min(g_max, desired))
            else:
                desired = max(g_min, min(g_max, desired))
        elif genre is not None and genre.backend != "off":
            g_min = int(round(_clip01(genre.groove_rate_min / 3.0) * 3.0))
            g_max = int(round(_clip01(genre.groove_rate_max / 3.0) * 3.0))
            d_max = int(round(_clip01(genre.drop_rate_max / 3.0) * 3.0))
            if g_min > g_max:
                g_min, g_max = g_max, g_min
            if d_max < g_max:
                d_max = g_max
            genre_ceiling = d_max if sec == "drop" else g_max

            if sec == "breakdown" or (kick_depth < 0.14 and bass_norm < 0.52):
                desired = min(desired, min(1, g_max))
            elif sec == "drop":
                desired = min(d_max, max(desired + 1, g_max))
            elif sec == "buildup":
                if build_score > 0.65:
                    desired = min(d_max, max(desired, g_max))
                else:
                    desired = max(g_min, min(g_max, desired))
            else:
                desired = max(g_min, min(g_max, desired))
        else:
            if sec == "breakdown" or (kick_depth < 0.14 and bass_norm < 0.52):
                desired = min(desired, 1)
            elif sec == "drop":
                desired = min(3, desired + 1)

        # Enforce genre hard ceiling & operator-configured safety bounds immediately.
        effective_hi = min(cfg_hi, max(cfg_lo, genre_ceiling))
        desired = max(cfg_lo, min(effective_hi, desired))
        if self._bpm_rate_index < cfg_lo or self._bpm_rate_index > effective_hi:
            idx = max(cfg_lo, min(effective_hi, desired))
            self._bpm_rate_down_timer = 0.0
        else:
            idx = self._apply_bpm_rate_decay(desired, decay_override=decay_override)

        target = float(_BPM_RATE_LEVELS[idx])
        if idx != self._bpm_rate_index or self._bpm_rate_target != target:
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

    def _apply_bpm_rate_decay(
        self, desired: int, *, decay_override: float | None = None
    ) -> int:
        """Step up immediately; hold before each step down (decay_seconds)."""
        i = self._bpm_rate_index
        if desired >= i:
            self._bpm_rate_down_timer = 0.0
            return desired
        decay = (
            max(0.0, float(decay_override))
            if decay_override is not None
            else max(0.0, float(self.bpm_rate_cfg.decay_seconds))
        )
        if decay <= 0.0:
            self._bpm_rate_down_timer = 0.0
            return desired
        self._bpm_rate_down_timer += self.dt
        if self._bpm_rate_down_timer >= decay:
            self._bpm_rate_down_timer = 0.0
            return i - 1
        return i

    def _update_fx_size(
        self,
        *,
        above_floor: bool,
        energy_norm: float,
        flux: float,
        pace_score: float,
        kick_depth: float,
        bass_norm: float,
        build_score: float,
        build_progress: float,
        section: str,
        policy: GenrePolicy | None,
    ) -> float:
        """Compute DMX 0..255 FX Size (Layer 1 genre base + Layer 2 fast energy/drop bump).

        Uses an asymmetric envelope (65 ms fast attack on rises/drops, 1.35 s
        smooth release) so moving heads and dimmer spreads punch up immediately
        on energy surges and drops without wobbling between individual kicks.
        """
        if policy is not None:
            size_base = max(0.0, min(255.0, float(policy.fx_size_base)))
            size_max = max(size_base, min(255.0, float(policy.fx_size_max)))
            reactivity = _clip01(float(policy.fx_size_reactivity))
        else:
            size_base = max(0.0, min(255.0, float(self.fx_size_cfg.base)))
            size_max = max(size_base, min(255.0, float(self.fx_size_cfg.max)))
            reactivity = _clip01(float(self.fx_size_cfg.reactivity))

        if not above_floor:
            alpha_off = 1.0 - float(np.exp(-self.dt / 0.85))
            self._fx_size_current += alpha_off * (0.0 - self._fx_size_current)
            return float(np.clip(self._fx_size_current, 0.0, 255.0))

        # Layer 2 fast track dynamics (~180 ms response, independent of slow genre EMA).
        a_fast = 1.0 - float(np.exp(-self.dt / 0.18))
        self._size_energy_fast += a_fast * (energy_norm - self._size_energy_fast)
        flux_norm = flux / (self._flux_base + _EPS) if self._flux_base > _EPS else 1.0
        self._size_flux_fast += a_fast * (flux_norm - self._size_flux_fast)

        energy_surge = _clip01((self._size_energy_fast - 0.94) / 0.42)
        flux_surge = _clip01((self._size_flux_fast - 1.15) / 1.10)
        track_lift = _clip01(0.68 * energy_surge + 0.32 * flux_surge)

        span = max(0.0, size_max - size_base)
        sec = (section or "groove").lower()
        bp = _clip01(float(build_progress))

        if sec == "drop":
            # Immediate bump to genre peak ceiling on DROP.
            drop_floor = size_base + span * (0.75 + 0.25 * reactivity)
            target = min(255.0, max(drop_floor, size_base + span * (0.85 + 0.20 * track_lift)))
        elif sec == "breakdown" or (kick_depth < 0.14 and bass_norm < 0.52):
            # Calm narrow spread in breakdowns, expanding gently if building out.
            calm_base = size_base * (0.42 + 0.35 * bp)
            target = min(size_max, calm_base + 0.30 * reactivity * span * track_lift)
        elif sec == "buildup":
            # Swell progressively with build_progress and rising track energy.
            swell = _clip01(0.18 + 0.58 * bp + 0.35 * reactivity * track_lift)
            target = min(size_max, size_base + span * swell)
        else:
            # Groove: genre baseline modulated by pace + fast bump on energy rises.
            groove_base = min(size_max, size_base * (0.85 + 0.25 * pace_score))
            bump = span * reactivity * _clip01(0.78 * track_lift + 0.22 * build_score)
            target = min(size_max, groove_base + bump)

        # Asymmetric follower: punch up fast (65 ms), settle smoothly (1.35 s).
        tau = 0.065 if target > self._fx_size_current else 1.35
        alpha = 1.0 - float(np.exp(-self.dt / tau))
        self._fx_size_current += alpha * (target - self._fx_size_current)
        return float(np.clip(self._fx_size_current, 0.0, 255.0))

    def _melody_score(
        self,
        bass_norm: float,
        mid_norm: float,
        treble_norm: float,
        tilt: float,
        build_score: float,
        onset_density: float,
    ) -> float:
        """Kick still present, but mid/high content is the focus."""
        if self._t < WARMUP_SECONDS:
            self._melody_smooth = 0.0
            return 0.0

        kick_gate = _clip01((bass_norm - 0.55) / 0.35)
        mid_lift = _clip01((mid_norm - 0.95) / 0.35)
        treble_lift = _clip01((treble_norm - 0.95) / 0.45)

        tilt_norm = tilt / (self._tilt_base + _EPS)
        tilt_lift = _clip01((tilt_norm - 1.05) / 0.3)

        tone = 0.55 * mid_lift + 0.15 * treble_lift + 0.30 * tilt_lift
        build_mute = 1.0 - 0.75 * build_score
        perc_mute = _clip01(1.0 - (onset_density - 3.5) / 5.0)
        raw = _clip01(1.85 * kick_gate * tone * build_mute * perc_mute)

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
        """Composite build tension from slopes, level lifts, and filter opens."""
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

        lift = max(_half_lift(self._rms_trend), _half_lift(self._treble_trend))

        treble_rise = _clip01(treble_slope / (cfg.full_scale_treble_slope + _EPS))
        bass_not_rising = _clip01(
            1.0 - bass_slope / (cfg.full_scale_energy_slope + _EPS)
        )
        divergence = treble_rise * bass_not_rising

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
    """Skewed EMA. Default: follow drops faster than rises (mid/treble)."""
    if hold_high:
        rate = alpha * (1.25 if value > base else 0.22)
    else:
        rate = alpha * (0.25 if value > base else 1.5)
    return base + rate * (value - base)
