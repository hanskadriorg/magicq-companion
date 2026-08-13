"""Minimal Art-Net (ArtDMX) sender over UDP. No dependencies."""

from __future__ import annotations

import socket
import struct

from .netif import bind_to_interface, interface_info

_HEADER = b"Art-Net\x00"
_OPCODE_DMX = 0x5000
_PROTOCOL_VERSION = 14
_DEFAULT_PORT = 6454


class ArtNetSender:
    def __init__(
        self,
        ip: str,
        port: int = _DEFAULT_PORT,
        universe: int = 0,
        *,
        mode: str = "unicast",
        interface: str = "",
    ) -> None:
        self.universe = max(0, min(32767, int(universe)))
        self.mode = "broadcast" if (mode or "").lower() == "broadcast" else "unicast"
        self.port = int(port) if port else _DEFAULT_PORT
        self._sequence = 0

        info = interface_info(interface) if interface else None
        if self.mode == "broadcast":
            dest = (
                info["broadcast"]
                if info
                else (
                    ip
                    if ip.endswith(".255") or ip == "255.255.255.255"
                    else "255.255.255.255"
                )
            )
        else:
            dest = ip or "127.0.0.1"

        self.address = (dest, self.port)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.mode == "broadcast":
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if interface:
            bind_to_interface(self._socket, interface, multicast=False)

    def send(self, dmx: bytes | bytearray) -> None:
        """Send one ArtDMX frame (dmx must be 2-512 bytes, padded to even)."""
        data = bytes(dmx)
        if len(data) % 2:
            data += b"\x00"
        self._sequence = self._sequence % 255 + 1
        packet = (
            _HEADER
            + struct.pack("<H", _OPCODE_DMX)
            + struct.pack(">H", _PROTOCOL_VERSION)
            + bytes([self._sequence, 0])  # sequence, physical port
            + struct.pack("<H", self.universe)
            + struct.pack(">H", len(data))
            + data
        )
        self._socket.sendto(packet, self.address)

    def blackout(self) -> None:
        self.send(bytes(512))

    def close(self) -> None:
        self._socket.close()
