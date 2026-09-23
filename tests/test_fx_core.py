"""Tests for standalone FX helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from magicq_companion.control_channels import (
    CONTROL_CHANNEL_NAMES,
    LEGACY_DEFAULT_ADDRESSES,
    migrate_channel_map_values,
)
from magicq_companion.fx_cue_select import CueShuffleBag
from magicq_companion.fx_loop import snap_trim_to_loop
from magicq_companion.fx_palette import (
    ColourPalette,
    PaletteState,
    restore_active_palette,
    sample_rgb_from_frame,
)
from magicq_companion.fx_patch import CsvPatchSource
from magicq_companion.fx_patch_csv import (
    PatchCsvError,
    colour_component,
    load_imported_patch,
    parse_dmx_token,
    parse_patch_csv,
    save_imported_patch,
)
from magicq_companion.fx_playback import apply_cue_frame
from magicq_companion.fx_store import FxCue, FxRecording
from magicq_companion.fx_tags import FadeMerge, FxTagKind, merge_value


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

    def test_merge_override_ltp_on_intensity_kind(self) -> None:
        self.assertEqual(
            merge_value(
                FxTagKind.INTENSITY, 150, 80, 1.0, 1.0, merge=FadeMerge.LTP
            ),
            80,
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


class TestPaletteSample(unittest.TestCase):
    def test_colour_component_names(self) -> None:
        self.assertEqual(colour_component("Red"), "r")
        self.assertEqual(colour_component("Green"), "g")
        self.assertEqual(colour_component("Blue"), "b")
        self.assertEqual(colour_component("Cyan"), "c")
        self.assertIsNone(colour_component("Dimmer"))
        self.assertIsNone(colour_component("Pan"))

    def test_sample_rgb_from_patch_names(self) -> None:
        text = (
            "head_name,dmx,channel_name,merge\n"
            "Bulb,1-010,Red,ltp\n"
            "Bulb,1-011,Green,ltp\n"
            "Bulb,1-012,Blue,ltp\n"
        )
        patch = parse_patch_csv(text)
        frame = bytearray(512)
        frame[9] = 200
        frame[10] = 100
        frame[11] = 50
        rgb = sample_rgb_from_frame(bytes(frame), patch=patch)
        self.assertEqual(rgb, (200, 100, 50))

    def test_sample_cmy_converts(self) -> None:
        text = (
            "head_name,dmx,channel_name,merge\n"
            "Mac,1-004,Cyan,ltp\n"
            "Mac,1-005,Magenta,ltp\n"
            "Mac,1-006,Yellow,ltp\n"
        )
        patch = parse_patch_csv(text)
        frame = bytearray(512)
        frame[3] = 0
        frame[4] = 55
        frame[5] = 255
        rgb = sample_rgb_from_frame(bytes(frame), patch=patch)
        self.assertEqual(rgb, (255, 200, 0))


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


class TestPatchCsv(unittest.TestCase):
    def test_parse_dmx_forms(self) -> None:
        self.assertEqual(parse_dmx_token("1-001"), (1, 1))
        self.assertEqual(parse_dmx_token("2/023"), (2, 23))
        self.assertEqual(parse_dmx_token("1.512"), (1, 512))

    def test_parse_universe_address_columns(self) -> None:
        text = (
            "head_name,universe,address,channel_offset,channel_name,merge\n"
            "MAC 700 1,1,1,1,Dimmer,HTP\n"
            "MAC 700 1,1,23,23,Pan,LTP\n"
        )
        patch = parse_patch_csv(text, source_name="rig.csv")
        self.assertEqual(len(patch.channels), 2)
        self.assertEqual(patch.channels[0].merge, FadeMerge.HTP)
        self.assertEqual(patch.channels[1].merge, FadeMerge.LTP)
        self.assertEqual(patch.channels[1].address, 23)
        merges = patch.channel_merges()
        self.assertEqual(merges[1], FadeMerge.HTP)
        self.assertEqual(merges[23], FadeMerge.LTP)
        tags = patch.channel_tags()
        self.assertEqual(tags[1], FxTagKind.INTENSITY)
        self.assertNotIn(23, tags)
        summary = patch.summary()
        self.assertEqual(summary["heads"], 1)
        self.assertEqual(summary["htp"], 1)
        self.assertEqual(summary["ltp"], 1)
        self.assertEqual(len(summary["entries"]), 2)
        self.assertEqual(summary["entries"][0]["merge"], "HTP")
        self.assertEqual(summary["entries"][1]["dmx"], "1-023")

    def test_parse_dmx_column(self) -> None:
        text = (
            "head_name,dmx,channel_name,merge\n"
            "FP5 401,1-100,Red,ltp\n"
            "Dimmer 1,1-200,Dimmer,htp\n"
        )
        patch = parse_patch_csv(text)
        self.assertEqual(patch.channels[0].universe, 1)
        self.assertEqual(patch.channels[0].address, 100)
        self.assertEqual(patch.channels[1].address, 200)
        tags = patch.channel_tags()
        self.assertEqual(tags[100], FxTagKind.COLOUR)
        self.assertEqual(tags[200], FxTagKind.INTENSITY)

    def test_reject_bad_merge(self) -> None:
        text = "head_name,universe,address,merge\nA,1,1,MIX\n"
        with self.assertRaises(PatchCsvError) as ctx:
            parse_patch_csv(text)
        self.assertIn("HTP or LTP", str(ctx.exception))
        self.assertIn("line 2", str(ctx.exception))

    def test_reject_bad_address(self) -> None:
        text = "head_name,universe,address,merge\nA,1,999,HTP\n"
        with self.assertRaises(PatchCsvError) as ctx:
            parse_patch_csv(text)
        self.assertIn("1–512", str(ctx.exception))

    def test_reject_missing_columns(self) -> None:
        with self.assertRaises(PatchCsvError):
            parse_patch_csv("head_name,merge\nA,HTP\n")

    def test_persist_roundtrip(self) -> None:
        text = (
            "head_name,dmx,merge\n"
            "Head A,1-010,HTP\n"
            "Head B,1-020,LTP\n"
        )
        patch = parse_patch_csv(text, source_name="x.csv")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fx_patch.json"
            save_imported_patch(path, patch)
            loaded = load_imported_patch(path)
        self.assertEqual(len(loaded.channels), 2)
        self.assertEqual(loaded.source_name, "x.csv")
        self.assertEqual(loaded.channel_merges()[10], FadeMerge.HTP)

    def test_csv_patch_source_drives_playback_merge(self) -> None:
        text = (
            "head_name,universe,address,merge\n"
            "D,1,1,HTP\n"
            "P,1,2,LTP\n"
        )
        src = CsvPatchSource(parse_patch_csv(text))
        rec = FxRecording("r", "r", 40, frames=[bytes([50, 40])])
        cue = FxCue("c", "c", "r")
        out = bytearray([100, 10])
        base = bytes([100, 10])
        apply_cue_frame(
            out,
            base,
            rec,
            cue,
            0,
            fade_t=1.0,
            cue_level=1.0,
            tags=src.channel_tags(),
            merges=src.channel_merges(),
        )
        self.assertEqual(out[0], 100)
        self.assertEqual(out[1], 40)

    def test_example_file_parses(self) -> None:
        example = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "examples"
            / "patch_example.csv"
        )
        text = example.read_text(encoding="utf-8")
        patch = parse_patch_csv(text)
        self.assertGreaterEqual(patch.summary()["heads"], 2)
        self.assertGreaterEqual(patch.summary()["channels"], 4)

    def test_magicq_view_chans_trailing_spaces_and_blank_rows(self) -> None:
        # Headers/cells mimic MagicQ View Chans (trailing spaces); blank
        # Type+Attrib rows are the unused-universe dump and must be skipped.
        text = (
            "DMX ,Head type ,Attrib ,Chan name ,Hd no ,Type\n"
            "1-001 ,Generic Dimmer ,Dimmer ,House ,1 ,HTP \n"
            "1-010 ,Chauvet ,Red ,FP7LunaBulb ,1 ,LTP \n"
            "1-002 ,,,,,\n"
            "1-003 ,SomeHead ,,,,\n"
            "1-023 ,MAC 700 Profile ,Pan ,,2 ,LTP \n"
        )
        patch = parse_patch_csv(text, source_name="view_chans.csv")
        self.assertEqual(len(patch.channels), 3)
        self.assertEqual(patch.channels[0].head_name, "House 1")
        self.assertEqual(patch.channels[0].channel_name, "Dimmer")
        self.assertEqual(patch.channels[0].merge, FadeMerge.HTP)
        self.assertEqual(patch.channels[0].address, 1)
        self.assertEqual(patch.channels[1].head_name, "FP7LunaBulb 1")
        self.assertEqual(patch.channels[1].channel_name, "Red")
        self.assertEqual(patch.channels[1].merge, FadeMerge.LTP)
        self.assertEqual(patch.channels[1].address, 10)
        # Chan name empty → Head type + Hd no
        self.assertEqual(patch.channels[2].head_name, "MAC 700 Profile 2")
        self.assertEqual(patch.channels[2].channel_name, "Pan")
        self.assertEqual(patch.summary()["heads"], 3)

    def test_magicq_view_chans_example_file(self) -> None:
        example = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "examples"
            / "patch_magicq_view_chans.csv"
        )
        text = example.read_text(encoding="utf-8")
        patch = parse_patch_csv(text)
        self.assertEqual(len(patch.channels), 5)
        self.assertIn("FP7LunaBulb 1", patch.summary()["head_names"])
        self.assertIn("MAC 700 Profile 2", patch.summary()["head_names"])
        self.assertEqual(patch.channel_merges()[1], FadeMerge.HTP)
        self.assertEqual(patch.channel_merges()[10], FadeMerge.LTP)


if __name__ == "__main__":
    unittest.main()
