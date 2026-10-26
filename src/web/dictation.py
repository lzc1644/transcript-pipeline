from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse

from src.dictation.schemas import (CleanupRequest, DraftEditRequest, EditRequest, SessionRequest,
    DocumentEditRequest, DocumentEnableRequest, load_dictation_settings)
from src.dictation.service import CleanupFailed, Conflict, DictationService, UploadLimit, WorkerIPCError
from src.dictation.storage import public_state
from src.dictation.streaming_asr import PREVIEW_THREADS

COOKIE = "dictation_owner"
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".webm", ".mp4"}


@contextmanager
def api_errors():
    try:
        yield
    except KeyError as exc:
        raise HTTPException(404, "会话或语句不存在") from exc
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except UploadLimit as exc:
        raise HTTPException(413, str(exc)) from exc
    except CleanupFailed as exc:
        raise HTTPException(504 if exc.timed_out else 502, str(exc)) from exc
    except WorkerIPCError as exc:
        raise HTTPException(503, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(408, "上传接收超过期限，请重新上传") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "正文保存失败，请保留本地编辑并重试或导出") from exc


def owner(request):
    value = request.cookies.get(COOKIE, "")
    if not value or len(value) > 128:
        raise HTTPException(401, "请先在本浏览器创建或选择语音会话")
    return value


def validate_origin(connection):
    origin = connection.headers.get("origin", "")
    parsed = urlparse(origin)
    allowed = {s.strip() for s in os.environ.get("TRANSCRIPT_DICTATION_ORIGINS", "").split(",") if s.strip()}
    host = connection.headers.get("host", "")
    if origin in allowed:
        return
    if parsed.scheme not in {"http", "https"} or parsed.netloc != host:
        raise HTTPException(403, "语音输入仅允许同源访问")


def install_dictation(app, root: Path):
    service = DictationService(root, recover=False)
    app.state.dictation = service
    router = APIRouter(prefix="/api/dictation", tags=["dictation"])

    @router.get("/config")
    async def config():
        with api_errors():
            settings = load_dictation_settings(root)
            return {"enabled": settings.enabled, "cleanup_model": settings.cleanup_model,
                    "document_cleanup_model": settings.document_cleanup_model,
                    "cleanup_reasoning_effort": settings.cleanup_reasoning_effort,
                    "document_reasoning_effort": settings.document_reasoning_effort,
                    "max_upload_mb": settings.max_upload_mb, "max_upload_minutes": settings.max_upload_minutes,
                    "max_active_sessions": settings.max_active_sessions,
                    "active_sessions": len(service.recognition.members),
                    "preview_threads": PREVIEW_THREADS,
                    "cleanup_concurrency": service.CLEANUP_CONCURRENCY,
                    "cleanup_timeout_seconds": settings.cleanup_timeout_seconds,
                    "document_cleanup_timeout_seconds": settings.document_cleanup_timeout_seconds,
                    "decode_timeout_seconds": settings.decode_timeout_seconds,
                    "upload_receive_timeout_seconds": service.UPLOAD_RECEIVE_TIMEOUT_SECONDS}

    @router.post("/sessions")
    async def create(request: Request, response: Response, body: SessionRequest):
        validate_origin(request)
        identifier = owner(request) if request.cookies.get(COOKIE) else secrets.token_urlsafe(32)
        with api_errors():
            state = await service.create(identifier, body)
        response.set_cookie(COOKIE, identifier, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 365)
        return public_state(state)

    @router.get("/sessions")
    async def history(request: Request, response: Response):
        identifier = request.cookies.get(COOKIE)
        if not identifier:
            # Establish ownership before an upload: a lost response must not
            # make the newly created session invisible to this browser.
            identifier = secrets.token_urlsafe(32)
            response.set_cookie(COOKIE, identifier, httponly=True, samesite="strict",
                                secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 365)
        return service.list(identifier)

    @router.get("/sessions/{sid}")
    async def snapshot(sid: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with api_errors():
            return public_state(service.get(sid, owner(request)))

    @router.get("/sessions/{sid}/draft.txt")
    async def download(sid: str, request: Request):
        with api_errors():
            state = service.get(sid, owner(request))
            return PlainTextResponse(service.storage.render_draft(state), headers={
                "Content-Disposition": 'attachment; filename="dictation.txt"', "Cache-Control": "no-store"})

    @router.get("/sessions/{sid}/utterances/{uid}/audio")
    async def utterance_audio(sid: str, uid: str, request: Request):
        with api_errors():
            state = service.get(sid, owner(request))
            if not uid.isascii() or not uid.isdecimal() or not any(u["id"] == uid for u in state["utterances"]):
                raise KeyError("语句不存在")
            directory = service.storage.path(sid) / "audio"
            path = directory / f"{uid}.wav"
            if directory.is_symlink() or path.is_symlink() or not path.is_file():
                raise KeyError("语句音频不存在")
            return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @router.patch("/sessions/{sid}/utterances/{uid}")
    async def edit(sid: str, uid: str, request: Request, body: EditRequest):
        validate_origin(request)
        with api_errors():
            return service.edit(sid, owner(request), uid, body.revision, body.text, body.discard_document)

    @router.patch("/sessions/{sid}/draft")
    async def edit_draft(sid: str, request: Request, body: DraftEditRequest):
        validate_origin(request)
        with api_errors():
            return service.edit_draft(sid, owner(request), body.revision, body.text)

    @router.post("/sessions/{sid}/document")
    async def enable_document(sid: str, request: Request, body: DocumentEnableRequest):
        validate_origin(request)
        with api_errors():
            return service.enable_document(sid, owner(request), body.revision)

    @router.patch("/sessions/{sid}/document")
    async def edit_document(sid: str, request: Request, body: DocumentEditRequest):
        validate_origin(request)
        with api_errors():
            return service.edit_document(sid, owner(request), body)

    @router.delete("/sessions/{sid}/suggestions/{suggestion_id}")
    async def discard_suggestion(sid: str, suggestion_id: str, request: Request):
        validate_origin(request)
        with api_errors():
            return service.discard_suggestion(sid, owner(request), suggestion_id)

    @router.post("/sessions/{sid}/cleanup")
    async def cleanup(sid: str, request: Request, body: CleanupRequest):
        validate_origin(request)
        with api_errors():
            return await service.cleanup(sid, owner(request), body)

    @router.post("/sessions/{sid}/stop")
    async def stop(sid: str, request: Request):
        validate_origin(request)
        with api_errors():
            state = service.get(sid, owner(request))
            runtime = service.runtimes.get(sid)
            if runtime:
                await runtime.stop()
            return public_state(state)

    @router.post("/sessions/{sid}/pause")
    async def pause_file(sid: str, request: Request):
        validate_origin(request)
        with api_errors():
            state = service.get(sid, owner(request))
            runtime = service.runtimes.get(sid)
            if runtime and runtime.task and not runtime.task.done():
                if state["mode"] == "upload" and not runtime.file_work_remaining():
                    await asyncio.gather(asyncio.shield(runtime.task), return_exceptions=True)
                else:
                    runtime.pause_file()
            return public_state(state)

    @router.post("/sessions/{sid}/resume")
    async def resume_file(sid: str, request: Request):
        validate_origin(request)
        with api_errors():
            state = service.get(sid, owner(request))
            runtime = service.runtimes.get(sid)
            if state["status"] == "waiting":
                return public_state(await service.start_upload(sid, owner(request)))
            if runtime and runtime.task and not runtime.task.done():
                runtime.resume_file()
            return public_state(state)

    @router.post("/sessions/{sid}/cancel")
    async def cancel(sid: str, request: Request):
        validate_origin(request)
        with api_errors():
            return await service.cancel(sid, owner(request))

    @router.delete("/sessions/{sid}")
    async def delete(sid: str, request: Request):
        validate_origin(request)
        with api_errors():
            await service.delete(sid, owner(request))
            return {"deleted": True}

    @router.post("/uploads")
    async def upload(request: Request, response: Response, filename: str, terms: str = "[]", cleanup: bool = True,
                     cleanup_model: str | None = None, document_cleanup_model: str | None = None,
                     cleanup_reasoning_effort: str | None = None, document_reasoning_effort: str | None = None,
                     editor_version: int | None = None):
        validate_origin(request)
        suffix = Path(filename).suffix.lower()
        if suffix not in AUDIO_EXTENSIONS:
            raise HTTPException(400, "不支持该录音格式")
        identifier = owner(request) if request.cookies.get(COOKIE) else secrets.token_urlsafe(32)
        with api_errors():
            length = int(request.headers.get("content-length", "0"))
            body = SessionRequest(editor_version=editor_version, terms=json.loads(terms), cleanup=cleanup,
                                  cleanup_model=cleanup_model, document_cleanup_model=document_cleanup_model,
                                  cleanup_reasoning_effort=cleanup_reasoning_effort,
                                  document_reasoning_effort=document_reasoning_effort)
            state = await service.create(identifier, body, upload_name="upload" + suffix, start=False)
            await service.receive_upload(state["id"], identifier, request.stream(), length)
        response.set_cookie(COOKIE, identifier, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 365)
        return public_state(state)

    @router.websocket("/sessions/{sid}/stream")
    async def stream(websocket: WebSocket, sid: str):
        try:
            validate_origin(websocket)
            identifier = owner(websocket)
            state = service.get(sid, identifier)
            runtime = service.runtimes.get(sid)
            if not runtime or runtime.attached:
                raise HTTPException(409, "会话不存在或已在其他窗口监听")
        except (HTTPException, KeyError, Conflict):
            await websocket.close(code=1008)
            return
        runtime.attached, runtime.ever_attached, runtime.disconnected_at = True, True, None
        await websocket.accept()

        async def transmit():
            sequence = state["event_sequence"]
            await websocket.send_json({"type": "session.snapshot", "snapshot": public_state(state)})
            while True:
                runtime.changed.clear()
                for event in list(runtime.events):
                    if event["event_sequence"] > sequence:
                        outgoing = dict(event)
                        if event["type"] not in {"audio.ack", "asr.partial"}:
                            outgoing["snapshot"] = public_state(state)
                        await websocket.send_json(outgoing)
                        sequence = event["event_sequence"]
                if runtime.task and runtime.task.done():
                    await websocket.send_json({"type": "session.snapshot", "snapshot": public_state(state)})
                    return
                await runtime.changed.wait()

        async def receive():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    await runtime.accept_audio(message["bytes"])
                elif message.get("text"):
                    if len(message["text"]) > 1024:
                        raise ValueError("控制消息过长")
                    command = json.loads(message["text"])
                    if not isinstance(command, dict):
                        raise ValueError("控制消息必须是 JSON 对象")
                    if command.get("type") == "stop":
                        await runtime.stop()
                    elif command.get("type") == "pause":
                        await runtime.pause(command.get("next_sequence"), command.get("samples"))
                    elif command.get("type") == "resume":
                        try:
                            await runtime.resume(command.get("next_sequence"), command.get("samples"),
                                                 command.get("document_version"), command.get("insertion_position"))
                        except Conflict as exc:
                            await websocket.send_json({"type": "session.control_rejected", "error": str(exc), "snapshot": public_state(state)})
                    elif command.get("type") == "cancel":
                        await service.cancel(sid, identifier)
                        return
                    elif command.get("type") != "ping":
                        raise ValueError("不支持的控制消息")

        tx, rx = asyncio.create_task(transmit()), asyncio.create_task(receive())
        try:
            done, pending = await asyncio.wait([tx, rx], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception():
                    raise task.exception()
        except (ValueError, Conflict) as exc:
            await websocket.send_json({"type": "session.error", "error": str(exc)})
            await runtime.stop(str(exc))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in (tx, rx):
                task.cancel()
            await asyncio.gather(tx, rx, return_exceptions=True)
            runtime.attached = False
            runtime.disconnected_at = time.monotonic()
            try:
                await websocket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass

    app.include_router(router)
    return service
