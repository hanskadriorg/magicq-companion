"""Standalone FX runtime: record console DMX, play cues on control channels."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .control_channels import CONTROL_CHANNEL_NAMES
from .dmx_receive import DmxReceiver
from .fx_cue_select import CueShuffleBag
from .fx_loop import detect_loop_window
from .fx_palette import (
    PALETTE_FADE_SECONDS,
    PaletteState,
    apply_rgb_to_colour_channels,
    blend_rgb,
    load_palette_state,
    new_palette_from_rgb,
    restore_active_palette,
    sample_rgb_from_frame,
    save_palette_state,
)
from .fx_patch import CsvPatchSource, ManualPatchSource, PatchSource
from .fx_patch_csv import ImportedPatch, load_imported_patch, save_imported_patch
from .fx_playback import apply_cue_frame, cue_fade_level
from .fx_store import (
    FxChannelAssignment,
    FxRecording,
    load_fx_library,
    save_fx_library,
    tags_as_kinds,
)
from .output import create_sender

DEFAULT_BPM = 128.0


@dataclass
class _ActiveCue:
    cue_id: str
    started: float
    release_at: float | None = None


@dataclass
class _ChannelPlayback:
    shuffle: CueShuffleBag
    active: _ActiveCue | None = None
    button_down: bool = False
    cue_ids: tuple[str, ...] = ()


@dataclass
class FxEngineState:
    recording: bool = False
    record_frames: list[bytes] = field(default_factory=list)
    record_started: float = 0.0
    loop_detected_at: int | None = None
    bpm: float = DEFAULT_BPM
    universe_warning: str | None = None


class FxEngine:
    def __init__(
        self,
        *,
        library_path,
        palette_path,
        output_cfg,
        input_universe: int,
        output_universe: int,
        channel_map: dict[str, int],
        dry_run: bool = False,
        record_fps: float = 40.0,
        patch_path=None,
        input_ip: str = "",
    ) -> None:
        self.library_path = library_path
        self.palette_path = palette_path
        self.patch_path = patch_path
        self.output_cfg = output_cfg
        self.input_universe = int(input_universe)
        self.input_ip = (input_ip or "").strip()
        self.output_universe = int(output_universe)
        self.channel_map = dict(channel_map)
        self.dry_run = dry_run
        self.record_fps = record_fps

        self.library = load_fx_library(library_path)
        self.palette_state = load_palette_state(palette_path)
        restore_active_palette(self.palette_state)
        self.imported_patch = (
            load_imported_patch(patch_path) if patch_path is not None else ImportedPatch()
        )
        self.patch = self._build_patch_source()

        self.state = FxEngineState()
        self._refresh_universe_warning()
        self._last_good_bpm = DEFAULT_BPM
        self.state.bpm = DEFAULT_BPM

        self._dmx_in = bytearray(512)
        self._dmx_out = bytearray(512)
        self._lock = threading.Lock()
        self._sender = None
        self._receiver: DmxReceiver | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._channels: dict[int, _ChannelPlayback] = {}
        self._palette_from: tuple[int, int, int] = (0, 0, 0)
        self._palette_to: tuple[int, int, int] = self._palette_rgb_target()
        self._palette_t0 = time.monotonic()
        self._palette_suppressed = False
        self._on_state: Callable[[dict], None] | None = None
        self._last_input_mono = 0.0
        self._input_packets = 0
        self._fps_window_t0 = time.monotonic()
        self._fps_window_count = 0
        self._input_fps = 0.0

    def _refresh_universe_warning(self) -> None:
        if self.input_universe == self.output_universe:
            self.state.universe_warning = (
                "Record on a universe Companion is not transmitting on."
            )
        else:
            self.state.universe_warning = None

    def _build_patch_source(self) -> PatchSource:
        if self.imported_patch.channels:
            return CsvPatchSource(self.imported_patch)
        return ManualPatchSource(tags_as_kinds(self.library.manual_tags))

    def set_imported_patch(self, patch: ImportedPatch) -> None:
        self.imported_patch = patch
        self.patch = self._build_patch_source()
        if self.patch_path is not None:
            save_imported_patch(self.patch_path, patch)

    def clear_imported_patch(self) -> None:
        from pathlib import Path

        self.imported_patch = ImportedPatch()
        self.patch = ManualPatchSource(tags_as_kinds(self.library.manual_tags))
        if self.patch_path is not None:
            Path(self.patch_path).unlink(missing_ok=True)

    def _palette_rgb_target(self) -> tuple[int, int, int]:
        for p in self.palette_state.palettes:
            if p.id == self.palette_state.active_id:
                return (p.red, p.green, p.blue)
        return (255, 255, 255)

    def set_bpm(self, bpm: float) -> None:
        if bpm and bpm > 10:
            self._last_good_bpm = float(bpm)
        self.state.bpm = self._last_good_bpm

    def save_all(self) -> None:
        save_fx_library(self.library_path, self.library)
        save_palette_state(self.palette_path, self.palette_state)
        if self.patch_path is not None and self.imported_patch.channels:
            save_imported_patch(self.patch_path, self.imported_patch)

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self.dry_run:
            self._sender = create_sender(self.output_cfg)
        self._start_receiver()
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick, name="fx-engine", daemon=True)
        self._thread.start()

    def _receiver_port(self) -> int:
        return 6454 if self.output_cfg.protocol != "sacn" else 5568

    def _start_receiver(self) -> None:
        if self._receiver is not None:
            self._receiver.stop()
            self._receiver = None
        self._receiver = DmxReceiver(
            port=self._receiver_port(),
            interface=self.output_cfg.interface if not self.input_ip else "",
            protocol=self.output_cfg.protocol,
            bind_host=self.input_ip,
            on_frame=self._on_input_frame,
        )
        self._receiver.start()

    def configure_input(
        self,
        *,
        input_universe: int | None = None,
        input_ip: str | None = None,
    ) -> None:
        """Update listen universe / bind IP; restart the receiver when needed."""
        restart = False
        if input_universe is not None and int(input_universe) != self.input_universe:
            self.input_universe = int(input_universe)
            restart = True
        if input_ip is not None:
            ip = str(input_ip or "").strip()
            if ip != self.input_ip:
                self.input_ip = ip
                restart = True
        self._refresh_universe_warning()
        if restart and self._thread is not None:
            self._start_receiver()

    def stop(self) -> None:
        self._stop.set()
        if self._receiver is not None:
            self._receiver.stop()
            self._receiver = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._sender is not None:
            self._sender.blackout()
            self._sender.close()
            self._sender = None

    def _on_input_frame(self, universe: int, data: bytes) -> None:
        if universe != self.input_universe:
            return
        now = time.monotonic()
        with self._lock:
            self._dmx_in[:] = data[:512]
            self._last_input_mono = now
            self._input_packets += 1
            self._fps_window_count += 1
            elapsed = now - self._fps_window_t0
            if elapsed >= 1.0:
                self._input_fps = self._fps_window_count / elapsed
                self._fps_window_t0 = now
                self._fps_window_count = 0
            if self.state.recording:
                self.state.record_frames.append(bytes(data[:512]))
                n = len(self.state.record_frames)
                if n > 16 and n % 8 == 0:
                    hint = detect_loop_window(self.state.record_frames)
                    if hint is not None:
                        self.state.loop_detected_at = hint

    def start_recording(self, name: str = "Recording") -> str:
        rid = uuid.uuid4().hex
        with self._lock:
            self.state.recording = True
            self.state.record_frames = []
            self.state.record_started = time.monotonic()
            self.state.loop_detected_at = None
            self._pending_rec_name = name
            self._pending_rec_id = rid
        return rid

    def stop_recording(self) -> FxRecording | None:
        with self._lock:
            if not self.state.recording:
                return None
            self.state.recording = False
            frames = list(self.state.record_frames)
            rid = getattr(self, "_pending_rec_id", uuid.uuid4().hex)
            name = getattr(self, "_pending_rec_name", "Recording")
            loop_hint = self.state.loop_detected_at
        rec = FxRecording(
            id=rid,
            name=name,
            fps=self.record_fps,
            frames=frames,
            loop_hint_frame=loop_hint,
        )
        self.library.recordings[rec.id] = rec
        self.save_all()
        return rec

    def set_palette(self, palette_id: str) -> None:
        for p in self.palette_state.palettes:
            if p.id == palette_id:
                self._palette_from = self._current_palette_rgb()
                self._palette_to = (p.red, p.green, p.blue)
                self._palette_t0 = time.monotonic()
                self.palette_state.active_id = palette_id
                self.save_all()
                break

    def record_palette(self, name: str = "Palette"):
        """Capture the current listen-universe look as a colour palette."""
        with self._lock:
            frame = bytes(self._dmx_in)
            age = (
                time.monotonic() - self._last_input_mono
                if self._last_input_mono
                else None
            )
        tags = self.patch.channel_tags()
        rgb = sample_rgb_from_frame(
            frame,
            patch=self.imported_patch if self.imported_patch.channels else None,
            colour_addrs=tags,
        )
        pal = new_palette_from_rgb(name, rgb)
        self.palette_state.palettes.append(pal)
        self.set_palette(pal.id)
        return pal, age

    def _current_palette_rgb(self) -> tuple[int, int, int]:
        t = time.monotonic() - self._palette_t0
        if t >= PALETTE_FADE_SECONDS:
            return self._palette_to
        u = t / PALETTE_FADE_SECONDS if PALETTE_FADE_SECONDS > 0 else 1.0
        return blend_rgb(self._palette_from, self._palette_to, u)

    def _control_values(self) -> dict[int, bool]:
        on: dict[int, bool] = {}
        for name in CONTROL_CHANNEL_NAMES:
            addr = int(self.channel_map.get(name, 1))
            if 1 <= addr <= 512:
                on[addr] = self._dmx_in[addr - 1] > 127
        return on

    def _playback_for(self, assign: FxChannelAssignment) -> _ChannelPlayback:
        ch = assign.control_channel
        ids = tuple(assign.cue_ids)
        pb = self._channels.get(ch)
        if pb is None or pb.cue_ids != ids:
            pb = _ChannelPlayback(shuffle=CueShuffleBag(ids), cue_ids=ids)
            self._channels[ch] = pb
        return pb

    def _tick(self) -> None:
        interval = 1.0 / max(1.0, self.record_fps)
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._step_playback(t0)
            if self._sender is not None:
                self._sender.send(self._dmx_out)
            if self._on_state is not None:
                self._on_state(self.public_state())
            sleep = interval - (time.monotonic() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _step_playback(self, now: float) -> None:
        with self._lock:
            base = bytes(self._dmx_out)
            out = bytearray(base)
            controls = self._control_values()
            tags = self.patch.channel_tags()
            merges = self.patch.channel_merges()

        colour_fx_active = False

        for assign in self.library.assignments:
            ch = assign.control_channel
            down = controls.get(ch, False)
            rt = self._playback_for(assign)
            rt.button_down = down
            momentary = (assign.trigger or "latched").lower() == "momentary"

            if not down and not momentary:
                rt.active = None
                continue
            if not down and momentary:
                if rt.active is not None and rt.active.release_at is None:
                    rt.active.release_at = now
                if rt.active is None:
                    continue
            if down and momentary and rt.active is None:
                cue_id = rt.shuffle.next()
                if cue_id:
                    rt.active = _ActiveCue(cue_id=cue_id, started=now)
            if down and not momentary and rt.active is None:
                cue_id = rt.shuffle.next()
                if cue_id:
                    rt.active = _ActiveCue(cue_id=cue_id, started=now)
            if (
                down
                and not momentary
                and rt.active is not None
                and assign.mode == "step"
            ):
                interval = assign.step_interval_s()
                if interval and now - rt.active.started >= interval:
                    cue_id = rt.shuffle.next()
                    if cue_id:
                        rt.active = _ActiveCue(cue_id=cue_id, started=now)

            active = rt.active
            if active is None:
                continue
            cue = self.library.cues.get(active.cue_id)
            if cue is None:
                rt.active = None
                continue
            rec = self.library.recordings.get(cue.recording_id)
            if rec is None or not rec.frames:
                rt.active = None
                continue

            t_in = now - active.started
            start = max(0, cue.trim_start)
            end = cue.trim_end if cue.trim_end > start else len(rec.frames)
            duration = max(0.0, (end - start) / max(1.0, rec.fps))
            released = active.release_at is not None
            level = cue_fade_level(
                t_in,
                duration,
                cue.fade_in_s,
                cue.fade_out_s,
                released=released,
                release_at=active.release_at,
                now=now if released else None,
            )
            if level <= 0.0 and released:
                rt.active = None
                continue
            fade_t = 1.0
            if cue.fade_in_s > 0 and t_in < cue.fade_in_s and not released:
                fade_t = t_in / cue.fade_in_s
            frame_i = start + int(t_in * rec.fps)
            if apply_cue_frame(
                out,
                base,
                rec,
                cue,
                frame_i,
                fade_t=fade_t,
                cue_level=level,
                tags=tags,
                merges=merges,
            ):
                colour_fx_active = True

        if colour_fx_active:
            self._palette_suppressed = True
        elif self._palette_suppressed:
            self._palette_suppressed = False
            self._palette_from = self._current_palette_rgb()
            self._palette_to = self._palette_rgb_target()
            self._palette_t0 = now

        if not self._palette_suppressed:
            pr, pg, pb = self._current_palette_rgb()
            apply_rgb_to_colour_channels(
                out,
                (pr, pg, pb),
                patch=self.imported_patch if self.imported_patch.channels else None,
                colour_addrs=tags,
            )

        with self._lock:
            self._dmx_out[:] = out

    def _input_signal(self) -> dict:
        now = time.monotonic()
        with self._lock:
            last = self._last_input_mono
            fps = self._input_fps
            packets = self._input_packets
        age = (now - last) if last else None
        live = age is not None and age < 1.0
        return {
            "live": live,
            "fps": round(fps, 1),
            "packets": packets,
            "last_packet_age_s": round(age, 2) if age is not None else None,
        }

    def public_state(self) -> dict:
        patch_summary = None
        if self.imported_patch.channels:
            patch_summary = self.imported_patch.summary()
        return {
            "recording": self.state.recording,
            "record_frames": len(self.state.record_frames),
            "loop_detected_at": self.state.loop_detected_at,
            "bpm": self.state.bpm,
            "palette_active": self.palette_state.active_id,
            "palettes": [p.to_dict() for p in self.palette_state.palettes],
            "universe_warning": self.state.universe_warning,
            "input_universe": self.input_universe,
            "input_ip": self.input_ip,
            "output_universe": self.output_universe,
            "input_signal": self._input_signal(),
            "patch": patch_summary,
        }
