"""Parse fixture patch CSV: simple columns or MagicQ View Chans export."""

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

# MagicQ / CSV attribute names → RGB or CMY component.
_COLOUR_RGB = {
    "red": "r",
    "r": "r",
    "green": "g",
    "g": "g",
    "blue": "b",
    "b": "b",
}
_COLOUR_CMY = {
    "cyan": "c",
    "c": "c",
    "magenta": "m",
    "m": "m",
    "yellow": "y",
    "y": "y",
}


def colour_component(channel_name: str) -> str | None:
    """Return 'r'|'g'|'b'|'c'|'m'|'y' for a colour attribute, else None."""
    raw = str(channel_name or "").strip().lower()
    if not raw:
        return None
    # Strip common suffixes: "Red fine", "Blue 1"
    token = re.split(r"[\s_/.-]+", raw, maxsplit=1)[0]
    if token in _COLOUR_RGB:
        return _COLOUR_RGB[token]
    if token in _COLOUR_CMY:
        return _COLOUR_CMY[token]
    if "red" in raw and "fine" not in raw:
        return "r"
    if "green" in raw and "fine" not in raw:
        return "g"
    if "blue" in raw and "fine" not in raw:
        return "b"
    if raw.startswith("cyan") or " cyan" in f" {raw}":
        return "c"
    if raw.startswith("magenta") or " magenta" in f" {raw}":
        return "m"
    if raw.startswith("yellow") and "fine" not in raw:
        return "y"
    return None


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
        """Map merge / attribute names onto tag kinds used by the fade path.

        HTP → intensity. Colour attribute names (Red/Green/Blue/…) → colour.
        Other LTP rows are left untagged so playback uses ``channel_merges()``
        without inventing false 16-bit position pairs.
        """
        out: dict[int, FxTagKind] = {}
        for ch in self.channels:
            if not (1 <= ch.address <= 512):
                continue
            if colour_component(ch.channel_name) is not None:
                out[ch.address] = FxTagKind.COLOUR
            elif ch.merge is FadeMerge.HTP:
                out[ch.address] = FxTagKind.INTENSITY
        return out

    def entries(self) -> list[dict]:
        """Readable per-channel rows for the dashboard patch list."""
        rows: list[dict] = []
        for ch in self.channels:
            rows.append(
                {
                    "head_name": ch.head_name,
                    "channel_name": ch.channel_name,
                    "universe": ch.universe,
                    "address": ch.address,
                    "dmx": f"{ch.universe}-{ch.address:03d}",
                    "merge": ch.merge.value.upper(),
                }
            )
        return rows

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
            "entries": self.entries(),
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


def _is_magicq_view_chans(field_map: dict[str, str]) -> bool:
    """True for MagicQ View Chans export headers (DMX / Attrib / Chan name / Type)."""
    if "dmx" not in field_map:
        return False
    if "type" not in field_map and "merge" not in field_map:
        return False
    return any(
        key in field_map
        for key in ("attrib", "chan_name", "hd_no", "head_type")
    )


def _head_name_from_view_chans(row: dict[str, str], *, line: int) -> str:
    """Build head label: ``Chan name`` + ``Hd no``, else ``Head type`` + ``Hd no``."""
    chan_name = str(row.get("chan_name") or "").strip()
    head_type = str(row.get("head_type") or "").strip()
    hd_no = str(row.get("hd_no") or "").strip()
    base = chan_name or head_type
    if not base:
        raise PatchCsvError(
            "need Chan name or Head type to build head_name",
            line=line,
        )
    if hd_no:
        return f"{base} {hd_no}"
    return base


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
    """Parse patch CSV text into an ``ImportedPatch``. Rejects bad rows clearly.

    Accepts the companion simple format (``head_name`` + ``merge`` + address)
    and MagicQ **View Chans** exports (``DMX``, ``Attrib``, ``Chan name``,
    ``Hd no``, ``Type``, optional ``Head type``). Headers and cells may carry
    trailing spaces; unused-universe rows (empty Type and Attrib) are skipped.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise PatchCsvError("CSV has no header row")

    field_map = {_norm_header(h): h for h in reader.fieldnames if h is not None}
    view_chans = _is_magicq_view_chans(field_map)
    needed_any = (
        "dmx" in field_map
        or "dmx_address" in field_map
        or ("universe" in field_map and "address" in field_map)
        or ("uni" in field_map and "addr" in field_map)
    )
    if view_chans:
        if not needed_any:
            raise PatchCsvError(
                "missing address columns (need dmx, or universe + address)"
            )
    else:
        if "head_name" not in field_map and "head" not in field_map:
            raise PatchCsvError("missing required column head_name")
        if "merge" not in field_map and "type" not in field_map:
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

        if view_chans:
            type_val = str(row.get("type") or row.get("merge") or "").strip()
            attrib = str(
                row.get("attrib")
                or row.get("channel_name")
                or row.get("attribute")
                or ""
            ).strip()
            # Unused universe dump: empty Type and empty Attrib
            if not type_val and not attrib:
                continue
            head_name = _head_name_from_view_chans(row, line=index)
            merge = _parse_merge(type_val, line=index)
            channel_name = attrib
        else:
            head_cell = row.get("head_name") or row.get("head") or ""
            if str(head_cell).lstrip().startswith("#"):
                continue
            head_name = str(head_cell).strip()
            if not head_name:
                raise PatchCsvError("head_name is required", line=index)

            merge = _parse_merge(
                row.get("merge") or row.get("type") or "",
                line=index,
            )
            channel_name = str(
                row.get("channel_name")
                or row.get("attrib")
                or row.get("attribute")
                or ""
            ).strip()

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
