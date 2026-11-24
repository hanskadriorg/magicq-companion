"""Entry point for MagicQ Venue Companion.

Usage examples:
    python -m magicq_companion --list-devices
    python -m magicq_companion --config config.toml
    python -m magicq_companion --ui
    python -m magicq_companion --simulate --dry-run --fast
"""

from __future__ import annotations

import argparse
import sys

from .config import Config, audio_output_cfg, load_config
from .monitor import Monitor
from .output import create_sender, describe_output
from .paths import default_config_path, default_genre_profiles_path, is_frozen
from .pipeline import Bridge


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    if argv is None:
        argv = sys.argv[1:]
        if is_frozen() and not argv:
            argv = ["--ui"]

    parser = argparse.ArgumentParser(
        prog="magicq_companion",
        description="MagicQ Venue Companion: adaptive genre & audio autopilot to Art-Net/sACN.",
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
        help="Use a synthetic multi-genre venue showcase instead of live audio input",
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="With --simulate: run faster than real time (for testing)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Analyze and display, but do not send Art-Net/sACN",
    )
    parser.add_argument(
        "--target", default=None,
        help="Override Art-Net/sACN target IP from the config",
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
        from .simulate import generate_venue_blocks

        loop_forever = not args.fast or args.duration is not None
        blocks = (block for _, block in generate_venue_blocks(sr, hop, loop=loop_forever))
        return blocks, "simulated multi-genre venue set"

    from .audio import open_capture

    return open_capture(
        sr, hop, cfg.audio.device, cfg.audio.input_channel or "left"
    )


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

    sender = None
    out_desc = "dry run, not sending"
    if not args.dry_run:
        sender = create_sender(audio_output_cfg(cfg))
        out_desc = f"{describe_output(audio_output_cfg(cfg))} @ {cfg.artnet.fps} fps"
    print(f"Output: {out_desc}")

    bridge = Bridge(
        cfg,
        sender=sender,
        pace=args.simulate and not args.fast,
        duration=args.duration,
        async_genre=not (args.simulate and args.fast),
        profiles_path=default_genre_profiles_path(config_path),
    )
    bridge.enabled = bool(cfg.server.startup_output)

    monitor = Monitor()
    try:
        bridge.run_supervised(
            lambda c: build_source(args, c),
            reconnect=not args.simulate,
            on_frame=lambda b: monitor.update(b.t, b.features, b.tracker),
            on_status=lambda s: print(f"Source: {s}"),
        )
    except KeyboardInterrupt:
        pass
    finally:
        monitor.close()
        if sender is not None:
            print("Sent blackout frame and closed DMX socket.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
