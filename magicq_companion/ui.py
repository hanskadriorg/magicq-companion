"""Web dashboard: broadcasts live audio, genre, policy, and DMX state over WebSocket,
with Two-Layer control (Genre Policy Layer with online profile learning +
Fast Track Reactivity Layer) and unattended host management.

Uses `aiohttp` when installed, with a built-in zero-dependency `asyncio`
HTTP + RFC 6455 WebSocket fallback.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import (
    ARCHETYPE_NAMES,
    BPM_RATE_NAMES,
    GENRE_MODES,
    INPUT_CHANNEL_CHOICES,
    NOISE_FLOOR_RANGE,
    STYLE_NAMES,
    VISUALIZER_MODES,
    Config,
    audio_output_cfg,
    clamp_noise_floor,
    coerce_genre_mode,
    coerce_input_channel,
    coerce_reboot_days,
    coerce_style,
    coerce_visualizer_mode,
    try_reboot_time,
)
from .control_channels import CONTROL_CHANNEL_NAMES, DEFAULT_RENUMBER
from .genre_profiles import ARCHETYPE_LABELS, GenrePolicy
from .output import (
    OUTPUT_PROTOCOLS,
    allowed_modes,
    coerce_mode,
    create_sender,
    describe_output,
    list_ipv4_interfaces,
)
from .paths import default_genre_profiles_path, default_layout_path, static_dir
from .pipeline import Bridge
from .schedule import apply_host_schedule

_DROP_SENSING_FIELDS = {
    "drop_rise_window": (0.12, 2.0),
    "drop_rise": (0.05, 2.0),
    "drop_energy_jump": (1.05, 3.0),
    "drop_min_bass": (0.10, 2.0),
    "drop_min_state_seconds": (0.0, 30.0),
    "drop_smooth_seconds": (0.02, 1.0),
}

_BROADCAST_INTERVAL = 0.05  # 20 updates per second
_LAYOUT_BOX_IDS = ("hero", "dmx", "log", "settings")
_SECTION_LOG_LIMIT = 200
_SECTION_LOG_SECONDS = 180.0
_HERO_PANES = ("head", "input", "spark", "meters")
_DEFAULT_LAYOUT = {
    "sparkHeight": 90,
    "metersHeight": 0,
    "heroOrder": list(_HERO_PANES),
    "rows": [{"ids": [box_id], "h": 0} for box_id in _LAYOUT_BOX_IDS],
}
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _Manager:
    """Owns the supervised analysis worker thread and applies runtime settings."""

    def __init__(
        self,
        cfg: Config,
        source_factory: Callable,
        dry_run: bool,
        pace: bool,
        duration: float | None,
        config_path: str | None,
        simulate: bool,
    ) -> None:
        self.cfg = cfg
        self.source_factory = source_factory
        self.dry_run = dry_run
        self.pace = pace
        self.duration = duration
        self.config_path = config_path
        self.layout_path = default_layout_path(config_path)
        self.profiles_path = default_genre_profiles_path(config_path)
        self.simulate = simulate

        self.state_lock = threading.Lock()
        self.state: dict = {"running": False}
        self.ctl_lock = threading.Lock()

        self.bridge: Bridge | None = None
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.source_desc = "starting…"
        self.enabled = bool(self.cfg.server.startup_output)
        self.cfg.artnet.enabled = self.enabled
        self.analyzing = True
        self.visualizer = bool(self.cfg.ui.visualizer)
        self.layout = self._load_layout()
        self.section_log: deque[dict] = deque(maxlen=_SECTION_LOG_LIMIT)
        self.log_seq = 0
        self._log_section: str | None = None

    # ------------------------------------------------------------------

    def start(self, prebuilt: tuple | None = None) -> None:
        sender = None
        if not self.dry_run:
            sender = create_sender(audio_output_cfg(self.cfg))
        if prebuilt is not None:
            self.source_desc = prebuilt[1]
        self.bridge = Bridge(
            self.cfg,
            sender=sender,
            pace=self.pace,
            duration=self.duration,
            async_genre=not (self.simulate and not self.pace),
            profiles_path=self.profiles_path,
        )
        self.bridge.enabled = self.enabled
        self.bridge.analyzing = self.analyzing
        self.bridge.extractor.visualizer = self.visualizer
        self.stop_event = threading.Event()
        self.worker = threading.Thread(
            target=self._run,
            args=(prebuilt,),
            daemon=True,
            name="venue-bridge",
        )
        with self.state_lock:
            self.state["running"] = True
            self.state["enabled"] = self.enabled
            self.state["analyzing"] = self.analyzing
            self.state["visualizer"] = self.visualizer
            if not self.visualizer:
                self.state["spectrum_db"] = []
            self.state["meta"] = self._meta()
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.bridge is not None:
            blocks = self.bridge.blocks
            close = getattr(blocks, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if self.worker is not None:
            self.worker.join(timeout=5)
            self.worker = None

    def stop_all(self) -> None:
        self.stop()

    def _on_source_status(self, desc: str) -> None:
        self.source_desc = desc
        with self.state_lock:
            meta = dict(self.state.get("meta") or self._meta())
            meta["source"] = desc
            self.state["meta"] = meta

    def _run(self, prebuilt: tuple | None) -> None:
        assert self.bridge is not None
        try:
            self.bridge.run_supervised(
                self.source_factory,
                initial=prebuilt,
                reconnect=not self.simulate or self.duration is None,
                on_frame=self._on_frame,
                on_status=self._on_source_status,
                stop_event=self.stop_event,
            )
        finally:
            with self.state_lock:
                self.state["running"] = False

    def _on_frame(self, b: Bridge) -> None:
        f = b.features
        ev = b.tracker.evidence()
        live = bool(b.signal_live)
        self._record_section(b)
        arch_key = str(ev.get("archetype") or "house_tech")
        edits_map = b.profile_store.edit_counts()
        policy_dict = b.policy.to_ui_dict() if self.cfg.genre.enabled else None
        with self.state_lock:
            self.state.update(
                t=round(b.t, 2),
                section=b.tracker.state.value,
                t_in_state=round(b.tracker.t_in_state, 1),
                bpm=round(f.bpm, 1),
                energy=round(f.energy_norm, 3),
                bass=round(f.bass_norm, 3),
                kick_depth=round(f.kick_depth, 3),
                music=round(f.music_score, 3),
                build=round(f.build_score, 3),
                melody=round(f.melody_score, 3),
                pace=round(f.pace_score, 3),
                bpm_rate=round(f.bpm_rate),
                fx_size=round(f.fx_size),
                ramp=round(b.tracker.build_progress, 3),
                beat_age=round(f.seconds_since_beat, 3),
                rms_db=round(f.rms_db, 1),
                peak_db=round(f.peak_db, 1),
                level=round(f.level, 3),
                clipping=bool(f.clipping),
                noise_floor_db=self.cfg.audio.noise_floor_db,
                grid_locked=bool(f.four_four_locked),
                spectrum_db=list(f.spectrum_db) if self.visualizer else [],
                dmx=list(b.dmx[:16]),
                enabled=b.enabled,
                analyzing=b.analyzing,
                visualizer=self.visualizer,
                visualizer_mode=self.cfg.ui.visualizer_mode,
                signal_live=live,
                quiet_s=round(b.quiet_s, 1),
                log_seq=self.log_seq,
                genre_mode=self.cfg.genre.mode,
                learning_enabled=bool(self.cfg.genre.learning_enabled),
                genre_edits=int(edits_map.get(arch_key, 0)),
                genre_total_edits=sum(edits_map.values()),
                policy=policy_dict,
                **ev,
            )

    def _record_section(self, b: Bridge) -> None:
        section = b.tracker.state.value
        if section == self._log_section:
            return
        now = time.time()
        if self.section_log:
            last = self.section_log[-1]
            last["held"] = round(max(0.0, now - float(last["ts"])), 1)
        self._log_section = section
        self.log_seq += 1
        self.section_log.append(
            {
                "seq": self.log_seq,
                "ts": now,
                "section": section,
                "held": 0.0,
                "bpm": round(b.features.bpm, 1),
                "pace": round(b.features.pace_score, 2),
                "fx_size": int(round(b.features.fx_size)),
                "archetype": b.features.archetype_label,
                "energy": round(b.features.energy_norm, 2),
                "build": round(b.features.build_score, 2),
                "level_db": round(b.features.rms_db, 1),
            }
        )
        self._prune_section_log(now)

    def _prune_section_log(self, now: float) -> None:
        cutoff = now - _SECTION_LOG_SECONDS
        kept: list[dict] = []
        rows = list(self.section_log)
        for i, row in enumerate(rows):
            start = float(row.get("ts") or 0)
            end = start + float(row.get("held") or 0) if i + 1 < len(rows) else now
            if end >= cutoff:
                kept.append(row)
        if len(kept) != len(rows):
            self.section_log.clear()
            self.section_log.extend(kept)

    def _channel_map_dict(self) -> dict[str, int]:
        ch = self.cfg.channels
        return {name: int(getattr(ch, name)) for name in CONTROL_CHANNEL_NAMES}

    def sections(self) -> list[dict]:
        now = time.time()
        cutoff = now - _SECTION_LOG_SECONDS
        items = list(self.section_log)
        out: list[dict] = []
        for i, row in enumerate(items):
            rec = dict(row)
            start = float(rec.get("ts") or 0)
            current = i + 1 == len(items)
            if current:
                rec["held"] = round(max(0.0, now - start), 1)
            rec["current"] = current
            end = start + float(rec.get("held") or 0)
            if end >= cutoff:
                out.append(rec)
        return out

    def genre_profiles_snapshot(self) -> dict[str, Any]:
        if self.bridge is None:
            return {"profiles": {}, "total_edits": 0}
        return {
            "profiles": self.bridge.profile_store.all_profiles_summary(),
            "total_edits": self.bridge.profile_store.total_edits(),
        }

    def reset_genre_profile(self, archetype: str | None) -> str:
        with self.ctl_lock:
            if self.bridge is None:
                return "Bridge not running"
            target = str(archetype or "current").strip().lower()
            if target == "current":
                target = self.bridge.genre.archetype
            if target != "all" and target not in ARCHETYPE_NAMES:
                raise ValueError(f"unknown archetype: {target}")
            self.bridge.profile_store.reset_profiles(target)
            probs = self.bridge.genre_agent.current_probabilities()
            self.bridge.policy = self.bridge.profile_store.resolve(probs)
            with self.state_lock:
                self.state["policy"] = self.bridge.policy.to_ui_dict()
                self.state["genre_edits"] = self.bridge.profile_store.edit_counts().get(
                    self.bridge.genre.archetype, 0
                )
                self.state["genre_total_edits"] = self.bridge.profile_store.total_edits()
                self.state["meta"] = self._meta()
            if target == "all":
                return "Reset all 8 genre profiles to factory defaults"
            return f"Reset {ARCHETYPE_LABELS.get(target, target)} profile to factory defaults"

    # ------------------------------------------------------------------

    def _meta(self) -> dict:
        audio_cfg = audio_output_cfg(self.cfg)
        artnet = "dry run, not sending" if self.dry_run else describe_output(audio_cfg)
        br = self.cfg.bpm_rate
        backend = (
            self.bridge.genre_agent.backend_name
            if self.bridge is not None
            else ("auto" if self.cfg.genre.enabled else "off")
        )
        policy: GenrePolicy | None = (
            self.bridge.policy if (self.bridge is not None and self.cfg.genre.enabled) else None
        )
        pol_ui = policy.to_ui_dict() if policy is not None else {}
        edits_total = (
            self.bridge.profile_store.total_edits() if self.bridge is not None else 0
        )
        return {
            "source": self.source_desc,
            "artnet": artnet,
            "device": self.cfg.audio.device,
            "input_channel": self.cfg.audio.input_channel,
            "noise_floor_db": self.cfg.audio.noise_floor_db,
            "visualizer_mode": self.cfg.ui.visualizer_mode,
            "protocol": self.cfg.artnet.protocol,
            "mode": coerce_mode(self.cfg.artnet.protocol, self.cfg.artnet.mode),
            "interface": self.cfg.network.interface,
            "ip": self.cfg.network.ip,
            "port": self.cfg.artnet.port,
            "universe": self.cfg.artnet.universe,
            "priority": self.cfg.artnet.priority,
            "bpm_rate_safety_min": br.min,
            "bpm_rate_safety_max": br.max,
            "bpm_rate_min": pol_ui.get("bpm_rate_min", br.min),
            "bpm_rate_max": pol_ui.get("bpm_rate_max", br.max),
            "bpm_rate_decay": pol_ui.get("bpm_rate_decay", br.decay_seconds),
            "fx_size_base": pol_ui.get("fx_size_base", self.cfg.fx_size.base),
            "fx_size_max": pol_ui.get("fx_size_max", self.cfg.fx_size.max),
            "fx_size_reactivity": pol_ui.get("fx_size_reactivity", self.cfg.fx_size.reactivity),
            "genre_enabled": bool(self.cfg.genre.enabled),
            "genre_mode": self.cfg.genre.mode,
            "learning_enabled": bool(self.cfg.genre.learning_enabled),
            "genre_backend": backend,
            "genre_total_edits": edits_total,
            "policy": pol_ui,
            "build_intensity": pol_ui.get("build_intensity", self.cfg.detection.build_intensity),
            "drop_intensity": pol_ui.get("drop_intensity", self.cfg.detection.drop_intensity),
            "build_decay_seconds": pol_ui.get(
                "build_decay_seconds", self.cfg.detection.build_decay_seconds
            ),
            "drop_hold_seconds": pol_ui.get(
                "drop_hold_seconds", self.cfg.detection.drop_hold_seconds
            ),
            "drop_rise_window": self.cfg.detection.drop_rise_window,
            "drop_rise": self.cfg.detection.drop_rise,
            "drop_energy_jump": self.cfg.detection.drop_energy_jump,
            "drop_min_bass": self.cfg.detection.drop_min_bass,
            "drop_min_state_seconds": self.cfg.detection.drop_min_state_seconds,
            "drop_smooth_seconds": self.cfg.detection.drop_smooth_seconds,
            "drop_from_groove": bool(self.cfg.detection.drop_from_groove),
            "drop_use_onset": bool(self.cfg.detection.drop_use_onset),
            "four_four_intensity": pol_ui.get(
                "four_four_intensity", self.cfg.detection.four_four_intensity
            ),
            "four_four_smooth_seconds": self.cfg.detection.four_four_smooth_seconds,
            "breakdown_intensity": pol_ui.get(
                "breakdown_intensity", self.cfg.detection.breakdown_intensity
            ),
            "breakdown_beats": pol_ui.get(
                "breakdown_beats", self.cfg.detection.breakdown_beats
            ),
            "drop_hit_seconds": self.cfg.detection.drop_hit_seconds,
            "drop_hit_fade_seconds": self.cfg.detection.drop_hit_fade_seconds,
            "style": self.cfg.detection.style,
            "silence_gate": bool(self.cfg.detection.silence_gate),
            "silence_timeout_seconds": self.cfg.detection.silence_timeout_seconds,
            "startup_output": bool(self.cfg.server.startup_output),
            "reboot_enabled": bool(self.cfg.server.reboot_enabled),
            "reboot_time": self.cfg.server.reboot_time,
            "reboot_days": list(self.cfg.server.reboot_days),
            "enabled": self.enabled,
            "analyzing": self.analyzing,
            "visualizer": self.visualizer,
            "dry_run": self.dry_run,
            "simulate": self.simulate,
            "control_channels": self._channel_map_dict(),
            "control_channel_map_doc": DEFAULT_RENUMBER.document_lines(),
        }

    def _load_layout(self) -> dict:
        path = self.layout_path
        if not path.exists():
            return json.loads(json.dumps(_DEFAULT_LAYOUT))
        try:
            data = json.loads(path.read_text())
            return self._normalize_layout(data)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return json.loads(json.dumps(_DEFAULT_LAYOUT))

    @staticmethod
    def _window_height(raw) -> int:
        try:
            height = int(raw or 0)
        except (TypeError, ValueError):
            return 0
        return height if 80 <= height <= 2000 else 0

    def _normalize_layout(self, data: dict) -> dict:
        rows: list[dict] = []
        seen: set[str] = set()
        raw_rows = data.get("rows")
        if isinstance(raw_rows, list) and raw_rows:
            for raw in raw_rows:
                if isinstance(raw, dict):
                    ids_raw = raw.get("ids") or []
                    height = self._window_height(raw.get("h"))
                elif isinstance(raw, list):
                    ids_raw = raw
                    height = 0
                else:
                    continue
                row_ids: list[str] = []
                for box_id in ids_raw:
                    box_id = str(box_id)
                    if box_id not in _LAYOUT_BOX_IDS or box_id in seen:
                        continue
                    row_ids.append(box_id)
                    seen.add(box_id)
                if row_ids:
                    rows.append({"ids": row_ids, "h": height})
        for box_id in _LAYOUT_BOX_IDS:
            if box_id not in seen:
                rows.append({"ids": [box_id], "h": 0})
        try:
            spark_h = int(data.get("sparkHeight", 90))
        except (TypeError, ValueError):
            spark_h = 90
        try:
            meters_h = int(data.get("metersHeight", 0) or 0)
        except (TypeError, ValueError):
            meters_h = 0
        order: list[str] = []
        seen_panes: set[str] = set()
        raw_order = data.get("heroOrder") or []
        if isinstance(raw_order, list):
            for pane in raw_order:
                pid = str(pane)
                if pid in _HERO_PANES and pid not in seen_panes:
                    order.append(pid)
                    seen_panes.add(pid)
        for pane in _HERO_PANES:
            if pane not in seen_panes:
                order.append(pane)
        return {
            "sparkHeight": max(48, min(360, spark_h)),
            "metersHeight": max(0, min(280, meters_h)),
            "heroOrder": order,
            "rows": rows,
        }

    def save_layout(self, data: dict) -> dict:
        layout = self._normalize_layout(data)
        self.layout_path.parent.mkdir(parents=True, exist_ok=True)
        self.layout_path.write_text(json.dumps(layout, indent=2) + "\n")
        self.layout = layout
        return layout

    def set_enabled(self, enabled: bool) -> str:
        with self.ctl_lock:
            self.enabled = bool(enabled)
            self.cfg.artnet.enabled = self.enabled
            if self.bridge is not None:
                self.bridge.enabled = self.enabled
            with self.state_lock:
                self.state["enabled"] = self.enabled
                self.state["meta"] = self._meta()
            self._persist()
            return "Output on" if self.enabled else "Output off (DMX blackout)"

    def set_analyzing(self, analyzing: bool) -> str:
        with self.ctl_lock:
            self.analyzing = bool(analyzing)
            if self.bridge is not None:
                self.bridge.analyzing = self.analyzing
            with self.state_lock:
                self.state["analyzing"] = self.analyzing
                self.state["meta"] = self._meta()
            if self.analyzing:
                return "Analyzer on"
            return "Analyzer off (audio drained, DMX blackout; UI stays up)"

    def set_visualizer(self, visualizer: bool) -> str:
        with self.ctl_lock:
            self.visualizer = bool(visualizer)
            self.cfg.ui.visualizer = self.visualizer
            if self.bridge is not None:
                self.bridge.extractor.visualizer = self.visualizer
            with self.state_lock:
                self.state["visualizer"] = self.visualizer
                if not self.visualizer:
                    self.state["spectrum_db"] = []
                self.state["meta"] = self._meta()
            self._persist()
            if self.visualizer:
                return "Visualizer on"
            return "Visualizer off (spectrum skipped)"

    def apply_settings(
        self,
        device: str | None = None,
        ip: str | None = None,
        universe: int | None = None,
        bpm_rate_min: str | None = None,
        bpm_rate_max: str | None = None,
        bpm_rate_decay: float | None = None,
        fx_size_base: int | None = None,
        fx_size_max: int | None = None,
        fx_size_reactivity: float | None = None,
        intensity: float | None = None,
        build_intensity: float | None = None,
        drop_intensity: float | None = None,
        build_decay_seconds: float | None = None,
        drop_hold_seconds: float | None = None,
        protocol: str | None = None,
        mode: str | None = None,
        port: int | None = None,
        priority: int | None = None,
        interface: str | None = None,
        input_channel: str | None = None,
        drop_sensing: dict | None = None,
        style: str | None = None,
        genre_mode: str | None = None,
        learning_enabled: bool | None = None,
        silence_gate: bool | None = None,
        noise_floor_db: float | None = None,
        visualizer_mode: str | None = None,
        four_four_intensity: float | None = None,
        four_four_smooth_seconds: float | None = None,
        breakdown_intensity: float | None = None,
        breakdown_beats: float | None = None,
        drop_hit_seconds: float | None = None,
        drop_hit_fade_seconds: float | None = None,
        startup_output: bool | None = None,
        reboot_enabled: bool | None = None,
        reboot_time: str | None = None,
        reboot_days: list[str] | None = None,
    ) -> str:
        with self.ctl_lock:
            cfg = self.cfg
            device_changed = (
                device is not None
                and device != cfg.audio.device
                and not self.simulate
            )
            if input_channel is not None:
                input_channel = coerce_input_channel(input_channel)
            channel_changed = (
                input_channel is not None
                and input_channel != cfg.audio.input_channel
                and not self.simulate
            )
            audio_changed = False
            if interface is not None and str(interface).strip().lower() in (
                "localhost",
                "lo",
                "127.0.0.1",
            ):
                interface = ""
                mode = "localhost"
            if interface is not None and interface != cfg.network.interface:
                cfg.network.interface = interface
                cfg.artnet.interface = interface
                audio_changed = True
            if ip is not None and ip != cfg.network.ip:
                cfg.network.ip = ip
                cfg.artnet.ip = ip
                audio_changed = True
            if protocol is not None and protocol != cfg.artnet.protocol:
                cfg.artnet.protocol = protocol
                audio_changed = True
            if mode is not None:
                mode = coerce_mode(cfg.artnet.protocol, mode)
                if mode != cfg.artnet.mode:
                    cfg.artnet.mode = mode
                    audio_changed = True
            elif audio_changed:
                cfg.artnet.mode = coerce_mode(cfg.artnet.protocol, cfg.artnet.mode)
            if universe is not None and universe != cfg.artnet.universe:
                cfg.artnet.universe = universe
                audio_changed = True
            if port is not None and port != cfg.artnet.port:
                cfg.artnet.port = port
                audio_changed = True
            if priority is not None and priority != cfg.artnet.priority:
                cfg.artnet.priority = priority
                audio_changed = True

            if intensity is not None:
                if build_intensity is None:
                    build_intensity = intensity
                if drop_intensity is None:
                    drop_intensity = intensity

            learning_flag_changed = False
            if learning_enabled is not None and bool(learning_enabled) != bool(
                cfg.genre.learning_enabled
            ):
                cfg.genre.learning_enabled = bool(learning_enabled)
                learning_flag_changed = True
                if self.bridge is not None:
                    self.bridge.profile_store.learning_enabled = cfg.genre.learning_enabled

            genre_changed = False
            if genre_mode is not None:
                coerced_gm = coerce_genre_mode(genre_mode)
                if coerced_gm != cfg.genre.mode:
                    cfg.genre.mode = coerced_gm
                    genre_changed = True
                    if self.bridge is not None:
                        self.bridge.genre_agent.cfg.mode = coerced_gm
                        self.bridge.genre = self.bridge.genre_agent.refresh_mode()
                        self.bridge.policy = self.bridge.profile_store.resolve(
                            self.bridge.genre_agent.current_probabilities()
                        )

            # ----------------------------------------------------------
            # Layer 1: Genre Policy & Online Profile Learning
            # (strictly separate from Layer 2 fast track reactivity!)
            # ----------------------------------------------------------
            policy_updates: dict[str, Any] = {}
            for key, val in (
                ("bpm_rate_min", bpm_rate_min),
                ("bpm_rate_max", bpm_rate_max),
                ("bpm_rate_decay", bpm_rate_decay),
                ("fx_size_base", fx_size_base),
                ("fx_size_max", fx_size_max),
                ("fx_size_reactivity", fx_size_reactivity),
                ("build_intensity", build_intensity),
                ("build_decay_seconds", build_decay_seconds),
                ("drop_intensity", drop_intensity),
                ("drop_hold_seconds", drop_hold_seconds),
                ("breakdown_intensity", breakdown_intensity),
                ("breakdown_beats", breakdown_beats),
                ("four_four_intensity", four_four_intensity),
            ):
                if val is not None:
                    policy_updates[key] = val

            learned_credits: list[tuple[str, float]] = []
            policy_changed = False
            if policy_updates:
                if cfg.genre.enabled and self.bridge is not None:
                    probs = self.bridge.genre_agent.current_probabilities()
                    new_policy, learned_credits = self.bridge.profile_store.learn_override(
                        probs, policy_updates
                    )
                    self.bridge.policy = new_policy
                    policy_changed = True

                # Also update fallback config fields (without shrinking global
                # bpm_rate outer safety bounds when genre agent is active!).
                if not cfg.genre.enabled:
                    if bpm_rate_min is not None:
                        cfg.bpm_rate.min = bpm_rate_min
                    if bpm_rate_max is not None:
                        cfg.bpm_rate.max = bpm_rate_max
                if bpm_rate_decay is not None:
                    cfg.bpm_rate.decay_seconds = bpm_rate_decay
                if fx_size_base is not None:
                    cfg.fx_size.base = int(fx_size_base)
                if fx_size_max is not None:
                    cfg.fx_size.max = max(cfg.fx_size.base, int(fx_size_max))
                if fx_size_reactivity is not None:
                    cfg.fx_size.reactivity = float(fx_size_reactivity)
                if build_intensity is not None:
                    cfg.detection.build_intensity = build_intensity
                if build_decay_seconds is not None:
                    cfg.detection.build_decay_seconds = build_decay_seconds
                if drop_intensity is not None:
                    cfg.detection.drop_intensity = drop_intensity
                if drop_hold_seconds is not None:
                    cfg.detection.drop_hold_seconds = drop_hold_seconds
                if breakdown_intensity is not None:
                    cfg.detection.breakdown_intensity = breakdown_intensity
                if breakdown_beats is not None:
                    cfg.detection.breakdown_beats = breakdown_beats
                if four_four_intensity is not None:
                    cfg.detection.four_four_intensity = four_four_intensity
                policy_changed = True

            # ----------------------------------------------------------
            # Layer 2: Fast Track Reactivity Layer (in-place, zero reset)
            # ----------------------------------------------------------
            sensing_changed = self._apply_drop_sensing(cfg.detection, drop_sensing)

            style_changed = False
            if style is not None and style != cfg.detection.style:
                cfg.detection.style = style
                style_changed = True

            silence_changed = False
            if silence_gate is not None and bool(silence_gate) != bool(
                cfg.detection.silence_gate
            ):
                cfg.detection.silence_gate = bool(silence_gate)
                silence_changed = True

            server_changed = False
            schedule_changed = False
            if startup_output is not None and bool(startup_output) != bool(
                cfg.server.startup_output
            ):
                cfg.server.startup_output = bool(startup_output)
                server_changed = True
            if reboot_enabled is not None and bool(reboot_enabled) != bool(
                cfg.server.reboot_enabled
            ):
                cfg.server.reboot_enabled = bool(reboot_enabled)
                server_changed = True
                schedule_changed = True
            if reboot_time is not None and reboot_time != cfg.server.reboot_time:
                cfg.server.reboot_time = reboot_time
                server_changed = True
                schedule_changed = True
            if reboot_days is not None and list(reboot_days) != list(cfg.server.reboot_days):
                cfg.server.reboot_days = list(reboot_days)
                server_changed = True
                schedule_changed = True

            four_four_smooth_changed = False
            if (
                four_four_smooth_seconds is not None
                and four_four_smooth_seconds != cfg.detection.four_four_smooth_seconds
            ):
                cfg.detection.four_four_smooth_seconds = four_four_smooth_seconds
                four_four_smooth_changed = True

            drop_hit_changed = False
            if (
                drop_hit_seconds is not None
                and drop_hit_seconds != cfg.detection.drop_hit_seconds
            ):
                cfg.detection.drop_hit_seconds = drop_hit_seconds
                drop_hit_changed = True
            if (
                drop_hit_fade_seconds is not None
                and drop_hit_fade_seconds != cfg.detection.drop_hit_fade_seconds
            ):
                cfg.detection.drop_hit_fade_seconds = drop_hit_fade_seconds
                drop_hit_changed = True

            input_changed = False
            if noise_floor_db is not None:
                floor = clamp_noise_floor(noise_floor_db)
                if floor != cfg.audio.noise_floor_db:
                    cfg.audio.noise_floor_db = floor
                    if self.bridge is not None:
                        self.bridge.extractor.noise_floor_db = floor
                    input_changed = True
            if visualizer_mode is not None:
                vmode = coerce_visualizer_mode(visualizer_mode)
                if vmode != cfg.ui.visualizer_mode:
                    cfg.ui.visualizer_mode = vmode
                    input_changed = True

            if not any(
                (
                    device_changed,
                    channel_changed,
                    audio_changed,
                    policy_changed,
                    sensing_changed,
                    style_changed,
                    genre_changed,
                    learning_flag_changed,
                    four_four_smooth_changed,
                    drop_hit_changed,
                    input_changed,
                    silence_changed,
                    server_changed,
                )
            ):
                return "No changes to apply."

            if device_changed or channel_changed:
                if device_changed:
                    cfg.audio.device = device  # type: ignore[assignment]
                if channel_changed:
                    cfg.audio.input_channel = input_channel  # type: ignore[assignment]
                self.stop()
                time.sleep(0.08)
                prebuilt = self.source_factory(cfg)
                self.start(prebuilt)
            elif audio_changed and not self.dry_run and self.bridge is not None:
                old = self.bridge.sender
                self.bridge.sender = create_sender(audio_output_cfg(cfg))
                if old is not None:
                    old.blackout()
                    old.close()

            if self.bridge is not None:
                self.bridge.extractor.bpm_rate_cfg = cfg.bpm_rate
                self.bridge.extractor.fx_size_cfg = cfg.fx_size

            with self.state_lock:
                self.state["meta"] = self._meta()
                self.state["genre_mode"] = cfg.genre.mode
                self.state["learning_enabled"] = cfg.genre.learning_enabled
                if self.bridge is not None and cfg.genre.enabled:
                    self.state["policy"] = self.bridge.policy.to_ui_dict()
                    self.state["genre_edits"] = self.bridge.profile_store.edit_counts().get(
                        self.bridge.genre.archetype, 0
                    )
                    self.state["genre_total_edits"] = self.bridge.profile_store.total_edits()
            self._persist()

            parts = []
            if device_changed:
                parts.append(f"input device -> {device or 'system default'}")
            if channel_changed:
                parts.append(f"input channel -> {cfg.audio.input_channel}")
            if audio_changed:
                parts.append("output " + describe_output(audio_output_cfg(cfg)))
            if genre_changed:
                parts.append(f"genre agent {cfg.genre.mode}")
            if learning_flag_changed:
                parts.append(
                    "profile learning on" if cfg.genre.learning_enabled else "profile learning off"
                )
            if "bpm_rate_min" in policy_updates or "bpm_rate_max" in policy_updates or "bpm_rate_decay" in policy_updates:
                pol = self.bridge.policy if self.bridge is not None else None
                lo_n = pol.bpm_rate_min_name if pol else cfg.bpm_rate.min
                hi_n = pol.bpm_rate_max_name if pol else cfg.bpm_rate.max
                dec_v = pol.bpm_rate_decay if pol else cfg.bpm_rate.decay_seconds
                parts.append(f"BPM divider {lo_n}…{hi_n} ({dec_v:g}s decay)")
            if "fx_size_base" in policy_updates or "fx_size_max" in policy_updates or "fx_size_reactivity" in policy_updates:
                pol = self.bridge.policy if self.bridge is not None else None
                sb = int(round(pol.fx_size_base)) if pol else cfg.fx_size.base
                sm = int(round(pol.fx_size_max)) if pol else cfg.fx_size.max
                sr_v = pol.fx_size_reactivity if pol else cfg.fx_size.reactivity
                parts.append(f"FX size {sb}–{sm} ({sr_v:.0%} react)")
            if "build_intensity" in policy_updates or "build_decay_seconds" in policy_updates:
                pol = self.bridge.policy if self.bridge is not None else None
                bi = pol.build_intensity if pol else cfg.detection.build_intensity
                bd = pol.build_decay_seconds if pol else cfg.detection.build_decay_seconds
                parts.append(f"build {bi:.0%}, decay {bd:g}s")
            if "drop_intensity" in policy_updates or "drop_hold_seconds" in policy_updates:
                pol = self.bridge.policy if self.bridge is not None else None
                di = pol.drop_intensity if pol else cfg.detection.drop_intensity
                dh = pol.drop_hold_seconds if pol else cfg.detection.drop_hold_seconds
                parts.append(f"drop {di:.0%}, hold {dh:g}s")
            if sensing_changed:
                parts.append("fast drop sensing")
            if style_changed:
                parts.append(f"style {cfg.detection.style}")
            if silence_changed:
                parts.append(
                    "pause if silent on" if cfg.detection.silence_gate else "pause if silent off"
                )
            if server_changed:
                parts.append(
                    "output at startup on"
                    if cfg.server.startup_output
                    else "output at startup off"
                )
                if cfg.server.reboot_enabled and cfg.server.reboot_days:
                    days = "/".join(cfg.server.reboot_days)
                    parts.append(f"reboot {days} at {cfg.server.reboot_time}")
                else:
                    parts.append("scheduled reboot off")
            if "four_four_intensity" in policy_updates or four_four_smooth_changed:
                pol = self.bridge.policy if self.bridge is not None else None
                ffi = pol.four_four_intensity if pol else cfg.detection.four_four_intensity
                parts.append(
                    f"4/4 sensitivity {ffi:.0%}, smoothing {cfg.detection.four_four_smooth_seconds:g}s"
                )
            if "breakdown_intensity" in policy_updates or "breakdown_beats" in policy_updates:
                pol = self.bridge.policy if self.bridge is not None else None
                bdi = pol.breakdown_intensity if pol else cfg.detection.breakdown_intensity
                bdb = pol.breakdown_beats if pol else cfg.detection.breakdown_beats
                parts.append(f"breakdown {bdi:.0%}, {bdb:g} beats")
            if drop_hit_changed:
                parts.append(
                    f"drop hit {cfg.detection.drop_hit_seconds:g}s, "
                    f"fade {cfg.detection.drop_hit_fade_seconds:g}s"
                )
            if input_changed:
                parts.append(
                    f"noise floor {cfg.audio.noise_floor_db:g} dBFS, "
                    f"visualizer {cfg.ui.visualizer_mode}"
                )
            if schedule_changed:
                try:
                    cron_root = (
                        Path(self.config_path).resolve().parent if self.config_path else None
                    )
                    cron_note = apply_host_schedule(cfg.server, root=cron_root)
                    if cron_note:
                        parts.append(cron_note)
                except Exception as exc:
                    parts.append(f"schedule not installed: {exc}")

            note = " and ".join(parts) if parts else "settings updated"
            if learned_credits and cfg.genre.learning_enabled:
                tags_str = " · ".join(
                    f"{ARCHETYPE_LABELS.get(k, k)} {int(round(w * 100))}%"
                    for k, w in learned_credits[:2]
                )
                note += f" [learned into {tags_str}]"
            if self.dry_run and audio_changed:
                note += " (dry run: not sending)"
            return "Applied: " + note

    @staticmethod
    def _apply_drop_sensing(det, raw: dict | None) -> bool:
        if not isinstance(raw, dict):
            return False
        changed = False
        for key, (lo, hi) in _DROP_SENSING_FIELDS.items():
            if key not in raw:
                continue
            try:
                value = float(raw[key])
            except (TypeError, ValueError):
                continue
            value = max(lo, min(hi, value))
            if getattr(det, key) != value:
                setattr(det, key, value)
                changed = True
        for key in ("drop_from_groove", "drop_use_onset"):
            if key not in raw:
                continue
            value = bool(raw[key])
            if bool(getattr(det, key)) != value:
                setattr(det, key, value)
                changed = True
        return changed

    def _persist(self) -> None:
        """Write changed values back into config.toml, keeping comments."""
        if self.config_path is None:
            return
        path = Path(self.config_path)
        if not path.exists():
            return
        text = path.read_text()
        text = self._ensure_section(text, "network")
        text = self._ensure_section(text, "genre")
        text = self._ensure_section(text, "fx_size")
        text = self._ensure_section(text, "ui")
        text = self._upsert_in_section(text, "network", "interface", self.cfg.network.interface)
        text = self._upsert_in_section(text, "network", "ip", self.cfg.network.ip)
        text = self._upsert_in_section(text, "artnet", "protocol", self.cfg.artnet.protocol)
        text = self._upsert_in_section(text, "artnet", "mode", self.cfg.artnet.mode)
        text = self._upsert_in_section(text, "artnet", "port", self.cfg.artnet.port)
        text = self._upsert_in_section(text, "artnet", "universe", self.cfg.artnet.universe)
        text = self._upsert_in_section(text, "artnet", "priority", self.cfg.artnet.priority)
        text = self._upsert_in_section(text, "artnet", "enabled", self.cfg.artnet.enabled)
        text = self._upsert_in_section(text, "audio", "device", self.cfg.audio.device)
        text = self._upsert_in_section(
            text, "audio", "input_channel", self.cfg.audio.input_channel
        )
        text = self._upsert_in_section(
            text, "audio", "noise_floor_db", float(self.cfg.audio.noise_floor_db)
        )
        text = self._upsert_in_section(text, "genre", "enabled", bool(self.cfg.genre.enabled))
        text = self._upsert_in_section(
            text, "genre", "learning_enabled", bool(self.cfg.genre.learning_enabled)
        )
        text = self._upsert_in_section(text, "genre", "mode", self.cfg.genre.mode)
        text = self._upsert_in_section(text, "fx_size", "base", int(self.cfg.fx_size.base))
        text = self._upsert_in_section(text, "fx_size", "max", int(self.cfg.fx_size.max))
        text = self._upsert_in_section(
            text, "fx_size", "reactivity", float(self.cfg.fx_size.reactivity)
        )
        det = self.cfg.detection
        for key in (
            "build_intensity",
            "drop_intensity",
            "build_decay_seconds",
            "drop_hold_seconds",
            "drop_energy_jump",
            "drop_rise_window",
            "drop_rise",
            "drop_min_bass",
            "drop_min_state_seconds",
            "drop_smooth_seconds",
            "drop_from_groove",
            "drop_use_onset",
            "four_four_intensity",
            "four_four_smooth_seconds",
            "breakdown_intensity",
            "breakdown_beats",
            "drop_hit_seconds",
            "drop_hit_fade_seconds",
            "style",
            "silence_gate",
            "silence_timeout_seconds",
        ):
            text = self._upsert_in_section(text, "detection", key, getattr(det, key))
        text = self._persist_bpm_rate(text)
        text = self._upsert_in_section(text, "ui", "visualizer", self.cfg.ui.visualizer)
        text = self._upsert_in_section(
            text, "ui", "visualizer_mode", self.cfg.ui.visualizer_mode
        )
        text = self._ensure_section(text, "server")
        srv = self.cfg.server
        text = self._upsert_in_section(text, "server", "startup_output", bool(srv.startup_output))
        text = self._upsert_in_section(text, "server", "reboot_enabled", bool(srv.reboot_enabled))
        text = self._upsert_in_section(text, "server", "reboot_time", srv.reboot_time)
        text = self._upsert_in_section(text, "server", "reboot_days", list(srv.reboot_days))
        path.write_text(text)

    @staticmethod
    def _ensure_section(text: str, section: str) -> str:
        if re.search(rf"(?m)^\[{re.escape(section)}\]\s*$", text):
            return text
        return text.rstrip() + f"\n\n[{section}]\n"

    @staticmethod
    def _upsert_in_section(text: str, section: str, key: str, value) -> str:
        if isinstance(value, bool):
            line = f"{key} = {'true' if value else 'false'}"
        elif isinstance(value, int) and not isinstance(value, bool):
            line = f"{key} = {int(value)}"
        elif isinstance(value, float):
            line = f"{key} = {value:g}"
        elif isinstance(value, (list, tuple)):
            inner = ", ".join(f'"{item}"' for item in value)
            line = f"{key} = [{inner}]"
        else:
            line = f'{key} = "{value}"'
        pattern = rf"(?ms)(^\[{re.escape(section)}\]\s*\n)(.*?)(?=^\[|\Z)"

        def repl(match: re.Match) -> str:
            body = match.group(2)
            key_pat = rf"(?m)^{re.escape(key)} = .*"
            if re.search(key_pat, body):
                body = re.sub(key_pat, line, body, count=1)
            else:
                body = line + "\n" + body
            return match.group(1) + body

        if re.search(rf"(?m)^\[{re.escape(section)}\]\s*$", text):
            return re.sub(pattern, repl, text, count=1)
        return text

    def _persist_bpm_rate(self, text: str) -> str:
        br = self.cfg.bpm_rate
        section = (
            "[bpm_rate]\n"
            f'min = "{br.min}"\n'
            f'max = "{br.max}"\n'
            f"decay_seconds = {br.decay_seconds:g}\n"
        )
        if re.search(r"(?m)^\[bpm_rate\]\s*$", text):
            return re.sub(
                r"(?m)^\[bpm_rate\]\s*\n(?:.*\n)*?(?=^\[|\Z)",
                section + "\n",
                text,
                count=1,
            )
        return text.rstrip() + "\n\n" + section


# ----------------------------------------------------------------------
# Shared HTTP Route Logic (used by both aiohttp and stdlib asyncio server)
# ----------------------------------------------------------------------


def _validate_and_apply_settings(manager: _Manager, body: dict) -> tuple[int, dict]:
    device = body.get("device")
    input_channel = body.get("input_channel")
    ip = body.get("ip")
    universe = body.get("universe")
    bpm_rate_min = body.get("bpm_rate_min")
    bpm_rate_max = body.get("bpm_rate_max")
    bpm_rate_decay = body.get("bpm_rate_decay")
    fx_size_base = body.get("fx_size_base")
    fx_size_max = body.get("fx_size_max")
    fx_size_reactivity = body.get("fx_size_reactivity")
    intensity = body.get("intensity")
    build_intensity = body.get("build_intensity")
    drop_intensity = body.get("drop_intensity")
    build_decay_seconds = body.get("build_decay_seconds")
    drop_hold_seconds = body.get("drop_hold_seconds")
    protocol = body.get("protocol")
    mode = body.get("mode")
    port = body.get("port")
    priority = body.get("priority")
    interface = body.get("interface")
    drop_sensing = body.get("drop_sensing")
    style = body.get("style")
    genre_mode = body.get("genre_mode")
    learning_enabled = body.get("learning_enabled")
    silence_gate = body.get("silence_gate")
    noise_floor_db = body.get("noise_floor_db")
    visualizer_mode = body.get("visualizer_mode")
    four_four_intensity = body.get("four_four_intensity")
    four_four_smooth_seconds = body.get("four_four_smooth_seconds")
    breakdown_intensity = body.get("breakdown_intensity")
    breakdown_beats = body.get("breakdown_beats")
    drop_hit_seconds = body.get("drop_hit_seconds")
    drop_hit_fade_seconds = body.get("drop_hit_fade_seconds")
    startup_output = body.get("startup_output")
    reboot_enabled = body.get("reboot_enabled")
    reboot_time = body.get("reboot_time")
    reboot_days = body.get("reboot_days")

    if drop_sensing is not None and not isinstance(drop_sensing, dict):
        return 400, {"error": "drop_sensing must be an object"}
    if protocol is not None:
        protocol = str(protocol).lower()
        if protocol not in OUTPUT_PROTOCOLS:
            return 400, {"error": "protocol must be artnet or sacn"}
    if mode is not None:
        mode = str(mode).lower()
        proto_for_mode = protocol or manager.cfg.artnet.protocol
        if mode not in allowed_modes(proto_for_mode):
            return 400, {
                "error": (
                    "Art-Net destination must be unicast, broadcast, or localhost"
                    if proto_for_mode != "sacn"
                    else "sACN destination must be unicast, multicast, or localhost"
                )
            }
    if interface is not None:
        interface = str(interface).strip()
        if interface.lower() in ("localhost", "lo", "127.0.0.1"):
            interface = ""
            mode = "localhost"
        elif interface:
            known = {i["name"] for i in list_ipv4_interfaces()}
            if interface not in known:
                return 400, {"error": f"unknown interface: {interface}"}
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            return 400, {"error": "port must be a number"}
        if not 1 <= port <= 65535:
            return 400, {"error": "port out of range"}
    if priority is not None:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            return 400, {"error": "priority must be a number"}
        if not 0 <= priority <= 200:
            return 400, {"error": "priority must be 0–200"}
    if ip is not None:
        ip = str(ip).strip()
        audio_mode_now = coerce_mode(
            protocol or manager.cfg.artnet.protocol,
            mode or manager.cfg.artnet.mode,
        )
        if audio_mode_now != "unicast":
            ip = None
        else:
            if not ip:
                return 400, {"error": "IP must not be empty"}
            if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) and ip != "localhost":
                return 400, {"error": "IP must look like 192.168.1.100"}
    if device is not None:
        device = str(device)
    if input_channel is not None:
        raw = str(input_channel).strip().lower()
        aliases = ("l", "r", "1", "2", "stereo", "mix", "lr", "l+r")
        if raw not in INPUT_CHANNEL_CHOICES and raw not in aliases:
            return 400, {"error": "input_channel must be left, right, or both"}
        input_channel = coerce_input_channel(raw)
    if universe is not None:
        try:
            universe = int(universe)
        except (TypeError, ValueError):
            return 400, {"error": "universe must be a number"}
        proto_u = protocol or manager.cfg.artnet.protocol
        hi = 63999 if proto_u == "sacn" else 32767
        lo = 1 if proto_u == "sacn" else 0
        if not lo <= universe <= hi:
            return 400, {"error": "universe out of range"}
    if style is not None:
        style = coerce_style(str(style))
        if style not in STYLE_NAMES:
            return 400, {"error": "unknown style"}
    if genre_mode is not None:
        raw_gm = str(genre_mode).strip().lower().replace("-", "_").replace(" ", "_")
        coerced_gm = coerce_genre_mode(raw_gm)
        if raw_gm not in GENRE_MODES and coerced_gm == "auto" and raw_gm not in ("auto", "adaptive"):
            return 400, {"error": f"unknown genre_mode: {genre_mode}"}
        genre_mode = coerced_gm
    if learning_enabled is not None:
        learning_enabled = bool(learning_enabled)
    if silence_gate is not None:
        silence_gate = bool(silence_gate)
    if startup_output is not None:
        startup_output = bool(startup_output)
    if reboot_enabled is not None:
        reboot_enabled = bool(reboot_enabled)
    if reboot_time is not None:
        parsed_time = try_reboot_time(reboot_time)
        if parsed_time is None:
            return 400, {"error": "reboot_time must be HH:MM"}
        reboot_time = parsed_time
    if reboot_days is not None:
        reboot_days = coerce_reboot_days(reboot_days)
        enabled_now = (
            reboot_enabled
            if reboot_enabled is not None
            else manager.cfg.server.reboot_enabled
        )
        if enabled_now and not reboot_days:
            return 400, {"error": "pick at least one reboot day"}
    if bpm_rate_min is not None:
        bpm_rate_min = str(bpm_rate_min).lower()
        if bpm_rate_min not in BPM_RATE_NAMES:
            return 400, {"error": "bpm_rate_min must be eighth/quarter/half/full"}
    if bpm_rate_max is not None:
        bpm_rate_max = str(bpm_rate_max).lower()
        if bpm_rate_max not in BPM_RATE_NAMES:
            return 400, {"error": "bpm_rate_max must be eighth/quarter/half/full"}
    if bpm_rate_decay is not None:
        try:
            bpm_rate_decay = float(bpm_rate_decay)
        except (TypeError, ValueError):
            return 400, {"error": "bpm_rate_decay must be a number"}
        if not 0.0 <= bpm_rate_decay <= 120.0:
            return 400, {"error": "bpm_rate_decay must be 0–120 seconds"}
    if fx_size_base is not None:
        try:
            fx_size_base = int(round(float(fx_size_base)))
        except (TypeError, ValueError):
            return 400, {"error": "fx_size_base must be a number"}
        if not 0 <= fx_size_base <= 255:
            return 400, {"error": "fx_size_base must be 0–255"}
    if fx_size_max is not None:
        try:
            fx_size_max = int(round(float(fx_size_max)))
        except (TypeError, ValueError):
            return 400, {"error": "fx_size_max must be a number"}
        if not 16 <= fx_size_max <= 255:
            return 400, {"error": "fx_size_max must be 16–255"}
    if fx_size_reactivity is not None:
        try:
            fx_size_reactivity = float(fx_size_reactivity)
        except (TypeError, ValueError):
            return 400, {"error": "fx_size_reactivity must be a number"}
        if not 0.0 <= fx_size_reactivity <= 1.0:
            return 400, {"error": "fx_size_reactivity must be 0–1"}

    validated_floats: dict[str, float | None] = {
        "intensity": None,
        "build_intensity": None,
        "drop_intensity": None,
        "build_decay_seconds": None,
        "drop_hold_seconds": None,
        "four_four_intensity": None,
        "four_four_smooth_seconds": None,
        "breakdown_intensity": None,
        "breakdown_beats": None,
        "drop_hit_seconds": None,
        "drop_hit_fade_seconds": None,
    }
    for field_name, val_obj, lo_v, hi_v in (
        ("intensity", intensity, 0.0, 1.0),
        ("build_intensity", build_intensity, 0.0, 1.0),
        ("drop_intensity", drop_intensity, 0.0, 1.0),
        ("build_decay_seconds", build_decay_seconds, 0.25, 60.0),
        ("drop_hold_seconds", drop_hold_seconds, 2.0, 120.0),
        ("four_four_intensity", four_four_intensity, 0.0, 1.0),
        ("four_four_smooth_seconds", four_four_smooth_seconds, 0.0, 30.0),
        ("breakdown_intensity", breakdown_intensity, 0.0, 1.0),
        ("breakdown_beats", breakdown_beats, 1.0, 16.0),
        ("drop_hit_seconds", drop_hit_seconds, 0.0, 5.0),
        ("drop_hit_fade_seconds", drop_hit_fade_seconds, 0.0, 5.0),
    ):
        if val_obj is not None:
            try:
                fval = float(val_obj)
            except (TypeError, ValueError):
                return 400, {"error": f"{field_name} must be a number"}
            if not lo_v <= fval <= hi_v:
                return 400, {"error": f"{field_name} must be {lo_v:g}–{hi_v:g}"}
            validated_floats[field_name] = fval

    if noise_floor_db is not None:
        try:
            noise_floor_db = float(noise_floor_db)
        except (TypeError, ValueError):
            return 400, {"error": "noise_floor_db must be a number"}
        lo_nf, hi_nf = NOISE_FLOOR_RANGE
        if not lo_nf <= noise_floor_db <= hi_nf:
            return 400, {"error": f"noise floor must be {lo_nf:g}–{hi_nf:g} dBFS"}
    if visualizer_mode is not None:
        visualizer_mode = str(visualizer_mode).strip().lower()
        if visualizer_mode not in VISUALIZER_MODES:
            return 400, {"error": "visualizer_mode must be auto, absolute, or level"}

    try:
        message = manager.apply_settings(
            device=device,
            ip=ip,
            universe=universe,
            bpm_rate_min=bpm_rate_min,
            bpm_rate_max=bpm_rate_max,
            bpm_rate_decay=bpm_rate_decay,
            fx_size_base=fx_size_base,
            fx_size_max=fx_size_max,
            fx_size_reactivity=fx_size_reactivity,
            intensity=validated_floats["intensity"],
            build_intensity=validated_floats["build_intensity"],
            drop_intensity=validated_floats["drop_intensity"],
            build_decay_seconds=validated_floats["build_decay_seconds"],
            drop_hold_seconds=validated_floats["drop_hold_seconds"],
            protocol=protocol,
            mode=mode,
            port=port,
            priority=priority,
            interface=interface,
            input_channel=input_channel,
            drop_sensing=drop_sensing,
            style=style,
            genre_mode=genre_mode,
            learning_enabled=learning_enabled,
            silence_gate=silence_gate,
            noise_floor_db=noise_floor_db,
            visualizer_mode=visualizer_mode,
            four_four_intensity=validated_floats["four_four_intensity"],
            four_four_smooth_seconds=validated_floats["four_four_smooth_seconds"],
            breakdown_intensity=validated_floats["breakdown_intensity"],
            breakdown_beats=validated_floats["breakdown_beats"],
            drop_hit_seconds=validated_floats["drop_hit_seconds"],
            drop_hit_fade_seconds=validated_floats["drop_hit_fade_seconds"],
            startup_output=startup_output,
            reboot_enabled=reboot_enabled,
            reboot_time=reboot_time,
            reboot_days=reboot_days,
        )
    except Exception as exc:
        return 400, {"error": str(exc)}
    return 200, {"ok": True, "message": message}


def _dispatch_get_sync(manager: _Manager, path: str) -> tuple[int, str, bytes]:
    if path == "/":
        html = (static_dir() / "index.html").read_bytes()
        return 200, "text/html; charset=utf-8", html
    if path == "/devices":
        from .audio import list_input_device_names

        names = list_input_device_names()
        return (
            200,
            "application/json",
            json.dumps({"devices": names, "current": manager.cfg.audio.device}).encode(),
        )
    if path == "/api/config":
        from .audio import list_input_device_names

        names = list_input_device_names()
        ifaces = list_ipv4_interfaces()
        return (
            200,
            "application/json",
            json.dumps(
                {
                    "meta": manager._meta(),
                    "devices": names,
                    "interfaces": ifaces,
                    "layout": manager.layout,
                }
            ).encode(),
        )
    if path == "/api/sections":
        return (
            200,
            "application/json",
            json.dumps({"sections": manager.sections(), "seq": manager.log_seq}).encode(),
        )
    if path == "/api/genre/profiles":
        return (
            200,
            "application/json",
            json.dumps(manager.genre_profiles_snapshot()).encode(),
        )
    if path == "/api/layout":
        return (
            200,
            "application/json",
            json.dumps({"layout": manager.layout, "boxes": list(_LAYOUT_BOX_IDS)}).encode(),
        )
    return 404, "application/json", json.dumps({"error": "not found"}).encode()


def _dispatch_post_sync(manager: _Manager, path: str, raw_body: bytes) -> tuple[int, dict]:
    if path == "/reboot":
        def _reboot() -> None:
            try:
                manager.stop_all()
            except Exception:
                pass
            for cmd in (
                ["sudo", "-n", "/sbin/reboot"],
                ["sudo", "-n", "/usr/sbin/reboot"],
                ["sudo", "-n", "reboot"],
            ):
                try:
                    subprocess.Popen(cmd)  # noqa: S603
                    return
                except OSError:
                    continue

        threading.Timer(0.4, _reboot).start()
        return 200, {"ok": True, "message": "Rebooting…"}

    if path == "/shutdown":
        def _exit() -> None:
            try:
                manager.stop_all()
            finally:
                os._exit(0)

        threading.Timer(0.25, _exit).start()
        return 200, {"ok": True, "message": "Shutting down…"}

    if path == "/restart":
        def _restart() -> None:
            try:
                manager.stop_all()
            except Exception:
                pass
            env = dict(os.environ)
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            has_systemd = (
                subprocess.run(
                    ["systemctl", "--user", "is-enabled", "magicq-venue.service"],
                    env=env,
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            )
            if has_systemd:
                subprocess.Popen(  # noqa: S603
                    ["systemctl", "--user", "restart", "magicq-venue.service"],
                    env=env,
                    start_new_session=True,
                )
                os._exit(0)
            root = (
                Path(manager.config_path).resolve().parent
                if manager.config_path
                else Path.cwd()
            )
            start = root / "packaging" / "start-companion.sh"
            config = (
                str(Path(manager.config_path).resolve())
                if manager.config_path
                else str(root / "config.toml")
            )
            lock = "/tmp/magicq-companion.lock"
            if start.is_file():
                cmd = f"sleep 1; rm -f {lock}; nohup {start} >/dev/null 2>&1 &"
            else:
                py = sys.executable
                cmd = (
                    f"sleep 1; rm -f {lock}; cd {root}; "
                    f"nohup {py} -m magicq_companion --ui --config {config} "
                    f">> {root / 'companion.log'} 2>&1 &"
                )
            subprocess.Popen(  # noqa: S603
                ["bash", "-lc", cmd],
                start_new_session=True,
            )
            os._exit(0)

        threading.Timer(0.35, _restart).start()
        return 200, {"ok": True, "message": "Restarting companion…"}

    try:
        body = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return 400, {"error": "invalid JSON"}
    if not isinstance(body, dict):
        return 400, {"error": "JSON body must be an object"}

    if path == "/api/genre/reset":
        try:
            msg = manager.reset_genre_profile(body.get("archetype"))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        return 200, {"ok": True, "message": msg}

    if path == "/api/layout":
        layout_data = body.get("layout", body)
        if not isinstance(layout_data, dict):
            return 400, {"error": "layout must be an object"}
        try:
            layout = manager.save_layout(layout_data)
        except (OSError, ValueError, TypeError) as exc:
            return 400, {"error": str(exc)}
        return 200, {"ok": True, "message": "Layout saved", "layout": layout}

    if path == "/enable":
        if "enabled" not in body and "analyzing" not in body and "visualizer" not in body:
            return 400, {"error": "enabled, analyzing, and/or visualizer required"}
        parts: list[str] = []
        if "enabled" in body:
            parts.append(manager.set_enabled(bool(body["enabled"])))
        if "analyzing" in body:
            parts.append(manager.set_analyzing(bool(body["analyzing"])))
        if "visualizer" in body:
            parts.append(manager.set_visualizer(bool(body["visualizer"])))
        return 200, {
            "ok": True,
            "message": "; ".join(parts),
            "enabled": manager.enabled,
            "analyzing": manager.analyzing,
            "visualizer": manager.visualizer,
        }

    if path == "/settings":
        return _validate_and_apply_settings(manager, body)

    return 404, {"error": "not found"}


# ----------------------------------------------------------------------
# Zero-Dependency Stdlib asyncio HTTP + RFC 6455 WebSocket Fallback
# ----------------------------------------------------------------------


def _ws_encode_text(text: str) -> bytes:
    data = text.encode("utf-8")
    n = len(data)
    if n < 126:
        header = bytes((0x81, n))
    elif n < 65536:
        header = struct.pack("!BBH", 0x81, 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 127, n)
    return header + data


async def _ws_drain_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            hdr = await reader.readexactly(2)
            opcode = hdr[0] & 0x0F
            masked = (hdr[1] & 0x80) != 0
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", await reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await reader.readexactly(8))[0]
            mask_key = await reader.readexactly(4) if masked else b""
            payload = await reader.readexactly(length) if length else b""
            if masked and payload:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                writer.write(b"\x88\x00")
                await writer.drain()
                break
            if opcode == 0x9:
                pong_len = min(len(payload), 125)
                writer.write(bytes((0x8A, pong_len)) + payload[:pong_len])
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return


async def _run_stdlib_server(manager: _Manager, host: str, port: int) -> None:
    loop = asyncio.get_running_loop()

    async def handle_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        except Exception:
            writer.close()
            return

        lines = raw_head.decode("iso-8859-1", errors="replace").split("\r\n")
        if not lines or not lines[0]:
            writer.close()
            return
        parts = lines[0].split(" ")
        if len(parts) < 2:
            writer.close()
            return
        method = parts[0].upper()
        path = parts[1].split("?", 1)[0]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        if (
            method == "GET"
            and path == "/ws"
            and headers.get("upgrade", "").lower() == "websocket"
            and "sec-websocket-key" in headers
        ):
            key = headers["sec-websocket-key"]
            accept = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            handshake = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            )
            writer.write(handshake.encode("ascii"))
            await writer.drain()
            drain_task = asyncio.create_task(_ws_drain_client(reader, writer))
            try:
                while not drain_task.done():
                    with manager.state_lock:
                        payload = json.dumps(manager.state)
                    writer.write(_ws_encode_text(payload))
                    await writer.drain()
                    await asyncio.sleep(_BROADCAST_INTERVAL)
            except (ConnectionError, OSError):
                pass
            finally:
                drain_task.cancel()
                writer.close()
            return

        content_len = int(headers.get("content-length") or "0")
        body = b""
        if content_len > 0:
            try:
                body = await asyncio.wait_for(reader.readexactly(content_len), timeout=10.0)
            except Exception:
                writer.close()
                return

        if method == "GET":
            status, ctype, payload_bytes = await loop.run_in_executor(
                None, _dispatch_get_sync, manager, path
            )
        elif method == "POST":
            status, resp_obj = await loop.run_in_executor(
                None, _dispatch_post_sync, manager, path, body
            )
            ctype = "application/json"
            payload_bytes = json.dumps(resp_obj).encode("utf-8")
        else:
            status = 405
            ctype = "application/json"
            payload_bytes = b'{"error":"method not allowed"}'

        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}.get(
            status, "OK"
        )
        resp_head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(payload_bytes)}\r\n"
            "Cache-Control: no-store, no-cache, must-revalidate\r\n"
            "Connection: close\r\n\r\n"
        )
        try:
            writer.write(resp_head.encode("ascii") + payload_bytes)
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle_client, host=host, port=port)
    async with server:
        await server.serve_forever()


def build_aiohttp_app(manager: _Manager):
    """Build the `aiohttp.web.Application` when `aiohttp` is installed."""
    from aiohttp import web

    async def get_route(request: web.Request) -> web.Response:
        loop = asyncio.get_running_loop()
        status, ctype, body = await loop.run_in_executor(
            None, _dispatch_get_sync, manager, request.path
        )
        return web.Response(
            body=body,
            status=status,
            content_type=ctype.split(";", 1)[0],
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    async def post_route(request: web.Request) -> web.Response:
        raw = await request.read()
        loop = asyncio.get_running_loop()
        status, obj = await loop.run_in_executor(
            None, _dispatch_post_sync, manager, request.path, raw
        )
        return web.json_response(obj, status=status)

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        try:
            while not ws.closed:
                with manager.state_lock:
                    payload = json.dumps(manager.state)
                await ws.send_str(payload)
                await asyncio.sleep(_BROADCAST_INTERVAL)
        except ConnectionResetError:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/", get_route)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/devices", get_route)
    app.router.add_get("/api/config", get_route)
    app.router.add_get("/api/sections", get_route)
    app.router.add_get("/api/genre/profiles", get_route)
    app.router.add_get("/api/layout", get_route)
    app.router.add_post("/api/layout", post_route)
    app.router.add_post("/api/genre/reset", post_route)
    app.router.add_post("/settings", post_route)
    app.router.add_post("/enable", post_route)
    app.router.add_post("/reboot", post_route)
    app.router.add_post("/restart", post_route)
    app.router.add_post("/shutdown", post_route)
    return app


def run_ui(
    cfg: Config,
    source_factory: Callable,
    dry_run: bool,
    pace: bool,
    duration: float | None,
    config_path: str | None,
    simulate: bool,
    port: int,
) -> None:
    manager = _Manager(
        cfg, source_factory, dry_run, pace, duration, config_path, simulate
    )
    prebuilt = None
    if simulate:
        prebuilt = source_factory(cfg)
    manager.start(prebuilt)
    with manager.state_lock:
        manager.state["meta"] = manager._meta()
    print(f"Source: {manager.source_desc}")
    print(f"Audio: {manager._meta()['artnet']}")
    print(f"Genre agent: {cfg.genre.mode} ({manager._meta()['genre_backend']})")
    print(f"Dashboard: http://localhost:{port}")

    try:
        try:
            from aiohttp import web  # type: ignore[import-not-found]

            app = build_aiohttp_app(manager)
            web.run_app(app, host="0.0.0.0", port=port, print=None)
        except ImportError:
            asyncio.run(_run_stdlib_server(manager, host="0.0.0.0", port=port))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        if getattr(exc, "errno", None) == 98 or "address already in use" in str(exc):
            raise SystemExit(
                f"Port {port} is already in use (another companion UI still running?).\n"
                f"Open the existing dashboard and click Restart companion,\n"
                f"or pick another port:  python -m magicq_companion --ui --ui-port {port + 1}"
            ) from None
        raise
    finally:
        manager.stop_all()
