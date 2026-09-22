"""HTTP handlers for standalone FX mode."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from aiohttp import web

from .baas_files import (
    BAAS_SET_NAME,
    BAAS_SHOW_NAME,
    baas_status,
    resolve_baas_download,
    store_baas_upload,
)
from .control_channels import DEFAULT_RENUMBER
from .fx_loop import snap_trim_to_loop
from .fx_patch import ManualPatchSource
from .fx_store import FxLibrary, tags_as_kinds
from .paths import bundle_dir


def bundle_root_path() -> Path:
    return bundle_dir()


def attach_fx_routes(app: web.Application, manager) -> None:
    async def fx_state_handler(_request: web.Request) -> web.Response:
        eng = manager.fx_engine
        if eng is None:
            return web.json_response({"active": False})
        return web.json_response({"active": True, **eng.public_state()})

    async def fx_record_start(request: web.Request) -> web.Response:
        eng = manager.ensure_fx_engine()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        name = str(body.get("name") or "Recording")
        rid = eng.start_recording(name)
        return web.json_response({"ok": True, "recording_id": rid})

    async def fx_record_stop(_request: web.Request) -> web.Response:
        eng = manager.ensure_fx_engine()
        rec = eng.stop_recording()
        if rec is None:
            return web.json_response({"error": "not recording"}, status=400)
        return web.json_response({"ok": True, "recording": rec.to_dict()})

    async def fx_library_handler(_request: web.Request) -> web.Response:
        eng = manager.ensure_fx_engine()
        lib = eng.library
        return web.json_response(
            {
                "recordings": {k: v.to_dict() for k, v in lib.recordings.items()},
                "cues": {k: v.to_dict() for k, v in lib.cues.items()},
                "assignments": [a.to_dict() for a in lib.assignments],
                "manual_tags": lib.manual_tags,
            }
        )

    async def fx_library_save(request: web.Request) -> web.Response:
        eng = manager.ensure_fx_engine()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        lib = FxLibrary.from_dict(body)
        eng.library = lib
        eng.patch = ManualPatchSource(tags_as_kinds(lib.manual_tags))
        eng.save_all()
        return web.json_response({"ok": True})

    async def fx_snap_handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        rec_id = str(body.get("recording_id") or "")
        cue_id = str(body.get("cue_id") or "")
        eng = manager.ensure_fx_engine()
        rec = eng.library.recordings.get(rec_id)
        cue = eng.library.cues.get(cue_id)
        if rec is None or cue is None:
            return web.json_response({"error": "not found"}, status=404)
        orig_start, orig_end = cue.trim_start, cue.trim_end or len(rec.frames)
        start, end, snapped = snap_trim_to_loop(
            rec.frames,
            orig_start,
            orig_end,
        )
        message = "Loop handles snapped." if snapped else "No matching loop seam; handles unchanged."
        if snapped:
            cue.trim_start = start
            cue.trim_end = end
            eng.library.cues[cue_id] = cue
            eng.save_all()
        return web.json_response(
            {
                "ok": True,
                "snapped": snapped,
                "message": message,
                "trim_start": cue.trim_start,
                "trim_end": cue.trim_end,
            }
        )

    async def fx_palette_handler(request: web.Request) -> web.Response:
        eng = manager.ensure_fx_engine()
        if request.method == "GET":
            return web.json_response(eng.palette_state.to_dict())
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        from .fx_palette import ColourPalette, PaletteState

        eng.palette_state = PaletteState.from_dict(body)
        eng.save_all()
        return web.json_response({"ok": True})

    async def fx_palette_activate(request: web.Request) -> web.Response:
        eng = manager.ensure_fx_engine()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        pid = str(body.get("id") or "")
        eng.set_palette(pid)
        return web.json_response({"ok": True, "active_id": pid})

    async def app_mode_handler(request: web.Request) -> web.Response:
        if request.method == "GET":
            return web.json_response(
                {
                    "mode": manager.cfg.app.mode,
                    "channel_map_doc": DEFAULT_RENUMBER.document_lines(),
                }
            )
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        mode = str(body.get("mode") or "sound").strip().lower()
        if mode not in ("sound", "standalone_fx"):
            return web.json_response({"error": "invalid mode"}, status=400)
        manager.set_app_mode(mode)
        return web.json_response({"ok": True, "mode": mode})

    async def baas_status_handler(_request: web.Request) -> web.Response:
        st = baas_status(bundle_root_path(), manager.config_path)
        return web.json_response(st)

    async def baas_download_handler(request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        path = resolve_baas_download(
            name, bundle_root_path(), manager.config_path
        )
        if path is None:
            return web.json_response(
                {"error": f"{name} not found in repo or uploads"}, status=404
            )
        return web.FileResponse(path)

    async def baas_upload_handler(request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        if name not in (BAAS_SHOW_NAME, BAAS_SET_NAME):
            return web.json_response({"error": "invalid name"}, status=400)
        reader = await request.multipart()
        field = await reader.next()
        if field is None:
            return web.json_response({"error": "file required"}, status=400)
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                tmp.write(chunk)
            tmp_path = Path(tmp.name)
        try:
            dest = store_baas_upload(name, tmp_path, manager.config_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return web.json_response({"ok": True, "path": str(dest)})

    app.router.add_get("/api/app/mode", app_mode_handler)
    app.router.add_post("/api/app/mode", app_mode_handler)
    app.router.add_get("/api/baas", baas_status_handler)
    app.router.add_get("/api/baas/{name}", baas_download_handler)
    app.router.add_post("/api/baas/{name}", baas_upload_handler)
    app.router.add_get("/api/fx/state", fx_state_handler)
    app.router.add_post("/api/fx/record/start", fx_record_start)
    app.router.add_post("/api/fx/record/stop", fx_record_stop)
    app.router.add_get("/api/fx/library", fx_library_handler)
    app.router.add_post("/api/fx/library", fx_library_save)
    app.router.add_post("/api/fx/snap", fx_snap_handler)
    app.router.add_get("/api/fx/palette", fx_palette_handler)
    app.router.add_post("/api/fx/palette", fx_palette_handler)
    app.router.add_post("/api/fx/palette/activate", fx_palette_activate)
