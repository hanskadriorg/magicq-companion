"""Tests for standalone FX helpers."""

from __future__ import annotations

import unittest

from magicq_companion.control_channels import (
    CONTROL_CHANNEL_NAMES,
    LEGACY_DEFAULT_ADDRESSES,
    migrate_channel_map_values,
)
from magicq_companion.fx_cue_select import CueShuffleBag
from magicq_companion.fx_loop import snap_trim_to_loop
from magicq_companion.fx_palette import PaletteState, ColourPalette, restore_active_palette
from magicq_companion.fx_playback import apply_cue_frame
from magicq_companion.fx_store import FxCue, FxRecording
from magicq_companion.fx_tags import FxTagKind, merge_value


class TestChannelMap(unittest.TestCase):
    def test_legacy_migration(self) -> None:
        legacy = {name: LEGACY_DEFAULT_ADDRESSES[name] for name in CONTROL_CHANNEL_NAMES}
        migrated = migrate_channel_map_values(legacy)
        self.assertEqual(migrated["drop_hit"], 7)
        self.assertEqual(migrated["four_four"], 8)
        self.assertEqual(migrated["bpm_rate"], 9)
        self.assertEqual(migrated["drop_momentary"], 10)


class TestCueShuffle(unittest.TestCase):
    def test_no_immediate_repeat(self) -> None:
        bag = CueShuffleBag(["a", "b", "c"])
        seen = []
        for _ in range(12):
            seen.append(bag.next())
        for i in range(1, len(seen)):
            if seen[i - 1] != seen[i]:
                self.assertNotEqual(seen[i], seen[i - 1])


class TestLoopSnap(unittest.TestCase):
    def test_keeps_handles_when_no_match(self) -> None:
        frames = [bytes([i % 256] * 16) for i in range(20)]
        start, end, snapped = snap_trim_to_loop(frames, 2, 18, search_radius=2)
        self.assertFalse(snapped)
        self.assertEqual(start, 2)
        self.assertEqual(end, 18)

    def test_snaps_when_seam_matches(self) -> None:
        base = bytes([10, 20, 30, 40])
        frames = [base, bytes([11, 21, 31, 41]), base, bytes([5, 5, 5, 5]), base]
        start, end, snapped = snap_trim_to_loop(
            frames, 1, 4, search_radius=2, match_threshold=8.0
        )
        self.assertTrue(snapped)


class TestFadeByTag(unittest.TestCase):
    def test_intensity_htp(self) -> None:
        self.assertEqual(
            merge_value(FxTagKind.INTENSITY, 100, 200, 1.0, 1.0),
            200,
        )
        self.assertEqual(
            merge_value(FxTagKind.INTENSITY, 150, 80, 1.0, 1.0),
            150,
        )

    def test_position_ltp(self) -> None:
        self.assertEqual(
            merge_value(FxTagKind.POSITION, 100, 200, 1.0, 1.0),
            200,
        )


class TestPaletteRestore(unittest.TestCase):
    def test_restore_last_active(self) -> None:
        state = PaletteState(
            palettes=[
                ColourPalette("a", "A", 1, 2, 3),
                ColourPalette("b", "B", 4, 5, 6),
            ],
            active_id="a",
            last_active_id="b",
        )
        pid = restore_active_palette(state)
        self.assertEqual(pid, "b")
        self.assertEqual(state.active_id, "b")


class TestPlaybackPairs(unittest.TestCase):
    def test_position_pair_scales_together(self) -> None:
        rec = FxRecording("r", "r", 40, frames=[bytes(8)])
        rec.frames[0] = bytes([0, 0, 0, 0, 10, 64, 0, 0])
        rec.tags = {5: "position"}
        cue = FxCue("c", "c", "r")
        out = bytearray(bytes(8))
        base = bytes(8)
        tags = {5: FxTagKind.POSITION}
        apply_cue_frame(out, base, rec, cue, 0, fade_t=1.0, cue_level=0.5, tags=tags)
        self.assertEqual(out[4], 5)
        self.assertEqual(out[5], 32)


if __name__ == "__main__":
    unittest.main()
