"""Matter → MagicQ DMX: Python owns the sender; the Node sidecar only reports levels."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
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
    """Node matter.js process: pairing + device state; Python sends Art-Net/sACN."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port = _SIDECAR_PORT
        self.last_error = ""
        self._wanted = False
        self._adopted = False
        self._start_kwargs: dict = {}
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def available(self) -> bool:
        return self.running() or self._adopted or self._health_ok()

    def status_label(self, *, probe: bool = False) -> str:
        if self.running() or self._adopted:
            return "running"
        if probe and self._adopt_if_healthy():
            return "running"
        if self._wanted:
            return self.last_error or "starting…"
        return self.last_error or "not started"

    def keep_alive(self, **kwargs) -> None:
        """Start the sidecar and restart it if it exits while Companion is up."""
        self._start_kwargs = dict(kwargs)
        self._wanted = True
        self.start(**kwargs)
        if self._watch_thread is None or not self._watch_thread.is_alive():
            self._watch_stop.clear()
            self._watch_thread = threading.Thread(
                target=self._watch, daemon=True, name="matter-sidecar-watch"
            )
            self._watch_thread.start()
            print("[matter] sidecar watchdog on")

    def _watch(self) -> None:
        fails = 0
        while self._wanted and not self._watch_stop.is_set():
            if self.running() or self._adopt_if_healthy():
                fails = 0
                self.last_error = ""
                self._watch_stop.wait(4)
                continue
            self._adopted = False
            print("[matter] sidecar is down, restarting…")
            try:
                self.start(**self._start_kwargs)
            except Exception as exc:
                print(f"[matter] sidecar restart failed: {exc}")
            if self.running():
                fails = 0
                self._watch_stop.wait(4)
                continue
            fails += 1
            self._watch_stop.wait(min(30, 4 * (2 ** min(fails, 3))))

    def start(
        self,
        *,
        interface: str,
        python_dmx_url: str,
        data_dir: Path,
        port: int = _SIDECAR_PORT,
    ) -> None:
        with self._lock:
            self._start_unlocked(
                interface=interface,
                python_dmx_url=python_dmx_url,
                data_dir=data_dir,
                port=port,
            )

    def _start_unlocked(
        self,
        *,
        interface: str,
        python_dmx_url: str,
        data_dir: Path,
        port: int = _SIDECAR_PORT,
    ) -> None:
        self.port = port
        self.last_error = ""
        if self._adopt_if_healthy():
            print(f"[matter] sidecar already listening on {self.base_url}")
            return
        if self.proc is not None:
            self._stop_process()
        if not self._health_ok():
            self._free_port(port)
        if self._adopt_if_healthy():
            print(f"[matter] sidecar already listening on {self.base_url}")
            return
        root = sidecar_dir()
        if not (root / "package.json").is_file():
            self.last_error = f"Matter sidecar missing at {root}"
            print(f"[matter] {self.last_error}")
            return
        node = _find_node()
        npm = _find_npm()
        if node is None:
            self.last_error = (
                "Node.js 20+ is not installed (needed for Matter devices). "
                "Install it, then restart Companion."
            )
            print(f"[matter] {self.last_error}")
            return
        if not self._ensure_built(root, npm):
            return
        entry = root / "dist" / "index.js"
        if not entry.is_file():
            self.last_error = "Matter sidecar failed to build (npm run build)."
            print(f"[matter] {self.last_error}")
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.setdefault("HOME", str(Path.home()))
        node_dir = str(Path(node).resolve().parent)
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
        env["HOST"] = "127.0.0.1"
        env["PORT"] = str(port)
        env["DATA_DIR"] = str(data_dir)
        env["PYTHON_DMX_URL"] = python_dmx_url
        if interface and interface.strip().lower() not in ("localhost", "lo", "127.0.0.1"):
            env["MATTER_MDNS_NETWORK_INTERFACE"] = interface.strip()
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
        if not self._wait_until_ready(timeout=20):
            if self._adopt_if_healthy():
                if self.proc is not None and self.proc.poll() is not None:
                    self.proc = None
                print(f"[matter] sidecar already listening on {self.base_url}")
                return
            code = self.proc.poll() if self.proc else None
            if self.proc is not None and code is None:
                print(
                    f"[matter] sidecar pid {self.proc.pid} starting "
                    f"(API not ready yet on {self.base_url})"
                )
                return
            self.last_error = (
                self.last_error
                or f"Matter sidecar did not start on port {port}"
                + (f" (exit {code})" if code is not None else "")
            )
            print(f"[matter] {self.last_error}")
            return
        self._adopted = False
        self.last_error = ""
        print(f"[matter] sidecar pid {self.proc.pid} on {self.base_url}")

    def restart(self, **kwargs) -> None:
        wanted = self._wanted
        if kwargs:
            self._start_kwargs.update(kwargs)
        self._stop_process()
        if not self._health_ok():
            self._free_port(int(self._start_kwargs.get("port") or self.port))
        if wanted or kwargs:
            self._wanted = True
            self.start(**self._start_kwargs)

    def stop(self) -> None:
        self._wanted = False
        self._watch_stop.set()
        self._stop_process()

    def _stop_process(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def _free_port(self, port: int) -> None:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._free_matter_ports()
        time.sleep(0.5)

    def _free_matter_ports(self) -> None:
        for spec in ("5540/udp", "5541/udp"):
            subprocess.run(
                ["fuser", "-k", spec],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _health_ok(self) -> bool:
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(f"{self.base_url}/api/health", timeout=0.4) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _adopt_if_healthy(self) -> bool:
        if not self._health_ok():
            return False
        self._adopted = True
        self.last_error = ""
        return True

    def push_output_state(self, enabled: bool) -> None:
        """Mirror the dashboard output toggle onto companion-output switches.

        Fire and forget on a worker thread: the toggle must never wait for
        (or fail because of) the sidecar.
        """
        if not self.running():
            return

        def _send() -> None:
            import urllib.error
            import urllib.request

            body = json.dumps({"enabled": bool(enabled)}).encode()
            request = urllib.request.Request(
                f"{self.base_url}/api/output",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=1.5):
                    pass
            except (urllib.error.URLError, TimeoutError, OSError):
                pass

        threading.Thread(target=_send, daemon=True).start()

    def _wait_until_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._health_ok():
                return True
            if self.proc is not None and self.proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

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

    def _ensure_built(self, root: Path, npm: str | None) -> bool:
        dist = root / "dist" / "index.js"
        tsc = root / "node_modules" / ".bin" / "tsc"
        src_mtime = max(
            (p.stat().st_mtime for p in (root / "src").rglob("*.ts")),
            default=0,
        )
        if dist.is_file() and dist.stat().st_mtime >= src_mtime:
            return True
        env = os.environ.copy()
        env.setdefault("HOME", str(Path.home()))
        env.pop("NODE_ENV", None)
        node_bin = _find_node()
        if node_bin:
            env["PATH"] = str(Path(node_bin).parent) + os.pathsep + env.get("PATH", "")
        need_install = not (root / "node_modules").is_dir() or not tsc.is_file()
        if need_install:
            if npm is None:
                if dist.is_file():
                    print("[matter] npm/typescript missing; using existing sidecar build")
                    return True
                self.last_error = "npm not found; cannot build the Matter sidecar."
                print(f"[matter] {self.last_error}")
                return False
            print("[matter] npm install…")
            inst = subprocess.run(  # noqa: S603
                [npm, "install"],
                cwd=str(root),
                capture_output=True,
                text=True,
                env=env,
            )
            if inst.returncode != 0:
                if dist.is_file():
                    print(
                        "[matter] npm install failed; using existing sidecar build\n"
                        f"{inst.stderr or inst.stdout}"
                    )
                    return True
                self.last_error = "npm install failed for matter_sidecar."
                print(f"[matter] {self.last_error}\n{inst.stderr or inst.stdout}")
                return False
        print("[matter] building sidecar…")
        tsc = root / "node_modules" / ".bin" / "tsc"
        build_cmd = [str(tsc)] if tsc.is_file() else ([npm, "run", "build"] if npm else None)
        if build_cmd is None:
            if dist.is_file():
                print("[matter] tsc missing; using existing sidecar build")
                return True
            self.last_error = "typescript is not installed; cannot build the Matter sidecar."
            print(f"[matter] {self.last_error}")
            return False
        built = subprocess.run(  # noqa: S603
            build_cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            env=env,
        )
        if built.returncode != 0:
            if dist.is_file():
                print(
                    "[matter] sidecar rebuild failed; using existing build\n"
                    f"{built.stderr or built.stdout}"
                )
                return True
            self.last_error = "npm run build failed for matter_sidecar."
            print(f"[matter] {self.last_error}\n{built.stderr or built.stdout}")
            return False
        return True


def _candidate_bins(name: str) -> list[Path]:
    found: list[Path] = []
    which = shutil.which(name)
    if which:
        found.append(Path(which))
    homes = []
    for env_key in ("HOME", "SUDO_USER"):
        raw = os.environ.get(env_key)
        if env_key == "SUDO_USER" and raw:
            homes.append(Path("/home") / raw)
        elif raw:
            homes.append(Path(raw))
    homes.append(Path.home())
    homes.append(Path("/home/oskar"))
    seen: set[Path] = set()
    unique_homes: list[Path] = []
    for home in homes:
        home = home.expanduser()
        if home not in seen:
            seen.add(home)
            unique_homes.append(home)
    extra_dirs = [
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/opt/nodejs/bin"),
        sidecar_dir().parent / ".node" / "bin",
    ]
    for home in unique_homes:
        extra_dirs.extend(
            sorted(home.glob(f".nvm/versions/node/*/bin"), reverse=True)
        )
        extra_dirs.append(home / ".fnm" / "current" / "bin")
        extra_dirs.append(home / ".local" / "share" / "fnm" / "current" / "bin")
        extra_dirs.append(home / ".volta" / "bin")
    for directory in extra_dirs:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            found.append(candidate)
    # Deduplicate while keeping order.
    out: list[Path] = []
    seen_res: set[Path] = set()
    for path in found:
        resolved = path.resolve()
        if resolved not in seen_res:
            seen_res.add(resolved)
            out.append(resolved)
    return out


def _find_node() -> str | None:
    bins = _candidate_bins("node")
    return str(bins[0]) if bins else None


def _find_npm() -> str | None:
    bins = _candidate_bins("npm")
    # Prefer the nvm/fnm shim in .../bin/npm over npm-cli.js.
    for path in bins:
        if path.name == "npm":
            return str(path)
    return str(bins[0]) if bins else None


def describe_matter(cfg: Config) -> str:
    if not cfg.matter.enabled:
        return "Matter output off"
    return "Matter " + describe_output(matter_output_cfg(cfg))
