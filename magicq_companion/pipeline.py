"""The analysis pipeline loop and unattended audio-reconnect supervisor."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from .config import Config
from .dmx import build_frame
from .features import FeatureExtractor, Features
from .genre_agent import GenreAgent, GenreControlVector
from .genre_profiles import GenrePolicy, GenreProfileStore
from .output import DmxSender
from .state_machine import SectionTracker


class Bridge:
    """Consumes audio blocks, runs Two-Layer genre + track DSP, and emits DMX.

    Layer 1 (`GenreProfileStore` -> `GenrePolicy`) drives slow genre-dependent
    settings and learns from user tweaks in `genre_profiles.json`.
    Layer 2 (`FeatureExtractor` & `SectionTracker`) runs fast 12 ms hop-level
    reactivity (`beat_pulse`, `drop_hit`, fast rises, `fx_size` surges) without
    ever being reset or slowed down by Layer 1 changes.
    """

    def __init__(
        self,
        cfg: Config,
        blocks: Iterator[np.ndarray] | None = None,
        sender: DmxSender | None = None,
        pace: bool = False,
        duration: float | None = None,
        *,
        async_genre: bool = True,
        profiles_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.blocks = blocks
        self.sender = sender
        self.pace = pace
        self.duration = duration

        sr = cfg.audio.samplerate
        hop = cfg.audio.blocksize
        self.dt = hop / sr
        self.extractor = FeatureExtractor(
            sr,
            hop,
            cfg.detection,
            cfg.build_score,
            cfg.bpm_rate,
            cfg.fx_size,
            noise_floor_db=cfg.audio.noise_floor_db,
        )
        self.tracker = SectionTracker(cfg.detection, self.extractor.fps)
        self.genre_agent = GenreAgent(
            cfg.genre,
            sr,
            async_worker=async_genre,
        )
        self.profile_store = GenreProfileStore(
            profiles_path,
            learning_enabled=cfg.genre.learning_enabled,
        )
        self.genre: GenreControlVector = self.genre_agent.control_vector()
        self.policy: GenrePolicy = self.profile_store.resolve(
            self.genre_agent.current_probabilities()
        )
        self.tracker._genre = self.genre
        self.tracker._policy = self.policy
        self.dmx = bytearray(512)

        self.t = 0.0
        self.features = Features()
        self.enabled = True
        self.analyzing = True
        self.signal_live = False
        self.quiet_s = 0.0
        self.below_floor_s = 0.0
        self._hum_quiet_s = 0.0
        self._above_floor_s = 0.0
        self._music_s = 0.0
        self._was_sending = False
        self._frame_idx = 0

    def active_policy(self) -> GenrePolicy | None:
        if not self.cfg.genre.enabled:
            return None
        return self.policy

    def _consume_stream(
        self,
        blocks: Iterator[np.ndarray],
        on_frame: Callable[[Bridge], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        send_every = max(1, round(self.extractor.fps / self.cfg.artnet.fps))
        wall_start = time.monotonic() - self.t
        for block in blocks:
            self._frame_idx += 1
            self.t += self.dt
            if self.analyzing:
                genre_vec = self.genre_agent.control_vector()
                if self.cfg.genre.enabled:
                    self.policy = self.profile_store.resolve(
                        self.genre_agent.current_probabilities()
                    )
                    policy_for_hop: GenrePolicy | None = self.policy
                else:
                    policy_for_hop = None

                self.features = self.extractor.process(
                    block,
                    genre=genre_vec,
                    policy=policy_for_hop,
                    section=self.tracker.state.value,
                    build_progress=self.tracker.build_progress,
                )
                self.genre = self.genre_agent.observe(
                    block,
                    above_floor=self.features.above_floor,
                    bpm=self.features.bpm,
                    kick_regularity=self.features.kick_regularity,
                    onset_density=self.features.onset_density,
                )
                self._update_signal_gate(self.features)
                if self.signal_live:
                    self.tracker.update(
                        self.features,
                        self.dt,
                        genre=self.genre,
                        policy=policy_for_hop,
                    )
                if self.enabled and self.signal_live:
                    build_frame(
                        self.dmx,
                        self.cfg.channels,
                        self.features,
                        self.tracker,
                        self.cfg.detection,
                    )
                else:
                    self.dmx[:] = b"\x00" * len(self.dmx)
            else:
                self.dmx[:] = b"\x00" * len(self.dmx)

            sending = self.analyzing and self.enabled and self.signal_live
            if self.sender is not None and self._frame_idx % send_every == 0:
                if sending:
                    self.sender.send(self.dmx)
                elif self._was_sending:
                    self.sender.blackout()
                self._was_sending = sending

            if on_frame is not None:
                on_frame(self)

            if self.pace:
                lag = self.t - (time.monotonic() - wall_start)
                if lag > 0:
                    time.sleep(lag)

            if stop_event is not None and stop_event.is_set():
                break
            if self.duration is not None and self.t >= self.duration:
                break

    def run(
        self,
        on_frame: Callable[[Bridge], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        try:
            if self.blocks is not None:
                self._consume_stream(self.blocks, on_frame=on_frame, stop_event=stop_event)
        finally:
            self.close()

    def run_supervised(
        self,
        source_factory: Callable[[Config], tuple[Iterator[np.ndarray], str]],
        *,
        initial: tuple[Iterator[np.ndarray], str] | None = None,
        reconnect: bool = True,
        on_frame: Callable[[Bridge], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run analysis continuously, auto-reopening audio capture on device/PipeWire drops."""
        wait_s = max(0.5, float(getattr(self.cfg.server, "audio_reconnect_seconds", 3.0)))
        current = initial
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if self.duration is not None and self.t >= self.duration:
                    break

                if current is None:
                    try:
                        current = source_factory(self.cfg)
                    except Exception as exc:
                        msg = f"audio unavailable ({exc}) — retrying in {wait_s:g}s"
                        if on_status is not None:
                            on_status(msg)
                        else:
                            print(f"[watchdog] {msg}", file=sys.stderr)
                        if not reconnect:
                            raise
                        if self._wait_stop(stop_event, wait_s):
                            break
                        continue

                blocks, desc = current
                current = None
                self.blocks = blocks
                if on_status is not None:
                    on_status(desc)

                stream_err: Exception | None = None
                try:
                    self._consume_stream(blocks, on_frame=on_frame, stop_event=stop_event)
                except Exception as exc:
                    stream_err = exc
                finally:
                    _close_blocks(blocks)

                if stop_event is not None and stop_event.is_set():
                    break
                if self.duration is not None and self.t >= self.duration:
                    break
                if not reconnect:
                    if stream_err is not None:
                        raise stream_err
                    break

                self.dmx[:] = b"\x00" * len(self.dmx)
                if self.sender is not None and self._was_sending:
                    self.sender.blackout()
                    self._was_sending = False
                reason = f": {stream_err}" if stream_err is not None else ""
                msg = f"audio stream interrupted{reason} — reconnecting in {wait_s:g}s"
                if on_status is not None:
                    on_status(msg)
                else:
                    print(f"[watchdog] {msg}", file=sys.stderr)
                if self._wait_stop(stop_event, wait_s):
                    break
        finally:
            self.close()

    @staticmethod
    def _wait_stop(stop_event: threading.Event | None, seconds: float) -> bool:
        if stop_event is not None:
            return stop_event.wait(timeout=seconds)
        time.sleep(seconds)
        return False

    def close(self) -> None:
        if self.blocks is not None:
            _close_blocks(self.blocks)
        self.genre_agent.close()
        if self.sender is not None:
            try:
                self.sender.blackout()
            except Exception:
                pass
            try:
                self.sender.close()
            except Exception:
                pass

    def _update_signal_gate(self, features: Features) -> None:
        det = self.cfg.detection
        if not bool(getattr(det, "silence_gate", True)):
            self.signal_live = True
            self.quiet_s = 0.0
            self.below_floor_s = 0.0
            self._above_floor_s = 0.0
            self._music_s = 0.0
            return

        above = bool(getattr(features, "above_floor", False))
        music = float(getattr(features, "music_score", 0.0))
        timeout = max(30.0, float(getattr(det, "silence_timeout_seconds", 600.0)))

        if not above:
            self.below_floor_s += self.dt
            self.quiet_s += self.dt
            self._hum_quiet_s = 0.0
            self._above_floor_s = 0.0
            self._music_s = 0.0
            # Turn off groove/build/DMX after 2.0 s below noise floor (longer than
            # a brief pre-drop stop gap, fast enough that pausing playback clears DMX).
            if self.signal_live and self.below_floor_s >= 2.0:
                self.signal_live = False
                self.tracker.reset_on_silence()
            return

        self._above_floor_s += self.dt
        self.below_floor_s = 0.0

        if music < 0.25:
            self._hum_quiet_s += self.dt
            self.quiet_s = self._hum_quiet_s
            self._music_s = 0.0
        else:
            self._hum_quiet_s = max(0.0, self._hum_quiet_s - 2.0 * self.dt)
            self.quiet_s = self._hum_quiet_s
            if music > 0.38:
                self._music_s += self.dt
            else:
                self._music_s = 0.0

        if self.signal_live:
            if self._hum_quiet_s >= timeout:
                self.signal_live = False
                self.tracker.reset_on_silence()
        else:
            # Wake up immediately (~80 ms) when audio rises above floor after a
            # silence pause (even hours long); require structured music only if
            # waking from a prolonged non-musical hum timeout.
            if self._hum_quiet_s < timeout and self._above_floor_s >= 0.08:
                self.signal_live = True
                self.quiet_s = 0.0
            elif self._music_s >= 1.2:
                self.signal_live = True
                self._hum_quiet_s = 0.0
                self.quiet_s = 0.0


def _close_blocks(blocks: Iterator[np.ndarray]) -> None:
    close = getattr(blocks, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
