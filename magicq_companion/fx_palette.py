"""Static colour palettes with timed crossfade."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


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
