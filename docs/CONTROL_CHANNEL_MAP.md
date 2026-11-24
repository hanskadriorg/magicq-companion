# Control channel map (legacy DMX -> sequential 1..10)

| Name | Legacy | Current | Behaviour |
|------|--------|---------|-----------|
| groove | 1 | 1 | 255 while steady main section is playing |
| breakdown | 2 | 2 | 255 while kick/beat drops out |
| buildup | 3 | 3 | 255 while tension is rising toward a drop |
| drop | 4 | 4 | 255 for the drop hold duration |
| build_progress | 5 | 5 | 0-255 ramp during buildup, 255 on drop |
| beat_pulse | 6 | 6 | Hard 0/255 toggle on every beat |
| drop_hit | 9 | 7 | One-shot 255 impact trigger at drop start |
| four_four | 11 | 8 | 255 while a 4/4 kick grid is locked |
| bpm_rate | 14 | 9 | 32 / 64 / 128 / 255 chase divider driven by Genre Agent + intrinsic pace |
| drop_momentary | 10 | 10 | Held at 0 in unattended venue autopilot |

Configs that still use legacy channel numbers (`drop_hit = 9`, `four_four = 11`, `bpm_rate = 14`) or retired `[app]`, `[fx]`, and `[matter]` sections are upgraded/ignored automatically on load.
