"""Entry point: audio in -> section analysis -> Art-Net out.

Usage examples:
    python -m magicq_audio_bridge --list-devices
    python -m magicq_audio_bridge --config config.toml
    python -m magicq_audio_bridge --ui
    python -m magicq_audio_bridge --simulate --dry-run --fast
"""

from __future__ import annotations

import argparse
import sys

from .config import audio_output_cfg, load_config
from .monitor import Monitor
from .output import create_sender, describe_output
from .paths import default_config_path, is_frozen
from .pipeline import Bridge


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Double-click / Start Menu launch on Windows should open the dashboard.
    if argv is None:
        argv = sys.argv[1:]
        if is_frozen() and not argv:
            argv = ["--ui"]

    parser = argparse.ArgumentParser(
        prog="magicq_audio_bridge",
        description="Real-time audio section analysis to Art-Net for MagicQ.",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to config.toml (default: ./config.toml if present)",
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="List audio input devices and exit",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Use a synthetic techno track instead of live audio input",
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="With --simulate: run faster than real time (for testing)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Analyze and display, but do not send Art-Net",
    )
    parser.add_argument(
        "--target", default=None,
        help="Override Art-Net target IP from the config",
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Stop after this many seconds of audio",
    )
    parser.add_argument(
        "--ui", action="store_true",
        help="Serve the web dashboard instead of the console monitor",
    )
    parser.add_argument(
        "--ui-port", type=int, default=8765,
        help="Port for the web dashboard (default: 8765)",
    )
    return parser.parse_args(argv)


def build_source(args: argparse.Namespace, cfg: Config):
    """Return (block iterator, human-readable source description)."""
    sr = cfg.audio.samplerate
    hop = cfg.audio.blocksize

    if args.simulate:
        from .simulate import generate_blocks

        blocks = (block for _, block in generate_blocks(sr, hop))
        return blocks, "simulated techno track"

    # USB interfaces often show up in PipeWire but not PortAudio. Prefer
    # pw-record when the configured name matches a PipeWire source; otherwise
    # use sounddevice, and fall back to pw-record if PortAudio can't see it.
    from .audio import (
        capture_blocks,
        capture_blocks_subprocess,
        find_device,
        pipewire_source_available,
    )

    label = cfg.audio.device or "default device"

    if pipewire_source_available(cfg.audio.device):
        blocks = capture_blocks_subprocess(sr, hop, cfg.audio.device)
        return blocks, f"live input via pw-record ({label})"

    try:
        device = find_device(cfg.audio.device)
        blocks = capture_blocks(sr, hop, device)
        return blocks, f"live input ({label})"
    except (OSError, ValueError):
        blocks = capture_blocks_subprocess(sr, hop, cfg.audio.device)
        return blocks, f"live input via pw-record/arecord ({label})"


def main() -> int:
    args = parse_args()

    if args.list_devices:
        from .audio import list_devices

        print(list_devices())
        return 0

    config_path = args.config
    if config_path is None:
        candidate = default_config_path()
        config_path = str(candidate) if candidate.exists() else None
    cfg = load_config(config_path)
    if args.target:
        cfg.network.ip = args.target
        cfg.artnet.ip = args.target

    if args.ui:
        from .ui import run_ui

        run_ui(
            cfg,
            source_factory=lambda c: build_source(args, c),
            dry_run=args.dry_run,
            pace=args.simulate and not args.fast,
            duration=args.duration,
            config_path=config_path,
            simulate=args.simulate,
            port=args.ui_port,
        )
        return 0

    blocks, source_desc = build_source(args, cfg)
    print(f"Source: {source_desc}")

    sender = None
    out_desc = "dry run, not sending"
    if not args.dry_run:
        sender = create_sender(audio_output_cfg(cfg))
        out_desc = f"{describe_output(audio_output_cfg(cfg))} @ {cfg.artnet.fps} fps"
    print(f"Output: {out_desc}")

    bridge = Bridge(
        cfg,
        blocks,
        sender=sender,
        pace=args.simulate and not args.fast,
        duration=args.duration,
    )

    monitor = Monitor()
    try:
        bridge.run(
            on_frame=lambda b: monitor.update(b.t, b.features, b.tracker)
        )
    except KeyboardInterrupt:
        pass
    finally:
        monitor.close()
        if sender is not None:
            print("Sent blackout frame and closed Art-Net socket.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
