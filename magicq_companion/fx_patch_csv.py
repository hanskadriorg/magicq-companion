"""Parse a flat MagicQ-style fixture patch CSV (heads, addresses, HTP/LTP)."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .fx_tags import FadeMerge, FxTagKind

_DMX_RE = re.compile(
    r"^\s*(\d+)\s*[-/.]\s*(\d+)\s*$",
    re.IGNORECASE,
)


class PatchCsvError(ValueError):
    """Raised when a CSV row or file cannot be imported."""

    def __init__(self, message: str, *, line: int | None = None) -> None:
        self.line = line
        prefix = f"line {line}: " if line is not None else ""
        super().__init__(f"{prefix}{message}")


@dataclass
class PatchChannel:
    head_name: str
    universe: int
    address: int
    merge: FadeMerge
    channel_offset: int | None = None
    channel_name: str = ""

    def to_dict(self) -> dict:
        return {
            "head_name": self.head_name,
            "universe": self.universe,
            "address": self.address,
            "merge": self.merge.value,
            "channel_offset": self.channel_offset,
            "channel_name": self.channel_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PatchChannel:
        merge_raw = str(data.get("merge") or "htp").strip().lower()
        try:
            merge = FadeMerge(merge_raw)
        except ValueError as exc:
            raise PatchCsvError(f"invalid merge {merge_raw!r}") from exc
        return cls(
            head_name=str(data.get("head_name") or ""),
            universe=int(data["universe"]),
            address=int(data["address"]),
            merge=merge,
            channel_offset=(
                int(data["channel_offset"])
                if data.get("channel_offset") not in (None, "")
                else None
            ),
            channel_name=str(data.get("channel_name") or ""),
        )


@dataclass
class ImportedPatch:
    channels: list[PatchChannel] = field(default_factory=list)
    source_name: str = ""

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name,
            "channels": [c.to_dict() for c in self.channels],
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> ImportedPatch:
        if not data:
            return cls()
        channels = [PatchChannel.from_dict(c) for c in data.get("channels") or []]
        return cls(
            channels=channels,
            source_name=str(data.get("source_name") or ""),
        )

    def channel_merges(self) -> dict[int, FadeMerge]:
        """Absolute address (1–512) → HTP/LTP. Last row wins on collision."""
        out: dict[int, FadeMerge] = {}
        for ch in self.channels:
            if 1 <= ch.address <= 512:
                out[ch.address] = ch.merge
        return out

    def channel_tags(self) -> dict[int, FxTagKind]:
        """Map merge flags onto tag kinds used by the existing fade path.

        HTP → intensity. LTP is not forced to position (avoids false 16-bit pairs);
        playback uses ``channel_merges()`` for the actual HTP/LTP rule.
        """
        out: dict[int, FxTagKind] = {}
        for ch in self.channels:
            if not (1 <= ch.address <= 512):
                continue
            if ch.merge is FadeMerge.HTP:
                out[ch.address] = FxTagKind.INTENSITY
        return out

    def summary(self) -> dict:
        heads = sorted({c.head_name for c in self.channels if c.head_name})
        htp = sum(1 for c in self.channels if c.merge is FadeMerge.HTP)
        ltp = sum(1 for c in self.channels if c.merge is FadeMerge.LTP)
        universes = sorted({c.universe for c in self.channels})
        return {
            "heads": len(heads),
            "head_names": heads,
            "channels": len(self.channels),
            "htp": htp,
            "ltp": ltp,
            "universes": universes,
            "source_name": self.source_name,
        }


def parse_dmx_token(token: str) -> tuple[int, int]:
    """Parse ``1-001``, ``1/001``, or ``1.001`` into (universe, address)."""
    text = str(token or "").strip()
    match = _DMX_RE.match(text)
    if not match:
        raise PatchCsvError(f"invalid dmx address {token!r} (expected e.g. 1-001)")
    universe = int(match.group(1))
    address = int(match.group(2))
    if universe < 1:
        raise PatchCsvError(f"universe must be >= 1, got {universe}")
    if not (1 <= address <= 512):
        raise PatchCsvError(f"address must be 1–512, got {address}")
    return universe, address


def _norm_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")


def _parse_merge(raw: str, *, line: int) -> FadeMerge:
    text = str(raw or "").strip().lower()
    if text in ("htp", "highest", "highest_takes_precedence"):
        return FadeMerge.HTP
    if text in ("ltp", "latest", "latest_takes_precedence"):
        return FadeMerge.LTP
    raise PatchCsvError(f"merge must be HTP or LTP, got {raw!r}", line=line)


def _row_universe_address(row: dict[str, str], *, line: int) -> tuple[int, int]:
    dmx = (row.get("dmx") or row.get("dmx_address") or "").strip()
    if dmx:
        try:
            return parse_dmx_token(dmx)
        except PatchCsvError as exc:
            raise PatchCsvError(str(exc), line=line) from exc

    uni_raw = (row.get("universe") or row.get("uni") or "").strip()
    addr_raw = (row.get("address") or row.get("addr") or "").strip()
    if not uni_raw or not addr_raw:
        raise PatchCsvError(
            "need dmx (e.g. 1-001) or both universe and address",
            line=line,
        )
    try:
        universe = int(uni_raw)
        address = int(addr_raw)
    except ValueError as exc:
        raise PatchCsvError(
            f"universe/address must be integers, got {uni_raw!r}/{addr_raw!r}",
            line=line,
        ) from exc
    if universe < 1:
        raise PatchCsvError(f"universe must be >= 1, got {universe}", line=line)
    if not (1 <= address <= 512):
        raise PatchCsvError(f"address must be 1–512, got {address}", line=line)
    return universe, address


def parse_patch_csv(
    text: str,
    *,
    source_name: str = "",
) -> ImportedPatch:
    """Parse patch CSV text into an ``ImportedPatch``. Rejects bad rows clearly."""
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise PatchCsvError("CSV has no header row")

    field_map = {_norm_header(h): h for h in reader.fieldnames if h is not None}
    needed_any = (
        "dmx" in field_map
        or "dmx_address" in field_map
        or ("universe" in field_map and "address" in field_map)
        or ("uni" in field_map and "addr" in field_map)
    )
    if "head_name" not in field_map and "head" not in field_map:
        raise PatchCsvError("missing required column head_name")
    if "merge" not in field_map:
        raise PatchCsvError("missing required column merge")
    if not needed_any:
        raise PatchCsvError(
            "missing address columns (need dmx, or universe + address)"
        )

    channels: list[PatchChannel] = []
    for index, raw in enumerate(reader, start=2):
        row = {
            _norm_header(k): (v.strip() if isinstance(v, str) else v)
            for k, v in raw.items()
            if k is not None
        }
        # Skip blank / comment rows
        values = [v for v in row.values() if v]
        if not values:
            continue
        head_cell = row.get("head_name") or row.get("head") or ""
        if str(head_cell).lstrip().startswith("#"):
            continue
        head_name = str(head_cell).strip()
        if not head_name:
            raise PatchCsvError("head_name is required", line=index)

        merge = _parse_merge(row.get("merge") or "", line=index)
        universe, address = _row_universe_address(row, line=index)

        offset_raw = (row.get("channel_offset") or row.get("offset") or "").strip()
        channel_offset: int | None = None
        if offset_raw:
            try:
                channel_offset = int(offset_raw)
            except ValueError as exc:
                raise PatchCsvError(
                    f"channel_offset must be an integer, got {offset_raw!r}",
                    line=index,
                ) from exc
            if channel_offset < 1:
                raise PatchCsvError(
                    f"channel_offset must be >= 1, got {channel_offset}",
                    line=index,
                )

        channel_name = str(
            row.get("channel_name") or row.get("attribute") or ""
        ).strip()
        channels.append(
            PatchChannel(
                head_name=head_name,
                universe=universe,
                address=address,
                merge=merge,
                channel_offset=channel_offset,
                channel_name=channel_name,
            )
        )

    if not channels:
        raise PatchCsvError("CSV has no channel rows")

    return ImportedPatch(channels=channels, source_name=source_name)


def load_imported_patch(path) -> ImportedPatch:
    import json
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return ImportedPatch()
    with open(p, encoding="utf-8") as f:
        return ImportedPatch.from_dict(json.load(f))


def save_imported_patch(path, patch: ImportedPatch) -> None:
    import json
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(patch.to_dict(), f, indent=2)
