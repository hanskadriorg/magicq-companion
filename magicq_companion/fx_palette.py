"""Static colour palettes with timed crossfade."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .fx_patch_csv import ImportedPatch, colour_component
from .fx_tags import FxTagKind


PALETTE_FADE_SECONDS = 3.0


@dataclass
class ColourPalette:
    id: str
    name: str
    red: int = 255
    green: int = 255
    blue: int = 255

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "red": self.red,
            "green": self.green,
            "blue": self.blue,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ColourPalette:
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or "Palette"),
            red=int(data.get("red", 255)),
            green=int(data.get("green", 255)),
            blue=int(data.get("blue", 255)),
        )


@dataclass
class PaletteState:
    palettes: list[ColourPalette] = field(default_factory=list)
    active_id: str | None = None
    last_active_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "palettes": [p.to_dict() for p in self.palettes],
            "active_id": self.active_id,
            "last_active_id": self.last_active_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PaletteState:
        pals = [ColourPalette.from_dict(p) for p in data.get("palettes") or []]
        return cls(
            palettes=pals,
            active_id=data.get("active_id"),
            last_active_id=data.get("last_active_id"),
        )


def restore_active_palette(state: PaletteState) -> str | None:
    """On startup, prefer last_active_id, then active_id."""
    if state.last_active_id and _find(state, state.last_active_id):
        state.active_id = state.last_active_id
        return state.active_id
    if state.active_id and _find(state, state.active_id):
        return state.active_id
    if state.palettes:
        state.active_id = state.palettes[0].id
        return state.active_id
    return None


def _find(state: PaletteState, pid: str) -> ColourPalette | None:
    for p in state.palettes:
        if p.id == pid:
            return p
    return None


def blend_rgb(
    a: tuple[int, int, int],
    b: tuple[int, int, int],
    t: float,
) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


def sample_rgb_from_frame(
    frame: bytes,
    *,
    patch: ImportedPatch | None = None,
    colour_addrs: dict[int, FxTagKind] | None = None,
) -> tuple[int, int, int]:
    """Sample a static look's RGB from a live DMX frame.

    Prefers patched Red/Green/Blue (or CMY→RGB) channel names; falls back to
    COLOUR-tagged addresses using coarse R/G/B slotting; last resort returns
    white when no colour map is available.
    """
    data = frame[:512] if frame else bytes(512)
    rs: list[int] = []
    gs: list[int] = []
    bs: list[int] = []
    if patch is not None and patch.channels:
        for ch in patch.channels:
            if not (1 <= ch.address <= 512):
                continue
            comp = colour_component(ch.channel_name)
            if comp is None:
                continue
            val = int(data[ch.address - 1])
            if comp == "r":
                rs.append(val)
            elif comp == "g":
                gs.append(val)
            elif comp == "b":
                bs.append(val)
            elif comp == "c":
                rs.append(255 - val)
            elif comp == "m":
                gs.append(255 - val)
            elif comp == "y":
                bs.append(255 - val)
        if rs or gs or bs:
            return (
                int(round(sum(rs) / len(rs))) if rs else 0,
                int(round(sum(gs) / len(gs))) if gs else 0,
                int(round(sum(bs) / len(bs))) if bs else 0,
            )
    tags = colour_addrs or {}
    colour_chs = sorted(
        a for a, k in tags.items() if k is FxTagKind.COLOUR and 1 <= a <= 512
    )
    if colour_chs:
        for addr in colour_chs:
            slot = (addr - 1) % 3
            val = int(data[addr - 1])
            if slot == 0:
                rs.append(val)
            elif slot == 1:
                gs.append(val)
            else:
                bs.append(val)
        return (
            int(round(sum(rs) / len(rs))) if rs else 0,
            int(round(sum(gs) / len(gs))) if gs else 0,
            int(round(sum(bs) / len(bs))) if bs else 0,
        )
    return (255, 255, 255)


def apply_rgb_to_colour_channels(
    out: bytearray,
    rgb: tuple[int, int, int],
    *,
    patch: ImportedPatch | None = None,
    colour_addrs: dict[int, FxTagKind] | None = None,
) -> None:
    """Write palette RGB onto colour channels in ``out`` (mutates in place)."""
    r, g, b = rgb
    written = False
    if patch is not None and patch.channels:
        for ch in patch.channels:
            if not (1 <= ch.address <= 512):
                continue
            comp = colour_component(ch.channel_name)
            if comp is None:
                continue
            written = True
            if comp == "r":
                out[ch.address - 1] = r
            elif comp == "g":
                out[ch.address - 1] = g
            elif comp == "b":
                out[ch.address - 1] = b
            elif comp == "c":
                out[ch.address - 1] = max(0, min(255, 255 - r))
            elif comp == "m":
                out[ch.address - 1] = max(0, min(255, 255 - g))
            elif comp == "y":
                out[ch.address - 1] = max(0, min(255, 255 - b))
    if written:
        return
    tags = colour_addrs or {}
    for addr, kind in tags.items():
        if kind is not FxTagKind.COLOUR or not (1 <= addr <= 512):
            continue
        slot = (addr - 1) % 3
        out[addr - 1] = r if slot == 0 else g if slot == 1 else b


def new_palette_from_rgb(name: str, rgb: tuple[int, int, int]) -> ColourPalette:
    return ColourPalette(
        id=uuid.uuid4().hex,
        name=name or "Palette",
        red=max(0, min(255, int(rgb[0]))),
        green=max(0, min(255, int(rgb[1]))),
        blue=max(0, min(255, int(rgb[2]))),
    )


def load_palette_state(path: Path) -> PaletteState:
    if not path.is_file():
        return PaletteState()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    state = PaletteState.from_dict(data)
    restore_active_palette(state)
    return state


def save_palette_state(path: Path, state: PaletteState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if state.active_id:
        state.last_active_id = state.active_id
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)
