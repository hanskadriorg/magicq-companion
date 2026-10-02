"""Tests for live audio feature extraction."""

from __future__ import annotations

import math
import unittest

import numpy as np

from magicq_companion.config import BpmRateConfig, BuildScoreConfig, DetectionConfig
from magicq_companion.features import FeatureExtractor


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


if __name__ == "__main__":
    unittest.main()
