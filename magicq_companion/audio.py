"""Live audio capture via sounddevice, with a subprocess fallback.

If the PortAudio system library is not installed, capture falls back to
piping raw audio from `pw-record` (PipeWire) or `arecord` (ALSA).
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
from collections.abc import Iterator

import numpy as np


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


def list_input_device_names() -> list[str]:
    """Input device names for the UI device selector.

    Merges PortAudio and PipeWire sources — a USB interface may show up in
    only one of the two, and the capture path will pick whichever works.
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(label: str) -> None:
        if not label or label in _SKIP_DEVICE_NAMES or label in seen:
            return
        seen.add(label)
        names.append(label)

    try:
        import sounddevice as sd

        for d in sd.query_devices():
            if d["max_input_channels"] > 0:
                _add(d["name"])
    except OSError:
        pass

    for _, name, description in _pipewire_sources():
        _add(description or name)

    return names


def _pipewire_sources() -> list[tuple[int, str, str]]:
    """Return (id, node name, description) of PipeWire audio capture nodes."""
    if not shutil.which("pw-dump"):
        return []
    out = subprocess.run(["pw-dump"], capture_output=True, text=True).stdout
    sources = []
    for obj in json.loads(out):
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") == "Audio/Source":
            sources.append((
                obj["id"],
                props.get("node.name", ""),
                props.get("node.description", ""),
            ))
    return sources


def find_device(name_substring: str) -> int | None:
    """Return the input device index matching a name substring, or None."""
    import sounddevice as sd

    if not name_substring:
        return None
    for index, device in enumerate(sd.query_devices()):
        if (
            name_substring.lower() in device["name"].lower()
            and device["max_input_channels"] > 0
        ):
            return index
    raise ValueError(f"No input device matching '{name_substring}' found")


def pipewire_source_available(name_substring: str) -> bool:
    """True if a PipeWire Audio/Source matches the name substring."""
    if not name_substring:
        return False
    needle = name_substring.lower()
    return any(
        needle in name.lower() or needle in description.lower()
        for _, name, description in _pipewire_sources()
    )


def capture_blocks(
    samplerate: int, blocksize: int, device: int | None
) -> Iterator[np.ndarray]:
    """Yield mono float64 blocks from the audio input, blocking as needed."""
    import sounddevice as sd

    blocks: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)

    def callback(indata, frames, time_info, status):
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata.copy()
        try:
            blocks.put_nowait(mono.astype(np.float64))
        except queue.Full:
            pass  # Drop a block rather than stall the audio thread.

    with sd.InputStream(
        samplerate=samplerate,
        blocksize=blocksize,
        channels=1,
        dtype="float32",
        device=device,
        callback=callback,
    ):
        while True:
            yield blocks.get()


def capture_blocks_subprocess(
    samplerate: int, blocksize: int, device: str = ""
) -> Iterator[np.ndarray]:
    """Yield mono float64 blocks by piping from pw-record or arecord.

    `device` is a case-insensitive substring matched against PipeWire
    source names/descriptions (e.g. "Scarlett"), or passed through as an
    ALSA device string when only arecord is available.
    """
    if shutil.which("pw-record"):
        cmd = [
            "pw-record", "--rate", str(samplerate),
            "--channels", "1", "--format", "f32",
        ]
        if device:
            cmd += ["--target", str(_match_pipewire_source(device))]
        cmd += ["-"]
    elif shutil.which("arecord"):
        cmd = [
            "arecord", "-q", "-f", "FLOAT_LE",
            "-r", str(samplerate), "-c", "1", "-t", "raw",
        ]
        if device:
            cmd += ["-D", device]
    else:
        raise RuntimeError(
            "No audio capture available: install the PortAudio library "
            "(e.g. libportaudio2) or pw-record/arecord."
        )
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    bytes_per_block = blocksize * 4
    yield from _blocks_from_pipe(proc, bytes_per_block)


def _match_pipewire_source(name_substring: str) -> int:
    """Find a PipeWire source id by name/description substring."""
    sources = _pipewire_sources()
    for node_id, name, description in sources:
        if (
            name_substring.lower() in name.lower()
            or name_substring.lower() in description.lower()
        ):
            return node_id
    available = ", ".join(d or n for _, n, d in sources) or "none"
    raise ValueError(
        f"No audio source matching '{name_substring}' found. "
        f"Available: {available}"
    )


def _blocks_from_pipe(
    proc: subprocess.Popen, bytes_per_block: int
) -> Iterator[np.ndarray]:
    try:
        while True:
            data = proc.stdout.read(bytes_per_block)
            if data is None or len(data) < bytes_per_block:
                break
            yield np.frombuffer(data, dtype=np.float32).astype(np.float64)
    finally:
        proc.terminate()
