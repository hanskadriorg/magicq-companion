"""Patch / fixture tag source seam (manual tags today; file parsers later)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .fx_tags import FxTagKind


class PatchSource(ABC):
    """Future: CSV, GDTF, or show-file driven tags. Not implemented."""

    @abstractmethod
    def channel_tags(self) -> dict[int, FxTagKind]:
        raise NotImplementedError


class ManualPatchSource(PatchSource):
    def __init__(self, tags: dict[int, FxTagKind] | None = None) -> None:
        self._tags = dict(tags or {})

    def channel_tags(self) -> dict[int, FxTagKind]:
        return dict(self._tags)

    def set_tag(self, channel: int, kind: FxTagKind) -> None:
        self._tags[int(channel)] = kind


class StubShowPatchSource(PatchSource):
    """Placeholder for a future .shw / patch import pipeline."""

    def __init__(self, reason: str = "show parsers not implemented") -> None:
        self.reason = reason

    def channel_tags(self) -> dict[int, FxTagKind]:
        return {}

    def metadata(self) -> dict[str, Any]:
        return {"stub": True, "reason": self.reason}
