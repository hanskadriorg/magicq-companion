"""Configuration loading with defaults, from a TOML file."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class NetworkConfig:
    """Shared NIC + unicast destination for both audio and Matter DMX."""

    interface: str = ""
    ip: str = "127.0.0.1"


@dataclass
class ArtNetConfig:
    # Network DMX output (section name kept as [artnet] for compatibility).
    # protocol: "artnet" | "sacn"
    protocol: str = "artnet"
    # mode: Art-Net = unicast|broadcast|localhost; sACN = unicast|multicast|localhost.
    mode: str = "unicast"
    # Local NIC to send from ("" = system default). Name like "eth0".
    interface: str = ""
    ip: str = "127.0.0.1"
    port: int = 6454
    universe: int = 0
    fps: int = 40
    # sACN packet priority (0–200). Higher wins when multiple sources collide.
    priority: int = 100
    # Live dashboard Output switch. Boot / process start uses [server] startup_output.
    enabled: bool = True


@dataclass
class MatterConfig:
    """Matter → MagicQ DMX segment (same NIC as audio, own universe/protocol)."""

    enabled: bool = True
    protocol: str = "artnet"
    mode: str = "broadcast"
    universe: int = 1
    port: int = 6454
    fps: int = 40
    priority: int = 100


INPUT_CHANNEL_CHOICES = ("left", "right", "both")


def coerce_input_channel(value: object) -> str:
    raw = str(value or "left").strip().lower()
    if raw in ("r", "right", "2"):
        return "right"
    if raw in ("both", "stereo", "mix", "lr", "l+r"):
        return "both"
    return "left"


@dataclass
class AudioConfig:
    samplerate: int = 44100
    blocksize: int = 512
    device: str = ""
    # Which captured channel(s) to analyse: left | right | both (average).
    input_channel: str = "left"
    # Everything quieter than this counts as noise, not music. Set it just
    # above the hiss you see on the dashboard input meter with the DJ paused.
    noise_floor_db: float = -55.0


@dataclass
class ChannelMap:
    groove: int = 1
    breakdown: int = 2
    buildup: int = 3
    drop: int = 4
    build_progress: int = 5
    beat_pulse: int = 6
    # One-shot pulse at the moment DROP begins (for cue/macro triggers).
    drop_hit: int = 7
    # 255 while a 4/4 kick grid is locked in the music (else 0).
    four_four: int = 8
    # Chase rate vs BPM: 255=full, 128=half, 64=quarter, 32=eighth (energy-picked).
    bpm_rate: int = 9
    # Momentary drop trigger (standalone: follows button; autopilot: held dark).
    drop_momentary: int = 10


@dataclass
class DetectionConfig:
    kick_absent_below: float = 0.45
    kick_present_above: float = 0.80
    build_score_on: float = 0.5
    evidence_seconds: float = 2.0
    min_dwell_seconds: float = 6.0
    drop_hold_seconds: float = 16.0
    build_timeout_seconds: float = 30.0
    expected_build_seconds: float = 12.0
    drop_energy_jump: float = 1.35
    baseline_seconds: float = 45.0
    # 0 = groove-oriented (harder), 1 = aggressive (easier). Independent.
    build_intensity: float = 0.55
    drop_intensity: float = 0.55
    # Seconds BUILDUP can sit below the build threshold before falling back.
    build_decay_seconds: float = 8.0
    # Fast drop: energy must rise by drop_rise within drop_rise_window seconds.
    drop_rise_window: float = 0.5
    drop_rise: float = 0.40
    # Minimum bass_norm at the hit (1.0 = recent average kick level).
    drop_min_bass: float = 0.70
    # How long we must already be in the current section (0 = immediate).
    drop_min_state_seconds: float = 0.15
    # Channel 9: hold 255 this long after DROP starts, then fade to 0.
    drop_hit_seconds: float = 0.15
    drop_hit_fade_seconds: float = 0.0
    # Energy/bass smoothing for drop sensing (smaller = faster, twitchier).
    drop_smooth_seconds: float = 0.08
    # Allow a drop hit while still labelled GROOVE (short/no build).
    drop_from_groove: bool = True
    # Also fire on a spectral-flux "onset" spike with bass (same family as
    # librosa onset strength, computed live each hop — no extra library).
    drop_use_onset: bool = True
    # 0 = stricter 4/4 lock, 1 = locks on a sloppier / shorter kick run.
    four_four_intensity: float = 0.55
    # Seconds the 4/4 must hold before channel 11 goes 255, and stay 255
    # after the grid slips. 0 = follow the lock immediately.
    four_four_smooth_seconds: float = 0.4
    # 0 = needs a long kick-out, 1 = breakdown on a shorter gap / weaker bass.
    breakdown_intensity: float = 0.55
    # Beats of missing kick before a breakdown cue (style still scales this).
    breakdown_beats: float = 4.0
    # Arrangement style: auto | club | long_build | punchy | breakdown
    style: str = "auto"
    # Blackout Art-Net/sACN (and Matter DMX) after this long with no music.
    silence_gate: bool = True
    silence_timeout_seconds: float = 600.0


STYLE_NAMES = ("auto", "club", "long_build", "punchy", "breakdown")

# Biases applied on top of the user's intensity / decay sliders. These are
# deliberately far apart: "punchy" reacts in about a third of the time that
# "long_build" takes, so switching style is audible on the lights.
STYLE_PROFILES = {
    "club": {
        "build_threshold_mul": 1.00,
        "build_decay_mul": 1.00,
        "drop_jump_mul": 1.00,
        "drop_intensity_add": 0.00,
        "breakdown_beats": 4.0,
        "evidence_mul": 1.00,
        "dwell_mul": 1.00,
        "breakdown_dwell_mul": 0.45,
        "expected_build_mul": 1.00,
        "drop_hold_mul": 1.00,
        "drop_rise_mul": 1.00,
        "kick_absent_mul": 1.00,
    },
    "long_build": {
        "build_threshold_mul": 0.52,
        "build_decay_mul": 2.40,
        "drop_jump_mul": 1.18,
        "drop_intensity_add": 0.00,
        "breakdown_beats": 8.0,
        "evidence_mul": 1.70,
        "dwell_mul": 1.80,
        "breakdown_dwell_mul": 0.90,
        "expected_build_mul": 2.20,
        "drop_hold_mul": 1.45,
        "drop_rise_mul": 1.25,
        "kick_absent_mul": 1.10,
    },
    "punchy": {
        "build_threshold_mul": 1.45,
        "build_decay_mul": 0.35,
        "drop_jump_mul": 0.62,
        "drop_intensity_add": 0.22,
        "breakdown_beats": 2.0,
        "evidence_mul": 0.32,
        "dwell_mul": 0.28,
        "breakdown_dwell_mul": 0.18,
        "expected_build_mul": 0.45,
        "drop_hold_mul": 0.55,
        "drop_rise_mul": 0.62,
        "kick_absent_mul": 0.75,
    },
    # Prefer atmospheric / kick-out over a 4/4 groove.
    "breakdown": {
        "build_threshold_mul": 1.18,
        "build_decay_mul": 0.70,
        "drop_jump_mul": 1.05,
        "drop_intensity_add": 0.00,
        "breakdown_beats": 1.5,
        "evidence_mul": 0.80,
        "dwell_mul": 0.50,
        "breakdown_dwell_mul": 0.12,
        "expected_build_mul": 0.80,
        "drop_hold_mul": 0.90,
        "drop_rise_mul": 1.00,
        "kick_absent_mul": 1.35,
        "breakdown_need_mul": 0.22,
        "groove_return_mul": 2.40,
        "fizzle_to": "breakdown",
    },
}


@dataclass
class BuildScoreConfig:
    # Slope cues (classic rising tension).
    weight_energy: float = 0.24
    weight_treble: float = 0.20
    weight_onsets: float = 0.16
    # Level lift vs earlier in the window (slow techno filter builds).
    weight_lift: float = 0.20
    # Highs rising while kick stays flat/falling (filter-open builds).
    weight_divergence: float = 0.12
    # Busy hats / spectral activity above the recent baseline.
    weight_activity: float = 0.08
    full_scale_energy_slope: float = 0.04
    full_scale_treble_slope: float = 0.05
    full_scale_onset_slope: float = 0.8
    full_scale_lift: float = 0.18
    full_scale_activity: float = 2.5


# Channel-14 chase-rate steps (DMX values).
BPM_RATE_NAMES = ("eighth", "quarter", "half", "full")
BPM_RATE_NAME_TO_LEVEL = {
    "eighth": 32,
    "quarter": 64,
    "half": 128,
    "full": 255,
}


VISUALIZER_MODES = ("auto", "absolute", "level")


def coerce_visualizer_mode(value: object) -> str:
    name = str(value or "auto").strip().lower()
    return name if name in VISUALIZER_MODES else "auto"


@dataclass
class UiConfig:
    """Dashboard options that also change what the Pi computes."""

    # When false, the Pi skips the spectrum visualiser (saves CPU/RAM).
    visualizer: bool = True
    # auto = autoranged bars, absolute = fixed dBFS scale, level = input scope.
    visualizer_mode: str = "auto"


REBOOT_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REBOOT_DAY_CRON = {
    "mon": "1",
    "tue": "2",
    "wed": "3",
    "thu": "4",
    "fri": "5",
    "sat": "6",
    "sun": "0",
}
_REBOOT_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
_REBOOT_DAY_ALIASES = {
    "monday": "mon",
    "mon": "mon",
    "1": "mon",
    "tuesday": "tue",
    "tue": "tue",
    "2": "tue",
    "wednesday": "wed",
    "wed": "wed",
    "3": "wed",
    "thursday": "thu",
    "thu": "thu",
    "thur": "thu",
    "thurs": "thu",
    "4": "thu",
    "friday": "fri",
    "fri": "fri",
    "5": "fri",
    "saturday": "sat",
    "sat": "sat",
    "6": "sat",
    "sunday": "sun",
    "sun": "sun",
    "0": "sun",
    "7": "sun",
}


def try_reboot_time(value: object) -> str | None:
    raw = str(value or "").strip()
    match = _REBOOT_TIME_RE.fullmatch(raw)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_reboot_time(value: object) -> str:
    return try_reboot_time(value) or "14:00"


def coerce_reboot_days(value: object) -> list[str]:
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.replace(";", ",").split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip().lower() for p in value if str(p).strip()]
    else:
        parts = ["mon", "tue", "wed", "thu", "fri"]
    seen: set[str] = set()
    for part in parts:
        name = _REBOOT_DAY_ALIASES.get(part)
        if name:
            seen.add(name)
    return [day for day in REBOOT_DAY_NAMES if day in seen]


def reboot_cron_dow(days: list[str]) -> str:
    names = coerce_reboot_days(days)
    if len(names) == 7:
        return "*"
    return ",".join(REBOOT_DAY_CRON[day] for day in names)


def _default_reboot_days() -> list[str]:
    return ["mon", "tue", "wed", "thu", "fri"]


APP_MODES = ("sound", "standalone_fx")


@dataclass
class AppConfig:
    """Which program runs on the Pi: automatic sound or standalone FX."""

    mode: str = "sound"


@dataclass
class FxConfig:
    """Standalone FX: listen on this universe; control channels use [channels]."""

    # Universe MagicQ transmits / Companion records (separate from [artnet] output).
    input_universe: int = 0
    # Local IPv4 to bind the Art-Net/sACN listener ("" = all interfaces).
    # Point the console at this address (or the NIC that owns it).
    input_ip: str = ""
    record_fps: float = 40.0


@dataclass
class ServerConfig:
    """Host defaults from the dashboard Server settings modal."""

    # Output switch after a process start or host reboot. Not the live toggle.
    startup_output: bool = True
    reboot_enabled: bool = False
    reboot_time: str = "14:00"
    reboot_days: list[str] = field(default_factory=_default_reboot_days)


@dataclass
class BpmRateConfig:
    """How energy maps onto DMX channel 14 (bpm_rate)."""

    # Inclusive endpoints among: eighth / quarter / half / full.
    # Quiet energy → min, loud energy → max (order is swapped if inverted).
    min: str = "eighth"
    max: str = "full"
    # Extra seconds to hold a higher rate before stepping down.
    decay_seconds: float = 3.0


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    fx: FxConfig = field(default_factory=FxConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    artnet: ArtNetConfig = field(default_factory=ArtNetConfig)
    matter: MatterConfig = field(default_factory=MatterConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    channels: ChannelMap = field(default_factory=ChannelMap)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    build_score: BuildScoreConfig = field(default_factory=BuildScoreConfig)
    bpm_rate: BpmRateConfig = field(default_factory=BpmRateConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


_SECTIONS = {
    "app": AppConfig,
    "fx": FxConfig,
    "network": NetworkConfig,
    "artnet": ArtNetConfig,
    "matter": MatterConfig,
    "audio": AudioConfig,
    "channels": ChannelMap,
    "detection": DetectionConfig,
    "build_score": BuildScoreConfig,
    "bpm_rate": BpmRateConfig,
    "ui": UiConfig,
    "server": ServerConfig,
}


# Keys that older config.toml files may still carry. They are read and
# ignored so an in-place upgrade never fails to start.
_RETIRED_KEYS = {
    ("detection", "intensity"),
    ("channels", "centroid"),
    ("channels", "tilt"),
    ("channels", "energy"),
    ("channels", "kick"),
    ("channels", "melody"),
    ("build_score", "weight_brightness"),
    ("build_score", "full_scale_brightness_slope"),
}


def load_config(path: str | Path | None) -> Config:
    """Load config from a TOML file, falling back to defaults per field."""
    cfg = Config()
    if path is None:
        return cfg
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    for section_name, cls in _SECTIONS.items():
        section_data = data.get(section_name, {})
        target = getattr(cfg, section_name)
        for key, value in section_data.items():
            if (section_name, key) in _RETIRED_KEYS:
                continue
            if hasattr(target, key):
                setattr(target, key, value)
            else:
                raise ValueError(f"Unknown config key: [{section_name}] {key}")
    _migrate_legacy_intensity(cfg, data.get("detection", {}))
    _migrate_control_channel_addresses(cfg)
    _sync_shared_network(cfg)
    cfg.app.mode = _coerce_app_mode(cfg.app.mode)
    cfg.audio.input_channel = coerce_input_channel(cfg.audio.input_channel)
    cfg.audio.noise_floor_db = clamp_noise_floor(cfg.audio.noise_floor_db)
    cfg.ui.visualizer = bool(cfg.ui.visualizer)
    cfg.ui.visualizer_mode = coerce_visualizer_mode(cfg.ui.visualizer_mode)
    cfg.detection.style = coerce_style(cfg.detection.style)
    cfg.detection.silence_gate = bool(cfg.detection.silence_gate)
    cfg.detection.silence_timeout_seconds = max(
        30.0, float(cfg.detection.silence_timeout_seconds)
    )
    cfg.server.startup_output = bool(cfg.server.startup_output)
    cfg.server.reboot_enabled = bool(cfg.server.reboot_enabled)
    cfg.server.reboot_time = parse_reboot_time(cfg.server.reboot_time)
    cfg.server.reboot_days = coerce_reboot_days(cfg.server.reboot_days)
    try:
        cfg.detection.four_four_intensity = max(
            0.0, min(1.0, float(cfg.detection.four_four_intensity))
        )
    except (TypeError, ValueError):
        cfg.detection.four_four_intensity = 0.55
    try:
        cfg.detection.four_four_smooth_seconds = max(
            0.0, min(30.0, float(cfg.detection.four_four_smooth_seconds))
        )
    except (TypeError, ValueError):
        cfg.detection.four_four_smooth_seconds = 0.4
    try:
        cfg.detection.breakdown_intensity = max(
            0.0, min(1.0, float(cfg.detection.breakdown_intensity))
        )
    except (TypeError, ValueError):
        cfg.detection.breakdown_intensity = 0.55
    try:
        cfg.detection.breakdown_beats = max(
            1.0, min(16.0, float(cfg.detection.breakdown_beats))
        )
    except (TypeError, ValueError):
        cfg.detection.breakdown_beats = 4.0
    try:
        cfg.detection.drop_hit_seconds = max(
            0.0, min(5.0, float(cfg.detection.drop_hit_seconds))
        )
    except (TypeError, ValueError):
        cfg.detection.drop_hit_seconds = 0.15
    try:
        cfg.detection.drop_hit_fade_seconds = max(
            0.0, min(5.0, float(cfg.detection.drop_hit_fade_seconds))
        )
    except (TypeError, ValueError):
        cfg.detection.drop_hit_fade_seconds = 0.0
    return cfg


NOISE_FLOOR_RANGE = (-90.0, -20.0)


def clamp_noise_floor(value: object) -> float:
    lo, hi = NOISE_FLOOR_RANGE
    try:
        db = float(value)
    except (TypeError, ValueError):
        return -55.0
    return max(lo, min(hi, db))


def coerce_style(value: str | None) -> str:
    name = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "4/4": "club",
        "four_on_floor": "club",
        "trance": "long_build",
        "atmospheric": "breakdown",
        "breakdown_first": "breakdown",
    }
    name = aliases.get(name, name)
    return name if name in STYLE_NAMES else "auto"


def coerce_app_mode(value: object) -> str:
    name = str(value or "sound").strip().lower()
    return name if name in APP_MODES else "sound"


def _coerce_app_mode(value: object) -> str:
    return coerce_app_mode(value)


def _migrate_control_channel_addresses(cfg: Config) -> None:
    from .control_channels import CONTROL_CHANNEL_NAMES, migrate_channel_map_values

    ch = cfg.channels
    values = {name: int(getattr(ch, name)) for name in CONTROL_CHANNEL_NAMES}
    migrated = migrate_channel_map_values(values)
    for name, addr in migrated.items():
        setattr(ch, name, addr)


def _migrate_legacy_intensity(cfg: Config, section: dict) -> None:
    """Copy the old shared [detection] intensity onto both sliders if needed."""
    if "intensity" not in section:
        return
    try:
        legacy = float(section["intensity"])
    except (TypeError, ValueError):
        return
    if "build_intensity" not in section:
        cfg.detection.build_intensity = legacy
    if "drop_intensity" not in section:
        cfg.detection.drop_intensity = legacy


def _sync_shared_network(cfg: Config) -> None:
    """One NIC and unicast IP for both segments; inherit from [artnet] if unset."""
    if not cfg.network.interface:
        cfg.network.interface = cfg.artnet.interface
    if not cfg.network.ip or cfg.network.ip == "127.0.0.1":
        if cfg.artnet.ip:
            cfg.network.ip = cfg.artnet.ip
    cfg.artnet.interface = cfg.network.interface
    cfg.artnet.ip = cfg.network.ip


def audio_output_cfg(cfg: Config) -> ArtNetConfig:
    return ArtNetConfig(
        protocol=cfg.artnet.protocol,
        mode=cfg.artnet.mode,
        interface=cfg.network.interface,
        ip=cfg.network.ip,
        port=cfg.artnet.port,
        universe=cfg.artnet.universe,
        fps=cfg.artnet.fps,
        priority=cfg.artnet.priority,
    )


def matter_output_cfg(cfg: Config) -> ArtNetConfig:
    return ArtNetConfig(
        protocol=cfg.matter.protocol,
        mode=cfg.matter.mode,
        interface=cfg.network.interface,
        ip=cfg.network.ip,
        port=cfg.matter.port,
        universe=cfg.matter.universe,
        fps=cfg.matter.fps,
        priority=cfg.matter.priority,
    )
