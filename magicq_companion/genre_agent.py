"""Offline music genre & archetype agent (ONNX Discogs-EffNet + DSP fallback).

Runs an 8-second audio ring buffer through a 96-band Slaney log-mel frontend
and classifies music across 400 Discogs genres (via `onnxruntime` when the
local `.onnx` model is present, with a harmonic/percussive + tempogram DSP
fallback when running without model weights).

Rather than hard-switching discrete presets, the 400 genre activations are
projected onto 8 continuous Musical Archetypes and smoothed over ~12 seconds
so both T14 and T15 adapt fluidly to any genre or DJ transition.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .config import ARCHETYPE_NAMES, GenreConfig, coerce_genre_mode
from .paths import default_model_path

_EPS = 1e-9
_TARGET_SR = 16_000
_WINDOW_SECONDS = 8.5
_MIN_ANALYZE_SECONDS = 2.2

_EFFNET_FFT = 512
_EFFNET_HOP = 256
_EFFNET_MELS = 96
_EFFNET_PATCH_FRAMES = 128
_EFFNET_PATCH_HOP = 64


@dataclass(frozen=True)
class ArchetypeSpec:
    name: str
    label: str
    natural_bpm_lo: float
    natural_bpm_hi: float
    arousal_prior: float
    groove_rate_min: float
    groove_rate_max: float
    drop_rate_max: float
    syncopation_tolerance: float
    build_threshold_mul: float = 1.00
    build_decay_mul: float = 1.00
    drop_jump_mul: float = 1.00
    drop_intensity_add: float = 0.00
    breakdown_beats: float = 4.0
    evidence_mul: float = 1.00
    dwell_mul: float = 1.00
    breakdown_dwell_mul: float = 0.45
    expected_build_mul: float = 1.00
    drop_hold_mul: float = 1.00
    drop_rise_mul: float = 1.00
    kick_absent_mul: float = 1.00
    breakdown_need_mul: float = 0.45
    groove_return_mul: float = 1.00
    fizzle_to_breakdown: float = 0.0


ARCHETYPE_SPECS: dict[str, ArchetypeSpec] = {
    "ambient_chill": ArchetypeSpec(
        name="ambient_chill",
        label="Ambient / Chill",
        natural_bpm_lo=62.0,
        natural_bpm_hi=106.0,
        arousal_prior=0.16,
        groove_rate_min=0.0,
        groove_rate_max=1.0,
        drop_rate_max=1.0,
        syncopation_tolerance=0.45,
        build_threshold_mul=1.32,
        build_decay_mul=0.80,
        drop_jump_mul=1.45,
        drop_intensity_add=-0.12,
        breakdown_beats=2.5,
        evidence_mul=1.35,
        dwell_mul=1.50,
        breakdown_dwell_mul=0.25,
        expected_build_mul=1.40,
        drop_hold_mul=0.65,
        drop_rise_mul=1.35,
        kick_absent_mul=1.20,
        breakdown_need_mul=0.30,
        groove_return_mul=1.60,
        fizzle_to_breakdown=1.0,
    ),
    "groovy_warm": ArchetypeSpec(
        name="groovy_warm",
        label="Deep / Warm Groove",
        natural_bpm_lo=104.0,
        natural_bpm_hi=127.0,
        arousal_prior=0.42,
        groove_rate_min=0.0,
        groove_rate_max=2.0,
        drop_rate_max=3.0,
        syncopation_tolerance=0.22,
        build_threshold_mul=1.08,
        build_decay_mul=1.15,
        drop_jump_mul=1.15,
        drop_intensity_add=-0.04,
        breakdown_beats=6.0,
        evidence_mul=1.20,
        dwell_mul=1.25,
        breakdown_dwell_mul=0.55,
        expected_build_mul=1.30,
        drop_hold_mul=1.00,
        drop_rise_mul=1.12,
        kick_absent_mul=1.00,
    ),
    "house_tech": ArchetypeSpec(
        name="house_tech",
        label="House / Tech House",
        natural_bpm_lo=116.0,
        natural_bpm_hi=133.0,
        arousal_prior=0.64,
        groove_rate_min=1.0,
        groove_rate_max=2.4,
        drop_rate_max=3.0,
        syncopation_tolerance=0.16,
        build_threshold_mul=0.96,
        build_decay_mul=1.05,
        drop_jump_mul=1.00,
        drop_intensity_add=0.02,
        breakdown_beats=4.0,
        evidence_mul=1.00,
        dwell_mul=1.00,
        breakdown_dwell_mul=0.45,
        expected_build_mul=1.00,
        drop_hold_mul=1.05,
        drop_rise_mul=1.00,
        kick_absent_mul=1.00,
    ),
    "melodic_prog": ArchetypeSpec(
        name="melodic_prog",
        label="Melodic / Progressive",
        natural_bpm_lo=120.0,
        natural_bpm_hi=145.0,
        arousal_prior=0.56,
        groove_rate_min=1.0,
        groove_rate_max=2.1,
        drop_rate_max=3.0,
        syncopation_tolerance=0.12,
        build_threshold_mul=0.60,
        build_decay_mul=2.15,
        drop_jump_mul=1.12,
        drop_intensity_add=0.00,
        breakdown_beats=8.0,
        evidence_mul=1.55,
        dwell_mul=1.65,
        breakdown_dwell_mul=0.80,
        expected_build_mul=1.95,
        drop_hold_mul=1.35,
        drop_rise_mul=1.18,
        kick_absent_mul=1.08,
    ),
    "club_peak": ArchetypeSpec(
        name="club_peak",
        label="Peak Club / Techno",
        natural_bpm_lo=126.0,
        natural_bpm_hi=158.0,
        arousal_prior=0.85,
        groove_rate_min=1.6,
        groove_rate_max=3.0,
        drop_rate_max=3.0,
        syncopation_tolerance=0.18,
        build_threshold_mul=0.90,
        build_decay_mul=0.90,
        drop_jump_mul=0.86,
        drop_intensity_add=0.10,
        breakdown_beats=4.0,
        evidence_mul=0.82,
        dwell_mul=0.82,
        breakdown_dwell_mul=0.38,
        expected_build_mul=0.92,
        drop_hold_mul=1.15,
        drop_rise_mul=0.88,
        kick_absent_mul=0.95,
    ),
    "hiphop_urban": ArchetypeSpec(
        name="hiphop_urban",
        label="Hip-Hop / R&B / Urban",
        natural_bpm_lo=68.0,
        natural_bpm_hi=112.0,
        arousal_prior=0.45,
        groove_rate_min=0.0,
        groove_rate_max=2.0,
        drop_rate_max=3.0,
        syncopation_tolerance=0.80,
        build_threshold_mul=1.16,
        build_decay_mul=0.75,
        drop_jump_mul=1.08,
        drop_intensity_add=0.00,
        breakdown_beats=6.0,
        evidence_mul=1.05,
        dwell_mul=1.10,
        breakdown_dwell_mul=0.55,
        expected_build_mul=0.80,
        drop_hold_mul=0.80,
        drop_rise_mul=1.05,
        kick_absent_mul=0.85,
    ),
    "fast_bass": ArchetypeSpec(
        name="fast_bass",
        label="DnB / Breaks / Bass",
        natural_bpm_lo=134.0,
        natural_bpm_hi=182.0,
        arousal_prior=0.78,
        # Cap standard fast-bass grooves at half-time (128, ~87 BPM feel) so
        # 174 BPM DnB does not strobe frantically; unlock 255 on peak drops.
        groove_rate_min=1.0,
        groove_rate_max=2.0,
        drop_rate_max=3.0,
        syncopation_tolerance=0.86,
        build_threshold_mul=1.04,
        build_decay_mul=0.65,
        drop_jump_mul=0.80,
        drop_intensity_add=0.14,
        breakdown_beats=6.0,
        evidence_mul=0.72,
        dwell_mul=0.70,
        breakdown_dwell_mul=0.42,
        expected_build_mul=0.75,
        drop_hold_mul=0.90,
        drop_rise_mul=0.82,
        kick_absent_mul=0.82,
    ),
    "rock_live": ArchetypeSpec(
        name="rock_live",
        label="Rock / Pop / Live",
        natural_bpm_lo=92.0,
        natural_bpm_hi=148.0,
        arousal_prior=0.56,
        groove_rate_min=0.0,
        groove_rate_max=2.2,
        drop_rate_max=3.0,
        syncopation_tolerance=0.68,
        build_threshold_mul=1.08,
        build_decay_mul=0.85,
        drop_jump_mul=1.00,
        drop_intensity_add=0.02,
        breakdown_beats=5.0,
        evidence_mul=0.95,
        dwell_mul=1.00,
        breakdown_dwell_mul=0.48,
        expected_build_mul=0.90,
        drop_hold_mul=0.85,
        drop_rise_mul=0.98,
        kick_absent_mul=0.90,
    ),
}


@dataclass(frozen=True)
class GenreControlVector:
    """Blended continuous tuning applied to FeatureExtractor and SectionTracker."""

    archetype: str = "house_tech"
    archetype_label: str = "House / Tech House"
    backend: str = "dsp"
    confidence: float = 0.0
    genre_tags: tuple[tuple[str, float], ...] = field(default_factory=tuple)
    natural_bpm_lo: float = 110.0
    natural_bpm_hi: float = 138.0
    arousal_prior: float = 0.58
    groove_rate_min: float = 0.5
    groove_rate_max: float = 2.4
    drop_rate_max: float = 3.0
    syncopation_tolerance: float = 0.20
    build_threshold_mul: float = 1.00
    build_decay_mul: float = 1.00
    drop_jump_mul: float = 1.00
    drop_intensity_add: float = 0.00
    breakdown_beats: float = 4.0
    evidence_mul: float = 1.00
    dwell_mul: float = 1.00
    breakdown_dwell_mul: float = 0.45
    expected_build_mul: float = 1.00
    drop_hold_mul: float = 1.00
    drop_rise_mul: float = 1.00
    kick_absent_mul: float = 1.00
    breakdown_need_mul: float = 0.45
    groove_return_mul: float = 1.00
    fizzle_to_breakdown: float = 0.0

    def formatted_tags(self) -> str:
        if not self.genre_tags:
            return self.archetype_label
        return " · ".join(
            f"{name} {int(round(prob * 100))}%" for name, prob in self.genre_tags[:2]
        )


def blend_control_vector(
    weights: dict[str, float],
    *,
    backend: str = "dsp",
    genre_tags: tuple[tuple[str, float], ...] = (),
) -> GenreControlVector:
    """Compute a probability-weighted GenreControlVector across ARCHETYPE_SPECS."""
    clean: dict[str, float] = {}
    total = 0.0
    for name in ARCHETYPE_NAMES:
        w = max(0.0, float(weights.get(name, 0.0)))
        clean[name] = w
        total += w
    if total <= _EPS:
        clean = {name: (1.0 if name == "house_tech" else 0.0) for name in ARCHETYPE_NAMES}
        total = 1.0
    else:
        for name in clean:
            clean[name] /= total

    dominant = max(ARCHETYPE_NAMES, key=lambda k: clean[k])
    dom_spec = ARCHETYPE_SPECS[dominant]
    conf = float(clean[dominant])

    def _avg(attr: str) -> float:
        return float(sum(clean[k] * float(getattr(ARCHETYPE_SPECS[k], attr)) for k in ARCHETYPE_NAMES))

    tags = genre_tags
    if not tags:
        ranked = sorted(ARCHETYPE_NAMES, key=lambda k: clean[k], reverse=True)
        tags = tuple(
            (ARCHETYPE_SPECS[k].label, round(clean[k], 3))
            for k in ranked[:2]
            if clean[k] >= 0.12
        )

    return GenreControlVector(
        archetype=dominant,
        archetype_label=dom_spec.label,
        backend=backend,
        confidence=round(conf, 3),
        genre_tags=tags,
        natural_bpm_lo=_avg("natural_bpm_lo"),
        natural_bpm_hi=_avg("natural_bpm_hi"),
        arousal_prior=_avg("arousal_prior"),
        groove_rate_min=_avg("groove_rate_min"),
        groove_rate_max=_avg("groove_rate_max"),
        drop_rate_max=_avg("drop_rate_max"),
        syncopation_tolerance=_avg("syncopation_tolerance"),
        build_threshold_mul=_avg("build_threshold_mul"),
        build_decay_mul=_avg("build_decay_mul"),
        drop_jump_mul=_avg("drop_jump_mul"),
        drop_intensity_add=_avg("drop_intensity_add"),
        breakdown_beats=_avg("breakdown_beats"),
        evidence_mul=_avg("evidence_mul"),
        dwell_mul=_avg("dwell_mul"),
        breakdown_dwell_mul=_avg("breakdown_dwell_mul"),
        expected_build_mul=_avg("expected_build_mul"),
        drop_hold_mul=_avg("drop_hold_mul"),
        drop_rise_mul=_avg("drop_rise_mul"),
        kick_absent_mul=_avg("kick_absent_mul"),
        breakdown_need_mul=_avg("breakdown_need_mul"),
        groove_return_mul=_avg("groove_return_mul"),
        fizzle_to_breakdown=_avg("fizzle_to_breakdown"),
    )


# ----------------------------------------------------------------------
# Slaney 96-band Log-Mel Frontend (matches Essentia Discogs-EffNet)
# ----------------------------------------------------------------------


def _hz_to_slaney_mel(freq_hz: np.ndarray) -> np.ndarray:
    f_sp = 200.0 / 3.0
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = math.log(6.4) / 27.0
    mels = freq_hz / f_sp
    above = freq_hz >= min_log_hz
    if np.any(above):
        mels = np.where(
            above,
            min_log_mel + np.log(np.maximum(freq_hz, _EPS) / min_log_hz) / logstep,
            mels,
        )
    return mels


def _slaney_mel_to_hz(mels: np.ndarray) -> np.ndarray:
    f_sp = 200.0 / 3.0
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = math.log(6.4) / 27.0
    freqs = f_sp * mels
    above = mels >= min_log_mel
    if np.any(above):
        freqs = np.where(above, min_log_hz * np.exp(logstep * (mels - min_log_mel)), freqs)
    return freqs


def build_slaney_mel_filterbank(
    sr: int = _TARGET_SR,
    n_fft: int = _EFFNET_FFT,
    n_mels: int = _EFFNET_MELS,
    fmin: float = 0.0,
    fmax: float = 8000.0,
) -> np.ndarray:
    """Return a `(n_fft // 2 + 1, n_mels)` Slaney-normalized Mel matrix."""
    fft_freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    mel_Bounds = _hz_to_slaney_mel(np.array([fmin, fmax], dtype=np.float64))
    mel_pts = np.linspace(mel_Bounds[0], mel_Bounds[1], n_mels + 2)
    hz_pts = _slaney_mel_to_hz(mel_pts)
    fdiff = np.maximum(np.diff(hz_pts), _EPS)

    ramps = hz_pts[None, :] - fft_freqs[:, None]
    weights = np.zeros((len(fft_freqs), n_mels), dtype=np.float32)
    for i in range(n_mels):
        lower = -ramps[:, i] / fdiff[i]
        upper = ramps[:, i + 2] / fdiff[i + 1]
        enorm = 2.0 / max(hz_pts[i + 2] - hz_pts[i], _EPS)
        weights[:, i] = np.maximum(0.0, np.minimum(lower, upper)) * enorm
    return weights


_MEL_BASIS = build_slaney_mel_filterbank()
_EFFNET_WINDOW = (
    0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(_EFFNET_FFT) / _EFFNET_FFT)
).astype(np.float32)


def resample_to_16k(audio: np.ndarray, src_sr: int) -> np.ndarray:
    """Band-limited FFT resample of a mono window to 16 kHz."""
    if len(audio) == 0:
        return np.zeros(0, dtype=np.float32)
    clean = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float64)
    if src_sr == _TARGET_SR:
        return clean.astype(np.float32)
    target_len = max(1, int(round(len(clean) * _TARGET_SR / max(src_sr, 1))))
    spec = np.fft.rfft(clean)
    out_bins = target_len // 2 + 1
    trimmed = np.zeros(out_bins, dtype=np.complex128)
    copy_bins = min(len(spec), out_bins)
    trimmed[:copy_bins] = spec[:copy_bins]
    scaled = np.fft.irfft(trimmed, n=target_len) * (target_len / len(clean))
    return np.clip(scaled, -1.0, 1.0).astype(np.float32)


def compute_log_mel_frames(audio_16k: np.ndarray) -> np.ndarray:
    """Return `(n_frames, 96)` log-mel spectrogram frames at 16 kHz."""
    if len(audio_16k) < _EFFNET_FFT:
        pad = np.zeros(_EFFNET_FFT, dtype=np.float32)
        pad[: len(audio_16k)] = audio_16k
        audio_16k = pad
    n_frames = 1 + (len(audio_16k) - _EFFNET_FFT) // _EFFNET_HOP
    strides = (audio_16k.strides[0] * _EFFNET_HOP, audio_16k.strides[0])
    frames = np.lib.stride_tricks.as_strided(
        audio_16k,
        shape=(n_frames, _EFFNET_FFT),
        strides=strides,
    )
    windowed = frames * _EFFNET_WINDOW[None, :]
    power = (np.abs(np.fft.rfft(windowed, n=_EFFNET_FFT, axis=1)) ** 2).astype(np.float32)
    mel = power @ _MEL_BASIS
    log_mel = np.log10(10_000.0 * np.maximum(mel, 0.0) + 1.0)
    return np.nan_to_num(log_mel, nan=0.0, posinf=4.0, neginf=0.0).astype(np.float32)


def compute_effnet_mel_patches(audio_16k: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return `(patches [B, 128, 96], full_mel [T, 96])` for Discogs-EffNet."""
    mel = compute_log_mel_frames(audio_16k)
    n_frames = mel.shape[0]
    if n_frames < _EFFNET_PATCH_FRAMES:
        padded = np.zeros((_EFFNET_PATCH_FRAMES, _EFFNET_MELS), dtype=np.float32)
        padded[:n_frames] = mel
        return padded[None, :, :], mel
    starts = list(range(0, n_frames - _EFFNET_PATCH_FRAMES + 1, _EFFNET_PATCH_HOP))
    last_start = n_frames - _EFFNET_PATCH_FRAMES
    if starts[-1] != last_start:
        starts.append(last_start)
    patches = np.stack([mel[s : s + _EFFNET_PATCH_FRAMES] for s in starts], axis=0)
    return patches.astype(np.float32), mel


# ----------------------------------------------------------------------
# Discogs 400 Class -> 8 Musical Archetype Projection
# ----------------------------------------------------------------------

# Keyword rules evaluated against "Parent---Subgenre" labels from Discogs-400.
_SUBGENRE_ARCHETYPE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "drum n bass",
            "jungle",
            "halftime",
            "neurofunk",
            "liquid funk",
            "jump-up",
            "breakbeat",
            "breaks",
            "uk garage",
            "speed garage",
            "2-step",
            "dubstep",
            "bassline",
            "juke",
            "footwork",
            "breakcore",
            "happy hardcore",
            "hardcore",
            "grime",
        ),
        "fast_bass",
    ),
    (
        (
            "ambient",
            "downtempo",
            "chillwave",
            "lounge",
            "trip hop",
            "illbient",
            "new age",
            "vaporwave",
            "ballad",
            "easy listening",
            "smooth jazz",
            "cool jazz",
            "modern classical",
            "classical",
            "drone",
            "field recording",
            "acoustic",
        ),
        "ambient_chill",
    ),
    (
        (
            "deep house",
            "organic",
            "garage house",
            "disco",
            "nu-disco",
            "italo-disco",
            "boogie",
            "soul",
            "neo soul",
            "funk",
            "jazz-funk",
            "afrobeat",
            "bossa nova",
            "tropical house",
            "balearic",
            "indie pop",
            "city pop",
        ),
        "groovy_warm",
    ),
    (
        (
            "progressive house",
            "trance",
            "progressive trance",
            "uplifting trance",
            "tech trance",
            "psy-trance",
            "goa trance",
            "melodic",
            "berlin-school",
            "dream pop",
            "synthwave",
            "italo dance",
        ),
        "melodic_prog",
    ),
    (
        (
            "hard techno",
            "techno",
            "minimal techno",
            "schranz",
            "acid",
            "industrial",
            "ebm",
            "electro",
            "eurodance",
            "hi nrg",
            "hands up",
            "hardstyle",
            "hard trance",
            "hard house",
            "jumpstyle",
            "gabber",
            "big beat",
            "ghetto tech",
            "new beat",
        ),
        "club_peak",
    ),
    (
        (
            "tech house",
            "house",
            "electro house",
            "chicago house",
            "acid house",
            "french house",
            "tribal house",
            "latin house",
            "euro house",
            "uk funky",
            "ghetto house",
            "hip-house",
        ),
        "house_tech",
    ),
    (
        (
            "trap",
            "rnb",
            "r&b",
            "swing",
            "reggaeton",
            "dancehall",
            "ragga",
            "reggae",
            "dub",
            "baile funk",
            "favela funk",
            "cumbia",
            "salsa",
            "bachata",
            "merengue",
            "pop rap",
            "boom bap",
            "gangsta",
            "crunk",
            "bounce",
            "conscious",
            "cloud rap",
            "k-pop",
            "europop",
            "dance-pop",
        ),
        "hiphop_urban",
    ),
)

_PARENT_FALLBACK: dict[str, str] = {
    "hip hop": "hiphop_urban",
    "reggae": "hiphop_urban",
    "latin": "hiphop_urban",
    "pop": "hiphop_urban",
    "funk / soul": "groovy_warm",
    "jazz": "ambient_chill",
    "classical": "ambient_chill",
    "stage & screen": "ambient_chill",
    "electronic": "house_tech",
    "rock": "rock_live",
    "blues": "rock_live",
    "folk, world, & country": "rock_live",
}


def map_discogs_label_to_archetype(label: str) -> str:
    """Map a Discogs `"Parent---Subgenre"` string to one of the 8 archetypes."""
    raw = str(label or "").strip()
    low = raw.lower()
    if "---" in low:
        parent, sub = low.split("---", 1)
    else:
        parent, sub = "", low

    for needles, archetype in _SUBGENRE_ARCHETYPE_RULES:
        if any(needle in sub for needle in needles):
            return archetype
    return _PARENT_FALLBACK.get(parent.strip(), "house_tech")


def clean_discogs_display_name(label: str) -> str:
    raw = str(label or "").strip()
    if "---" in raw:
        return raw.split("---", 1)[1].strip()
    return raw


def build_discogs_projection_matrix(classes: list[str]) -> np.ndarray:
    """Build `(len(classes), len(ARCHETYPE_NAMES))` projection matrix."""
    arch_to_idx = {name: i for i, name in enumerate(ARCHETYPE_NAMES)}
    mat = np.zeros((len(classes), len(ARCHETYPE_NAMES)), dtype=np.float32)
    for row, label in enumerate(classes):
        arch = map_discogs_label_to_archetype(label)
        mat[row, arch_to_idx[arch]] = 1.0
    return mat


# ----------------------------------------------------------------------
# Acoustic HPSS + Tempogram DSP Classifier (Fallback & Arousal Anchor)
# ----------------------------------------------------------------------


def classify_mel_dsp(
    mel: np.ndarray,
    *,
    bpm_hint: float = 0.0,
    kick_regularity: float = 0.0,
    onset_density: float = 0.0,
) -> dict[str, float]:
    """Estimate 8-archetype weights from a 16 kHz log-mel spectrogram."""
    if mel.size == 0 or mel.shape[0] < 8:
        return {name: (1.0 if name == "house_tech" else 0.0) for name in ARCHETYPE_NAMES}

    # Approximate harmonic vs percussive energy using short median filters
    # along time (harmonic sustained) vs frequency (percussive broadband).
    lin = np.maximum(10.0 ** mel - 1.0, 0.0)
    t_len, f_len = lin.shape
    k_t = min(9, t_len if t_len % 2 == 1 else max(1, t_len - 1))
    k_f = min(9, f_len if f_len % 2 == 1 else max(1, f_len - 1))
    harm = _sliding_median_axis0(lin, k_t) if k_t >= 3 else lin
    perc = _sliding_median_axis1(lin, k_f) if k_f >= 3 else lin

    h_sum = float(np.sum(harm)) + _EPS
    p_sum = float(np.sum(perc)) + _EPS
    perc_ratio = p_sum / (h_sum + p_sum)

    # Perceptually balanced band shares & hat pulse rate in log-mel domain.
    mel_means = np.mean(mel, axis=0) + _EPS
    mel_total = float(np.sum(mel_means)) + _EPS
    low_share = float(np.sum(mel_means[:10])) / mel_total
    warm_share = float(np.sum(mel_means[10:30])) / mel_total
    mid_share = float(np.sum(mel_means[30:60])) / mel_total
    high_share = float(np.sum(mel_means[60:])) / mel_total

    high_env = np.mean(mel[:, 58:], axis=1)
    high_diff = np.maximum(np.diff(high_env), 0.0)
    frame_hz = _TARGET_SR / _EFFNET_HOP
    refractory_frames = max(1, int(round(0.055 * frame_hz)))
    hat_hits = 0
    if high_diff.size and float(np.max(high_env)) > 0.05:
        p90 = float(np.percentile(high_diff, 90))
        p50 = float(np.median(high_diff))
        high_thresh = max(p50 + 0.35 * (p90 - p50), float(np.max(high_diff)) * 0.22, 0.015)
        last_hit = -refractory_frames
        for idx_h, val in enumerate(high_diff):
            if val > high_thresh and (idx_h - last_hit) >= refractory_frames:
                hat_hits += 1
                last_hit = idx_h
    duration_s = max(t_len / frame_hz, 0.5)
    hat_hz = hat_hits / duration_s

    # Weight low/kick mel bands higher than random-phase white-noise hats for BPM.
    low_flux = np.sum(np.maximum(np.diff(mel[:, :18], axis=0), 0.0), axis=1)
    all_flux = np.sum(np.maximum(np.diff(mel, axis=0), 0.0), axis=1)
    flux = 2.5 * low_flux + 0.35 * all_flux
    ac_bpm = _autocorr_bpm(flux, frame_hz=frame_hz)
    bpm = bpm_hint if 62.0 <= bpm_hint <= 185.0 else ac_bpm
    # Disambiguate half-time 2-step DnB/Jungle kicks (~85-92 BPM kick, >=5.0 Hz breakbeat hats).
    if 78.0 <= bpm <= 96.0 and (hat_hz >= 5.0 or ac_bpm >= 156.0) and perc_ratio >= 0.35:
        bpm *= 2.0

    scores: dict[str, float] = {name: 0.04 for name in ARCHETYPE_NAMES}

    # Slow / chill vs urban / hip-hop (< 108 BPM or very low percussive drive).
    if perc_ratio < 0.34 and onset_density < 2.2 and hat_hz < 3.0:
        scores["ambient_chill"] += 0.75
        scores["groovy_warm"] += 0.20
    if 60.0 <= bpm <= 108.0 and hat_hz < 4.8:
        if perc_ratio >= 0.30 or low_share > 0.22:
            scores["hiphop_urban"] += 0.72
            if kick_regularity < 0.55:
                scores["hiphop_urban"] += 0.20
        else:
            scores["ambient_chill"] += 0.58

    # Warm deep/disco/soulful groove (104..125 BPM, warm harmonic mids, moderate hats).
    if 104.0 <= bpm <= 125.5 and hat_hz < 6.8:
        scores["groovy_warm"] += 0.52 + 0.32 * min(1.0, (warm_share + mid_share) / 0.50)
        if kick_regularity >= 0.48 and bpm >= 118.0:
            scores["house_tech"] += 0.28

    # House / Tech House (118..133 BPM, solid 4/4 kick + crisp 8th/16th hats).
    if 118.0 <= bpm <= 133.5 and kick_regularity >= 0.45 and hat_hz >= 3.0:
        scores["house_tech"] += 0.58 + 0.25 * min(1.0, hat_hz / 7.5)

    # Melodic / Progressive / Trance (harmonic dominance with steady pulse).
    if 120.0 <= bpm <= 146.0 and perc_ratio < 0.44 and (warm_share + mid_share) > 0.45:
        scores["melodic_prog"] += 0.55

    # Peak Club / Techno (128..158 BPM, strong percussion + dense 16th-note hats).
    if 128.0 <= bpm <= 158.0 and (perc_ratio >= 0.44 or hat_hz >= 6.2 or onset_density >= 4.0):
        scores["club_peak"] += 0.72 + 0.25 * min(1.0, high_share * 4.0)

    # Fast Bass: Drum & Bass / Breaks / UKG.
    if bpm >= 156.0 or (bpm >= 132.0 and kick_regularity < 0.48 and hat_hz >= 4.8):
        scores["fast_bass"] += 0.88

    # Rock / Live: mid-heavy spectrum with moderate syncopation and lower sub share.
    if mid_share > 0.38 and low_share < 0.22 and perc_ratio >= 0.38 and kick_regularity < 0.55:
        scores["rock_live"] += 0.55

    total = sum(scores.values()) or 1.0
    return {k: v / total for k, v in scores.items()}


def _sliding_median_axis0(arr: np.ndarray, k: int) -> np.ndarray:
    pad = k // 2
    padded = np.pad(arr, ((pad, pad), (0, 0)), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, k, axis=0)
    return np.median(windows, axis=-1)


def _sliding_median_axis1(arr: np.ndarray, k: int) -> np.ndarray:
    pad = k // 2
    padded = np.pad(arr, ((0, 0), (pad, pad)), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, k, axis=1)
    return np.median(windows, axis=-1)


def _autocorr_bpm(onset_env: np.ndarray, frame_hz: float) -> float:
    """Estimate fundamental beat tempo in 64..184 BPM, rejecting bar subharmonics."""
    if len(onset_env) < int(frame_hz * 2.0):
        return 124.0
    env = onset_env - float(np.mean(onset_env))
    norm = float(np.dot(env, env))
    if norm <= _EPS:
        return 124.0
    ac = np.correlate(env, env, mode="full")[len(env) - 1 :] / norm
    min_lag = max(1, int(round(frame_hz * 60.0 / 184.0)))
    max_lag = min(len(ac) - 1, int(round(frame_hz * 60.0 / 64.0)))
    if max_lag <= min_lag:
        return 124.0
    lags = np.arange(min_lag, max_lag + 1, dtype=np.float64)
    bpms = 60.0 * frame_hz / lags
    # Gentle log-tempo prior centered at 122 BPM so 2-beat/4-beat multiples do
    # not beat the fundamental beat lag when both have equal correlation.
    prior = np.exp(-0.5 * (np.log2(bpms / 122.0) / 0.68) ** 2)
    weighted = np.maximum(ac[min_lag : max_lag + 1], 0.0) * (0.72 + 0.28 * prior)
    best_lag = min_lag + int(np.argmax(weighted))
    # If half-lag (double BPM) has >= 78% of the correlation, best_lag was a
    # 2-beat multiple and half-lag is the true beat period.
    half_lag = int(round(best_lag / 2.0))
    if half_lag >= min_lag and ac[half_lag] >= 0.78 * ac[best_lag]:
        best_lag = half_lag
    return float(60.0 * frame_hz / max(best_lag, 1))


# ----------------------------------------------------------------------
# ONNX Session Wrapper
# ----------------------------------------------------------------------


class _OnnxDiscogsClassifier:
    """Loads Essentia's `discogs-effnet-bsdynamic-1.onnx` and maps 400 classes."""

    def __init__(self, model_path: Path) -> None:
        import onnxruntime as ort  # type: ignore[import-not-found]

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        inp = self.session.get_inputs()[0]
        self.input_name: str = inp.name
        self.input_rank: int = len(inp.shape) if inp.shape else 3

        classes = _load_discogs_classes(model_path)
        self.classes = classes
        self.output_name = _pick_class_output_name(self.session, len(classes))
        self.proj = build_discogs_projection_matrix(classes) if classes else None

    def predict(self, patches: np.ndarray) -> tuple[dict[str, float], tuple[tuple[str, float], ...]]:
        x = patches.astype(np.float32)
        if self.input_rank == 4:
            x = x[:, None, :, :]
        outputs = self.session.run([self.output_name], {self.input_name: x})
        scores = np.asarray(outputs[0], dtype=np.float64)
        if scores.ndim > 2:
            scores = scores.reshape(scores.shape[0], -1)
        mean_scores = np.mean(scores, axis=0)
        if np.min(mean_scores) < 0.0 or np.max(mean_scores) > 1.0001:
            shifted = mean_scores - np.max(mean_scores)
            exp_s = np.exp(shifted)
            mean_scores = exp_s / (np.sum(exp_s) + _EPS)
        mean_scores = np.maximum(mean_scores, 0.0)

        n_cls = len(mean_scores)
        if self.proj is not None and self.proj.shape[0] == n_cls:
            arch_vec = mean_scores @ self.proj
        else:
            arch_vec = np.ones(len(ARCHETYPE_NAMES), dtype=np.float64)
        arch_total = float(np.sum(arch_vec)) + _EPS
        weights = {
            name: float(arch_vec[i] / arch_total)
            for i, name in enumerate(ARCHETYPE_NAMES)
        }

        tags: list[tuple[str, float]] = []
        if self.classes and len(self.classes) == n_cls:
            top_idx = np.argsort(mean_scores)[::-1][:3]
            tag_norm = float(np.sum(mean_scores[top_idx])) + _EPS
            for idx in top_idx:
                raw_prob = float(mean_scores[idx])
                if raw_prob <= 0.01:
                    continue
                conf = raw_prob if raw_prob <= 1.0 else raw_prob / tag_norm
                tags.append((clean_discogs_display_name(self.classes[int(idx)]), round(conf, 3)))
        return weights, tuple(tags)


def _load_discogs_classes(model_path: Path) -> list[str]:
    for candidate in (
        model_path.with_suffix(".json"),
        model_path.parent / "discogs-effnet-bsdynamic-1.json",
        model_path.parent / "discogs_classes.json",
    ):
        if candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("classes"), list):
                    return [str(x) for x in payload["classes"]]
                if isinstance(payload, list):
                    return [str(x) for x in payload]
            except Exception:
                continue
    return []


def _pick_class_output_name(session: Any, expected_classes: int) -> str:
    outputs = session.get_outputs()
    for out in outputs:
        if "activation" in out.name.lower() or "prob" in out.name.lower():
            return out.name
        shape = out.shape or []
        if shape and expected_classes and shape[-1] == expected_classes:
            return out.name
        if shape and shape[-1] == 400:
            return out.name
    return outputs[0].name


# ----------------------------------------------------------------------
# Main GenreAgent Runtime
# ----------------------------------------------------------------------


class GenreAgent:
    """Thread-safe music genre & archetype tracker for live or simulated audio."""

    def __init__(
        self,
        cfg: GenreConfig | None = None,
        samplerate: int = 44_100,
        *,
        async_worker: bool = True,
    ) -> None:
        self.cfg = cfg or GenreConfig()
        self.sr = max(8_000, int(samplerate))
        self.async_worker = bool(async_worker)

        cap = max(self.sr * 2, int(round(_WINDOW_SECONDS * self.sr)))
        self._ring = np.zeros(cap, dtype=np.float32)
        self._ring_write = 0
        self._ring_filled = 0
        self._ring_lock = threading.Lock()

        self._state_lock = threading.Lock()
        self._probs: dict[str, float] = {
            name: (1.0 if name == "house_tech" else 0.0) for name in ARCHETYPE_NAMES
        }
        self._has_estimate = False
        self._audio_t = 0.0
        self._last_infer_t = -999.0
        self._quiet_s = 0.0
        self._bpm_hint = 0.0
        self._kick_regularity = 0.0
        self._onset_density = 0.0

        self._onnx: _OnnxDiscogsClassifier | None = None
        self._onnx_error: str = ""
        self._init_onnx()

        self._control = self._build_initial_control()

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker: threading.Thread | None = None
        if self.async_worker and self.cfg.enabled:
            self._worker = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="genre-agent",
            )
            self._worker.start()

    def _init_onnx(self) -> None:
        custom = str(self.cfg.model_path or "").strip()
        path = Path(custom) if custom else default_model_path()
        if not path.is_file():
            self._onnx = None
            return
        try:
            self._onnx = _OnnxDiscogsClassifier(path)
        except Exception as exc:
            self._onnx = None
            self._onnx_error = str(exc)

    def _build_initial_control(self) -> GenreControlVector:
        mode = coerce_genre_mode(self.cfg.mode)
        if mode != "auto":
            return blend_control_vector({mode: 1.0}, backend="manual")
        backend = "onnx" if self._onnx is not None else "dsp"
        return blend_control_vector(self._probs, backend=backend)

    @property
    def backend_name(self) -> str:
        mode = coerce_genre_mode(self.cfg.mode)
        if not self.cfg.enabled:
            return "off"
        if mode != "auto":
            return "manual"
        return "onnx" if self._onnx is not None else "dsp"

    def control_vector(self) -> GenreControlVector:
        with self._state_lock:
            mode = coerce_genre_mode(self.cfg.mode)
            if not self.cfg.enabled:
                return GenreControlVector(backend="off")
            if mode != "auto":
                return blend_control_vector({mode: 1.0}, backend="manual")
            return self._control

    def refresh_mode(self) -> GenreControlVector:
        """Re-evaluate control vector immediately after a live mode change."""
        with self._state_lock:
            mode = coerce_genre_mode(self.cfg.mode)
            if not self.cfg.enabled:
                self._control = GenreControlVector(backend="off")
            elif mode != "auto":
                self._control = blend_control_vector({mode: 1.0}, backend="manual")
            else:
                backend = "onnx" if self._onnx is not None else "dsp"
                self._control = blend_control_vector(self._probs, backend=backend)
            return self._control

    def observe(
        self,
        block: np.ndarray,
        *,
        above_floor: bool,
        bpm: float = 0.0,
        kick_regularity: float = 0.0,
        onset_density: float = 0.0,
    ) -> GenreControlVector:
        """Feed one hop of mono audio and return the current GenreControlVector."""
        n = len(block)
        if n == 0:
            return self.control_vector()

        dt = n / self.sr
        self._audio_t += dt
        self._bpm_hint = float(bpm)
        self._kick_regularity = float(kick_regularity)
        self._onset_density = float(onset_density)

        if not above_floor:
            self._quiet_s += dt
            # After a track stop / DJ cut (>4 s silence), let the next track
            # lock its genre fast instead of dragging the previous song's EMA.
            if self._quiet_s >= 4.0:
                with self._state_lock:
                    self._has_estimate = False
            return self.control_vector()

        self._quiet_s = 0.0
        samples = np.nan_to_num(block, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
        with self._ring_lock:
            cap = len(self._ring)
            if n >= cap:
                self._ring[:] = samples[-cap:]
                self._ring_write = 0
                self._ring_filled = cap
            else:
                end = self._ring_write + n
                if end <= cap:
                    self._ring[self._ring_write : end] = samples
                else:
                    first = cap - self._ring_write
                    self._ring[self._ring_write :] = samples[:first]
                    self._ring[: end - cap] = samples[first:]
                self._ring_write = end % cap
                self._ring_filled = min(cap, self._ring_filled + n)
            filled_s = self._ring_filled / self.sr

        if not self.cfg.enabled or coerce_genre_mode(self.cfg.mode) != "auto":
            return self.control_vector()

        interval = max(1.0, float(self.cfg.interval_seconds))
        if filled_s >= _MIN_ANALYZE_SECONDS and (self._audio_t - self._last_infer_t) >= interval:
            self._last_infer_t = self._audio_t
            if self.async_worker and self._worker is not None:
                self._wake_event.set()
            else:
                self._run_inference_once()

        return self.control_vector()

    def _snapshot_audio(self) -> np.ndarray:
        with self._ring_lock:
            if self._ring_filled <= 0:
                return np.zeros(0, dtype=np.float32)
            if self._ring_filled < len(self._ring):
                return self._ring[: self._ring_filled].copy()
            idx = self._ring_write
            return np.concatenate((self._ring[idx:], self._ring[:idx]))

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=0.5)
            if self._stop_event.is_set():
                break
            if not self._wake_event.is_set():
                continue
            self._wake_event.clear()
            try:
                self._run_inference_once()
            except Exception:
                continue

    def _run_inference_once(self) -> None:
        raw = self._snapshot_audio()
        if len(raw) < int(_MIN_ANALYZE_SECONDS * self.sr):
            return
        audio_16k = resample_to_16k(raw, self.sr)
        patches, mel = compute_effnet_mel_patches(audio_16k)

        dsp_weights = classify_mel_dsp(
            mel,
            bpm_hint=self._bpm_hint,
            kick_regularity=self._kick_regularity,
            onset_density=self._onset_density,
        )

        tags: tuple[tuple[str, float], ...] = ()
        backend = "dsp"
        if self._onnx is not None:
            try:
                onnx_weights, tags = self._onnx.predict(patches)
                # Blend 85% ONNX genre semantics + 15% real-time acoustic HPSS/tempo
                # so intro/outro energy within a track still anchors accurately.
                weights = {
                    k: 0.85 * onnx_weights.get(k, 0.0) + 0.15 * dsp_weights.get(k, 0.0)
                    for k in ARCHETYPE_NAMES
                }
                backend = "onnx"
            except Exception as exc:
                self._onnx_error = str(exc)
                weights = dsp_weights
        else:
            weights = dsp_weights

        interval = max(0.5, float(self.cfg.interval_seconds))
        smooth_s = max(1.0, float(self.cfg.smooth_seconds))
        alpha = 1.0 - math.exp(-interval / smooth_s)

        with self._state_lock:
            if not self._has_estimate:
                for k in ARCHETYPE_NAMES:
                    self._probs[k] = float(weights.get(k, 0.0))
                self._has_estimate = True
            else:
                for k in ARCHETYPE_NAMES:
                    self._probs[k] += alpha * (float(weights.get(k, 0.0)) - self._probs[k])
            if coerce_genre_mode(self.cfg.mode) == "auto":
                self._control = blend_control_vector(
                    self._probs, backend=backend, genre_tags=tags
                )

    def close(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=1.5)
        self._worker = None
