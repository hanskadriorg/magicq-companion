# Fixture patch CSV

Standalone FX learns HTP / LTP merge rules from a flat CSV — one row per DMX channel. Hand-edit or export from MagicQ (heads + channel HTP/LTP), then import on the Standalone FX screen.

## Columns

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

## Merge rules

- **HTP** — highest takes precedence (intensity-like fades).
- **LTP** — latest takes precedence (position / colour / other).

## Example

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

Imported patches are stored next to the FX library as `fx_patch.json` and survive restart.
