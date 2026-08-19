"""Configuration loading with defaults, from a TOML file."""

from __future__ import annotations

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
    # mode: Art-Net = unicast|broadcast; sACN = unicast|multicast.
    mode: str = "unicast"
    # Local NIC to send from ("" = system default). Name like "eth0".
    interface: str = ""
    ip: str = "127.0.0.1"
    port: int = 6454
    universe: int = 0
    fps: int = 40
    # sACN packet priority (0–200). Higher wins when multiple sources collide.
    priority: int = 100


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


@dataclass
class AudioConfig:
    samplerate: int = 44100
    blocksize: int = 512
    device: str = ""


@dataclass
class ChannelMap:
    groove: int = 1
    breakdown: int = 2
    buildup: int = 3
    drop: int = 4
    build_progress: int = 5
    beat_pulse: int = 6
    energy: int = 7
    kick: int = 8
    # One-shot pulse at the moment DROP begins (for cue/macro triggers).
    drop_hit: int = 9
    # Continuous 0-255: kick still going, focus on mid/high (melody/pads).
    melody: int = 10
    # 4/4 quarter-note grid pulse (phase-locked; keeps ticking if a kick is missed).
    four_four: int = 11
    # Spectral centroid (dark/bass-heavy → bright/trebly), 0-255.
    centroid: int = 12
    # (mid+high)/bass tilt — higher = more open/bright relative to the kick.
    tilt: int = 13
    # Chase rate vs BPM: 255=full, 128=half, 64=quarter, 32=eighth (energy-picked).
    bpm_rate: int = 14


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
    # 0 = groove-oriented (harder builds/drops), 1 = aggressive (easier).
    intensity: float = 0.55
    # Fast drop: energy/bass must rise by this much within drop_rise_window.
    drop_rise_window_seconds: float = 0.5
    drop_rise_amount: float = 0.40
    # Envelope smoothing for drop sensing (smaller = snappier).
    drop_smooth_seconds: float = 0.15
    # Ignore slams this soon after entering the current section.
    drop_min_in_state: float = 0.25
    # Energy (or bass) that many seconds ago must be below this, so a
    # single groove kick is not treated as a drop.
    drop_quiet_below: float = 1.8
    # If true, a slam can fire even while still in GROOVE.
    drop_from_groove: bool = True


@dataclass
class BuildScoreConfig:
    # Slope cues (classic rising tension).
    weight_energy: float = 0.22
    weight_treble: float = 0.16
    weight_onsets: float = 0.16
    # Brightness opening (centroid / spectral tilt).
    weight_brightness: float = 0.14
    # Level lift vs earlier in the window (slow techno filter builds).
    weight_lift: float = 0.16
    # Highs rising while kick stays flat/falling (filter-open builds).
    weight_divergence: float = 0.10
    # Busy hats / spectral activity above the recent baseline.
    weight_activity: float = 0.06
    full_scale_energy_slope: float = 0.04
    full_scale_treble_slope: float = 0.05
    full_scale_onset_slope: float = 0.8
    full_scale_brightness_slope: float = 0.04
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


@dataclass
class BpmRateConfig:
    """How energy maps onto DMX channel 14 (bpm_rate)."""

    # Inclusive endpoints among: eighth / quarter / half / full.
    # Quiet energy → min, loud energy → max (order is swapped if inverted).
    min: str = "eighth"
    max: str = "full"
    # Extra seconds to hold a higher rate before stepping down.
    decay_seconds: float = 0.0


@dataclass
class Config:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    artnet: ArtNetConfig = field(default_factory=ArtNetConfig)
    matter: MatterConfig = field(default_factory=MatterConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    channels: ChannelMap = field(default_factory=ChannelMap)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    build_score: BuildScoreConfig = field(default_factory=BuildScoreConfig)
    bpm_rate: BpmRateConfig = field(default_factory=BpmRateConfig)


_SECTIONS = {
    "network": NetworkConfig,
    "artnet": ArtNetConfig,
    "matter": MatterConfig,
    "audio": AudioConfig,
    "channels": ChannelMap,
    "detection": DetectionConfig,
    "build_score": BuildScoreConfig,
    "bpm_rate": BpmRateConfig,
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
            if hasattr(target, key):
                setattr(target, key, value)
            else:
                raise ValueError(f"Unknown config key: [{section_name}] {key}")
    _sync_shared_network(cfg)
    return cfg


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
