"""Tests for the offline ONNX Genre Agent, Mel frontend, and archetype blending."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from magicq_companion.config import ARCHETYPE_NAMES, GenreConfig
from magicq_companion.genre_agent import (
    GenreAgent,
    blend_control_vector,
    compute_effnet_mel_patches,
    map_discogs_label_to_archetype,
    resample_to_16k,
)


class TestGenreAgent(unittest.TestCase):
    def test_mel_frontend_shape_and_numerical_stability(self) -> None:
        for sr in (44_100, 48_000):
            raw = np.sin(2 * np.pi * 440.0 * np.arange(sr * 6) / sr)
            raw[10] = np.nan
            raw[20] = np.inf
            a16 = resample_to_16k(raw, sr)
            self.assertEqual(len(a16), 16_000 * 6)
            patches, mel = compute_effnet_mel_patches(a16)
            self.assertEqual(patches.ndim, 3)
            self.assertEqual(patches.shape[1:], (128, 96))
            self.assertGreaterEqual(patches.shape[0], 4)
            self.assertEqual(mel.shape[1], 96)
            self.assertTrue(np.isfinite(patches).all())

    def test_discogs_400_label_mapping_covers_venue_genres(self) -> None:
        cases = {
            "Electronic---Deep House": "groovy_warm",
            "Funk / Soul---Disco": "groovy_warm",
            "Electronic---Tech House": "house_tech",
            "Electronic---House": "house_tech",
            "Electronic---Progressive Trance": "melodic_prog",
            "Electronic---Techno": "club_peak",
            "Electronic---Hard Techno": "club_peak",
            "Hip Hop---Trap": "hiphop_urban",
            "Hip Hop---RnB/Swing": "hiphop_urban",
            "Latin---Reggaeton": "hiphop_urban",
            "Electronic---Drum n Bass": "fast_bass",
            "Electronic---UK Garage": "fast_bass",
            "Electronic---Ambient": "ambient_chill",
            "Electronic---Downtempo": "ambient_chill",
            "Rock---Indie Rock": "rock_live",
        }
        for label, expected in cases.items():
            self.assertEqual(map_discogs_label_to_archetype(label), expected, label)

    def test_blend_control_vector_interpolates_smoothly(self) -> None:
        vec = blend_control_vector({"groovy_warm": 0.5, "club_peak": 0.5})
        self.assertGreater(vec.arousal_prior, 0.55)
        self.assertLess(vec.arousal_prior, 0.75)
        self.assertTrue(all(hasattr(vec, attr) for attr in ("natural_bpm_lo", "syncopation_tolerance")))

    def test_onnx_backend_integration_with_mock_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            model_file = tmp_path / "discogs-effnet-bsdynamic-1.onnx"
            model_file.write_bytes(b"fake-onnx")
            classes = [
                "Electronic---Deep House",
                "Electronic---Afro House",
                "Electronic---Techno",
                "Hip Hop---Trap",
            ]
            (tmp_path / "discogs-effnet-bsdynamic-1.json").write_text(
                json.dumps({"classes": classes}), encoding="utf-8"
            )

            class _FakeMeta:
                def __init__(self, name: str, shape: list[int | None]) -> None:
                    self.name = name
                    self.shape = shape

            class _FakeSession:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def get_inputs(self):
                    return [_FakeMeta("melspectrogram", [None, 128, 96])]

                def get_outputs(self):
                    return [
                        _FakeMeta("activations", [None, 4]),
                        _FakeMeta("embeddings", [None, 1280]),
                    ]

                def run(self, output_names, feed):
                    self.last_shape = feed["melspectrogram"].shape
                    batch = self.last_shape[0]
                    probs = np.tile(np.array([[0.68, 0.20, 0.07, 0.05]], dtype=np.float32), (batch, 1))
                    return [probs]

            fake_ort = types.SimpleNamespace(
                SessionOptions=lambda: types.SimpleNamespace(intra_op_num_threads=1, inter_op_num_threads=1),
                InferenceSession=_FakeSession,
            )

            with mock.patch.dict(sys.modules, {"onnxruntime": fake_ort}):
                cfg = GenreConfig(
                    enabled=True,
                    mode="auto",
                    interval_seconds=2.0,
                    smooth_seconds=4.0,
                    model_path=str(model_file),
                )
                agent = GenreAgent(cfg, samplerate=44_100, async_worker=False)
                self.assertEqual(agent.backend_name, "onnx")

                sr = 44_100
                sig = 0.4 * np.sin(2 * np.pi * 110.0 * np.arange(sr * 4) / sr)
                for idx in range(0, len(sig), 512):
                    vec = agent.observe(
                        sig[idx : idx + 512],
                        above_floor=True,
                        bpm=121.0,
                        kick_regularity=0.8,
                        onset_density=2.5,
                    )
                self.assertEqual(vec.backend, "onnx")
                self.assertEqual(vec.archetype, "groovy_warm")
                self.assertIn("Deep House", vec.formatted_tags())
                agent.close()

    def test_manual_mode_locks_archetype(self) -> None:
        cfg = GenreConfig(enabled=True, mode="ambient_chill")
        agent = GenreAgent(cfg, samplerate=44_100, async_worker=False)
        self.assertEqual(agent.backend_name, "manual")
        self.assertEqual(agent.control_vector().archetype, "ambient_chill")
        self.assertIn("ambient_chill", ARCHETYPE_NAMES)
        agent.close()


if __name__ == "__main__":
    unittest.main()
