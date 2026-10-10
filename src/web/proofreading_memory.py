"""Web adapter for the isolated memory experiment, not a pipeline stage."""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from src.codex_lb_client import CodexLBClient
from src.config_loader import load_settings
from src.proofreading_memory import (
    MemoryExperimentError, MemoryStore, analyze_request, canonical, export_analysis_request,
    parse_json, prepare_analysis, read_json, require, validate_proposals, validate_request,
    write_artifact, write_frozen_context,
)
from src.web.frontend_settings import load_frontend_settings
from src.web.state_store import create_initial_state, write_json_file

Role = Literal["asr", "system", "human", "reference"]
Status = Literal["pending", "approved", "rejected", "disabled"]
MAX_UPLOAD_BYTES = 1024 * 1024


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreparePayload(Payload):
    inputs: dict[str, str]
    metadata: dict
    reference_metadata: dict | None = None
    fragment_ids: list[str] | None = None


class AnalyzePayload(Payload):
    allow_network: StrictBool = False
    model: str | None = Field(default=None, min_length=1, max_length=200)


class ImportPayload(Payload):
    # Keep the original JSON text: duplicate keys must not be lost by HTTP decoding.
    response_json: str = Field(max_length=MAX_UPLOAD_BYTES)
    response_mode: Literal["offline", "replay"]


class ReviewPayload(Payload):
    action: Literal["approve", "reject", "disable"]
    reviewer: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    expected_version: int = Field(ge=1, strict=True)


class ContextPayload(Payload):
    query: str = Field(max_length=100_000)
    max_items: int = Field(default=8, ge=0, le=50, strict=True)
    max_chars: int = Field(default=4000, ge=0, le=40_000, strict=True)


@contextmanager
def domain_errors():
    try:
        yield
    except MemoryExperimentError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except (OSError, sqlite3.Error, UnicodeError) as exc:
        # Do not expose filesystem details, transport headers or configured secrets.
        raise HTTPException(500, detail="实验文件或数据库操作失败；未自动恢复，请检查实验目录。") from exc


def create_memory_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/api/proofreading-memory", tags=["proofreading-memory"])
    root = app.state.project_root / "data/proofreading-memory"
    database = root / "memory.sqlite3"
    # Single API process owns this directory. Not a cross-worker lease or lock.
    active: set[str] = set()
    guard = threading.Lock()

    def identifier(value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise HTTPException(400, detail="无效的实验标识。")
        return value

    def confined(path: Path) -> Path:
        if not path.resolve().is_relative_to(root.resolve()):
            raise HTTPException(400, detail="路径超出独立实验目录。")
        return path

    def experiment_dir(experiment_id: str) -> Path:
        path = confined(root / "experiments" / identifier(experiment_id))
        if not (path / "request.json").is_file():
            raise HTTPException(404, detail="实验不存在。")
        confined(path / "request.json")
        return path

    def request_for(experiment_id: str) -> dict:
        request = read_json(experiment_dir(experiment_id) / "request.json")
        validate_request(request)
        return request

    def upload_path(role: str, token: str) -> Path:
        require(bool(re.fullmatch(r"[a-f0-9]{32}\.(txt|md|json)", token)), "invalid upload token")
        require(role == "system" or Path(token).suffix != ".json", "only system accepts JSON")
        path = confined(root / "uploads" / role / token)
        require(path.is_file(), f"{role}: upload missing; upload this source on the memory page")
        return path

    def matching_entries(request: dict, status: Status | None = None) -> list[dict]:
        if not database.exists():
            return []
        with MemoryStore(confined(database)) as store:
            return [entry for entry in store.list_entries(status=status)
                    if entry["candidate"]["scope"] == request["metadata"]["scope"]
                    and entry["candidate"]["dataset_kind"] == request["metadata"]["dataset_kind"]]

    def analysis_dir(experiment_id: str, analysis_id: str) -> Path:
        path = confined(experiment_dir(experiment_id) / "analyses" / identifier(analysis_id))
        if not (path / "state.json").is_file():
            raise HTTPException(404, detail="分析记录不存在。")
        return path

    def analysis_state(path: Path) -> dict:
        with guard:
            state = read_json(confined(path / "state.json"))
            if state["status"] in {"pending", "running"} and state["id"] not in active:
                state.update(status="failed", error_message="分析已中断或服务重启；可明确重新发起分析，旧记录保留。")
                write_json_file(path / "state.json", state)
        return state

    def run_analysis(experiment_id: str, analysis_id: str, model: str | None) -> None:
        path = analysis_dir(experiment_id, analysis_id)
        state_path = path / "state.json"
        state = read_json(state_path)
        try:
            state.update(status="running", current_stage="analyze")
            write_json_file(state_path, state)
            web_settings = load_frontend_settings(app.state.project_root)
            settings = load_settings(project_root=app.state.project_root, profile_name=web_settings.profile or None)
            if web_settings.reasoning_effort:
                settings.settings.llm.reasoning_effort = web_settings.reasoning_effort
            state["model"] = model or web_settings.model or settings.settings.llm.model
            client = CodexLBClient(settings.settings.codex_lb, timeout_seconds=settings.settings.llm.timeout_seconds,
                                   base_url_override=web_settings.codex_lb_base_url or None,
                                   api_key_override=web_settings.codex_lb_api_key or None,
                                   bypass_proxy=web_settings.codex_lb_bypass_proxy)
            request = request_for(experiment_id)
            response = analyze_request(request, allow_network=True, loaded_settings=settings,
                                       model=state["model"], client=client)
            validate_proposals(request, response)
            write_artifact(confined(path / "response.json"), canonical(response) + "\n")
            state.update(status="success", current_stage="done", candidate_count=len(response["candidates"]))
        except Exception:
            state.update(status="failed", current_stage="failed",
                         error_message="分析失败；请检查运行设置、输入预算或模型响应。没有候选自动入库。")
        finally:
            # Publish completion and release ownership together: a poll must not
            # classify a stale running read as interrupted after ownership ends.
            with guard:
                try:
                    write_json_file(state_path, state)
                finally:
                    active.discard(analysis_id)

    def import_response(request: dict, response: dict, mode: str) -> dict:
        validate_proposals(request, response)
        with MemoryStore(confined(database)) as store:
            return store.import_candidates(request, response, response_mode=mode)

    @router.post("/uploads/{role}", status_code=201)
    async def upload(role: Role, request: Request, filename: str = Query(min_length=1, max_length=200)):
        suffix = Path(filename).suffix.lower()
        if suffix not in ({".txt", ".md", ".json"} if role == "system" else {".txt", ".md"}):
            raise HTTPException(400, detail="请上传 UTF-8 TXT/MD；系统稿也支持含 final_markdown 的 JSON。")
        token = uuid4().hex + suffix
        destination = confined(root / "uploads" / role / token)
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with destination.open("xb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, detail="每份实验输入最多 1 MiB，请选用较小的明确片段。")
                    stream.write(chunk)
            if size == 0:
                raise HTTPException(400, detail="文件为空。")
            destination.read_bytes().decode("utf-8")
        except BaseException as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, UnicodeError):
                raise HTTPException(400, detail="输入必须为 UTF-8 文本。") from exc
            if isinstance(exc, OSError):
                raise HTTPException(500, detail="保存实验输入失败。") from exc
            raise
        return {"token": token, "name": Path(filename).name, "size": size}

    @router.post("/experiments", status_code=201)
    def prepare(payload: PreparePayload):
        with domain_errors():
            require({"asr", "system", "human"} <= payload.inputs.keys()
                    and payload.inputs.keys() <= {"asr", "system", "human", "reference"}, "explicit three sources required")
            inputs = {role: upload_path(role, token) for role, token in payload.inputs.items()}
            request = prepare_analysis(inputs["asr"], inputs["system"], inputs["human"], metadata=payload.metadata,
                                       reference_path=inputs.get("reference"), reference_metadata=payload.reference_metadata,
                                       selected_fragment_ids=payload.fragment_ids)
            experiment_id = uuid4().hex
            path = confined(root / "experiments" / experiment_id / "request.json")
            export_analysis_request(request, path)
            return {"id": experiment_id, "request": request}

    @router.get("/experiments")
    def list_experiments():
        with domain_errors():
            items = []
            for path in sorted((root / "experiments").glob("*/request.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                identifier(path.parent.name)
                request = read_json(confined(path))
                items.append({"id": path.parent.name, "metadata": request["metadata"], "coverage": request["coverage"]})
            return {"items": items}

    @router.get("/experiments/{experiment_id}")
    def get_experiment(experiment_id: str):
        with domain_errors():
            path = experiment_dir(experiment_id)
            analyses = [analysis_state(confined(p.parent)) for p in sorted((path / "analyses").glob("*/state.json"),
                                                                         key=lambda p: p.stat().st_mtime, reverse=True)]
            return {"id": experiment_id, "request": request_for(experiment_id), "analyses": analyses}

    @router.get("/experiments/{experiment_id}/request")
    def download_request(experiment_id: str):
        return FileResponse(experiment_dir(experiment_id) / "request.json", filename="memory-analysis-request.json")

    @router.post("/experiments/{experiment_id}/analyses", status_code=202)
    def start_analysis(experiment_id: str, payload: AnalyzePayload):
        if payload.allow_network is not True:
            raise HTTPException(400, detail="必须明确同意将选中修改窗口、ASR 和可选参考文本发送至已配置的模型后端。")
        with domain_errors():
            request_for(experiment_id)
            path = experiment_dir(experiment_id)
            with guard:
                for previous in (path / "analyses").glob("*/state.json"):
                    if read_json(confined(previous))["id"] in active:
                        raise HTTPException(409, detail="本实验已有分析正在运行，请勿重复提交。")
                analysis_id = uuid4().hex
                state = create_initial_state(analysis_id, "proofreading-memory-analysis")
                state["model"] = payload.model or ""
                write_json_file(confined(path / "analyses" / analysis_id / "state.json"), state)
                active.add(analysis_id)
            try:
                if app.state.run_tasks_inline:
                    run_analysis(experiment_id, analysis_id, payload.model)
                else:
                    app.state.executor.submit(run_analysis, experiment_id, analysis_id, payload.model)
            except Exception as exc:
                with guard:
                    active.discard(analysis_id)
                raise HTTPException(503, detail="分析任务未能启动；请稍后明确重试。") from exc
            return {"analysis_id": analysis_id}

    @router.get("/experiments/{experiment_id}/analyses/{analysis_id}")
    def get_analysis(experiment_id: str, analysis_id: str):
        with domain_errors():
            path = analysis_dir(experiment_id, analysis_id)
            state = analysis_state(path)
            if state["status"] == "success":
                state["response"] = read_json(confined(path / "response.json"))
            return state

    @router.post("/experiments/{experiment_id}/analyses/{analysis_id}/import")
    def import_analysis(experiment_id: str, analysis_id: str):
        with domain_errors():
            path = analysis_dir(experiment_id, analysis_id)
            require(analysis_state(path)["status"] == "success", "analysis is not successful")
            return import_response(request_for(experiment_id), read_json(confined(path / "response.json")), "remote")

    @router.post("/experiments/{experiment_id}/import-response")
    def import_offline(experiment_id: str, payload: ImportPayload):
        with domain_errors():
            return import_response(request_for(experiment_id), parse_json(payload.response_json), payload.response_mode)

    @router.get("/experiments/{experiment_id}/memories")
    def list_memories(experiment_id: str, status: Status | None = None):
        with domain_errors():
            return {"items": matching_entries(request_for(experiment_id), status)}

    @router.post("/experiments/{experiment_id}/memories/{memory_id}/review")
    def review(experiment_id: str, memory_id: str, payload: ReviewPayload):
        with domain_errors():
            entries = matching_entries(request_for(experiment_id))
            if not any(entry["memory_id"] == memory_id for entry in entries):
                raise HTTPException(404, detail="此范围内的记忆不存在。")
            with MemoryStore(confined(database)) as store:
                return store.review(memory_id, **payload.model_dump())

    @router.post("/experiments/{experiment_id}/contexts", status_code=201)
    def export_context(experiment_id: str, payload: ContextPayload):
        with domain_errors():
            request = request_for(experiment_id)
            with MemoryStore(confined(database)) as store:
                context = store.export_context(task_scope=request["metadata"]["scope"],
                                               dataset_kind=request["metadata"]["dataset_kind"], **payload.model_dump())
            context_id = uuid4().hex
            path = confined(experiment_dir(experiment_id) / "contexts" / context_id)
            write_frozen_context(context, path / "context.json", block_path=path / "block.txt")
            return {"context_id": context_id, "context": context}

    @router.get("/experiments/{experiment_id}/contexts/{context_id}/{format}")
    def download_context(experiment_id: str, context_id: str, format: Literal["json", "txt"]):
        path = confined(experiment_dir(experiment_id) / "contexts" / identifier(context_id)
                        / ("context.json" if format == "json" else "block.txt"))
        if not path.is_file():
            raise HTTPException(404, detail="冻结上下文不存在。")
        return FileResponse(path, filename=f"memory-context-{context_id}.{format}")

    return router
