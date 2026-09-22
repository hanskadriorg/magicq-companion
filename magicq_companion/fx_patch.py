"""Patch / fixture tag source seam (manual tags, CSV import)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .fx_patch_csv import ImportedPatch
from .fx_tags import FadeMerge, FxTagKind


class PatchSource(ABC):
    """Channel tags and optional HTP/LTP merge overrides for playback fades."""

    @abstractmethod
    def channel_tags(self) -> dict[int, FxTagKind]:
        raise NotImplementedError

    def channel_merges(self) -> dict[int, FadeMerge]:
        """Optional absolute-address → HTP/LTP map (CSV import)."""
        return {}

    def summary(self) -> dict[str, Any]:
        return {}


class ManualPatchSource(PatchSource):
    def __init__(self, tags: dict[int, FxTagKind] | None = None) -> None:
        self._tags = dict(tags or {})

    def channel_tags(self) -> dict[int, FxTagKind]:
        return dict(self._tags)

    def set_tag(self, channel: int, kind: FxTagKind) -> None:
        self._tags[int(channel)] = kind


class CsvPatchSource(PatchSource):
    """Patch built from the flat MagicQ-style CSV importer."""

    def __init__(self, patch: ImportedPatch | None = None) -> None:
        self.patch = patch or ImportedPatch()

    def channel_tags(self) -> dict[int, FxTagKind]:
        return self.patch.channel_tags()

    def channel_merges(self) -> dict[int, FadeMerge]:
        return self.patch.channel_merges()

    def summary(self) -> dict[str, Any]:
        return self.patch.summary()


class StubShowPatchSource(PatchSource):
    """Placeholder — .shw / GDTF parsers are not implemented."""

    def __init__(self, reason: str = "show parsers not implemented") -> None:
        self.reason = reason

    def channel_tags(self) -> dict[int, FxTagKind]:
        return {}

    def metadata(self) -> dict[str, Any]:
        return {"stub": True, "reason": self.reason}
