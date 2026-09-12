"""The analysis pipeline loop, shared by the console mode and the web UI."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator

import numpy as np

from .config import Config
from .dmx import build_frame
from .features import FeatureExtractor, Features
from .output import DmxSender
from .state_machine import SectionTracker


class Bridge:
    """Consumes audio blocks, runs analysis, and emits DMX frames.

    After each processed block the current state is available on the
    instance: `t`, `features`, `tracker`, and `dmx`.
    """

    def __init__(
        self,
        cfg: Config,
        blocks: Iterator[np.ndarray],
        sender: DmxSender | None = None,
        pace: bool = False,
        duration: float | None = None,
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
            noise_floor_db=cfg.audio.noise_floor_db,
        )
        self.tracker = SectionTracker(cfg.detection, self.extractor.fps)
        self.dmx = bytearray(512)

        self.t = 0.0
        self.features = Features()
        # Output gate: Art-Net blackout while analysis can stay live.
        self.enabled = True
        # Analyzer gate: skip feature/section work; UI server stays up.
        self.analyzing = True
        # Music gate: False after a long stretch of hiss/silence.
        self.signal_live = True
        self.quiet_s = 0.0
        self._music_s = 0.0
        self._was_sending = True

    def run(
        self,
        on_frame: Callable[[Bridge], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        send_every = max(1, round(self.extractor.fps / self.cfg.artnet.fps))
        wall_start = time.monotonic()
        try:
            for i, block in enumerate(self.blocks):
                self.t += self.dt
                # Always consume audio so capture doesn't back up / block.
                if self.analyzing:
                    self.features = self.extractor.process(block)
                    self._update_signal_gate(self.features)
                    self.tracker.update(self.features, self.dt)
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
                if self.sender is not None and i % send_every == 0:
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
        finally:
            if self.sender is not None:
                self.sender.blackout()
                self.sender.close()

    def _update_signal_gate(self, features: Features) -> None:
        det = self.cfg.detection
        if not bool(getattr(det, "silence_gate", True)):
            self.signal_live = True
            self.quiet_s = 0.0
            self._music_s = 0.0
            return
        if self.t < 20.0:
            self.signal_live = True
            return
        timeout = max(30.0, float(getattr(det, "silence_timeout_seconds", 600.0)))
        music = float(getattr(features, "music_score", 0.0))
        if music < 0.28:
            self.quiet_s += self.dt
            self._music_s = 0.0
        else:
            self.quiet_s = max(0.0, self.quiet_s - 2.0 * self.dt)
            if music > 0.42:
                self._music_s += self.dt
            else:
                self._music_s = 0.0
        if self.signal_live:
            if self.quiet_s >= timeout:
                self.signal_live = False
        elif self._music_s >= 1.5:
            self.signal_live = True
            self.quiet_s = 0.0
