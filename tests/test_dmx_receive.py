"""Tests for the console DMX input listener."""

from __future__ import annotations

import socket
import struct
import threading
import unittest

from magicq_companion.dmx_receive import DmxReceiver


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _artdmx(universe: int, data: bytes) -> bytes:
    return (
        b"Art-Net\x00"
        + struct.pack("<H", 0x5000)
        + struct.pack(">H", 14)
        + bytes([0, 0])
        + struct.pack("<H", universe)
        + struct.pack(">H", len(data))
        + data
    )


class DmxReceiverTests(unittest.TestCase):
    def test_start_with_interface_set_receives_frames(self) -> None:
        port = _free_udp_port()
        got: list[tuple[int, bytes]] = []
        received = threading.Event()

        def on_frame(universe: int, data: bytes) -> None:
            got.append((universe, data))
            received.set()

        rx = DmxReceiver(port=port, interface="lo", on_frame=on_frame)
        rx.start()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
                tx.sendto(_artdmx(3, bytes([255, 128])), ("127.0.0.1", port))
            self.assertTrue(received.wait(2))
        finally:
            rx.stop()
        universe, data = got[0]
        self.assertEqual(universe, 3)
        self.assertEqual(data[:2], bytes([255, 128]))
        self.assertEqual(len(data), 512)


if __name__ == "__main__":
    unittest.main()
