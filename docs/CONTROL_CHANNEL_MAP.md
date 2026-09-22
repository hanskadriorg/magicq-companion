# Control channel map (legacy DMX → new 1..n)

| Name | Legacy | New |
|------|--------|-----|
| groove | 1 | 1 |
| breakdown | 2 | 2 |
| buildup | 3 | 3 |
| drop | 4 | 4 |
| build_progress | 5 | 5 |
| beat_pulse | 6 | 6 |
| drop_hit | 9 | 7 |
| four_four | 11 | 8 |
| bpm_rate | 14 | 9 |
| drop_momentary | 10 | 10 |

Configs that still use legacy numbers are upgraded on load; unknown custom addresses are left unchanged.

Legacy autopilot output forced channels **7, 8, and 10** to 0; **12** and **13** were unused. **MagicQ must be repatched for channels 7–10** (`drop_hit`, `four_four`, `bpm_rate`, `drop_momentary`).

## Standalone FX interim notes

- Companion is the only transmitter to fixtures; record console input on a **different universe** than output.
- Cue order is a **shuffle bag** with no immediate repeat.
- **Momentary** assignments follow the button and ignore the step timer; a short press still completes fade-in then fade-out.
- **Latched** assignments may use the step timer while the channel stays on.
- BPM holds the last good value from audio analysis; until then **128**.
- Fades release cleanly (no pan/tilt slammed to 0); hard zero only on explicit blackout.
- Position tags fade **16-bit coarse/fine pairs** together.
