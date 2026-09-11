"""Live audio capture via sounddevice, with a subprocess fallback.

If the PortAudio system library is not installed, capture falls back to
piping raw audio from `pw-record` (PipeWire) or `arecord` (ALSA).
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time

import numpy as np


# Blocks allowed to queue up before stale audio is discarded. 16 hops of
# 512 samples is roughly 190 ms — the ceiling on capture-side latency.
_MAX_BACKLOG_BLOCKS = 16


def list_devices() -> str:
    try:
        import sounddevice as sd

        return str(sd.query_devices())
    except OSError:
        sources = _pipewire_sources()
        if not sources:
            return "No audio sources found (PortAudio and PipeWire unavailable)."
        lines = ["Audio sources (via PipeWire; PortAudio not installed):"]
        lines += [f"  {node_id:>4}  {description or name}" for node_id, name, description in sources]
        return "\n".join(lines)


# ALSA plugin aliases that aren't useful as capture targets.
_SKIP_DEVICE_NAMES = frozenset({
    "lavrate", "samplerate", "speexrate", "speex", "upmix", "vdownmix",
})
# PortAudio / UI labels that mean "whatever the OS default is" — never
# treat these as a substring match against a Scarlett / HDMI / monitor.
_GENERIC_DEVICE_LABELS = frozenset({
    "", "default", "pipewire", "pulse", "pulseaudio", "system default",
})


def list_input_device_names() -> list[dict[str, str]]:
    """Input devices for the UI selector: ``{"value", "label"}``.

    PipeWire node names are the stored value (unique per jack, e.g. Scarlett
    Mic1 vs Mic2). PortAudio names are included when they aren't the same
    source already listed via PipeWire.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    pw_needles: list[str] = []

    def _add(value: str, label: str) -> None:
        if not value or value in _SKIP_DEVICE_NAMES or _is_generic_device_label(value):
            return
        if value in seen:
            return
        seen.add(value)
        items.append({"value": value, "label": label or value})

    for _, name, description in _pipewire_sources():
        if not name:
            continue
        _add(name, _pw_source_label(name, description))
        pw_needles.append(name.lower())
        if description:
            pw_needles.append(description.lower())

    try:
        import sounddevice as sd

        for d in sd.query_devices():
            if int(d.get("max_input_channels") or 0) <= 0:
                continue
            pa_name = str(d.get("name") or "")
            low = pa_name.lower()
            if any(low in n or n in low for n in pw_needles if n):
                continue
            _add(pa_name, pa_name)
    except Exception:
        pass

    return items


def _pw_source_label(name: str, description: str) -> str:
    """Human label for a PipeWire source, with Mic1/Mic2 disambiguation."""
    label = description or name
    short = ""
    if "__" in name:
        parts = [p for p in name.split("__") if p and not p.endswith("source")]
        if parts:
            short = parts[-1]
    if short and short.lower() not in label.lower():
        return f"{label} ({short})"
    return label


def _pipewire_sources() -> list[tuple[int, str, str]]:
    """Return (id, node name, description) of PipeWire audio capture nodes."""
    if not shutil.which("pw-dump"):
        return []
    proc = subprocess.run(["pw-dump"], capture_output=True, text=True)
    out = proc.stdout or ""
    if not out.strip():
        return []
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return []
    sources = []
    for obj in payload:
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") in ("Audio/Source", "Audio/Source/Internal"):
            name = props.get("node.name", "") or ""
            if name.endswith(".monitor"):
                continue
            sources.append((
                obj["id"],
                name,
                props.get("node.description", "") or "",
            ))
    return sources


def _is_generic_device_label(label: str | None) -> bool:
    return str(label or "").strip().lower() in _GENERIC_DEVICE_LABELS


def find_device(name_substring: str) -> int | None:
    """Return the input device index matching a name, or None for default."""
    import sounddevice as sd

    if _is_generic_device_label(name_substring):
        return None
    needle = name_substring.lower().strip()
    exact: list[int] = []
    partial: list[int] = []
    for index, device in enumerate(sd.query_devices()):
        if int(device.get("max_input_channels") or 0) <= 0:
            continue
        name = str(device.get("name") or "").lower()
        if name == needle:
            exact.append(index)
        elif needle in name:
            partial.append(index)
    if exact:
        return exact[0]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        # Prefer the shortest name (usually the more specific device).
        return min(partial, key=lambda i: len(str(sd.query_devices(i)["name"])))
    raise ValueError(f"No input device matching '{name_substring}' found")


def pipewire_source_available(name_substring: str) -> bool:
    """True if a PipeWire Audio/Source matches this specific device label."""
    if _is_generic_device_label(name_substring):
        return False
    try:
        _match_pipewire_source(name_substring)
        return True
    except ValueError:
        return False


def downmix_block(samples: np.ndarray, channel: str) -> np.ndarray:
    """Reduce interleaved samples to one analysis channel."""
    if samples.ndim == 1:
        return samples.astype(np.float64, copy=False)
    if samples.shape[1] <= 1:
        return samples.reshape(-1).astype(np.float64, copy=False)
    if channel == "left":
        return samples[:, 0].astype(np.float64, copy=False)
    if channel == "right":
        return samples[:, 1].astype(np.float64, copy=False)
    return samples.mean(axis=1).astype(np.float64)


_SENTINEL = object()


class AudioCapture:
    """Closeable live-audio iterator. ``close()`` unblocks the analysis thread."""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._stream = None
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    def __iter__(self) -> AudioCapture:
        return self

    def __next__(self) -> np.ndarray:
        while not self._closed.is_set():
            try:
                item = self._q.get(timeout=0.15)
            except queue.Empty:
                continue
            if item is _SENTINEL:
                break
            block = item
            while self._q.qsize() > _MAX_BACKLOG_BLOCKS:
                try:
                    extra = self._q.get_nowait()
                except queue.Empty:
                    break
                if extra is _SENTINEL:
                    raise StopIteration
                block = extra
            return block
        raise StopIteration

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._q.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1)

    def _put(self, block: np.ndarray) -> None:
        if self._closed.is_set():
            return
        try:
            self._q.put_nowait(block)
        except queue.Full:
            pass

    def start_portaudio(
        self,
        samplerate: int,
        blocksize: int,
        device: int | None,
        input_channel: str,
    ) -> None:
        import sounddevice as sd

        nch = 2
        try:
            info = sd.query_devices(device, "input")
            nch = max(1, min(2, int(info.get("max_input_channels") or 1)))
        except Exception:
            nch = 2

        def callback(indata, frames, time_info, status):
            self._put(downmix_block(indata, input_channel))

        stream = sd.InputStream(
            samplerate=samplerate,
            blocksize=blocksize,
            channels=nch,
            dtype="float32",
            device=device,
            callback=callback,
        )
        stream.start()
        self._stream = stream

    def start_subprocess(
        self,
        samplerate: int,
        blocksize: int,
        device: str,
        input_channel: str,
    ) -> None:
        want = 2
        proc, nch = _open_record_pipe(samplerate, device, want)
        self._proc = proc
        bytes_per_block = blocksize * 4 * nch

        def reader() -> None:
            stdout = proc.stdout
            try:
                while not self._closed.is_set() and stdout is not None:
                    data = stdout.read(bytes_per_block)
                    if data is None or len(data) < bytes_per_block:
                        break
                    samples = np.frombuffer(data, dtype=np.float32)
                    if nch > 1:
                        samples = samples.reshape(-1, nch)
                    self._put(downmix_block(samples, input_channel))
            finally:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    self._q.put_nowait(_SENTINEL)
                except queue.Full:
                    pass

        self._reader = threading.Thread(target=reader, daemon=True, name="pw-record")
        self._reader.start()


def capture_blocks(
    samplerate: int,
    blocksize: int,
    device: int | None,
    input_channel: str = "left",
) -> AudioCapture:
    """Yield mono float64 blocks from the audio input, blocking as needed."""
    cap = AudioCapture()
    cap.start_portaudio(samplerate, blocksize, device, input_channel)
    return cap


def capture_blocks_subprocess(
    samplerate: int,
    blocksize: int,
    device: str = "",
    input_channel: str = "left",
) -> AudioCapture:
    """Yield mono float64 blocks by piping from pw-record or arecord.

    `device` is matched against PipeWire source names/descriptions
    (exact first, then a unique substring). Generic labels like
    "pipewire" / "default" use the OS default source (no --target).
    """
    cap = AudioCapture()
    cap.start_subprocess(samplerate, blocksize, device, input_channel)
    return cap


def open_capture(
    samplerate: int,
    blocksize: int,
    device_label: str,
    input_channel: str,
) -> tuple[AudioCapture, str]:
    """Open the best capture path for this device label and channel mix."""
    mix = input_channel or "left"
    label = device_label or "default device"
    if _is_generic_device_label(device_label) and shutil.which("pw-record"):
        cap = capture_blocks_subprocess(samplerate, blocksize, "", mix)
        return cap, f"live input via pw-record ({label}, {mix})"
    if pipewire_source_available(device_label):
        cap = capture_blocks_subprocess(samplerate, blocksize, device_label, mix)
        return cap, f"live input via pw-record ({label}, {mix})"
    try:
        device = find_device(device_label)
        cap = capture_blocks(samplerate, blocksize, device, mix)
        return cap, f"live input ({label}, {mix})"
    except (OSError, ValueError, ImportError):
        cap = capture_blocks_subprocess(samplerate, blocksize, device_label, mix)
        return cap, f"live input via pw-record/arecord ({label}, {mix})"


def _open_record_pipe(
    samplerate: int, device: str, want_channels: int
) -> tuple[subprocess.Popen, int]:
    """Start pw-record/arecord, falling back to mono if stereo is refused."""
    last_err: Exception | None = None
    for nch in (want_channels, 1) if want_channels != 1 else (1,):
        if shutil.which("pw-record"):
            cmd = [
                "pw-record", "--rate", str(samplerate),
                "--channels", str(nch), "--format", "f32",
            ]
            if device and not _is_generic_device_label(device):
                try:
                    cmd += ["--target", str(_match_pipewire_source(device))]
                except ValueError:
                    pass
            cmd += ["-"]
        elif shutil.which("arecord"):
            cmd = [
                "arecord", "-q", "-f", "FLOAT_LE",
                "-r", str(samplerate), "-c", str(nch), "-t", "raw",
            ]
            if device and not _is_generic_device_label(device):
                cmd += ["-D", device]
        else:
            raise RuntimeError(
                "No audio capture available: install the PortAudio library "
                "(e.g. libportaudio2) or pw-record/arecord."
            )
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.15)
        if proc.poll() is None:
            return proc, nch
        err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
        last_err = RuntimeError(err or f"capture exited ({nch} ch)")
    raise last_err or RuntimeError("audio capture failed")


def _match_pipewire_source(name_substring: str) -> int:
    """Find a PipeWire source id. Exact name/description wins over substring."""
    if _is_generic_device_label(name_substring):
        raise ValueError("generic device label is not a PipeWire source")
    needle = name_substring.lower().strip()
    sources = _pipewire_sources()
    exact: list[int] = []
    partial: list[tuple[int, str]] = []
    for node_id, name, description in sources:
        hay_name = name.lower()
        hay_desc = description.lower()
        if hay_name == needle or hay_desc == needle:
            exact.append(node_id)
        elif needle in hay_name or needle in hay_desc:
            partial.append((node_id, description or name))
    if exact:
        return exact[0]
    if len(partial) == 1:
        return partial[0][0]
    if len(partial) > 1:
        node_id, _ = min(partial, key=lambda item: len(item[1]))
        return node_id
    available = ", ".join(d or n for _, n, d in sources) or "none"
    raise ValueError(
        f"No audio source matching '{name_substring}' found. "
        f"Available: {available}"
    )


