# Fixture patch CSV

Standalone FX learns HTP / LTP merge rules from a flat CSV — one row per DMX channel. Import on the Standalone FX screen from either:

1. A **hand-edited simple** CSV, or
2. A MagicQ **View Chans** export, used as-is (no reformat).

## Simple format

| Column | Required | Meaning |
|--------|----------|---------|
| `head_name` | yes | Fixture / head label (e.g. `MAC 700 1`) |
| `universe` | one of\* | DMX universe (1-based, as in MagicQ `1-001`) |
| `address` | one of\* | Absolute address in that universe (1–512) |
| `dmx` | one of\* | Compact MagicQ-style address: `1-001`, `1/001`, or `1.001` |
| `channel_offset` | no | Offset inside the fixture (1-based); informational |
| `channel_name` | no | Attribute name (Dimmer, Pan, Red, …) |
| `merge` | yes | `HTP` or `LTP` |

\* Provide either `dmx` **or** both `universe` and `address`.

Header names are case-insensitive. Extra columns are ignored. Blank lines and `#` comments are skipped.

### Example

See [`docs/examples/patch_example.csv`](examples/patch_example.csv).

```csv
head_name,universe,address,channel_offset,channel_name,merge
MAC 700 1,1,1,1,Dimmer,HTP
MAC 700 1,1,2,2,Dimmer fine,HTP
MAC 700 1,1,23,23,Pan,LTP
FP5 401,1,100,1,Red,LTP
```

Equivalent with `dmx`:

```csv
head_name,dmx,channel_offset,channel_name,merge
MAC 700 1,1-001,1,Dimmer,HTP
MAC 700 1,1-023,23,Pan,LTP
```

## MagicQ View Chans export

In MagicQ, use **View Chans** and export the channel list. The companion accepts that file directly.

Typical headers (trailing spaces are fine — they are stripped):

`DMX`, `Head type`, `Attrib`, `Chan name`, `Hd no`, `Type`

| MagicQ column | Maps to |
|---------------|---------|
| `DMX` | address (`1-001` / `1/001` / `1.001`) |
| `Type` | merge (`HTP` or `LTP`) |
| `Attrib` | `channel_name` (Dimmer, Red, …) |
| `Chan name` + `Hd no` | `head_name` (e.g. `FP7LunaBulb 1`) |
| `Head type` | personality only; used for `head_name` if `Chan name` is empty (`Head type` + `Hd no`) |

Rows with **both** `Type` and `Attrib` empty are skipped (unused-universe dump — a full export can be tens of thousands of rows with only the patched channels kept).

Whitespace on headers and cell values is stripped.

### Example

See [`docs/examples/patch_magicq_view_chans.csv`](examples/patch_magicq_view_chans.csv).

```csv
DMX ,Head type ,Attrib ,Chan name ,Hd no ,Type
1-001 ,Generic Dimmer ,Dimmer ,House ,1 ,HTP
1-010 ,Chauvet ,Red ,FP7LunaBulb ,1 ,LTP
1-002 ,,,,,
```

(The blank Type/Attrib row is ignored.)

## Persistence

Imported patches are stored next to the FX library as `fx_patch.json` and survive restart.
