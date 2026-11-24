"""Tests for Two-Layer GenrePolicy, NLMS online profile learning, and fx_size DSP."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from magicq_companion.config import (
    BpmRateConfig,
    BuildScoreConfig,
    ChannelMap,
    DetectionConfig,
    FxSizeConfig,
)
from magicq_companion.dmx import build_frame
from magicq_companion.features import FeatureExtractor
from magicq_companion.genre_profiles import DEFAULT_GENRE_POLICIES, GenreProfileStore
from magicq_companion.state_machine import Section, SectionTracker


class TestGenreProfilesAndFxSize(unittest.TestCase):
    def test_nlms_online_learning_matches_target_and_persists_per_genre(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "genre_profiles.json"
            store = GenreProfileStore(path, learning_enabled=True)

            # Suppose Deep/Warm Groove (75%) + House/Tech (25%) is playing.
            probs = {"groovy_warm": 0.75, "house_tech": 0.25}
            before_chill = store.profile_for("ambient_chill")
            before_peak = store.profile_for("club_peak")

            updated, credited = store.learn_override(
                probs,
                {
                    "fx_size_base": 125,
                    "fx_size_max": 248,
                    "drop_intensity": 0.70,
                    "bpm_rate_min": "quarter",
                    "bpm_rate_max": "full",
                },
            )
            self.assertAlmostEqual(updated.fx_size_base, 125.0, places=1)
            self.assertAlmostEqual(updated.fx_size_max, 248.0, places=1)
            self.assertAlmostEqual(updated.drop_intensity, 0.70, places=2)
            self.assertEqual([k for k, _ in credited], ["groovy_warm", "house_tech"])

            # Unrelated genres must remain completely untouched.
            self.assertEqual(store.profile_for("ambient_chill"), before_chill)
            self.assertEqual(store.profile_for("club_peak"), before_peak)
            self.assertEqual(store.edit_counts()["ambient_chill"], 0)
            self.assertEqual(store.edit_counts()["groovy_warm"], 1)

            # Reload from disk and confirm the learned profile is recalled next time.
            reloaded = GenreProfileStore(path, learning_enabled=True)
            recalled = reloaded.resolve(probs)
            self.assertAlmostEqual(recalled.fx_size_base, 125.0, places=1)
            self.assertAlmostEqual(recalled.drop_intensity, 0.70, places=2)

            # Reset groovy_warm and confirm factory defaults return.
            reloaded.reset_profiles("groovy_warm")
            self.assertEqual(
                reloaded.profile_for("groovy_warm"),
                DEFAULT_GENRE_POLICIES["groovy_warm"],
            )
            self.assertEqual(reloaded.edit_counts()["groovy_warm"], 0)
            self.assertEqual(reloaded.edit_counts()["house_tech"], 1)

    def test_fx_size_responds_to_breakdown_groove_energy_surge_and_drop(self) -> None:
        sr, hop = 44_100, 512
        extractor = FeatureExtractor(
            samplerate=sr,
            hop=hop,
            detection=DetectionConfig(),
            build_cfg=BuildScoreConfig(),
            bpm_rate_cfg=BpmRateConfig(),
            fx_size_cfg=FxSizeConfig(base=90, max=240, reactivity=0.80),
        )
        tracker = SectionTracker(DetectionConfig(), extractor.fps)
        channels = ChannelMap()
        dmx = bytearray(512)
        policy = DEFAULT_GENRE_POLICIES["house_tech"]

        t = np.arange(sr * 8) / sr
        period = 60.0 / 125.0
        phase = t % period
        groove_audio = (
            0.45 * np.sin(2 * np.pi * 56.0 * t) * np.exp(-phase / 0.06) * (phase < 0.22)
            + 0.12 * np.sin(2 * np.pi * 240.0 * t)
        )

        # 1. Warm up steady groove (~4s).
        f_groove = None
        for idx in range(0, int(4.0 * sr) - hop, hop):
            f_groove = extractor.process(
                groove_audio[idx : idx + hop], policy=policy, section="groove"
            )
        assert f_groove is not None

        # 2. Sudden energy surge inside groove (~1s at 1.8x amplitude) -> fx_size bumps up!
        f_surge = None
        for idx in range(int(4.0 * sr), int(5.0 * sr) - hop, hop):
            f_surge = extractor.process(
                np.clip(groove_audio[idx : idx + hop] * 1.8, -0.98, 0.98),
                policy=policy,
                section="groove",
            )
        assert f_surge is not None
        self.assertGreater(f_surge.fx_size, f_groove.fx_size + 25.0)

        # 3. Drop section (~0.3s) -> fast 65 ms attack slams fx_size near policy.fx_size_max!
        f_drop = None
        for idx in range(int(5.0 * sr), int(5.35 * sr) - hop, hop):
            f_drop = extractor.process(
                np.clip(groove_audio[idx : idx + hop] * 1.8, -0.98, 0.98),
                policy=policy,
                section="drop",
            )
        assert f_drop is not None
        self.assertGreaterEqual(f_drop.fx_size, policy.fx_size_max * 0.85)

        tracker.state = Section.DROP
        build_frame(dmx, channels, f_drop, tracker, DetectionConfig())
        self.assertEqual(dmx[channels.fx_size - 1], int(round(f_drop.fx_size)))

        # 4. Breakdown section -> smooth release glides down calmly (no instant snap to 0).
        pad_only = 0.10 * np.sin(2 * np.pi * 240.0 * t[: int(3.5 * sr)])
        f_break = None
        for idx in range(0, len(pad_only) - hop, hop):
            f_break = extractor.process(
                pad_only[idx : idx + hop], policy=policy, section="breakdown"
            )
        assert f_break is not None
        self.assertLess(f_break.fx_size, f_groove.fx_size)
        self.assertGreater(f_break.fx_size, 15.0)

    def test_two_layer_isolation_preserves_fast_beat_and_drop_sensing(self) -> None:
        sr, hop = 44_100, 512
        det = DetectionConfig(drop_rise_window=0.33, drop_smooth_seconds=0.05)
        extractor = FeatureExtractor(sr, hop, det, BuildScoreConfig())
        tracker = SectionTracker(det, extractor.fps)

        t = np.arange(sr * 5) / sr
        period = 60.0 / 128.0
        phase = t % period
        sig = 0.6 * np.sin(2 * np.pi * 55.0 * t) * np.exp(-phase / 0.06) * (phase < 0.22)

        chill_pol = DEFAULT_GENRE_POLICIES["ambient_chill"]
        peak_pol = DEFAULT_GENRE_POLICIES["club_peak"]

        for idx in range(0, int(3.0 * sr) - hop, hop):
            f = extractor.process(sig[idx : idx + hop], policy=chill_pol, section="groove")
            tracker.update(f, hop / sr, policy=chill_pol)

        bpm_before = f.bpm
        phase_before = extractor._grid_phase
        self.assertGreater(bpm_before, 120.0)

        # Switch Layer 1 policy abruptly to club_peak: Layer 2 PLL/BPM & fast drop
        # config must remain intact with zero reset or interruption.
        f_next = extractor.process(sig[int(3.0 * sr) : int(3.0 * sr) + hop], policy=peak_pol, section="groove")
        tracker.update(f_next, hop / sr, policy=peak_pol)

        self.assertAlmostEqual(f_next.bpm, bpm_before, places=1)
        self.assertNotEqual(extractor._grid_phase, 0.0)
        self.assertNotEqual(extractor._grid_phase, phase_before)
        self.assertEqual(det.drop_rise_window, 0.33)
        self.assertEqual(det.drop_smooth_seconds, 0.05)
        self.assertAlmostEqual(tracker.drop_intensity, peak_pol.drop_intensity, places=2)


if __name__ == "__main__":
    unittest.main()
