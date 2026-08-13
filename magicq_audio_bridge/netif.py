"""List physical IPv4 NICs and bind a UDP socket to one of them."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

_SYS_NET = Path("/sys/class/net")
_SKIP_PREFIXES = (
    "lo",
    "docker",
    "br-",
    "veth",
    "virbr",
    "tailscale",
    "tun",
    "wg",
    "zt",
    "cni",
    "flannel",
    "podman",
)


def list_ipv4_interfaces() -> list[dict[str, str]]:
    """Physical Ethernet/Wi-Fi NICs, including ones with no address yet."""
    addrs = _ipv4_table()
    nm = _nm_device_state()
    found: list[dict[str, str]] = []
    for name, kind in _physical_nics():
        ip, bcast, prefix, origin = addrs.get(
            name, ("", "255.255.255.255", "", "")
        )
        nm_state, connection = nm.get(name, ("", ""))
        state = nm_state or _operstate(name)
        if nm_state:
            connected = nm_state.startswith("connected")
        else:
            connected = _has_carrier(name)
        found.append(
            {
                "name": name,
                "ip": ip,
                "broadcast": bcast,
                "kind": kind,
                "state": state,
                "connected": "1" if connected else "0",
                "prefix": prefix,
                "origin": origin,
                "connection": connection,
            }
        )
    return found


def interface_info(name: str) -> dict[str, str] | None:
    if not name:
        return None
    for nic in list_ipv4_interfaces():
        if nic["name"] == name:
            return nic
    return None


def bind_to_interface(sock: socket.socket, interface: str, *, multicast: bool) -> None:
    """Force outbound packets onto a named NIC when possible."""
    info = interface_info(interface)
    if info is None or not info.get("ip"):
        return
    try:
        sock.bind((info["ip"], 0))
    except OSError:
        pass
    if multicast:
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(info["ip"]),
            )
        except OSError:
            pass


def _physical_nics() -> list[tuple[str, str]]:
    nics: list[tuple[str, str]] = []
    if not _SYS_NET.is_dir():
        return nics
    for path in sorted(_SYS_NET.iterdir()):
        name = path.name
        if _skip_name(name):
            continue
        kind = _nic_kind(path)
        if kind is None:
            continue
        nics.append((name, kind))
    return nics


def _skip_name(name: str) -> bool:
    if name == "lo":
        return True
    return any(name == p or name.startswith(p) for p in _SKIP_PREFIXES if p != "lo")


def _nic_kind(path: Path) -> str | None:
    if (path / "wireless").is_dir():
        return "wifi"
    uevent = _read(path / "uevent") or ""
    if "DEVTYPE=wlan" in uevent:
        return "wifi"
    if "DEVTYPE=bridge" in uevent or "DEVTYPE=veth" in uevent:
        return None
    if "DEVTYPE=tun" in uevent:
        return None
    try:
        iftype = int(_read(path / "type") or "0")
    except ValueError:
        return None
    # ARPHRD_ETHER
    if iftype == 1:
        return "ethernet"
    return None


def _operstate(name: str) -> str:
    return (_read(_SYS_NET / name / "operstate") or "unknown").strip()


def _has_carrier(name: str) -> bool:
    raw = _read(_SYS_NET / name / "carrier")
    if raw is not None:
        return raw.strip() == "1"
    return _operstate(name) == "up"


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _ipv4_table() -> dict[str, tuple[str, str, str, str]]:
    """name -> (ip, broadcast, prefixlen, origin) from `ip -4 -o addr`."""
    table: dict[str, tuple[str, str, str, str]] = {}
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return table
    for line in out.splitlines():
        # 2: eth0    inet 2.0.0.44/24 brd 2.0.0.255 scope global dynamic ...
        parts = line.split()
        try:
            name = parts[1]
            inet_idx = parts.index("inet")
            cidr = parts[inet_idx + 1]
            ip, _, prefix = cidr.partition("/")
        except (IndexError, ValueError):
            continue
        bcast = "255.255.255.255"
        if "brd" in parts:
            bcast = parts[parts.index("brd") + 1]
        elif prefix.isdigit():
            bcast = _directed_broadcast(ip, int(prefix))
        origin = "dhcp" if "dynamic" in parts else "static"
        scope = parts[parts.index("scope") + 1] if "scope" in parts else ""
        existing = table.get(name)
        # Prefer a global address over link-local.
        if existing is None or (existing[0].startswith("169.254.") and scope == "global"):
            table[name] = (ip, bcast, prefix, origin)
    return table


def _nm_device_state() -> dict[str, tuple[str, str]]:
    """name -> (NM state, connection id) when NetworkManager is present."""
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return {}
    table: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1]
        connection = parts[2] if len(parts) > 2 and parts[2] != "--" else ""
        table[name] = (state, connection)
    return table


def _directed_broadcast(ip: str, prefix: int) -> str:
    try:
        packed = socket.inet_aton(ip)
        addr = int.from_bytes(packed, "big")
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix else 0
        bcast = (addr & mask) | (~mask & 0xFFFFFFFF)
        return socket.inet_ntoa(bcast.to_bytes(4, "big"))
    except OSError:
        return "255.255.255.255"
