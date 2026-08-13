"""Create the configured DMX network sender (Art-Net or sACN)."""

from __future__ import annotations

from typing import Protocol

from .artnet import ArtNetSender
from .config import ArtNetConfig
from .netif import interface_info, list_ipv4_interfaces
from .sacn import SacnSender, sacn_multicast_ip

OUTPUT_PROTOCOLS = ("artnet", "sacn")
OUTPUT_MODES = ("unicast", "broadcast", "multicast")
ARTNET_MODES = ("unicast", "broadcast")
SACN_MODES = ("unicast", "multicast")


class DmxSender(Protocol):
    address: tuple[str, int]

    def send(self, dmx: bytes | bytearray) -> None: ...
    def blackout(self) -> None: ...
    def close(self) -> None: ...


def default_port(protocol: str) -> int:
    return 5568 if protocol == "sacn" else 6454


def allowed_modes(protocol: str) -> tuple[str, ...]:
    return SACN_MODES if protocol == "sacn" else ARTNET_MODES


def coerce_mode(protocol: str, mode: str | None) -> str:
    """Map a mode onto what the selected protocol actually supports."""
    allowed = allowed_modes(protocol)
    m = (mode or "").lower()
    if m in allowed:
        return m
    if protocol == "sacn":
        return "multicast" if m == "broadcast" else "unicast"
    return "broadcast" if m == "multicast" else "unicast"


def create_sender(cfg: ArtNetConfig) -> DmxSender:
    protocol = (cfg.protocol or "artnet").lower()
    if protocol not in OUTPUT_PROTOCOLS:
        protocol = "artnet"
    mode = coerce_mode(protocol, cfg.mode)

    port = int(cfg.port) if cfg.port else default_port(protocol)
    # If the user left the other protocol's default port, switch automatically.
    if protocol == "sacn" and port == 6454:
        port = 5568
    elif protocol == "artnet" and port == 5568:
        port = 6454

    iface = (cfg.interface or "").strip()
    if protocol == "sacn":
        return SacnSender(
            cfg.ip,
            port=port,
            universe=cfg.universe,
            mode=mode,
            priority=cfg.priority,
            interface=iface,
        )
    return ArtNetSender(
        cfg.ip,
        port=port,
        universe=cfg.universe,
        mode=mode,
        interface=iface,
    )


def describe_output(cfg: ArtNetConfig) -> str:
    """Short status string for logs / UI meta."""
    protocol = (cfg.protocol or "artnet").lower()
    mode = coerce_mode(protocol, cfg.mode)
    port = int(cfg.port) if cfg.port else default_port(protocol)
    if protocol == "sacn" and port == 6454:
        port = 5568
    elif protocol == "artnet" and port == 5568:
        port = 6454

    iface = interface_info(cfg.interface) if cfg.interface else None
    if protocol == "sacn" and mode == "multicast":
        uni = max(1, int(cfg.universe) or 1)
        dest = f"{sacn_multicast_ip(uni)}:{port}"
    elif mode == "broadcast":
        bcast = (
            iface["broadcast"]
            if iface
            else (
                cfg.ip
                if str(cfg.ip).endswith(".255") or cfg.ip == "255.255.255.255"
                else "255.255.255.255"
            )
        )
        dest = f"{bcast}:{port}"
    else:
        dest = f"{cfg.ip}:{port}"

    label = "sACN" if protocol == "sacn" else "Art-Net"
    via = f" via {cfg.interface}" if cfg.interface else ""
    return f"{label} {mode} universe {cfg.universe} -> {dest}{via}"
