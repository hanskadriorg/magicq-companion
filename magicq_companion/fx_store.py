"""Persist standalone FX recordings, cues, and channel assignments."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .fx_tags import FxTagKind


@dataclass
class FxCue:
    id: str
    name: str
    recording_id: str
    fade_in_s: float = 0.0
    fade_out_s: float = 0.0
    trim_start: int = 0
    trim_end: int = 0
    loop: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "recording_id": self.recording_id,
            "fade_in_s": self.fade_in_s,
            "fade_out_s": self.fade_out_s,
            "trim_start": self.trim_start,
            "trim_end": self.trim_end,
            "loop": self.loop,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FxCue:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name") or "Cue"),
            recording_id=str(data.get("recording_id") or ""),
            fade_in_s=float(data.get("fade_in_s", 0)),
            fade_out_s=float(data.get("fade_out_s", 0)),
            trim_start=int(data.get("trim_start", 0)),
            trim_end=int(data.get("trim_end", 0)),
            loop=bool(data.get("loop", True)),
        )


@dataclass
class FxRecording:
    id: str
    name: str
    fps: float
    frames: list[bytes] = field(default_factory=list)
    tags: dict[int, str] = field(default_factory=dict)
    loop_hint_frame: int | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "fps": self.fps,
            "frames_b64": [f.hex() for f in self.frames],
            "tags": {str(k): v for k, v in self.tags.items()},
            "loop_hint_frame": self.loop_hint_frame,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FxRecording:
        frames = [bytes.fromhex(s) for s in data.get("frames_b64") or []]
        tags_raw = data.get("tags") or {}
        tags = {int(k): str(v) for k, v in tags_raw.items()}
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name") or "Recording"),
            fps=float(data.get("fps", 40)),
            frames=frames,
            tags=tags,
            loop_hint_frame=data.get("loop_hint_frame"),
        )


@dataclass
class FxChannelAssignment:
    """Control channel (1-based DMX) → cue ids and step timer."""

    control_channel: int
    cue_ids: list[str] = field(default_factory=list)
    mode: str = "loop"  # loop | step
    step_minutes: int = 0
    step_seconds: int = 0
    # momentary = follow button (ignore step timer); latched = hold + optional step.
    trigger: str = "latched"

    def step_interval_s(self) -> float | None:
        if self.mode != "step":
            return None
        return float(self.step_minutes * 60 + self.step_seconds)

    def to_dict(self) -> dict:
        return {
            "control_channel": self.control_channel,
            "cue_ids": list(self.cue_ids),
            "mode": self.mode,
            "step_minutes": self.step_minutes,
            "step_seconds": self.step_seconds,
            "trigger": self.trigger,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FxChannelAssignment:
        return cls(
            control_channel=int(data.get("control_channel", 1)),
            cue_ids=[str(x) for x in data.get("cue_ids") or []],
            mode=str(data.get("mode") or "loop"),
            step_minutes=int(data.get("step_minutes", 0)),
            step_seconds=int(data.get("step_seconds", 0)),
            trigger=str(data.get("trigger") or "latched"),
        )


@dataclass
class FxLibrary:
    recordings: dict[str, FxRecording] = field(default_factory=dict)
    cues: dict[str, FxCue] = field(default_factory=dict)
    assignments: list[FxChannelAssignment] = field(default_factory=list)
    manual_tags: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "recordings": {k: v.to_dict() for k, v in self.recordings.items()},
            "cues": {k: v.to_dict() for k, v in self.cues.items()},
            "assignments": [a.to_dict() for a in self.assignments],
            "manual_tags": {str(k): v for k, v in self.manual_tags.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> FxLibrary:
        recs = {
            k: FxRecording.from_dict(v)
            for k, v in (data.get("recordings") or {}).items()
        }
        cues = {k: FxCue.from_dict(v) for k, v in (data.get("cues") or {}).items()}
        assigns = [
            FxChannelAssignment.from_dict(a) for a in data.get("assignments") or []
        ]
        tags = {int(k): str(v) for k, v in (data.get("manual_tags") or {}).items()}
        return cls(
            recordings=recs,
            cues=cues,
            assignments=assigns,
            manual_tags=tags,
        )


def tags_as_kinds(manual: dict[int, str]) -> dict[int, FxTagKind]:
    out: dict[int, FxTagKind] = {}
    for ch, name in manual.items():
        try:
            out[int(ch)] = FxTagKind(str(name).lower())
        except ValueError:
            out[int(ch)] = FxTagKind.INTENSITY
    return out


def load_fx_library(path: Path) -> FxLibrary:
    if not path.is_file():
        return FxLibrary()
    with open(path, encoding="utf-8") as f:
        return FxLibrary.from_dict(json.load(f))


def save_fx_library(path: Path, lib: FxLibrary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lib.to_dict(), f, indent=2)
