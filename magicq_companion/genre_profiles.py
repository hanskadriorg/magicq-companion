"""Slow Genre Policy Layer & online user-in-the-loop genre profile learning.

Architectural separation:
  * Layer 1 (this module — Slow Genre Policy Layer):
      Maps the Genre Agent's 8-archetype probability distribution onto macro
      settings (`build_intensity`, `drop_intensity`, `drop_hold_seconds`,
      `breakdown_intensity`, `breakdown_beats`, `four_four_intensity`,
      `bpm_rate` floor/ceiling/decay, and `fx_size` base/max/reactivity).
      When an operator steps in to adjust any of these macro parameters while
      music is playing, `learn_override()` applies a Normalized Least-Mean-
      Squares (NLMS) credit update across the active genres and persists the
      learned profile to `genre_profiles.json`.
  * Layer 2 (FeatureExtractor / SectionTracker — Fast Track Reactivity Layer):
      Beat pulses, 4/4 PLL phase, spectral flux spikes, `drop_rise_window`,
      `drop_smooth_seconds`, `drop_hit` flashes, and fast within-track energy
      surges on `fx_size` run independently at the 12 ms hop rate and are
      never reset, lagged, or dampened by Layer 1 updates.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import ARCHETYPE_NAMES, BPM_RATE_NAMES

_EPS = 1e-9
_BPM_NAME_TO_IDX = {name: float(i) for i, name in enumerate(BPM_RATE_NAMES)}

ARCHETYPE_LABELS: dict[str, str] = {
    "ambient_chill": "Ambient / Chill",
    "groovy_warm": "Deep / Warm Groove",
    "house_tech": "House / Tech House",
    "melodic_prog": "Melodic / Progressive",
    "club_peak": "Peak Club / Techno",
    "hiphop_urban": "Hip-Hop / R&B / Urban",
    "fast_bass": "DnB / Breaks / Bass",
    "rock_live": "Rock / Pop / Live",
}


@dataclass
class GenrePolicy:
    """Macro genre-layer settings for one archetype or a probability blend."""

    build_intensity: float = 0.56
    build_decay_seconds: float = 8.0
    drop_intensity: float = 0.58
    drop_hold_seconds: float = 9.0
    breakdown_intensity: float = 0.56
    breakdown_beats: float = 4.0
    four_four_intensity: float = 0.58
    bpm_rate_min_idx: float = 1.0
    bpm_rate_max_idx: float = 2.4
    bpm_rate_drop_max_idx: float = 3.0
    bpm_rate_decay: float = 3.5
    fx_size_base: float = 105.0
    fx_size_max: float = 235.0
    fx_size_reactivity: float = 0.72

    @property
    def bpm_rate_min_name(self) -> str:
        idx = max(0, min(3, int(round(self.bpm_rate_min_idx))))
        return BPM_RATE_NAMES[idx]

    @property
    def bpm_rate_max_name(self) -> str:
        idx = max(0, min(3, int(round(max(self.bpm_rate_max_idx, self.bpm_rate_min_idx)))))
        return BPM_RATE_NAMES[idx]

    def to_ui_dict(self) -> dict[str, Any]:
        lo_idx = max(0, min(3, int(round(self.bpm_rate_min_idx))))
        hi_idx = max(lo_idx, min(3, int(round(self.bpm_rate_max_idx))))
        return {
            "build_intensity": round(self.build_intensity, 3),
            "build_decay_seconds": round(self.build_decay_seconds, 2),
            "drop_intensity": round(self.drop_intensity, 3),
            "drop_hold_seconds": round(self.drop_hold_seconds, 1),
            "breakdown_intensity": round(self.breakdown_intensity, 3),
            "breakdown_beats": round(self.breakdown_beats, 1),
            "four_four_intensity": round(self.four_four_intensity, 3),
            "bpm_rate_min": BPM_RATE_NAMES[lo_idx],
            "bpm_rate_max": BPM_RATE_NAMES[hi_idx],
            "bpm_rate_decay": round(self.bpm_rate_decay, 1),
            "fx_size_base": int(round(self.fx_size_base)),
            "fx_size_max": int(round(self.fx_size_max)),
            "fx_size_reactivity": round(self.fx_size_reactivity, 3),
        }


# Parameter bounds: (min_value, max_value)
POLICY_BOUNDS: dict[str, tuple[float, float]] = {
    "build_intensity": (0.0, 1.0),
    "build_decay_seconds": (0.5, 30.0),
    "drop_intensity": (0.0, 1.0),
    "drop_hold_seconds": (2.0, 45.0),
    "breakdown_intensity": (0.0, 1.0),
    "breakdown_beats": (1.0, 16.0),
    "four_four_intensity": (0.0, 1.0),
    "bpm_rate_min_idx": (0.0, 3.0),
    "bpm_rate_max_idx": (0.0, 3.0),
    "bpm_rate_drop_max_idx": (0.0, 3.0),
    "bpm_rate_decay": (0.0, 30.0),
    "fx_size_base": (0.0, 255.0),
    "fx_size_max": (16.0, 255.0),
    "fx_size_reactivity": (0.0, 1.0),
}


# Deliberately distinct defaults so detected genres clearly reshape lighting feel.
DEFAULT_GENRE_POLICIES: dict[str, GenrePolicy] = {
    "ambient_chill": GenrePolicy(
        build_intensity=0.20,
        build_decay_seconds=5.0,
        drop_intensity=0.15,
        drop_hold_seconds=5.0,
        breakdown_intensity=0.82,
        breakdown_beats=2.0,
        four_four_intensity=0.30,
        bpm_rate_min_idx=0.0,
        bpm_rate_max_idx=1.0,
        bpm_rate_drop_max_idx=1.0,
        bpm_rate_decay=4.5,
        fx_size_base=32.0,
        fx_size_max=110.0,
        fx_size_reactivity=0.45,
    ),
    "groovy_warm": GenrePolicy(
        build_intensity=0.38,
        build_decay_seconds=9.5,
        drop_intensity=0.38,
        drop_hold_seconds=8.0,
        breakdown_intensity=0.55,
        breakdown_beats=6.0,
        four_four_intensity=0.52,
        bpm_rate_min_idx=0.0,
        bpm_rate_max_idx=2.0,
        bpm_rate_drop_max_idx=3.0,
        bpm_rate_decay=4.0,
        fx_size_base=72.0,
        fx_size_max=195.0,
        fx_size_reactivity=0.62,
    ),
    "house_tech": GenrePolicy(
        build_intensity=0.56,
        build_decay_seconds=8.0,
        drop_intensity=0.58,
        drop_hold_seconds=9.0,
        breakdown_intensity=0.56,
        breakdown_beats=4.0,
        four_four_intensity=0.58,
        bpm_rate_min_idx=1.0,
        bpm_rate_max_idx=2.3,
        bpm_rate_drop_max_idx=3.0,
        bpm_rate_decay=3.5,
        fx_size_base=105.0,
        fx_size_max=235.0,
        fx_size_reactivity=0.72,
    ),
    "melodic_prog": GenrePolicy(
        build_intensity=0.76,
        build_decay_seconds=15.0,
        drop_intensity=0.54,
        drop_hold_seconds=14.0,
        breakdown_intensity=0.64,
        breakdown_beats=8.0,
        four_four_intensity=0.55,
        bpm_rate_min_idx=1.0,
        bpm_rate_max_idx=2.0,
        bpm_rate_drop_max_idx=3.0,
        bpm_rate_decay=5.0,
        fx_size_base=88.0,
        fx_size_max=255.0,
        fx_size_reactivity=0.82,
    ),
    "club_peak": GenrePolicy(
        build_intensity=0.72,
        build_decay_seconds=6.5,
        drop_intensity=0.78,
        drop_hold_seconds=11.0,
        breakdown_intensity=0.48,
        breakdown_beats=4.0,
        four_four_intensity=0.65,
        bpm_rate_min_idx=2.0,
        bpm_rate_max_idx=3.0,
        bpm_rate_drop_max_idx=3.0,
        bpm_rate_decay=2.5,
        fx_size_base=142.0,
        fx_size_max=255.0,
        fx_size_reactivity=0.88,
    ),
    "hiphop_urban": GenrePolicy(
        build_intensity=0.36,
        build_decay_seconds=5.5,
        drop_intensity=0.52,
        drop_hold_seconds=6.0,
        breakdown_intensity=0.40,
        breakdown_beats=6.0,
        four_four_intensity=0.32,
        bpm_rate_min_idx=0.0,
        bpm_rate_max_idx=2.0,
        bpm_rate_drop_max_idx=3.0,
        bpm_rate_decay=2.5,
        fx_size_base=68.0,
        fx_size_max=215.0,
        fx_size_reactivity=0.75,
    ),
    "fast_bass": GenrePolicy(
        build_intensity=0.64,
        build_decay_seconds=5.0,
        drop_intensity=0.74,
        drop_hold_seconds=8.0,
        breakdown_intensity=0.44,
        breakdown_beats=6.0,
        four_four_intensity=0.30,
        bpm_rate_min_idx=1.0,
        bpm_rate_max_idx=2.0,
        bpm_rate_drop_max_idx=3.0,
        bpm_rate_decay=2.0,
        fx_size_base=118.0,
        fx_size_max=255.0,
        fx_size_reactivity=0.85,
    ),
    "rock_live": GenrePolicy(
        build_intensity=0.50,
        build_decay_seconds=6.5,
        drop_intensity=0.58,
        drop_hold_seconds=7.5,
        breakdown_intensity=0.45,
        breakdown_beats=5.0,
        four_four_intensity=0.38,
        bpm_rate_min_idx=0.0,
        bpm_rate_max_idx=2.2,
        bpm_rate_drop_max_idx=3.0,
        bpm_rate_decay=3.0,
        fx_size_base=95.0,
        fx_size_max=230.0,
        fx_size_reactivity=0.76,
    ),
}


def _clamp_policy(p: GenrePolicy) -> GenrePolicy:
    vals: dict[str, float] = {}
    for field_name, (lo, hi) in POLICY_BOUNDS.items():
        raw = float(getattr(p, field_name))
        vals[field_name] = max(lo, min(hi, raw))
    if vals["bpm_rate_min_idx"] > vals["bpm_rate_max_idx"]:
        vals["bpm_rate_min_idx"] = vals["bpm_rate_max_idx"]
    if vals["bpm_rate_drop_max_idx"] < vals["bpm_rate_max_idx"]:
        vals["bpm_rate_drop_max_idx"] = vals["bpm_rate_max_idx"]
    if vals["fx_size_base"] > vals["fx_size_max"]:
        vals["fx_size_base"] = vals["fx_size_max"]
    return GenrePolicy(**vals)


def _copy_default(name: str) -> GenrePolicy:
    base = DEFAULT_GENRE_POLICIES[name]
    return GenrePolicy(**asdict(base))


class GenreProfileStore:
    """Thread-safe store of base + user-learned GenrePolicy profiles."""

    def __init__(self, path: Path | None = None, *, learning_enabled: bool = True) -> None:
        self.path = path
        self.learning_enabled = bool(learning_enabled)
        self._lock = threading.Lock()
        self._profiles: dict[str, GenrePolicy] = {
            name: _copy_default(name) for name in ARCHETYPE_NAMES
        }
        self._edit_counts: dict[str, int] = {name: 0 for name in ARCHETYPE_NAMES}
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        profiles_raw = raw.get("profiles")
        if not isinstance(profiles_raw, dict):
            return
        for name in ARCHETYPE_NAMES:
            entry = profiles_raw.get(name)
            if not isinstance(entry, dict):
                continue
            merged = asdict(_copy_default(name))
            for key in POLICY_BOUNDS:
                if key in entry:
                    try:
                        merged[key] = float(entry[key])
                    except (TypeError, ValueError):
                        continue
            self._profiles[name] = _clamp_policy(GenrePolicy(**merged))
            try:
                self._edit_counts[name] = max(0, int(entry.get("edits", 0)))
            except (TypeError, ValueError):
                self._edit_counts[name] = 0

    def _save_locked(self) -> None:
        if self.path is None:
            return
        payload = {
            "version": 1,
            "profiles": {
                name: {
                    **{k: round(float(getattr(self._profiles[name], k)), 4) for k in POLICY_BOUNDS},
                    "edits": int(self._edit_counts.get(name, 0)),
                }
                for name in ARCHETYPE_NAMES
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            return

    def resolve(self, probs: dict[str, float]) -> GenrePolicy:
        """Blend GenrePolicy parameters smoothly from archetype probabilities."""
        with self._lock:
            return self._resolve_locked(probs)

    def _resolve_locked(self, probs: dict[str, float]) -> GenrePolicy:
        clean: dict[str, float] = {}
        total = 0.0
        for name in ARCHETYPE_NAMES:
            w = max(0.0, float(probs.get(name, 0.0)))
            clean[name] = w
            total += w
        if total <= _EPS:
            clean = {name: (1.0 if name == "house_tech" else 0.0) for name in ARCHETYPE_NAMES}
        else:
            for name in clean:
                clean[name] /= total

        blended: dict[str, float] = {}
        for key in POLICY_BOUNDS:
            blended[key] = sum(
                clean[name] * float(getattr(self._profiles[name], key))
                for name in ARCHETYPE_NAMES
            )
        return _clamp_policy(GenrePolicy(**blended))

    def edit_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._edit_counts)

    def total_edits(self) -> int:
        with self._lock:
            return sum(self._edit_counts.values())

    def profile_for(self, archetype: str) -> GenrePolicy:
        with self._lock:
            p = self._profiles.get(archetype, self._profiles["house_tech"])
            return GenrePolicy(**asdict(p))

    def all_profiles_summary(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                name: {
                    **self._profiles[name].to_ui_dict(),
                    "label": ARCHETYPE_LABELS[name],
                    "edits": int(self._edit_counts.get(name, 0)),
                }
                for name in ARCHETYPE_NAMES
            }

    def learn_override(
        self,
        probs: dict[str, float],
        ui_updates: dict[str, Any],
    ) -> tuple[GenrePolicy, list[tuple[str, float]]]:
        """Learn user parameter tweaks into the currently active genre profile(s).

        Uses Normalized Least-Mean-Squares (NLMS) credit assignment across
        archetypes with responsibility `p_k >= 0.12` so:
          1. The blended GenrePolicy immediately matches the user's target.
          2. Unrelated genres (`p_k < 0.12`) are completely untouched.
          3. Learned preferences persist to `genre_profiles.json` for next time.
        """
        policy_targets = _ui_updates_to_policy_fields(ui_updates)
        if not policy_targets:
            return self.resolve(probs), []

        with self._lock:
            clean: dict[str, float] = {}
            total = 0.0
            for name in ARCHETYPE_NAMES:
                w = max(0.0, float(probs.get(name, 0.0)))
                clean[name] = w
                total += w
            if total <= _EPS:
                clean = {name: (1.0 if name == "house_tech" else 0.0) for name in ARCHETYPE_NAMES}
            else:
                for name in clean:
                    clean[name] /= total

            active = [name for name in ARCHETYPE_NAMES if clean[name] >= 0.12]
            if not active:
                active = [max(ARCHETYPE_NAMES, key=lambda n: clean[n])]

            norm_sq = sum(clean[name] ** 2 for name in active) + _EPS
            current_blend = self._resolve_locked(clean)

            for key, target_val in policy_targets.items():
                lo, hi = POLICY_BOUNDS[key]
                clamped_target = max(lo, min(hi, float(target_val)))
                cur_val = float(getattr(current_blend, key))
                delta = clamped_target - cur_val
                if abs(delta) <= 1e-6:
                    continue
                for name in active:
                    step = (clean[name] / norm_sq) * delta
                    old_v = float(getattr(self._profiles[name], key))
                    setattr(self._profiles[name], key, max(lo, min(hi, old_v + step)))

            # If the user explicitly lowered/raised bpm_rate_max, keep drop ceiling coherent.
            if "bpm_rate_max_idx" in policy_targets:
                want_max = float(policy_targets["bpm_rate_max_idx"])
                for name in active:
                    if want_max < self._profiles[name].bpm_rate_drop_max_idx and want_max < 2.5:
                        # Respect user ceiling cap while allowing +1 on drops only if desired >= 2
                        self._profiles[name].bpm_rate_drop_max_idx = min(
                            3.0, want_max + (1.0 if want_max >= 1.0 else 0.0)
                        )
                    elif want_max > self._profiles[name].bpm_rate_drop_max_idx:
                        self._profiles[name].bpm_rate_drop_max_idx = want_max

            for name in active:
                self._profiles[name] = _clamp_policy(self._profiles[name])
                if self.learning_enabled:
                    self._edit_counts[name] = int(self._edit_counts.get(name, 0)) + 1

            if self.learning_enabled:
                self._save_locked()

            credited = sorted(
                [(name, clean[name]) for name in active],
                key=lambda item: item[1],
                reverse=True,
            )
            return self._resolve_locked(clean), credited

    def reset_profiles(self, archetype: str | None = None) -> None:
        """Reset one archetype (or all when `archetype` is None/'all') to defaults."""
        with self._lock:
            if archetype is None or archetype == "all":
                targets = list(ARCHETYPE_NAMES)
            elif archetype in ARCHETYPE_NAMES:
                targets = [archetype]
            else:
                return
            for name in targets:
                self._profiles[name] = _copy_default(name)
                self._edit_counts[name] = 0
            self._save_locked()


def _ui_updates_to_policy_fields(ui_updates: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    direct_keys = (
        "build_intensity",
        "build_decay_seconds",
        "drop_intensity",
        "drop_hold_seconds",
        "breakdown_intensity",
        "breakdown_beats",
        "four_four_intensity",
        "bpm_rate_decay",
        "fx_size_base",
        "fx_size_max",
        "fx_size_reactivity",
    )
    for key in direct_keys:
        if ui_updates.get(key) is not None:
            try:
                out[key] = float(ui_updates[key])
            except (TypeError, ValueError):
                continue
    if ui_updates.get("bpm_rate_min") is not None:
        name = str(ui_updates["bpm_rate_min"]).strip().lower()
        if name in _BPM_NAME_TO_IDX:
            out["bpm_rate_min_idx"] = _BPM_NAME_TO_IDX[name]
    if ui_updates.get("bpm_rate_max") is not None:
        name = str(ui_updates["bpm_rate_max"]).strip().lower()
        if name in _BPM_NAME_TO_IDX:
            out["bpm_rate_max_idx"] = _BPM_NAME_TO_IDX[name]
    return out
