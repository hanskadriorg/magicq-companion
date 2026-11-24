"""Control channel numbering: sequential 1..10 and legacy address migration."""

from __future__ import annotations

from dataclasses import dataclass

# Logical control channels mapped onto MagicQ PB1..PB10 (sequential 1..10).
CONTROL_CHANNEL_NAMES: tuple[str, ...] = (
    "groove",
    "breakdown",
    "buildup",
    "drop",
    "build_progress",
    "beat_pulse",
    "drop_hit",
    "four_four",
    "bpm_rate",
    "fx_size",
)

# Default DMX addresses before the 1..10 renumber (gaps at 7, 8, 10, 12, 13).
LEGACY_DEFAULT_ADDRESSES: dict[str, int] = {
    "groove": 1,
    "breakdown": 2,
    "buildup": 3,
    "drop": 4,
    "build_progress": 5,
    "beat_pulse": 6,
    "drop_hit": 9,
    "four_four": 11,
    "bpm_rate": 14,
    "fx_size": 10,
}


@dataclass(frozen=True)
class ChannelRenumberMap:
    """Maps legacy DMX addresses to the sequential 1..10 layout."""

    old_to_new: dict[int, int]
    new_to_old: dict[int, int]
    name_to_new: dict[str, int]

    def migrate_address(self, address: int) -> int:
        """Return the new address if `address` is a known legacy control channel."""
        return self.old_to_new.get(int(address), int(address))

    def document_lines(self) -> list[str]:
        lines = [
            "# Control channel map (legacy DMX address -> sequential 1..10)",
            "",
            "| Name | Legacy | Current |",
            "|------|--------|---------|",
        ]
        for i, name in enumerate(CONTROL_CHANNEL_NAMES, start=1):
            legacy = LEGACY_DEFAULT_ADDRESSES[name]
            lines.append(f"| {name} | {legacy} | {i} |")
        lines.append("")
        lines.append(
            "Configs that still use legacy numbers are upgraded on load; "
            "unknown addresses are left unchanged."
        )
        return lines


def build_renumber_map(
    legacy_addresses: dict[str, int] | None = None,
) -> ChannelRenumberMap:
    legacy = legacy_addresses or LEGACY_DEFAULT_ADDRESSES
    old_to_new: dict[int, int] = {}
    new_to_old: dict[int, int] = {}
    name_to_new: dict[str, int] = {}
    for i, name in enumerate(CONTROL_CHANNEL_NAMES, start=1):
        old = int(legacy.get(name, i))
        old_to_new[old] = i
        new_to_old[i] = old
        name_to_new[name] = i
    return ChannelRenumberMap(
        old_to_new=old_to_new,
        new_to_old=new_to_old,
        name_to_new=name_to_new,
    )


DEFAULT_RENUMBER = build_renumber_map()


def sequential_addresses() -> dict[str, int]:
    return {name: i for i, name in enumerate(CONTROL_CHANNEL_NAMES, start=1)}


def migrate_channel_map_values(values: dict[str, int]) -> dict[str, int]:
    """If values match legacy defaults or explicit legacy slots, renumber to 1..10."""
    renum = DEFAULT_RENUMBER
    out: dict[str, int] = {}
    for name in CONTROL_CHANNEL_NAMES:
        raw = int(values.get(name, renum.name_to_new[name]))
        if raw in renum.old_to_new and renum.old_to_new[raw] == renum.name_to_new[name]:
            out[name] = renum.name_to_new[name]
        elif raw == renum.name_to_new[name]:
            out[name] = raw
        else:
            if raw == LEGACY_DEFAULT_ADDRESSES.get(name, raw):
                out[name] = renum.name_to_new[name]
            else:
                out[name] = raw
    return out


def control_channel_index(address: int, channel_map: dict[str, int]) -> int | None:
    """Return 0-based control index when `address` is one of the mapped outputs."""
    for i, name in enumerate(CONTROL_CHANNEL_NAMES):
        if int(channel_map.get(name, i + 1)) == int(address):
            return i
    return None
