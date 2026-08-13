"""sACN (ANSI E1.31) DMX sender over UDP. No dependencies."""

from __future__ import annotations

import socket
import struct
import uuid

from .netif import bind_to_interface

# ACN packet identifier "ASC-E1.17\0\0\0"
_ACN_ID = b"ASC-E1.17\x00\x00\x00"
_ROOT_VECTOR = 0x00000004
_FRAME_VECTOR = 0x00000002
_DMP_VECTOR = 0x02
_DMP_ADDR_TYPE = 0xA1
_DEFAULT_PORT = 5568
_SOURCE_NAME = b"magicq-companion"


def sacn_multicast_ip(universe: int) -> str:
    """E1.31 multicast address for a universe: 239.255.H.L."""
    u = max(1, min(63999, int(universe)))
    return f"239.255.{(u >> 8) & 0xFF}.{u & 0xFF}"


def _flags_and_length(length: int) -> bytes:
    """High nibble 0x7 + 12-bit PDU length (from this field to end)."""
    return struct.pack("!H", 0x7000 | (length & 0x0FFF))


class SacnSender:
    """Send E1.31 DATA packets (full 512-slot universes)."""

    def __init__(
        self,
        ip: str,
        port: int = _DEFAULT_PORT,
        universe: int = 1,
        *,
        mode: str = "multicast",
        priority: int = 100,
        source_name: str = "magicq-companion",
        interface: str = "",
    ) -> None:
        # sACN universes are 1..63999 (0 is reserved).
        self.universe = max(1, min(63999, int(universe) or 1))
        self.priority = max(0, min(200, int(priority)))
        self.mode = "unicast" if (mode or "").lower() == "unicast" else "multicast"
        self.port = int(port) if port else _DEFAULT_PORT
        self._sequence = 0
        self._cid = uuid.uuid4().bytes
        name = source_name.encode("utf-8")[:63]
        self._source_name = name + b"\x00" * (64 - len(name))

        dest_ip = ip or "127.0.0.1" if self.mode == "unicast" else sacn_multicast_ip(
            self.universe
        )

        self.address = (dest_ip, self.port)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.mode == "multicast":
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
        if interface:
            bind_to_interface(
                self._socket, interface, multicast=self.mode == "multicast"
            )

    def send(self, dmx: bytes | bytearray) -> None:
        data = bytes(dmx)[:512]
        if len(data) < 512:
            data = data + bytes(512 - len(data))

        self._sequence = (self._sequence + 1) % 256
        prop_values = bytes([0]) + data  # start code + slots
        prop_count = len(prop_values)  # 513

        # DMP PDU length includes flags through property values.
        dmp_len = 10 + prop_count
        # Framing PDU: flags(2)+vector(4)+name(64)+pri(1)+sync(2)+seq(1)+opt(1)+uni(2)+DMP
        frame_len = 77 + dmp_len
        # Root PDU: flags(2)+vector(4)+CID(16)+Framing
        root_len = 22 + frame_len

        packet = (
            struct.pack("!HH", 0x0010, 0x0000)
            + _ACN_ID
            + _flags_and_length(root_len)
            + struct.pack("!I", _ROOT_VECTOR)
            + self._cid
            + _flags_and_length(frame_len)
            + struct.pack("!I", _FRAME_VECTOR)
            + self._source_name
            + bytes([self.priority])
            + struct.pack("!H", 0)  # sync address
            + bytes([self._sequence, 0])  # sequence, options
            + struct.pack("!H", self.universe)
            + _flags_and_length(dmp_len)
            + bytes([_DMP_VECTOR, _DMP_ADDR_TYPE])
            + struct.pack("!HHH", 0x0000, 0x0001, prop_count)
            + prop_values
        )
        self._socket.sendto(packet, self.address)

    def blackout(self) -> None:
        self.send(bytes(512))

    def close(self) -> None:
        self._socket.close()
