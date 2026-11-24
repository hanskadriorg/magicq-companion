"""Integration tests for config migration, syncopated grooves, watchdog, and Web UI."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from magicq_companion.config import Config, load_config
from magicq_companion.features import Features
from magicq_companion.genre_agent import blend_control_vector
from magicq_companion.pipeline import Bridge
from magicq_companion.state_machine import Section, SectionTracker
from magicq_companion.ui import _Manager, _run_stdlib_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestVenueSystem(unittest.TestCase):
    def test_legacy_config_with_retired_sections_and_channels_loads_cleanly(self) -> None:
        legacy_toml = """
[app]
mode = "standalone_fx"

[fx]
input_universe = 2
input_ip = "0.0.0.0"
record_fps = 40

[matter]
enabled = true
protocol = "artnet"
universe = 1

[network]
ip = "127.0.0.1"

[artnet]
protocol = "sacn"
mode = "localhost"
port = 5568
universe = 10

[channels]
groove = 1
breakdown = 2
buildup = 3
drop = 4
build_progress = 5
beat_pulse = 6
drop_hit = 9
four_four = 11
bpm_rate = 14
drop_momentary = 10

[detection]
intensity = 0.42
"""
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(legacy_toml)
            cfg_path = Path(handle.name)
        try:
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.channels.drop_hit, 7)
            self.assertEqual(cfg.channels.four_four, 8)
            self.assertEqual(cfg.channels.bpm_rate, 9)
            self.assertAlmostEqual(cfg.detection.build_intensity, 0.42)
            self.assertAlmostEqual(cfg.detection.drop_intensity, 0.42)
        finally:
            cfg_path.unlink(missing_ok=True)

    def test_syncopated_non_4_4_groove_avoids_false_breakdown(self) -> None:
        cfg = Config()
        fps = 44_100 / 512
        dt = 1.0 / fps
        tracker = SectionTracker(cfg.detection, fps)
        urban_vec = blend_control_vector({"hiphop_urban": 0.6, "fast_bass": 0.4})

        # Simulate 25s of syncopated groove: kick only hits every ~3.2s, while
        # snares/hats (onset_density=3.4) and bassline (bass_norm=0.78) stay active.
        t = 0.0
        for step in range(int(25.0 * fps)):
            t += dt
            sparse_kick = (step % int(3.2 * fps)) == 0
            f = Features(
                bass_norm=0.78,
                energy_norm=1.0,
                kick_depth=0.18,
                kick_regularity=0.25,
                onset_density=3.4,
                music_score=0.75,
                bpm=88.0,
                beat=sparse_kick,
            )
            tracker.update(f, dt, genre=urban_vec)
        self.assertEqual(tracker.state, Section.GROOVE)

        # Now drop both bass and percussion -> must enter BREAKDOWN cleanly.
        for _ in range(int(8.0 * fps)):
            f = Features(
                bass_norm=0.20,
                energy_norm=0.45,
                kick_depth=0.0,
                kick_regularity=0.0,
                onset_density=0.4,
                music_score=0.55,
                bpm=0.0,
                beat=False,
            )
            tracker.update(f, dt, genre=urban_vec)
        self.assertEqual(tracker.state, Section.BREAKDOWN)

    def test_supervised_bridge_auto_reconnects_after_capture_failure(self) -> None:
        cfg = Config()
        cfg.server.audio_reconnect_seconds = 0.05
        stop_event = threading.Event()
        attempts = 0
        statuses: list[str] = []

        def flaky_source(_cfg: Config):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                def first_stream():
                    for _ in range(8):
                        yield np.zeros(512, dtype=np.float64)
                    raise RuntimeError("USB interface reset")
                return first_stream(), "usb-card-attempt-1"
            if attempts == 2:
                raise OSError("device busy")

            def recovered_stream():
                for _ in range(12):
                    yield 0.2 * np.sin(np.linspace(0, 6.28, 512))
                stop_event.set()
            return recovered_stream(), "usb-card-recovered"

        bridge = Bridge(cfg, pace=False, async_genre=False)
        bridge.run_supervised(
            flaky_source,
            reconnect=True,
            on_status=statuses.append,
            stop_event=stop_event,
        )
        self.assertEqual(attempts, 3)
        self.assertIn("usb-card-recovered", statuses)
        self.assertGreaterEqual(bridge._frame_idx, 20)

    def test_web_ui_http_routes_and_websocket_telemetry(self) -> None:
        cfg = Config()
        port = _free_port()

        def sim_source(_c: Config):
            def gen():
                t = 0.0
                while True:
                    arr = 0.4 * np.sin(2 * np.pi * 55.0 * (np.arange(512) / 44_100 + t))
                    t += 512 / 44_100
                    yield arr
            return gen(), "test-sim"

        manager = _Manager(
            cfg,
            source_factory=sim_source,
            dry_run=True,
            pace=True,
            duration=None,
            config_path=None,
            simulate=True,
        )
        manager.start(sim_source(cfg))

        loop = asyncio.new_event_loop()
        srv_task: list[asyncio.Task] = []

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            task = loop.create_task(_run_stdlib_server(manager, "127.0.0.1", port))
            srv_task.append(task)
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
            finally:
                loop.close()

        server_thread = threading.Thread(target=_run_loop, daemon=True)
        server_thread.start()
        time.sleep(0.25)

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
                html = resp.read().decode("utf-8")
                self.assertEqual(resp.status, 200)
                self.assertIn("MAGICQ VENUE COMPANION", html)
                self.assertNotIn("Matter to DMX", html)
                self.assertNotIn("Standalone FX", html)
                self.assertNotIn("Pixel mapper", html)

            for removed in ("/api/matter/devices", "/pixel-mapper/", "/api/fx/state"):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}{removed}", timeout=2)
                self.assertEqual(ctx.exception.code, 404)

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/settings",
                data=json.dumps({"genre_mode": "groovy_warm"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(result["ok"])
                self.assertEqual(manager.cfg.genre.mode, "groovy_warm")

            # Verify RFC 6455 WebSocket upgrade and live frame payload.
            with socket.create_connection(("127.0.0.1", port), timeout=3) as ws_sock:
                key = base64.b64encode(b"0123456789abcdef").decode("ascii")
                ws_req = (
                    f"GET /ws HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                )
                ws_sock.sendall(ws_req.encode("ascii"))
                resp_buf = b""
                while b"\r\n\r\n" not in resp_buf:
                    resp_buf += ws_sock.recv(1024)
                head, rest = resp_buf.split(b"\r\n\r\n", 1)
                self.assertIn(b"101 Switching Protocols", head)
                while len(rest) < 4:
                    rest += ws_sock.recv(4096)
                length = rest[1] & 0x7F
                offset = 2
                if length == 126:
                    length = struct.unpack("!H", rest[2:4])[0]
                    offset = 4
                while len(rest) < offset + length:
                    rest += ws_sock.recv(4096)
                frame_json = json.loads(rest[offset : offset + length].decode("utf-8"))
                self.assertIn("archetype", frame_json)
                self.assertIn("pace", frame_json)
                self.assertIn("dmx", frame_json)
        finally:
            manager.stop_all()
            if srv_task:
                loop.call_soon_threadsafe(srv_task[0].cancel)
            server_thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
