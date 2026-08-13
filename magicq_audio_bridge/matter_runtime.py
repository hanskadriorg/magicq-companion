"""Matter → MagicQ DMX: Python owns the sender; the Node sidecar only reports levels."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from .config import ArtNetConfig, Config, matter_output_cfg
from .output import DmxSender, create_sender, describe_output

_SIDECAR_PORT = 3081


def sidecar_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "matter_sidecar"


def matter_data_dir(config_path: str | None) -> Path:
    base = Path(config_path).resolve().parent if config_path else Path.cwd()
    return base / "data"


class MatterOutput:
    """512-slot universe refreshed at the Matter segment fps."""

    def __init__(self, cfg: ArtNetConfig, *, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.dmx = bytearray(512)
        self.lock = threading.Lock()
        self.enabled = True
        self.sender: DmxSender | None = None if dry_run else create_sender(cfg)
        self._stop = threading.Event()
        interval = 1.0 / max(1, int(cfg.fps) or 40)
        self._thread = threading.Thread(
            target=self._loop, args=(interval,), daemon=True, name="matter-dmx"
        )
        self._thread.start()

    def apply_updates(self, updates: dict[int, int]) -> None:
        with self.lock:
            for channel, value in updates.items():
                if not isinstance(channel, int):
                    try:
                        channel = int(channel)
                    except (TypeError, ValueError):
                        continue
                if 1 <= channel <= 512:
                    self.dmx[channel - 1] = max(0, min(255, int(value)))
            self._flush_locked()

    def replace_sender(self, cfg: ArtNetConfig) -> None:
        with self.lock:
            old = self.sender
            self.cfg = cfg
            self.sender = None if self.dry_run else create_sender(cfg)
            if old is not None:
                try:
                    old.blackout()
                    old.close()
                except OSError:
                    pass
            self._flush_locked()

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.enabled = bool(enabled)
            if not self.enabled and self.sender is not None:
                self.sender.blackout()
            else:
                self._flush_locked()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        with self.lock:
            if self.sender is not None:
                try:
                    self.sender.blackout()
                    self.sender.close()
                except OSError:
                    pass
                self.sender = None

    def _flush_locked(self) -> None:
        if self.sender is not None and self.enabled:
            self.sender.send(self.dmx)

    def _loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            with self.lock:
                self._flush_locked()


class MatterSidecar:
    """Node matter.js process: pairing + device state, no UDP Art-Net."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port = _SIDECAR_PORT

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(
        self,
        *,
        interface: str,
        python_dmx_url: str,
        data_dir: Path,
        port: int = _SIDECAR_PORT,
    ) -> None:
        self.port = port
        root = sidecar_dir()
        if not (root / "package.json").is_file():
            print("[matter] sidecar package.json missing; Matter devices disabled")
            return
        node = shutil.which("node")
        if node is None:
            print("[matter] Node.js not found; Matter devices disabled")
            return
        self._ensure_built(root)
        entry = root / "dist" / "index.js"
        if not entry.is_file():
            print("[matter] sidecar build missing; Matter devices disabled")
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["HOST"] = "127.0.0.1"
        env["PORT"] = str(port)
        env["DATA_DIR"] = str(data_dir)
        env["PYTHON_DMX_URL"] = python_dmx_url
        if interface:
            env["MATTER_MDNS_NETWORK_INTERFACE"] = interface
        else:
            env.pop("MATTER_MDNS_NETWORK_INTERFACE", None)
        self.proc = subprocess.Popen(  # noqa: S603
            [node, str(entry)],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        threading.Thread(target=self._pump_logs, daemon=True).start()
        print(f"[matter] sidecar pid {self.proc.pid} on {self.base_url}")

    def restart(self, **kwargs) -> None:
        self.stop()
        self.start(**kwargs)

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _pump_logs(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                continue
            if line:
                print(f"[matter] {line}")

    def _ensure_built(self, root: Path) -> None:
        dist = root / "dist" / "index.js"
        src_mtime = max(
            (p.stat().st_mtime for p in (root / "src").rglob("*.ts")),
            default=0,
        )
        if dist.is_file() and dist.stat().st_mtime >= src_mtime:
            return
        npm = shutil.which("npm")
        if npm is None:
            print("[matter] npm not found; cannot build sidecar")
            return
        if not (root / "node_modules").is_dir():
            print("[matter] npm install…")
            subprocess.run([npm, "install"], cwd=str(root), check=False)  # noqa: S603
        print("[matter] building sidecar…")
        subprocess.run([npm, "run", "build"], cwd=str(root), check=False)  # noqa: S603


def describe_matter(cfg: Config) -> str:
    if not cfg.matter.enabled:
        return "Matter output off"
    return "Matter " + describe_output(matter_output_cfg(cfg))
