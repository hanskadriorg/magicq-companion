"""Receive Art-Net ArtDMX and sACN E1.31 DMX (console input, not our output)."""

from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Callable

from .netif import bind_to_interface

_ARTNET_HEADER = b"Art-Net\x00"
_ARTNET_DMX = 0x5000
_SACN_ID = b"ASC-E1.17\x00\x00\x00"


def parse_artnet_dmx(packet: bytes) -> tuple[int, bytes] | None:
    if len(packet) < 18 or packet[:8] != _ARTNET_HEADER:
        return None
    opcode = struct.unpack_from("<H", packet, 8)[0]
    if opcode != _ARTNET_DMX:
        return None
    universe = struct.unpack_from("<H", packet, 14)[0]
    length = struct.unpack_from(">H", packet, 16)[0]
    data = packet[18 : 18 + length]
    if len(data) < 512:
        data = data + bytes(512 - len(data))
    return universe, data[:512]


def parse_sacn_dmx(packet: bytes) -> tuple[int, bytes] | None:
    if len(packet) < 126 or packet[:12] != _SACN_ID:
        return None
    try:
        universe = struct.unpack_from(">H", packet, 113)[0]
        prop_count = struct.unpack_from(">H", packet, 123)[0]
        if prop_count < 513:
            return None
        start = 126
        data = packet[start : start + 512]
        if len(data) < 512:
            data = data + bytes(512 - len(data))
        return universe, data[:512]
    except struct.error:
        return None


class DmxReceiver:
    """UDP listener that delivers (universe, 512-byte frame) to a callback."""

    def __init__(
        self,
        *,
        port: int = 6454,
        interface: str = "",
        protocol: str = "artnet",
        on_frame: Callable[[int, bytes], None] | None = None,
    ) -> None:
        self.port = int(port)
        self.interface = interface or ""
        self.protocol = (protocol or "artnet").lower()
        self._on_frame = on_frame
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        if self.interface:
            bind_to_interface(self._sock, self.interface, multicast=self.protocol == "sacn")
        self._sock.bind(("", self.port))
        self._thread = threading.Thread(target=self._run, name="dmx-receive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                packet, _addr = sock.recvfrom(2048)
            except OSError:
                break
            parsed = None
            if self.protocol == "sacn":
                parsed = parse_sacn_dmx(packet)
            else:
                parsed = parse_artnet_dmx(packet)
            if parsed is None:
                # Accept either protocol on the wire.
                parsed = parse_sacn_dmx(packet) or parse_artnet_dmx(packet)
            if parsed is None:
                continue
            universe, data = parsed
            if self._on_frame is not None:
                self._on_frame(universe, data)
