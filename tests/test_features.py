"""Tests for live audio feature extraction, true BPM, and Intrinsic Pace Score."""

from __future__ import annotations

import math
import unittest

import numpy as np

from magicq_companion.config import BpmRateConfig, BuildScoreConfig, DetectionConfig
from magicq_companion.features import FeatureExtractor
from magicq_companion.genre_agent import ARCHETYPE_SPECS, blend_control_vector


def _render_slow_rnb(sr: int, seconds: float, bpm: float = 80.0, gain: float = 0.7) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    period = 60.0 / bpm
    phase = t % period
    kick = 0.55 * np.sin(2 * np.pi * 54.0 * t) * np.exp(-phase / 0.065) * (phase < 0.25)
    pad = 0.30 * (np.sin(2 * np.pi * 220.0 * t) + 0.6 * np.sin(2 * np.pi * 277.2 * t))
    rng = np.random.default_rng(42)
    hat = 0.02 * rng.standard_normal(len(t)) * np.exp(-phase / 0.015) * (phase < 0.04)
    return np.clip((kick + pad + hat) * gain, -0.98, 0.98)


def _render_compressed_techno(sr: int, seconds: float, bpm: float = 138.0) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    period = 60.0 / bpm
    k_phase = t % period
    kick = 1.1 * np.sin(2 * np.pi * 56.0 * t) * np.exp(-k_phase / 0.055) * (k_phase < 0.22)
    h_phase = t % (period / 4.0)
    rng = np.random.default_rng(99)
    hats = 0.32 * rng.standard_normal(len(t)) * np.exp(-h_phase / 0.014) * (h_phase < 0.045)
    bass = 0.25 * np.sin(2 * np.pi * 82.0 * t)
    # Soft-clip / brickwall limit to simulate flat mastered club audio.
    return np.tanh((kick + hats + bass) * 1.8) * 0.85


class TestFeatureExtractor(unittest.TestCase):
    def test_non_finite_capture_block_is_treated_as_silence(self) -> None:
        extractor = FeatureExtractor(
            samplerate=44_100,
            hop=512,
            detection=DetectionConfig(),
            build_cfg=BuildScoreConfig(),
            bpm_rate_cfg=BpmRateConfig(),
        )
        block = np.full(512, np.nan)
        block[0] = np.inf
        block[1] = -np.inf

        features = extractor.process(block)

        self.assertTrue(math.isfinite(features.rms))
        self.assertTrue(math.isfinite(features.energy_norm))
        self.assertTrue(math.isfinite(features.pace_score))
        self.assertTrue(all(isinstance(value, int) for value in features.spectrum_db))

    def test_spectrum_db_survives_non_finite_magnitudes(self) -> None:
        extractor = FeatureExtractor(
            samplerate=44_100,
            hop=512,
            detection=DetectionConfig(),
            build_cfg=BuildScoreConfig(),
            bpm_rate_cfg=BpmRateConfig(),
        )
        mag = np.ones(extractor.win // 2 + 1, dtype=np.float64)
        mag[3] = np.nan
        mag[5] = np.inf
        spectrum = extractor._spectrum_db(mag)
        self.assertTrue(spectrum)
        self.assertTrue(all(isinstance(value, int) for value in spectrum))
        self.assertTrue(all(-99 <= value <= 6 for value in spectrum))

    def test_slow_track_keeps_true_80_bpm_and_low_chase_rate(self) -> None:
        sr, hop = 44_100, 512
        extractor = FeatureExtractor(
            samplerate=sr,
            hop=hop,
            detection=DetectionConfig(),
            build_cfg=BuildScoreConfig(),
            bpm_rate_cfg=BpmRateConfig(min="eighth", max="full", decay_seconds=1.0),
        )
        genre_vec = blend_control_vector({"hiphop_urban": 0.7, "ambient_chill": 0.3})
        audio = _render_slow_rnb(sr, seconds=65.0, bpm=80.0, gain=0.85)

        rates_after_warmup: list[float] = []
        last_features = None
        for idx in range(0, len(audio) - hop, hop):
            last_features = extractor.process(
                audio[idx : idx + hop],
                genre=genre_vec,
                section="groove",
            )
            if extractor._t >= 20.0:
                rates_after_warmup.append(last_features.bpm_rate)

        assert last_features is not None
        # Must stay near 80 BPM, never octave-doubled to 160 BPM.
        self.assertGreaterEqual(last_features.bpm, 76.0)
        self.assertLessEqual(last_features.bpm, 84.0)
        self.assertLess(last_features.pace_score, 0.45)
        # Even after 65s of loud steady RMS, slow groove must never hit 255 (full).
        self.assertTrue(all(r in (32.0, 64.0) for r in rates_after_warmup))

    def test_compressed_fast_club_track_maintains_high_pace_and_chase_rate(self) -> None:
        sr, hop = 44_100, 512
        extractor = FeatureExtractor(
            samplerate=sr,
            hop=hop,
            detection=DetectionConfig(),
            build_cfg=BuildScoreConfig(),
            bpm_rate_cfg=BpmRateConfig(min="eighth", max="full", decay_seconds=3.0),
        )
        genre_vec = blend_control_vector({"club_peak": 1.0})
        audio = _render_compressed_techno(sr, seconds=65.0, bpm=138.0)

        rates_tail: list[float] = []
        last_features = None
        for idx in range(0, len(audio) - hop, hop):
            last_features = extractor.process(
                audio[idx : idx + hop],
                genre=genre_vec,
                section="groove",
            )
            if extractor._t >= 45.0:
                rates_tail.append(last_features.bpm_rate)

        assert last_features is not None
        self.assertAlmostEqual(last_features.bpm, 138.0, delta=4.0)
        self.assertGreaterEqual(last_features.pace_score, 0.68)
        # Despite flat brickwall RMS after 45s baseline adaptation, chase stays fast.
        self.assertTrue(all(r in (128.0, 255.0) for r in rates_tail))

    def test_dnb_caps_groove_at_half_and_unlocks_full_on_drop(self) -> None:
        sr, hop = 44_100, 512
        extractor = FeatureExtractor(
            samplerate=sr,
            hop=hop,
            detection=DetectionConfig(),
            build_cfg=BuildScoreConfig(),
            bpm_rate_cfg=BpmRateConfig(min="eighth", max="full", decay_seconds=0.0),
        )
        dnb_vec = blend_control_vector({"fast_bass": 1.0})
        self.assertEqual(ARCHETYPE_SPECS["fast_bass"].groove_rate_max, 2.0)
        audio = _render_compressed_techno(sr, seconds=18.0, bpm=174.0)

        groove_f = None
        for idx in range(0, int(12.0 * sr) - hop, hop):
            groove_f = extractor.process(audio[idx : idx + hop], genre=dnb_vec, section="groove")
        drop_f = None
        for idx in range(int(12.0 * sr), len(audio) - hop, hop):
            drop_f = extractor.process(audio[idx : idx + hop], genre=dnb_vec, section="drop")

        assert groove_f is not None and drop_f is not None
        self.assertAlmostEqual(groove_f.bpm, 174.0, delta=5.0)
        self.assertEqual(groove_f.bpm_rate, 128.0)
        self.assertEqual(drop_f.bpm_rate, 255.0)

        # Switching to ambient_chill must clamp bpm_rate <= 64 immediately,
        # without waiting out a slow decay_seconds hold from 255.
        extractor.bpm_rate_cfg = BpmRateConfig(min="eighth", max="full", decay_seconds=15.0)
        chill_vec = blend_control_vector({"ambient_chill": 1.0})
        chill_f = extractor.process(audio[:hop], genre=chill_vec, section="drop")
        self.assertLessEqual(chill_f.bpm_rate, 64.0)


if __name__ == "__main__":
    unittest.main()
