"""HTTP adapter for the persistent PDF library."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from src.config_loader import ConfigLoadError, load_settings
from src.library import LibraryError, LibraryService
from src.llm.connection import connection_from_settings
from src.llm.gateway import GatewayClient
from src.schemas import CodexLBSettings
from src.web.frontend_settings import gateway_connection_for_frontend, load_frontend_settings


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookChange(_Body):
    base_version: int = Field(ge=1)
    title: str | None = None
    author: str | None = None
    edition: str | None = None
    tags: list[str] | None = None
    archived: bool | None = None


class PageChange(_Body):
    text: str
    base_revision_id: str | int | None
    note: str = ""


class PageRange(_Body):
    book_id: str
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class MaterialCreate(_Body):
    name: str = Field(min_length=1)
    ranges: list[PageRange] = Field(default_factory=list)


class MaterialChange(_Body):
    base_version: int = Field(ge=1)
    name: str | None = None
    ranges: list[PageRange] | None = None
    archived: bool | None = None


class PublishRequest(_Body):
    base_version: int = Field(ge=1)


class OCRRequest(_Body):
    pages: list[int] | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    fast_mode: bool | None = None
    max_concurrency: int | None = Field(default=None, ge=1)
    submit_interval_seconds: float | None = Field(default=None, ge=0)


class ImportRequest(_Body):
    candidate_ids: list[str] = Field(min_length=1)


async def _body(request: Request, model: type[_Body]) -> _Body:
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            detail = "；".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        else:
            detail = "请求体不是有效的 JSON。"
        raise HTTPException(status_code=400, detail=detail) from exc


async def _call(function, *args, **kwargs):
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except LibraryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _page_number(value: str) -> int:
    try:
        page = int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="PDF 物理页码必须是正整数。") from exc
    if page < 1:
        raise HTTPException(status_code=400, detail="PDF 物理页码必须是正整数。")
    return page


def _ranges(values: list[PageRange]) -> list[dict[str, Any]]:
    return [item.model_dump() for item in values]


def _gateway_for_snapshot(root: Path, options: dict[str, Any]) -> GatewayClient:
    """Rebuild a frozen non-secret connection with the current configured credential."""
    snapshot = options["connection"]
    settings = CodexLBSettings(**{key: value for key, value in snapshot.items() if key != "bypass_proxy"})
    frontend = load_frontend_settings(root)
    connection = connection_from_settings(
        settings,
        base_url=snapshot["base_url"],
        api_key=frontend.codex_lb_api_key or None,
        bypass_proxy=bool(snapshot["bypass_proxy"]),
    )
    return GatewayClient(connection, timeout_seconds=options["timeout_seconds"], require_stream_completed=True, total_timeout=True)


def _ocr_options(root: Path, request: OCRRequest) -> tuple[dict[str, Any], GatewayClient]:
    try:
        loaded = load_settings(project_root=root)
    except ConfigLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    frontend = load_frontend_settings(root)
    settings = loaded.settings.reference
    backend = frontend.ocr_backend or settings.ai_ocr_backend
    if backend != "codex_api":
        raise HTTPException(status_code=400, detail="书库按页识别需要当前 OCR 后端为 codex_api，请先在运行设置中调整。")
    connection = gateway_connection_for_frontend(loaded.settings.codex_lb, frontend)
    parsed = urlsplit(connection.base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="OCR 网关地址不能包含凭据、查询参数或片段。")
    model = (request.model or frontend.ocr_model or settings.codex_ocr_model).strip()
    if not model:
        raise HTTPException(status_code=400, detail="请先设置 OCR 模型。")
    options = {
        "model": model,
        "reasoning_effort": (request.reasoning_effort or frontend.ocr_reasoning_effort or settings.codex_ocr_reasoning_effort),
        "fast_mode": request.fast_mode if request.fast_mode is not None else (frontend.ocr_fast_mode if frontend.ocr_fast_mode is not None else settings.codex_ocr_fast_mode),
        "max_concurrency": request.max_concurrency if request.max_concurrency is not None else settings.codex_ocr_max_concurrency,
        "submit_interval_seconds": request.submit_interval_seconds if request.submit_interval_seconds is not None else settings.codex_ocr_submit_interval_seconds,
        "timeout_seconds": settings.ocr_timeout_seconds,
        "connection": {
            "base_url": connection.base_url,
            "responses_path": connection.responses_path,
            "codex_responses_path": connection.codex_responses_path,
            "files_create_path": connection.files_create_path,
            "files_finalize_path_template": connection.files_finalize_path_template,
            "api_key_env": connection.api_key_env,
            "bypass_proxy": frontend.codex_lb_bypass_proxy,
        },
    }
    return options, GatewayClient(connection, timeout_seconds=settings.ocr_timeout_seconds, require_stream_completed=True, total_timeout=True)


def install_library(app: FastAPI, root: Path) -> None:
    root = Path(root).resolve()
    service = LibraryService(root)
    service.gateway_factory = lambda options: _gateway_for_snapshot(root, options)
    app.state.library = service
    prefix = "/api/library"

    @app.get(prefix + "/books")
    async def books(q: str = "", page: int = 1, page_size: int = 20, archived: bool = False):
        if page < 1 or page_size < 1:
            raise HTTPException(status_code=400, detail="页码和每页数量必须为正整数。")
        return await _call(service.list_books, q=q, page=page, page_size=page_size, archived=archived)

    @app.post(prefix + "/books", status_code=201)
    async def add_book(request: Request, filename: str):
        safe_name = Path(filename.replace("\\", "/")).name
        if not safe_name or Path(safe_name).suffix.lower() != ".pdf":
            raise HTTPException(status_code=400, detail="请选择 PDF 文件。")
        staging = root / "data/library/staging/uploads"
        await run_in_threadpool(staging.mkdir, parents=True, exist_ok=True)
        handle = await run_in_threadpool(tempfile.NamedTemporaryFile, mode="wb", suffix=".pdf", dir=staging, delete=False)
        path = Path(handle.name)
        total = 0
        try:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(handle.write, chunk)
                    total += len(chunk)
            await run_in_threadpool(handle.close)
            if not total:
                raise HTTPException(status_code=400, detail="上传文件为空。")
            return await _call(service.add_book, path, filename=safe_name)
        finally:
            if not handle.closed:
                await run_in_threadpool(handle.close)
            await run_in_threadpool(path.unlink, missing_ok=True)

    @app.get(prefix + "/books/{book_id}")
    async def book(book_id: str):
        return await _call(service.get_book, book_id)

    @app.patch(prefix + "/books/{book_id}")
    async def change_book(book_id: str, request: Request):
        body = await _body(request, BookChange)
        assert isinstance(body, BookChange)
        if body.archived is not None:
            if any(value is not None for value in (body.title, body.author, body.edition, body.tags)):
                raise HTTPException(status_code=400, detail="归档和修改书目信息请分别提交。")
            return await _call(service.archive_book, book_id, base_version=body.base_version, archived=body.archived)
        existing = await _call(service.get_book, book_id)
        return await _call(service.update_book, book_id, base_version=body.base_version,
                           title=body.title if body.title is not None else existing["title"],
                           author=body.author if body.author is not None else existing["author"],
                           edition=body.edition if body.edition is not None else existing["edition"],
                           tags=body.tags if body.tags is not None else existing["tags"])

    @app.delete(prefix + "/books/{book_id}")
    async def remove_book(book_id: str, base_version: int | None = None, purge: bool = False):
        if purge:
            return await _call(service.purge_book, book_id)
        if base_version is None:
            raise HTTPException(status_code=400, detail="归档需要提供当前版本。")
        return await _call(service.archive_book, book_id, base_version=base_version, archived=True)

    @app.get(prefix + "/books/{book_id}/pages/{page}")
    async def get_page(book_id: str, page: str):
        return await _call(service.get_page, book_id, _page_number(page))

    @app.get(prefix + "/books/{book_id}/pages/{page}/preview")
    async def preview(book_id: str, page: str):
        path = await _call(service.preview_page, book_id, _page_number(page))
        return FileResponse(path, media_type="image/png")

    @app.get(prefix + "/books/{book_id}/pages/{page}/history")
    async def page_history(book_id: str, page: str):
        return await _call(service.page_history, book_id, _page_number(page))

    @app.put(prefix + "/books/{book_id}/pages/{page}/text")
    async def save_page(book_id: str, page: str, request: Request):
        body = await _body(request, PageChange)
        assert isinstance(body, PageChange)
        return await _call(service.save_page, book_id, _page_number(page), text=body.text,
                           base_revision_id=body.base_revision_id, note=body.note)

    @app.post(prefix + "/books/{book_id}/ocr-jobs", status_code=202)
    async def start_ocr(book_id: str, request: Request):
        body = await _body(request, OCRRequest)
        assert isinstance(body, OCRRequest)
        options, gateway = await run_in_threadpool(_ocr_options, root, body)
        return await _call(service.enqueue_ocr, book_id, body.pages, options=options, gateway=gateway)

    @app.get(prefix + "/jobs")
    async def list_jobs():
        return await _call(service.list_jobs)

    @app.delete(prefix + "/cache/previews")
    async def clear_previews():
        return await _call(service.clear_preview_cache)

    @app.get(prefix + "/jobs/{job_id}")
    async def get_job(job_id: str):
        return await _call(service.get_job, job_id)

    @app.post(prefix + "/jobs/{job_id}/resume", status_code=202)
    async def resume_job(job_id: str):
        return await _call(service.resume_job, job_id)

    @app.get(prefix + "/materials")
    async def materials(archived: bool = False):
        return await _call(service.list_materials, archived=archived)

    @app.post(prefix + "/materials", status_code=201)
    async def create_material(request: Request):
        body = await _body(request, MaterialCreate)
        assert isinstance(body, MaterialCreate)
        return await _call(service.create_material, body.name, _ranges(body.ranges))

    @app.get(prefix + "/materials/{material_id}")
    async def material(material_id: str):
        return await _call(service.get_material, material_id)

    @app.patch(prefix + "/materials/{material_id}")
    async def change_material(material_id: str, request: Request):
        body = await _body(request, MaterialChange)
        assert isinstance(body, MaterialChange)
        if body.archived is not None:
            if body.name is not None or body.ranges is not None:
                raise HTTPException(status_code=400, detail="归档和修改材料请分别提交。")
            return await _call(service.archive_material, material_id, base_version=body.base_version, archived=body.archived)
        current = await _call(service.get_material, material_id)
        return await _call(service.update_material, material_id, base_version=body.base_version,
                           name=body.name if body.name is not None else current["name"],
                           ranges=_ranges(body.ranges) if body.ranges is not None else current["ranges"])

    @app.delete(prefix + "/materials/{material_id}")
    async def remove_material(material_id: str, base_version: int | None = None, purge: bool = False):
        if purge:
            return await _call(service.purge_material, material_id)
        if base_version is None:
            raise HTTPException(status_code=400, detail="归档需要提供当前版本。")
        return await _call(service.archive_material, material_id, base_version=base_version, archived=True)

    @app.get(prefix + "/materials/{material_id}/revisions")
    async def revisions(material_id: str):
        return await _call(service.list_material_revisions, material_id)

    @app.post(prefix + "/materials/{material_id}/revisions", status_code=201)
    async def publish(material_id: str, request: Request):
        body = await _body(request, PublishRequest)
        assert isinstance(body, PublishRequest)
        return await _call(service.publish_material, material_id, base_version=body.base_version)

    @app.get(prefix + "/revisions/{revision_id}/{kind}")
    async def revision_file(revision_id: str, kind: str):
        if kind not in {"pdf", "txt", "sources"}:
            raise HTTPException(status_code=400, detail="仅支持下载 PDF、TXT 或来源清单。")
        path = await _call(service.revision_file, revision_id, kind)
        media = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8", "sources": "application/json"}[kind]
        return FileResponse(path, filename=path.name, media_type=media)

    @app.get(prefix + "/imports")
    async def import_candidates():
        return await _call(service.history_candidates)

    @app.post(prefix + "/imports")
    async def import_history(request: Request):
        body = await _body(request, ImportRequest)
        assert isinstance(body, ImportRequest)
        return await _call(service.import_history, body.candidate_ids)
