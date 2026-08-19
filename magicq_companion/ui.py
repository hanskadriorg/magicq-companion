"""Web dashboard: aiohttp server broadcasting analysis state over WebSocket,
with runtime control of the input device and Art-Net target."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from aiohttp import ClientSession, web

from .config import BPM_RATE_NAMES, Config, audio_output_cfg, matter_output_cfg
from .matter_runtime import MatterOutput, MatterSidecar, describe_matter
from .output import (
    OUTPUT_PROTOCOLS,
    allowed_modes,
    coerce_mode,
    create_sender,
    describe_output,
    list_ipv4_interfaces,
)
from .paths import default_layout_path, static_dir
from .pipeline import Bridge

_BROADCAST_INTERVAL = 0.05  # 20 updates per second
_LAYOUT_BOX_IDS = ("settings", "hero", "meters", "spark", "dmx", "log", "matter")
_DEFAULT_LAYOUT = {
    "cols": 1,
        "rows": 7,
    "items": [
        {"id": "settings", "row": 1, "col": 1, "colSpan": 1, "rowSpan": 1},
        {"id": "hero", "row": 2, "col": 1, "colSpan": 1, "rowSpan": 1},
        {"id": "meters", "row": 3, "col": 1, "colSpan": 1, "rowSpan": 1},
        {"id": "spark", "row": 4, "col": 1, "colSpan": 1, "rowSpan": 1},
        {"id": "dmx", "row": 5, "col": 1, "colSpan": 1, "rowSpan": 1},
        {"id": "log", "row": 6, "col": 1, "colSpan": 1, "rowSpan": 1},
        {"id": "matter", "row": 7, "col": 1, "colSpan": 1, "rowSpan": 1},
    ],
}


class _Manager:
    """Owns the analysis worker thread and applies settings changes.

    A device change rebuilds the audio source and restarts the pipeline;
    an Art-Net change just swaps the sender on the running bridge.
    """

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
        self.simulate = simulate

        self.state_lock = threading.Lock()
        self.state: dict = {"running": False}
        self.ctl_lock = threading.Lock()

        self.bridge: Bridge | None = None
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.source_desc = "starting…"
        self.enabled = True
        self.analyzing = True
        self.layout = self._load_layout()
        self.matter_out: MatterOutput | None = None
        self.sidecar = MatterSidecar()

    # ------------------------------------------------------------------

    def start(self, prebuilt: tuple | None = None) -> None:
        blocks, source_desc = prebuilt or self.source_factory(self.cfg)
        sender = None
        if not self.dry_run:
            sender = create_sender(audio_output_cfg(self.cfg))
        if self.matter_out is None:
            self.matter_out = MatterOutput(
                matter_output_cfg(self.cfg), dry_run=self.dry_run
            )
            self.matter_out.enabled = bool(self.cfg.matter.enabled)
        self.source_desc = source_desc
        self.bridge = Bridge(
            self.cfg, blocks, sender=sender, pace=self.pace, duration=self.duration
        )
        self.bridge.enabled = self.enabled
        self.bridge.analyzing = self.analyzing
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._run, daemon=True)
        with self.state_lock:
            self.state["running"] = True
            self.state["enabled"] = self.enabled
            self.state["analyzing"] = self.analyzing
            self.state["meta"] = self._meta()
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.bridge is not None:
            blocks = self.bridge.blocks
            close = getattr(blocks, "close", None)
            if callable(close):
                try:
                    close()  # ends pw-record/arecord / sounddevice wait
                except Exception:
                    pass
        if self.worker is not None:
            self.worker.join(timeout=5)
            self.worker = None

    def stop_all(self) -> None:
        self.stop()
        if self.matter_out is not None:
            self.matter_out.close()
            self.matter_out = None
        self.sidecar.stop()

    def _run(self) -> None:
        try:
            self.bridge.run(on_frame=self._on_frame, stop_event=self.stop_event)
        finally:
            with self.state_lock:
                self.state["running"] = False

    def _on_frame(self, b: Bridge) -> None:
        f = b.features
        with self.state_lock:
            self.state.update(
                t=round(b.t, 2),
                section=b.tracker.state.value,
                t_in_state=round(b.tracker.t_in_state, 1),
                bpm=round(f.bpm, 1),
                energy=round(f.energy_norm, 3),
                bass=round(f.bass_norm, 3),
                build=round(f.build_score, 3),
                melody=round(f.melody_score, 3),
                centroid=round(f.spectral_centroid_norm, 3),
                tilt=round(f.tilt_norm, 3),
                bpm_rate=round(f.bpm_rate),
                ramp=round(b.tracker.build_progress, 3),
                beat_age=round(f.seconds_since_beat, 3),
                dmx=list(b.dmx[:16]),
                enabled=b.enabled,
                analyzing=b.analyzing,
            )

    # ------------------------------------------------------------------

    def _meta(self) -> dict:
        audio_cfg = audio_output_cfg(self.cfg)
        artnet = "dry run, not sending" if self.dry_run else describe_output(audio_cfg)
        br = self.cfg.bpm_rate
        return {
            "source": self.source_desc,
            "artnet": artnet,
            "matter": describe_matter(self.cfg) if not self.dry_run else "Matter dry run",
            "device": self.cfg.audio.device,
            "protocol": self.cfg.artnet.protocol,
            "mode": coerce_mode(self.cfg.artnet.protocol, self.cfg.artnet.mode),
            "interface": self.cfg.network.interface,
            "ip": self.cfg.network.ip,
            "port": self.cfg.artnet.port,
            "universe": self.cfg.artnet.universe,
            "priority": self.cfg.artnet.priority,
            "matter_protocol": self.cfg.matter.protocol,
            "matter_mode": coerce_mode(self.cfg.matter.protocol, self.cfg.matter.mode),
            "matter_port": self.cfg.matter.port,
            "matter_universe": self.cfg.matter.universe,
            "matter_priority": self.cfg.matter.priority,
            "matter_enabled": bool(self.cfg.matter.enabled),
            "bpm_rate_min": br.min,
            "bpm_rate_max": br.max,
            "bpm_rate_decay": br.decay_seconds,
            "intensity": self.cfg.detection.intensity,
            "drop_hold_seconds": self.cfg.detection.drop_hold_seconds,
            "enabled": self.enabled,
            "analyzing": self.analyzing,
            "dry_run": self.dry_run,
            "simulate": self.simulate,
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

    def _normalize_layout(self, data: dict) -> dict:
        cols = max(1, min(4, int(data.get("cols", 1))))
        rows = max(1, min(8, int(data.get("rows", 6))))
        raw_items = data.get("items") or []
        items: list[dict] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            box_id = str(raw.get("id", ""))
            if box_id not in _LAYOUT_BOX_IDS or box_id in seen:
                continue
            col = max(1, min(cols, int(raw.get("col", 1))))
            row = max(1, min(rows, int(raw.get("row", 1))))
            col_span = max(1, min(cols - col + 1, int(raw.get("colSpan", 1))))
            row_span = max(1, min(rows - row + 1, int(raw.get("rowSpan", 1))))
            items.append(
                {
                    "id": box_id,
                    "row": row,
                    "col": col,
                    "colSpan": col_span,
                    "rowSpan": row_span,
                }
            )
            seen.add(box_id)
        # Keep any missing boxes stacked after the placed ones.
        next_row = max((it["row"] + it["rowSpan"] - 1 for it in items), default=0) + 1
        for box_id in _LAYOUT_BOX_IDS:
            if box_id in seen:
                continue
            if next_row > rows:
                rows = min(8, next_row)
            items.append(
                {
                    "id": box_id,
                    "row": min(rows, next_row),
                    "col": 1,
                    "colSpan": 1,
                    "rowSpan": 1,
                }
            )
            next_row += 1
        return {"cols": cols, "rows": rows, "items": items}

    def save_layout(self, data: dict) -> dict:
        layout = self._normalize_layout(data)
        self.layout_path.parent.mkdir(parents=True, exist_ok=True)
        self.layout_path.write_text(json.dumps(layout, indent=2) + "\n")
        self.layout = layout
        return layout

    def set_enabled(self, enabled: bool) -> str:
        with self.ctl_lock:
            self.enabled = bool(enabled)
            if self.bridge is not None:
                self.bridge.enabled = self.enabled
            with self.state_lock:
                self.state["enabled"] = self.enabled
                self.state["meta"] = self._meta()
            return "Output on" if self.enabled else "Output off (Art-Net blackout)"

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
            return "Analyzer off (audio drained, Art-Net blackout; UI stays up)"

    def snapshot(self) -> dict:
        with self.state_lock:
            return dict(self.state)

    def apply_settings(
        self,
        device: str | None,
        ip: str | None,
        universe: int | None,
        bpm_rate_min: str | None = None,
        bpm_rate_max: str | None = None,
        bpm_rate_decay: float | None = None,
        intensity: float | None = None,
        drop_hold_seconds: float | None = None,
        protocol: str | None = None,
        mode: str | None = None,
        port: int | None = None,
        priority: int | None = None,
        interface: str | None = None,
        matter_protocol: str | None = None,
        matter_mode: str | None = None,
        matter_universe: int | None = None,
        matter_port: int | None = None,
        matter_priority: int | None = None,
        matter_enabled: bool | None = None,
    ) -> str:
        with self.ctl_lock:
            cfg = self.cfg
            device_changed = (
                device is not None
                and device != cfg.audio.device
                and not self.simulate
            )
            audio_changed = False
            matter_changed = False
            nic_changed = False
            if interface is not None and interface != cfg.network.interface:
                cfg.network.interface = interface
                cfg.artnet.interface = interface
                nic_changed = True
                audio_changed = True
                matter_changed = True
            if ip is not None and ip != cfg.network.ip:
                cfg.network.ip = ip
                cfg.artnet.ip = ip
                audio_changed = True
                matter_changed = True
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
            if matter_protocol is not None and matter_protocol != cfg.matter.protocol:
                cfg.matter.protocol = matter_protocol
                matter_changed = True
            if matter_mode is not None:
                matter_mode = coerce_mode(cfg.matter.protocol, matter_mode)
                if matter_mode != cfg.matter.mode:
                    cfg.matter.mode = matter_mode
                    matter_changed = True
            elif matter_changed:
                cfg.matter.mode = coerce_mode(cfg.matter.protocol, cfg.matter.mode)
            if matter_universe is not None and matter_universe != cfg.matter.universe:
                cfg.matter.universe = matter_universe
                matter_changed = True
            if matter_port is not None and matter_port != cfg.matter.port:
                cfg.matter.port = matter_port
                matter_changed = True
            if matter_priority is not None and matter_priority != cfg.matter.priority:
                cfg.matter.priority = matter_priority
                matter_changed = True
            if matter_enabled is not None and bool(matter_enabled) != bool(cfg.matter.enabled):
                cfg.matter.enabled = bool(matter_enabled)
                matter_changed = True
            bpm_changed = False
            if bpm_rate_min is not None and bpm_rate_min != cfg.bpm_rate.min:
                cfg.bpm_rate.min = bpm_rate_min
                bpm_changed = True
            if bpm_rate_max is not None and bpm_rate_max != cfg.bpm_rate.max:
                cfg.bpm_rate.max = bpm_rate_max
                bpm_changed = True
            if bpm_rate_decay is not None and bpm_rate_decay != cfg.bpm_rate.decay_seconds:
                cfg.bpm_rate.decay_seconds = bpm_rate_decay
                bpm_changed = True
            intensity_changed = False
            if intensity is not None and intensity != cfg.detection.intensity:
                cfg.detection.intensity = intensity
                intensity_changed = True
            drop_changed = False
            if (
                drop_hold_seconds is not None
                and drop_hold_seconds != cfg.detection.drop_hold_seconds
            ):
                cfg.detection.drop_hold_seconds = drop_hold_seconds
                drop_changed = True

            if (
                not device_changed
                and not audio_changed
                and not matter_changed
                and not bpm_changed
                and not intensity_changed
                and not drop_changed
            ):
                return "No changes to apply."

            if device_changed:
                cfg.audio.device = device
                prebuilt = self.source_factory(cfg)
                self.stop()
                self.start(prebuilt)
            elif audio_changed and not self.dry_run and self.bridge is not None:
                old = self.bridge.sender
                self.bridge.sender = create_sender(audio_output_cfg(cfg))
                if old is not None:
                    old.blackout()
                    old.close()

            if matter_changed and self.matter_out is not None:
                self.matter_out.enabled = bool(cfg.matter.enabled)
                self.matter_out.replace_sender(matter_output_cfg(cfg))
            if nic_changed:
                self._restart_sidecar()

            if bpm_changed and self.bridge is not None:
                self.bridge.extractor.bpm_rate_cfg = cfg.bpm_rate

            with self.state_lock:
                self.state["meta"] = self._meta()
            self._persist()

            parts = []
            if device_changed:
                parts.append(f"input device -> {device or 'system default'}")
            if audio_changed:
                parts.append("audio " + describe_output(audio_output_cfg(cfg)))
            if matter_changed:
                parts.append(describe_matter(cfg))
            if bpm_changed:
                parts.append(
                    f"ch14 rate {cfg.bpm_rate.min}…{cfg.bpm_rate.max}, "
                    f"decay {cfg.bpm_rate.decay_seconds:g}s"
                )
            if intensity_changed:
                parts.append(f"intensity {cfg.detection.intensity:.0%}")
            if drop_changed:
                parts.append(f"drop length {cfg.detection.drop_hold_seconds:g}s")
            note = " and ".join(parts)
            if self.dry_run and (audio_changed or matter_changed):
                note += " (dry run: not sending)"
            return "Applied: " + note

    def _restart_sidecar(self) -> None:
        url = getattr(self, "python_dmx_url", "")
        if not url:
            return
        from .matter_runtime import matter_data_dir

        self.sidecar.restart(
            interface=self.cfg.network.interface,
            python_dmx_url=url,
            data_dir=matter_data_dir(self.config_path),
        )

    def _persist(self) -> None:
        """Write the changed values back into config.toml, keeping comments."""
        if self.config_path is None:
            return
        path = Path(self.config_path)
        text = path.read_text()
        text = self._ensure_section(text, "network")
        text = self._ensure_section(text, "matter")
        text = self._upsert_in_section(text, "network", "interface", self.cfg.network.interface)
        text = self._upsert_in_section(text, "network", "ip", self.cfg.network.ip)
        text = self._upsert_in_section(text, "artnet", "protocol", self.cfg.artnet.protocol)
        text = self._upsert_in_section(text, "artnet", "mode", self.cfg.artnet.mode)
        text = self._upsert_in_section(text, "artnet", "port", self.cfg.artnet.port)
        text = self._upsert_in_section(text, "artnet", "universe", self.cfg.artnet.universe)
        text = self._upsert_in_section(text, "artnet", "priority", self.cfg.artnet.priority)
        text = self._upsert_in_section(text, "matter", "enabled", self.cfg.matter.enabled)
        text = self._upsert_in_section(text, "matter", "protocol", self.cfg.matter.protocol)
        text = self._upsert_in_section(text, "matter", "mode", self.cfg.matter.mode)
        text = self._upsert_in_section(text, "matter", "port", self.cfg.matter.port)
        text = self._upsert_in_section(text, "matter", "universe", self.cfg.matter.universe)
        text = self._upsert_in_section(text, "matter", "priority", self.cfg.matter.priority)
        text = self._upsert_in_section(text, "audio", "device", self.cfg.audio.device)
        text = self._upsert_in_section(
            text, "detection", "intensity", self.cfg.detection.intensity
        )
        text = self._upsert_in_section(
            text, "detection", "drop_hold_seconds", self.cfg.detection.drop_hold_seconds
        )
        text = self._persist_bpm_rate(text)
        path.write_text(text)

    def _ensure_section(self, text: str, section: str) -> str:
        if re.search(rf"(?m)^\[{re.escape(section)}\]\s*$", text):
            return text
        return text.rstrip() + f"\n\n[{section}]\n"

    def _upsert_in_section(self, text: str, section: str, key: str, value) -> str:
        if isinstance(value, bool):
            line = f"{key} = {'true' if value else 'false'}"
        elif isinstance(value, int) and not isinstance(value, bool):
            line = f"{key} = {int(value)}"
        elif isinstance(value, float):
            line = f"{key} = {value:g}"
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

    def _upsert_toml_str(self, text: str, key: str, value: str) -> str:
        return self._upsert_in_section(text, "artnet", key, value)

    def _upsert_toml_int(self, text: str, key: str, value: int) -> str:
        return self._upsert_in_section(text, "artnet", key, value)

    def _persist_bpm_rate(self, text: str) -> str:
        """Update or append the [bpm_rate] section."""
        br = self.cfg.bpm_rate
        section = (
            "[bpm_rate]\n"
            f'min = "{br.min}"\n'
            f'max = "{br.max}"\n'
            f"decay_seconds = {br.decay_seconds:g}\n"
        )
        if re.search(r"(?m)^\[bpm_rate\]\s*$", text):
            text = re.sub(
                r"(?m)^\[bpm_rate\]\s*\n(?:.*\n)*?(?=^\[|\Z)",
                section + "\n",
                text,
                count=1,
            )
            return text
        return text.rstrip() + "\n\n" + section


def dashboard_urls(host: str, port: int) -> list[str]:
    """Human-readable dashboard URLs for startup logs."""
    if host not in ("0.0.0.0", "::", ""):
        h = "127.0.0.1" if host in ("localhost", "::1") else host
        return [f"http://{h}:{port}"]

    from .netif import list_ipv4_interfaces

    urls: list[str] = []
    seen: set[str] = set()
    for nic in list_ipv4_interfaces():
        ip = (nic.get("ip") or "").strip()
        if not ip or ip.startswith("127.") or ip in seen:
            continue
        seen.add(ip)
        urls.append(f"http://{ip}:{port}")
    if not urls:
        urls.append(f"http://127.0.0.1:{port}")
    return urls


def _print_dashboard_urls(host: str, port: int) -> None:
    urls = dashboard_urls(host, port)
    print(f"Dashboard (this Pi): http://127.0.0.1:{port}")
    if host in ("0.0.0.0", "::", "") and len(urls) > 0:
        print("Dashboard (LAN — use from phone/tablet/FOH):")
        for url in urls:
            print(f"  {url}")
        print(
            "If other devices cannot connect, open the firewall:\n"
            f"  sudo ufw allow {port}/tcp\n"
            "  (or run: bash packaging/open-dashboard-port.sh)"
        )
    elif host not in ("0.0.0.0", "::", ""):
        print(f"Dashboard: {urls[0]}")


def run_ui(
    cfg: Config,
    source_factory: Callable,
    dry_run: bool,
    pace: bool,
    duration: float | None,
    config_path: str | None,
    simulate: bool,
    port: int,
    host: str = "0.0.0.0",
) -> None:
    manager = _Manager(
        cfg, source_factory, dry_run, pace, duration, config_path, simulate
    )
    manager.start()
    manager.python_dmx_url = f"http://127.0.0.1:{port}/internal/matter/dmx"
    from .matter_runtime import matter_data_dir

    manager.sidecar.start(
        interface=cfg.network.interface,
        python_dmx_url=manager.python_dmx_url,
        data_dir=matter_data_dir(config_path),
    )
    print(f"Source: {manager.source_desc}")
    print(f"Audio: {manager._meta()['artnet']}")
    print(f"Matter: {manager._meta()['matter']}")

    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            static_dir() / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )

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

    async def devices_handler(_request: web.Request) -> web.Response:
        from .audio import list_input_device_names

        loop = asyncio.get_running_loop()
        names = await loop.run_in_executor(None, list_input_device_names)
        return web.json_response(
            {"devices": names, "current": cfg.audio.device},
            headers={"Cache-Control": "no-store"},
        )

    async def config_handler(_request: web.Request) -> web.Response:
        """Bootstrap settings without waiting for the WebSocket."""
        from .audio import list_input_device_names

        loop = asyncio.get_running_loop()
        names = await loop.run_in_executor(None, list_input_device_names)
        ifaces = await loop.run_in_executor(None, list_ipv4_interfaces)
        meta = manager._meta()
        return web.json_response(
            {
                "meta": meta,
                "devices": names,
                "interfaces": ifaces,
                "layout": manager.layout,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def settings_handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        device = body.get("device")
        ip = body.get("ip")
        universe = body.get("universe")
        bpm_rate_min = body.get("bpm_rate_min")
        bpm_rate_max = body.get("bpm_rate_max")
        bpm_rate_decay = body.get("bpm_rate_decay")
        intensity = body.get("intensity")
        drop_hold_seconds = body.get("drop_hold_seconds")
        protocol = body.get("protocol")
        mode = body.get("mode")
        port = body.get("port")
        priority = body.get("priority")
        interface = body.get("interface")
        matter_protocol = body.get("matter_protocol")
        matter_mode = body.get("matter_mode")
        matter_universe = body.get("matter_universe")
        matter_port = body.get("matter_port")
        matter_priority = body.get("matter_priority")
        matter_enabled = body.get("matter_enabled")
        if protocol is not None:
            protocol = str(protocol).lower()
            if protocol not in OUTPUT_PROTOCOLS:
                return web.json_response(
                    {"error": "protocol must be artnet or sacn"}, status=400
                )
        if mode is not None:
            mode = str(mode).lower()
            proto_for_mode = protocol or manager.cfg.artnet.protocol
            if mode not in allowed_modes(proto_for_mode):
                return web.json_response(
                    {
                        "error": (
                            "Art-Net destination must be unicast or broadcast"
                            if proto_for_mode != "sacn"
                            else "sACN destination must be unicast or multicast"
                        )
                    },
                    status=400,
                )
        if interface is not None:
            interface = str(interface).strip()
            if interface:
                known = {i["name"] for i in list_ipv4_interfaces()}
                if interface not in known:
                    return web.json_response(
                        {"error": f"unknown interface: {interface}"}, status=400
                    )
        if port is not None:
            try:
                port = int(port)
            except (TypeError, ValueError):
                return web.json_response({"error": "port must be a number"}, status=400)
            if not 1 <= port <= 65535:
                return web.json_response({"error": "port out of range"}, status=400)
        if priority is not None:
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "priority must be a number"}, status=400
                )
            if not 0 <= priority <= 200:
                return web.json_response(
                    {"error": "priority must be 0–200"}, status=400
                )
        if ip is not None:
            ip = str(ip).strip()
            audio_mode_now = coerce_mode(
                protocol or manager.cfg.artnet.protocol,
                mode or manager.cfg.artnet.mode,
            )
            matter_mode_now = coerce_mode(
                matter_protocol or manager.cfg.matter.protocol,
                matter_mode or manager.cfg.matter.mode,
            )
            if audio_mode_now != "unicast" and matter_mode_now != "unicast":
                ip = None
            else:
                if not ip:
                    return web.json_response(
                        {"error": "IP must not be empty"}, status=400
                    )
                if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) and ip != "localhost":
                    return web.json_response(
                        {"error": "IP must look like 192.168.1.100"}, status=400
                    )
        if device is not None:
            device = str(device)
        if universe is not None:
            try:
                universe = int(universe)
            except (TypeError, ValueError):
                return web.json_response({"error": "universe must be a number"}, status=400)
            proto_u = protocol or manager.cfg.artnet.protocol
            hi = 63999 if proto_u == "sacn" else 32767
            lo = 1 if proto_u == "sacn" else 0
            if not lo <= universe <= hi:
                return web.json_response({"error": "universe out of range"}, status=400)
        if matter_protocol is not None:
            matter_protocol = str(matter_protocol).lower()
            if matter_protocol not in OUTPUT_PROTOCOLS:
                return web.json_response(
                    {"error": "matter protocol must be artnet or sacn"}, status=400
                )
        if matter_mode is not None:
            matter_mode = str(matter_mode).lower()
            proto_m = matter_protocol or manager.cfg.matter.protocol
            if matter_mode not in allowed_modes(proto_m):
                return web.json_response(
                    {"error": "Matter destination mode is invalid for that protocol"},
                    status=400,
                )
        if matter_port is not None:
            try:
                matter_port = int(matter_port)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "matter port must be a number"}, status=400
                )
            if not 1 <= matter_port <= 65535:
                return web.json_response({"error": "matter port out of range"}, status=400)
        if matter_priority is not None:
            try:
                matter_priority = int(matter_priority)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "matter priority must be a number"}, status=400
                )
            if not 0 <= matter_priority <= 200:
                return web.json_response(
                    {"error": "matter priority must be 0–200"}, status=400
                )
        if matter_universe is not None:
            try:
                matter_universe = int(matter_universe)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "matter universe must be a number"}, status=400
                )
            proto_mu = matter_protocol or manager.cfg.matter.protocol
            hi = 63999 if proto_mu == "sacn" else 32767
            lo = 1 if proto_mu == "sacn" else 0
            if not lo <= matter_universe <= hi:
                return web.json_response(
                    {"error": "matter universe out of range"}, status=400
                )
        if matter_enabled is not None:
            matter_enabled = bool(matter_enabled)
        if bpm_rate_min is not None:
            bpm_rate_min = str(bpm_rate_min).lower()
            if bpm_rate_min not in BPM_RATE_NAMES:
                return web.json_response(
                    {"error": "bpm_rate_min must be eighth/quarter/half/full"},
                    status=400,
                )
        if bpm_rate_max is not None:
            bpm_rate_max = str(bpm_rate_max).lower()
            if bpm_rate_max not in BPM_RATE_NAMES:
                return web.json_response(
                    {"error": "bpm_rate_max must be eighth/quarter/half/full"},
                    status=400,
                )
        if bpm_rate_decay is not None:
            try:
                bpm_rate_decay = float(bpm_rate_decay)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "bpm_rate_decay must be a number"}, status=400
                )
            if not 0.0 <= bpm_rate_decay <= 120.0:
                return web.json_response(
                    {"error": "bpm_rate_decay must be 0–120 seconds"}, status=400
                )
        if intensity is not None:
            try:
                intensity = float(intensity)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "intensity must be a number"}, status=400
                )
            if not 0.0 <= intensity <= 1.0:
                return web.json_response(
                    {"error": "intensity must be 0–1"}, status=400
                )
        if drop_hold_seconds is not None:
            try:
                drop_hold_seconds = float(drop_hold_seconds)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "drop_hold_seconds must be a number"}, status=400
                )
            if not 2.0 <= drop_hold_seconds <= 120.0:
                return web.json_response(
                    {"error": "drop length must be 2–120 seconds"}, status=400
                )
        loop = asyncio.get_running_loop()
        try:
            message = await loop.run_in_executor(
                None,
                lambda: manager.apply_settings(
                    device,
                    ip,
                    universe,
                    bpm_rate_min,
                    bpm_rate_max,
                    bpm_rate_decay,
                    intensity,
                    drop_hold_seconds,
                    protocol,
                    mode,
                    port,
                    priority,
                    interface,
                    matter_protocol,
                    matter_mode,
                    matter_universe,
                    matter_port,
                    matter_priority,
                    matter_enabled,
                ),
            )
        except Exception as exc:  # e.g. unknown device name
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"ok": True, "message": message})

    async def layout_get_handler(_request: web.Request) -> web.Response:
        return web.json_response(
            {"layout": manager.layout, "boxes": list(_LAYOUT_BOX_IDS)},
            headers={"Cache-Control": "no-store"},
        )

    async def layout_post_handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        layout_data = body.get("layout", body)
        if not isinstance(layout_data, dict):
            return web.json_response({"error": "layout must be an object"}, status=400)
        try:
            layout = manager.save_layout(layout_data)
        except (OSError, ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(
            {"ok": True, "message": "Layout saved", "layout": layout}
        )

    async def enable_handler(request: web.Request) -> web.Response:
        """Toggle Art-Net output and/or analyzer without stopping the server."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if "enabled" not in body and "analyzing" not in body:
            return web.json_response(
                {"error": "enabled and/or analyzing required"}, status=400
            )
        parts: list[str] = []
        if "enabled" in body:
            parts.append(manager.set_enabled(bool(body["enabled"])))
        if "analyzing" in body:
            parts.append(manager.set_analyzing(bool(body["analyzing"])))
        return web.json_response(
            {
                "ok": True,
                "message": "; ".join(parts),
                "enabled": manager.enabled,
                "analyzing": manager.analyzing,
            }
        )

    async def reboot_handler(_request: web.Request) -> web.Response:
        """Reboot the host (Raspberry Pi). Requires passwordless sudo reboot."""

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
        return web.json_response(
            {"ok": True, "message": "Rebooting…"},
        )

    async def shutdown_handler(_request: web.Request) -> web.Response:
        """Stop analysis, release the port, and exit the process."""

        def _exit() -> None:
            try:
                manager.stop_all()
            finally:
                os._exit(0)

        threading.Timer(0.25, _exit).start()
        return web.json_response({"ok": True, "message": "Shutting down…"})

    async def matter_dmx_handler(request: web.Request) -> web.Response:
        peer = request.remote or ""
        if peer not in ("127.0.0.1", "::1"):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        raw = body.get("updates") or {}
        if not isinstance(raw, dict):
            return web.json_response({"error": "updates must be an object"}, status=400)
        updates: dict[int, int] = {}
        for key, value in raw.items():
            try:
                updates[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
        if manager.matter_out is not None:
            manager.matter_out.apply_updates(updates)
        return web.json_response({"ok": True})

    async def matter_proxy_handler(request: web.Request) -> web.Response:
        suffix = request.match_info.get("path", "")
        url = f"{manager.sidecar.base_url}/api/{suffix}"
        if request.query_string:
            url = f"{url}?{request.query_string}"
        try:
            body = await request.read()
        except Exception:
            body = b""
        headers = {}
        ctype = request.headers.get("Content-Type")
        if ctype:
            headers["Content-Type"] = ctype
        timeout = aiohttp_timeout()
        try:
            async with ClientSession() as session:
                async with session.request(
                    request.method,
                    url,
                    data=body if body else None,
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    payload = await resp.read()
                    return web.Response(
                        body=payload,
                        status=resp.status,
                        content_type=resp.content_type,
                    )
        except Exception as exc:
            return web.json_response(
                {"error": f"Matter sidecar unavailable: {exc}"}, status=503
            )

    def aiohttp_timeout():
        from aiohttp import ClientTimeout

        return ClientTimeout(total=30)

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/devices", devices_handler)
    app.router.add_get("/api/config", config_handler)
    app.router.add_get("/api/layout", layout_get_handler)
    app.router.add_post("/api/layout", layout_post_handler)
    app.router.add_post("/settings", settings_handler)
    app.router.add_post("/enable", enable_handler)
    app.router.add_post("/reboot", reboot_handler)
    app.router.add_post("/shutdown", shutdown_handler)
    app.router.add_post("/internal/matter/dmx", matter_dmx_handler)
    app.router.add_route("*", "/api/matter/{path:.*}", matter_proxy_handler)

    _print_dashboard_urls(host, port)
    try:
        web.run_app(app, host=host, port=port, print=None)
    except OSError as exc:
        if getattr(exc, "errno", None) == 98 or "address already in use" in str(exc):
            raise SystemExit(
                f"Port {port} is already in use (another bridge UI still running?).\n"
                f"Stop it with:  fuser -k {port}/tcp\n"
                f"Or open the existing dashboard and click Stop server,\n"
                f"or pick another port:  python -m magicq_companion --ui --ui-port {port + 1}"
            ) from None
        raise
    finally:
        manager.stop_all()
