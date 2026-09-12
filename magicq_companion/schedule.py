"""Install @reboot companion start and optional scheduled host reboot."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from .config import ServerConfig, load_config, parse_reboot_time, reboot_cron_dow

BOOT_MARKER = "# magicq-companion boot"
SCHEDULED_MARKER = "# magicq-companion scheduled reboot"
REBOOT_MARKERS = (
    "# magicq-companion weekday reboot",
    "# magicq-companion daily reboot",
    SCHEDULED_MARKER,
)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def start_script(root: Path | None = None) -> Path:
    return (root or repo_root()) / "packaging" / "start-companion.sh"


def _can_sudo() -> bool:
    try:
        return (
            subprocess.run(
                ["sudo", "-n", "true"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except OSError:
        return False


def _read_crontab(*, as_root: bool) -> str:
    cmd = ["sudo", "-n", "crontab", "-l"] if as_root else ["crontab", "-l"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stdout or ""


def _write_crontab(text: str, *, as_root: bool) -> None:
    body = text if text.endswith("\n") else text + "\n"
    if not body.strip():
        body = "\n"
    cmd = ["sudo", "-n", "crontab", "-"] if as_root else ["crontab", "-"]
    subprocess.run(cmd, input=body, text=True, check=True, capture_output=True)


def _strip_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    return [line for line in text.splitlines() if not any(marker in line for marker in markers)]


def _join_lines(lines: list[str]) -> str:
    cleaned = [line.rstrip() for line in lines]
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return "\n".join(cleaned) + ("\n" if cleaned else "")


def reboot_cron_spec(server: ServerConfig) -> str | None:
    if not server.reboot_enabled:
        return None
    hour, minute = parse_reboot_time(server.reboot_time).split(":")
    dow = reboot_cron_dow(server.reboot_days)
    if not dow:
        return None
    return f"{int(minute)} {int(hour)} * * {dow}"


def apply_host_schedule(server: ServerConfig, *, root: Path | None = None) -> str:
    """Rewrite companion crontab lines from [server] settings. Never reboots now."""
    root = Path(root) if root else repo_root()
    start = start_script(root)
    start.chmod(start.stat().st_mode | 0o111)
    boot_entry = f"@reboot sleep 20 && {start} {BOOT_MARKER}"
    spec = reboot_cron_spec(server)
    notes: list[str] = []

    user_lines = _strip_markers(_read_crontab(as_root=False), REBOOT_MARKERS + (BOOT_MARKER,))
    user_lines.append(boot_entry)
    notes.append(f"boot start → {start}")

    root_ok = _can_sudo()
    if spec:
        if root_ok:
            root_lines = _strip_markers(_read_crontab(as_root=True), REBOOT_MARKERS)
            root_lines.append(f"{spec} /sbin/reboot {SCHEDULED_MARKER}")
            _write_crontab(_join_lines(root_lines), as_root=True)
            notes.append(f"root reboot {spec}")
        else:
            user_lines.append(f"{spec} sudo -n /sbin/reboot {SCHEDULED_MARKER}")
            notes.append(
                f"user reboot {spec} (needs passwordless sudo for /sbin/reboot)"
            )
    elif root_ok:
        root_lines = _strip_markers(_read_crontab(as_root=True), REBOOT_MARKERS)
        _write_crontab(_join_lines(root_lines), as_root=True)

    _write_crontab(_join_lines(user_lines), as_root=False)
    if not spec:
        notes.append("scheduled reboot off")
    return "; ".join(notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply Companion @reboot start and [server] reboot schedule."
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--root", default=None)
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else repo_root()
    cfg_path = Path(args.config) if args.config else root / "config.toml"
    os.chdir(root)
    cfg = load_config(cfg_path)
    print(apply_host_schedule(cfg.server, root=root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
