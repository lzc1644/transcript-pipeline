from __future__ import annotations

import json
import threading
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from scripts.run_pipeline import STAGE_EXIT_PARTIAL, run_stage
from src.config_loader import ConfigLoadError, load_settings
from src.pdf_book_ocr import PDFBookOCRError, PDFBookOCRProgress, ocr_pdf_book_batch
from src.reference_utils import ReferenceFileProgress
from src.job_runner import (
    BatchJobRuntime,
    BatchJobSpec,
    BatchRunSummary,
    JobRunnerError,
    build_batch_root,
    build_final_output_filename,
    build_job_paths,
    copy_final_output,
    execute_batch_stage_for_runtime,
    execute_remote_pipeline_for_runtime,
    load_batch_job_specs,
    prepare_batch_jobs,
    prepare_job_inputs,
    runtime_stage_names,
    serialize_batch_runtime,
    write_batch_summary,
    write_job_manifest,
    write_job_settings,
)
from src.runtime_utils import normalize_stage_name, setup_logging
from src.settings_overrides import ModelOverrides, apply_model_overrides
from src.web.frontend_settings import (
    codex_lb_environment,
    load_frontend_settings,
)
from src.web.models import (
    BatchJobRequest,
    JobRerunRequest,
    PDFBookOCRRequest,
    SingleJobRequest,
    StageFileRunRequest,
    StageRunRequest,
)
from src.web.pdf_book_ocr import (
    PDFBookOCRTaskPaths,
    build_pdf_book_ocr_task_paths,
    relative_pdf_book_ocr_output_path,
    resolve_uploaded_pdf_ocr_input,
)
from src.web.stage_file_runs import (
    StageFileRunError,
    build_stage_file_workspace,
    build_stage_result_archive,
    place_stage_inputs,
)
from src.web.state_store import create_initial_state, read_json_file, write_json_file


def first_text(*values: str | None) -> str | None:
    for value in values:
        normalized = (value or "").strip()
        if normalized:
            return normalized
    return None


def secondary_asr_override(request, frontend_settings) -> str | None:
    # 空字符串是显式关闭，不能用 or 吞掉并重新启用全局默认。
    value = request.secondary_asr_candidate
    return value if value is not None else frontend_settings.secondary_asr_candidate


def request_payload_with_effective_ocr_settings(request, loaded_settings, *, freeze_asr: bool = False) -> dict[str, object]:
    payload = request.model_dump()
    reference_settings = loaded_settings.settings.reference
    if hasattr(request, "asr_candidate"):
        payload["asr_candidate"] = loaded_settings.settings.asr.candidate or "whisper-existing"
        payload["secondary_asr_candidate"] = loaded_settings.settings.asr.secondary_candidate or ""
    if freeze_asr:
        payload["profile"] = loaded_settings.active_profile_name
        payload["config"] = str(loaded_settings.settings_path)
    payload.update(
        {
            "ocr_backend": reference_settings.ai_ocr_backend,
            "ocr_model": reference_settings.codex_ocr_model,
            "ocr_reasoning_effort": reference_settings.codex_ocr_reasoning_effort,
            "ocr_max_concurrency": reference_settings.codex_ocr_max_concurrency,
            "ocr_submit_interval_seconds": reference_settings.codex_ocr_submit_interval_seconds,
        }
    )
    return payload


def stage_output_path(loaded_settings, stage_name: str) -> str:
    field_map = {
        "extract-audio": "audio_dir",
        "transcribe": "asr_dir",
        "prepare-reference": "extracted_text_dir",
        "align": "aligned_dir",
        "classify": "classified_dir",
        "refine": "refined_dir",
        "export-markdown": "final_dir",
    }
    field_name = field_map.get(stage_name)
    if field_name is None:
        return ""
    return str(loaded_settings.path_for(field_name))


def serialize_reference_file_progress(progress: ReferenceFileProgress) -> dict[str, object]:
    return {
        "source_file": progress.source_path.name,
        "output_file": progress.output_text_path.name if not progress.resumable else "",
        "success": not progress.resumable,
        "page_count": progress.page_count,
        "completed_pages": progress.completed_pages,
        "failed_pages": len(progress.failed_page_numbers),
        "failed_page_numbers": list(progress.failed_page_numbers),
        "page_errors": {str(page_number): error for page_number, error in progress.page_errors.items()},
        "resumable": progress.resumable,
    }


def update_reference_progress_state(
    *,
    app: FastAPI,
    state_path: Path,
    progress_items: dict[str, dict[str, object]],
    progress: ReferenceFileProgress,
) -> None:
    item = serialize_reference_file_progress(progress)
    progress_items[str(item["source_file"])] = item
    items = [progress_items[key] for key in sorted(progress_items)]
    app.state.update_state(
        state_path,
        ocr_items=items,
        pages_total=sum(int(entry["page_count"]) for entry in items),
        pages_completed=sum(int(entry["completed_pages"]) for entry in items),
        pages_failed=sum(int(entry["failed_pages"]) for entry in items),
        resumable=any(bool(entry["resumable"]) for entry in items),
    )


def submit_task(fastapi_app: FastAPI, func, **kwargs) -> None:
    if fastapi_app.state.run_tasks_inline:
        func(**kwargs)
        return
    fastapi_app.state.executor.submit(func, **kwargs)


def read_job_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise JobRunnerError(f"无法读取 job manifest: {manifest_path} | {exc}") from exc
    except json.JSONDecodeError as exc:
        raise JobRunnerError(f"job manifest JSON 解析失败: {manifest_path} | {exc}") from exc
    if not isinstance(payload, dict):
        raise JobRunnerError(f"job manifest 结构无效: {manifest_path}")
    return payload


def resolve_rerun_stages(raw_stages: list[str], start_stage: str) -> list[str]:
    stages = [normalize_stage_name(stage_name) for stage_name in raw_stages]
    normalized_start_stage = normalize_stage_name(start_stage)
    if normalized_start_stage not in stages:
        raise JobRunnerError(
            f"无法从指定阶段重跑: {normalized_start_stage} 不在当前 job 流水线中，当前阶段为: {', '.join(stages)}"
        )
    return stages[stages.index(normalized_start_stage) :]


def ensure_job_state_file(state_path: Path, job_id: str) -> None:
    if state_path.exists():
        return
    write_json_file(state_path, create_initial_state(job_id, "job"))


def batch_runtime_from_state_item(project_root: Path, item: dict[str, Any]) -> BatchJobRuntime:
    job_id = str(item.get("job_id") or "")
    spec = BatchJobSpec(
        video=str(item.get("video_source") or ""),
        reference=str(item.get("reference_source") or ""),
        output_dir=str(item.get("output_dir") or ""),
        mode=str(item.get("mode") or ""),
        content_type=str(item.get("content_type") or "book_club"),
        book_name=str(item.get("book_name") or "") or None,
        chapter=str(item.get("chapter") or "") or None,
        glossary_file=str(item.get("glossary_file") or "") or None,
    )
    copied_output_path = str(item.get("copied_output_path") or "").strip()
    return BatchJobRuntime(
        job_id=job_id,
        job_root=build_job_paths(project_root, job_id).job_root,
        spec=spec,
        status=str(item.get("status") or "pending"),
        current_stage=str(item.get("current_stage") or "pending"),
        completed_stages=[str(stage) for stage in item.get("completed_stages") or [] if str(stage)],
        failed_stage=str(item.get("failed_stage") or "") or None,
        error_message=str(item.get("error_message") or "") or None,
        copied_output_path=Path(copied_output_path) if copied_output_path else None,
        ocr_items={
            str(ocr_item.get("source_file") or index): dict(ocr_item)
            for index, ocr_item in enumerate(item.get("ocr_items") or [])
            if isinstance(ocr_item, dict)
        },
    )


def rewrite_batch_summary_from_state(*, project_root: Path, batch_id: str, state: dict[str, Any]) -> None:
    raw_items = state.get("items") or []
    items = [
        batch_runtime_from_state_item(project_root, item)
        for item in raw_items
        if isinstance(item, dict)
    ]
    write_batch_summary(
        project_root=project_root,
        summary=BatchRunSummary(
            batch_id=batch_id,
            total=len(items),
            success=sum(1 for item in items if item.status == "success"),
            failed=sum(1 for item in items if item.status == "failed"),
            partial=sum(1 for item in items if item.status == "partial"),
            items=items,
        ),
    )


def update_batch_item_state(
    *,
    app: FastAPI,
    batch_id: str,
    item_job_id: str,
    item_changes: dict[str, Any],
    batch_changes: dict[str, Any],
) -> dict[str, Any]:
    state_path = app.state.batch_state_path(batch_id)
    state = read_json_file(state_path)
    raw_items = state.get("items") or []
    if not isinstance(raw_items, list):
        raise JobRunnerError(f"batch 状态结构无效，items 不是列表: {batch_id}")

    updated_items: list[Any] = []
    found = False
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            updated_items.append(raw_item)
            continue
        if str(raw_item.get("job_id") or "") != item_job_id:
            updated_items.append(raw_item)
            continue
        found = True
        updated = dict(raw_item)
        updated.update(item_changes)
        updated_items.append(updated)

    if not found:
        raise JobRunnerError(f"批量子任务不存在: {item_job_id}")

    success_count = sum(1 for item in updated_items if isinstance(item, dict) and item.get("status") == "success")
    failed_count = sum(1 for item in updated_items if isinstance(item, dict) and item.get("status") == "failed")
    partial_count = sum(1 for item in updated_items if isinstance(item, dict) and item.get("status") == "partial")
    requested_status = str(batch_changes.pop("status", "") or "")
    if requested_status == "running":
        next_status = "running"
    elif partial_count:
        next_status = "partial"
    elif failed_count:
        next_status = "failed"
    else:
        next_status = "success"
    updated_state = app.state.update_state(
        state_path,
        status=next_status,
        total=len(updated_items),
        success=success_count,
        failed=failed_count,
        partial=partial_count,
        items=updated_items,
        **batch_changes,
    )
    rewrite_batch_summary_from_state(project_root=app.state.project_root, batch_id=batch_id, state=updated_state)
    return updated_state


def run_job_rerun(*, app: FastAPI, job_id: str, payload: dict, state_path: Path) -> dict[str, Any]:
    root = app.state.project_root
    ensure_job_state_file(state_path, job_id)

    try:
        request = JobRerunRequest.model_validate(payload)
        frontend_settings = load_frontend_settings(root)
        # A changed global profile can change Whisper's model too. Historical
        # reruns use their snapshot unless this request explicitly overrides it.
        effective_profile = first_text(request.profile)
        effective_backend = first_text(request.backend, frontend_settings.backend)
        job_paths = build_job_paths(root, job_id)
        if not job_paths.settings_path.exists():
            raise JobRunnerError(f"job 缺少生成配置，无法重跑: {job_paths.settings_path}")
        if not job_paths.manifest_path.exists():
            raise JobRunnerError(f"job 缺少 manifest，无法重跑: {job_paths.manifest_path}")

        manifest = read_job_manifest(job_paths.manifest_path)
        loaded_settings = load_settings(
            settings_path=job_paths.settings_path,
            profile_name=effective_profile or str(manifest.get("profile") or ""),
            project_root=root,
        )
        saved_candidate = loaded_settings.settings.asr.candidate or "whisper-existing"
        if request.asr_candidate is not None and request.asr_candidate != saved_candidate:
            raise JobRunnerError("切换 ASR 候选请新建任务或独立阶段文件运行；历史任务仅允许同候选重试")
        if request.secondary_asr_candidate is not None and (request.secondary_asr_candidate or None) != loaded_settings.settings.asr.secondary_candidate:
            raise JobRunnerError("切换第二 ASR 候选请新建任务；历史重跑沿用原双 ASR 快照")
        apply_model_overrides(
            loaded_settings,
            ModelOverrides(
                llm_model=request.model or frontend_settings.model or None,
                llm_reasoning_effort=request.reasoning_effort or frontend_settings.reasoning_effort or None,
                asr_candidate=request.asr_candidate,
                secondary_asr_candidate=request.secondary_asr_candidate,
                # job 的生成配置已经固化首次运行的 OCR 身份；重跑未显式覆盖时必须沿用原值。
                ocr_backend=request.ocr_backend,
                ocr_model=request.ocr_model,
                ocr_reasoning_effort=request.ocr_reasoning_effort,
                ocr_max_concurrency=request.ocr_max_concurrency,
                ocr_submit_interval_seconds=request.ocr_submit_interval_seconds,
            ),
        )
        raw_stages = loaded_settings.settings.pipeline.stages if loaded_settings.settings.pipeline else []
        stages = resolve_rerun_stages(raw_stages, request.start_stage)
        logger = setup_logging(loaded_settings.settings.runtime.log_level)
        current_state = read_json_file(state_path)
        progress_items = {
            str(item.get("source_file") or index): dict(item)
            for index, item in enumerate(current_state.get("ocr_items") or [])
            if isinstance(item, dict)
        }

        def reference_progress(progress: ReferenceFileProgress) -> None:
            update_reference_progress_state(
                app=app,
                state_path=state_path,
                progress_items=progress_items,
                progress=progress,
            )

        app.state.update_state(
            state_path,
            status="running",
            current_stage=stages[0],
            error_message="",
            output_path="",
        )
        with codex_lb_environment(frontend_settings):
            for stage_name in stages:
                app.state.update_state(
                    state_path,
                    status="running",
                    current_stage=stage_name,
                )
                current_backend = effective_backend if stage_name == "refine" else None
                exit_code = run_stage(
                    stage_name,
                    loaded_settings,
                    logger,
                    backend_override=current_backend,
                    prepare_reference_progress_callback=(
                        reference_progress if stage_name == "prepare-reference" else None
                    ),
                )
                if exit_code == STAGE_EXIT_PARTIAL:
                    app.state.update_state(
                        state_path,
                        status="partial",
                        current_stage=stage_name,
                        error_message="准备参考 OCR 尚有缺页，请重试缺失页。",
                        resumable=True,
                    )
                    return read_json_file(state_path)
                if exit_code != 0:
                    raise JobRunnerError(f"job 重跑失败: stage={stage_name} exit_code={exit_code}")

        output_path = stage_output_path(loaded_settings, stages[-1])
        if "export-markdown" in stages:
            output_dir = str(manifest.get("output_dir") or "").strip()
            video_source = str(manifest.get("video_source") or "").strip()
            if not output_dir or not video_source:
                raise JobRunnerError("job manifest 缺少 output_dir 或 video_source，无法复制最终输出。")
            _, copied_output_path = copy_final_output(
                job_paths,
                Path(output_dir).expanduser().resolve(),
                build_final_output_filename(
                    Path(video_source),
                    book_name=str(manifest.get("book_name") or "") or None,
                    chapter=str(manifest.get("chapter") or "") or None,
                ),
            )
            output_path = str(copied_output_path)

        app.state.update_state(
            state_path,
            status="success",
            current_stage="done",
            output_path=output_path,
            error_message="",
            resumable=False,
        )
    except (ConfigLoadError, JobRunnerError, ValueError, OSError) as exc:
        app.state.update_state(
            state_path,
            status="failed",
            error_message=str(exc),
        )
    return read_json_file(state_path)


def execute_single_job(*, app: FastAPI, job_id: str, payload: dict) -> None:
    state_path = app.state.job_state_path(job_id)
    root = app.state.project_root

    app.state.active_jobs.add(job_id)
    try:
        request = SingleJobRequest.model_validate(payload)
        frontend_settings = load_frontend_settings(root)
        effective_profile = first_text(request.profile, frontend_settings.profile)
        effective_backend = first_text(request.backend, frontend_settings.backend)
        effective_book_name = first_text(request.book_name, frontend_settings.book_name)
        effective_chapter = first_text(request.chapter, frontend_settings.chapter)
        effective_glossary_file = first_text(request.glossary_file, frontend_settings.glossary_file)
        effective_refine_prompt = first_text(request.refine_prompt)
        model_overrides = ModelOverrides(
            asr_candidate=request.asr_candidate or frontend_settings.asr_candidate or None,
            secondary_asr_candidate=secondary_asr_override(request, frontend_settings),
            llm_model=request.model or frontend_settings.model or None,
            llm_reasoning_effort=request.reasoning_effort or frontend_settings.reasoning_effort or None,
            ocr_backend=request.ocr_backend or frontend_settings.ocr_backend or None,
            ocr_model=request.ocr_model or frontend_settings.ocr_model or None,
            ocr_reasoning_effort=request.ocr_reasoning_effort or frontend_settings.ocr_reasoning_effort or None,
            ocr_max_concurrency=request.ocr_max_concurrency,
            ocr_submit_interval_seconds=request.ocr_submit_interval_seconds,
        )
        base_loaded_settings = load_settings(
            settings_path=request.config,
            profile_name=effective_profile,
            project_root=root,
        )
        profile_name = effective_profile or base_loaded_settings.active_profile_name
        video_source = Path(request.video).expanduser().resolve()
        output_dir = Path(request.output_dir).expanduser().resolve()

        job_paths = build_job_paths(root, job_id)
        prepared_inputs = prepare_job_inputs(
            video_source=video_source,
            reference_source=request.reference,
            job_paths=job_paths,
            content_type=request.content_type,
        )
        generated_settings_path = write_job_settings(
            project_root=root,
            loaded_settings=base_loaded_settings,
            job_paths=job_paths,
            profile_name=profile_name,
            content_type=request.content_type,
            glossary_file=effective_glossary_file,
            book_name=effective_book_name,
            chapter=effective_chapter,
            model_overrides=model_overrides,
            refine_prompt=effective_refine_prompt,
        )
        write_job_manifest(
            loaded_settings=base_loaded_settings,
            job_paths=job_paths,
            prepared_inputs=prepared_inputs,
            video_source=video_source,
            reference_source=request.reference,
            output_dir=output_dir,
            profile_name=profile_name,
            content_type=request.content_type,
            book_name=effective_book_name,
            chapter=effective_chapter,
            glossary_file=effective_glossary_file,
        )

        job_loaded_settings = load_settings(
            settings_path=generated_settings_path,
            profile_name=profile_name,
            project_root=root,
        )
        logger = setup_logging(job_loaded_settings.settings.runtime.log_level)
        app.state.update_state(state_path, asr_candidate=job_loaded_settings.settings.asr.candidate or "whisper-existing",
                               secondary_asr_candidate=job_loaded_settings.settings.asr.secondary_candidate or "")
        stages = [normalize_stage_name(stage_name) for stage_name in job_loaded_settings.settings.pipeline.stages]
        progress_items: dict[str, dict[str, object]] = {}

        def reference_progress(progress: ReferenceFileProgress) -> None:
            update_reference_progress_state(
                app=app,
                state_path=state_path,
                progress_items=progress_items,
                progress=progress,
            )

        with codex_lb_environment(frontend_settings):
            for stage_name in stages:
                app.state.update_state(
                    state_path,
                    status="running",
                    current_stage=stage_name,
                )
                current_backend = effective_backend if stage_name == "refine" else None
                exit_code = run_stage(
                    stage_name,
                    job_loaded_settings,
                    logger,
                    backend_override=current_backend,
                    prepare_reference_progress_callback=(
                        reference_progress if stage_name == "prepare-reference" else None
                    ),
                )
                if exit_code == STAGE_EXIT_PARTIAL:
                    app.state.update_state(
                        state_path,
                        status="partial",
                        current_stage=stage_name,
                        error_message="准备参考 OCR 尚有缺页，请重试缺失页。",
                        resumable=True,
                    )
                    return
                if exit_code != 0:
                    raise JobRunnerError(f"job 主链失败: stage={stage_name} exit_code={exit_code}")

        _, copied_output_path = copy_final_output(
            job_paths,
            output_dir,
            build_final_output_filename(
                video_source,
                book_name=effective_book_name,
                chapter=effective_chapter,
            ),
        )
        app.state.update_state(
            state_path,
            status="success",
            current_stage="done",
            output_path=str(copied_output_path),
            error_message="",
            resumable=False,
        )
    except (ConfigLoadError, JobRunnerError, ValueError, OSError) as exc:
        app.state.update_state(
            state_path,
            status="failed",
            error_message=str(exc),
        )
    finally:
        app.state.active_jobs.discard(job_id)


def execute_job_rerun(*, app: FastAPI, job_id: str, payload: dict) -> None:
    state_path = app.state.job_state_path(job_id)

    app.state.active_jobs.add(job_id)
    try:
        run_job_rerun(app=app, job_id=job_id, payload=payload, state_path=state_path)
    finally:
        app.state.active_jobs.discard(job_id)


def execute_batch_item_rerun(*, app: FastAPI, batch_id: str, item_job_id: str, payload: dict) -> None:
    root = app.state.project_root
    request = JobRerunRequest.model_validate(payload)
    batch_root = build_batch_root(root, batch_id)
    item_rerun_state_path = batch_root / "item-reruns" / item_job_id / "state.json"

    app.state.active_jobs.add(batch_id)
    app.state.active_jobs.add(item_job_id)
    try:
        update_batch_item_state(
            app=app,
            batch_id=batch_id,
            item_job_id=item_job_id,
            item_changes={
                "status": "running",
                "current_stage": request.start_stage,
                "failed_stage": "",
                "error_message": "",
                "copied_output_path": "",
            },
            batch_changes={
                "status": "running",
                "current_stage": request.start_stage,
                "error_message": "",
            },
        )
        final_state = run_job_rerun(
            app=app,
            job_id=item_job_id,
            payload=payload,
            state_path=item_rerun_state_path,
        )
        final_status = str(final_state.get("status") or "")
        if final_status == "success":
            update_batch_item_state(
                app=app,
                batch_id=batch_id,
                item_job_id=item_job_id,
                item_changes={
                    "status": "success",
                    "current_stage": "done",
                    "failed_stage": "",
                    "error_message": "",
                    "copied_output_path": str(final_state.get("output_path") or ""),
                },
                batch_changes={
                    "status": "success",
                    "current_stage": "done",
                    "error_message": "",
                },
            )
            return
        if final_status == "partial":
            update_batch_item_state(
                app=app,
                batch_id=batch_id,
                item_job_id=item_job_id,
                item_changes={
                    "status": "partial",
                    "current_stage": "prepare-reference",
                    "failed_stage": "prepare-reference",
                    "error_message": str(
                        final_state.get("error_message")
                        or "准备参考 OCR 尚有缺页，请重试缺失页。"
                    ),
                    "copied_output_path": "",
                    "ocr_items": list(final_state.get("ocr_items") or []),
                    "pages_total": int(final_state.get("pages_total") or 0),
                    "pages_completed": int(final_state.get("pages_completed") or 0),
                    "pages_failed": int(final_state.get("pages_failed") or 0),
                    "resumable": True,
                },
                batch_changes={
                    "status": "partial",
                    "current_stage": "prepare-reference",
                    "error_message": "",
                },
            )
            return

        update_batch_item_state(
            app=app,
            batch_id=batch_id,
            item_job_id=item_job_id,
            item_changes={
                "status": "failed",
                "current_stage": str(final_state.get("current_stage") or request.start_stage),
                "failed_stage": str(final_state.get("current_stage") or request.start_stage),
                "error_message": str(final_state.get("error_message") or "子任务重跑失败"),
                "copied_output_path": "",
            },
            batch_changes={
                "status": "failed",
                "current_stage": "done",
            },
        )
    except (ConfigLoadError, JobRunnerError, ValueError, OSError) as exc:
        update_batch_item_state(
            app=app,
            batch_id=batch_id,
            item_job_id=item_job_id,
            item_changes={
                "status": "failed",
                "current_stage": request.start_stage,
                "failed_stage": request.start_stage,
                "error_message": str(exc),
                "copied_output_path": "",
            },
            batch_changes={
                "status": "failed",
                "current_stage": "done",
            },
        )
    finally:
        app.state.active_jobs.discard(item_job_id)
        app.state.active_jobs.discard(batch_id)


def execute_batch_job(*, app: FastAPI, batch_id: str, payload: dict) -> None:
    state_path = app.state.batch_state_path(batch_id)
    root = app.state.project_root

    app.state.active_jobs.add(batch_id)
    try:
        request = BatchJobRequest.model_validate(payload)
        frontend_settings = load_frontend_settings(root)
        effective_profile = first_text(request.profile, frontend_settings.profile)
        effective_backend = first_text(request.backend, frontend_settings.backend)
        effective_book_name = first_text(request.book_name, frontend_settings.book_name)
        effective_chapter = first_text(request.chapter, frontend_settings.chapter)
        effective_glossary_file = first_text(request.glossary_file, frontend_settings.glossary_file)
        effective_refine_prompt = first_text(request.refine_prompt)
        effective_remote_concurrency = request.remote_concurrency or frontend_settings.remote_concurrency
        model_overrides = ModelOverrides(
            asr_candidate=request.asr_candidate or frontend_settings.asr_candidate or None,
            secondary_asr_candidate=secondary_asr_override(request, frontend_settings),
            llm_model=request.model or frontend_settings.model or None,
            llm_reasoning_effort=request.reasoning_effort or frontend_settings.reasoning_effort or None,
            ocr_backend=request.ocr_backend or frontend_settings.ocr_backend or None,
            ocr_model=request.ocr_model or frontend_settings.ocr_model or None,
            ocr_reasoning_effort=request.ocr_reasoning_effort or frontend_settings.ocr_reasoning_effort or None,
            ocr_max_concurrency=request.ocr_max_concurrency,
            ocr_submit_interval_seconds=request.ocr_submit_interval_seconds,
        )
        base_loaded_settings = load_settings(
            settings_path=request.config,
            profile_name=effective_profile,
            project_root=root,
        )
        app.state.update_state(state_path, asr_candidate=model_overrides.asr_candidate or base_loaded_settings.settings.asr.candidate or "whisper-existing")
        job_specs, failed_runtimes = load_batch_job_specs(
            base_loaded_settings=base_loaded_settings,
            manifest=request.manifest,
            videos_dir=request.videos_dir,
            reference_dir=request.reference_dir,
            shared_reference=request.shared_reference,
            output_dir=request.output_dir,
            content_type=request.content_type,
            book_name=effective_book_name,
            chapter=effective_chapter,
            glossary_file=effective_glossary_file,
        )
        runtimes = list(failed_runtimes)
        runtimes.extend(
            prepare_batch_jobs(
                project_root=root,
                base_loaded_settings=base_loaded_settings,
                job_specs=job_specs,
                model_overrides=model_overrides,
                refine_prompt=effective_refine_prompt,
            )
        )

        app.state.update_state(
            state_path,
            status="running",
            current_stage="prepare-job",
            total=len(runtimes),
            items=[serialize_batch_runtime(item) for item in runtimes],
        )

        logger = setup_logging(base_loaded_settings.settings.runtime.log_level)
        progress_lock = threading.Lock()

        def sync_batch_progress() -> None:
            """把并发子任务的最新状态一次性持久化到批量状态文件。"""
            with progress_lock:
                app.state.update_state(
                    state_path,
                    status="running",
                    current_stage="staggered-pipeline",
                    total=len(runtimes),
                    success=sum(1 for item in runtimes if item.status == "success"),
                    failed=sum(1 for item in runtimes if item.status == "failed"),
                    partial=sum(1 for item in runtimes if item.status == "partial"),
                    items=[serialize_batch_runtime(item) for item in runtimes],
                )

        def batch_reference_progress(
            runtime: BatchJobRuntime,
            progress: ReferenceFileProgress,
        ) -> None:
            item = serialize_reference_file_progress(progress)
            with progress_lock:
                runtime.ocr_items[str(item["source_file"])] = item
            sync_batch_progress()

        # 本地阶段保持顺序执行；每个子任务完成转写后立即进入远程后续链路，
        # 因而不会再等待整批视频都完成 ASR 才开始参考提取与校对。
        with codex_lb_environment(frontend_settings):
            with ThreadPoolExecutor(max_workers=effective_remote_concurrency) as executor:
                remote_futures = []
                for runtime in runtimes:
                    if runtime.status in {"failed", "partial"}:
                        continue
                    runtime_stages = runtime_stage_names(runtime, root)
                    for stage_name in ("extract-audio", "transcribe"):
                        if stage_name not in runtime_stages:
                            continue
                        execute_batch_stage_for_runtime(
                            stage_name=stage_name,
                            runtime=runtime,
                            project_root=root,
                            logger=logger,
                            backend_override=effective_backend,
                            stage_callback=lambda _runtime: sync_batch_progress(),
                        )
                        if runtime.status in {"failed", "partial"}:
                            break

                    if runtime.status in {"failed", "partial"}:
                        continue
                    remote_futures.append(
                        executor.submit(
                            execute_remote_pipeline_for_runtime,
                            runtime=runtime,
                            project_root=root,
                            logger=logger,
                            backend_override=effective_backend,
                            progress_callback=batch_reference_progress,
                            stage_callback=lambda _runtime: sync_batch_progress(),
                        )
                    )

                for future in as_completed(remote_futures):
                    future.result()

        success_count = sum(1 for item in runtimes if item.status == "success")
        failed_count = sum(1 for item in runtimes if item.status == "failed")
        partial_count = sum(1 for item in runtimes if item.status == "partial")
        summary = BatchRunSummary(
            batch_id=batch_id,
            total=len(runtimes),
            success=success_count,
            failed=failed_count,
            partial=partial_count,
            items=runtimes,
        )
        write_batch_summary(project_root=root, summary=summary)

        app.state.update_state(
            state_path,
            status="partial" if partial_count else "success" if failed_count == 0 else "failed",
            current_stage="done",
            output_path=str(build_batch_root(root, batch_id) / "summary.json"),
            total=summary.total,
            success=summary.success,
            failed=summary.failed,
            partial=summary.partial,
            items=[serialize_batch_runtime(item) for item in runtimes],
        )
    except (ConfigLoadError, JobRunnerError, ValueError, OSError) as exc:
        app.state.update_state(
            state_path,
            status="failed",
            error_message=str(exc),
        )
    finally:
        app.state.active_jobs.discard(batch_id)


def execute_stage_run(*, app: FastAPI, run_id: str, stage_name: str, payload: dict) -> None:
    state_path = app.state.stage_run_state_path(run_id)
    root = app.state.project_root

    app.state.active_jobs.add(run_id)
    try:
        request = StageRunRequest.model_validate(payload)
        frontend_settings = load_frontend_settings(root)
        normalized_stage_name = normalize_stage_name(stage_name)
        effective_profile = first_text(request.profile, frontend_settings.profile)
        effective_backend = first_text(request.backend, frontend_settings.backend)
        loaded_settings = load_settings(
            settings_path=request.config,
            profile_name=effective_profile,
            project_root=root,
        )
        apply_model_overrides(
            loaded_settings,
            ModelOverrides(
                asr_candidate=request.asr_candidate or frontend_settings.asr_candidate or None,
                secondary_asr_candidate=secondary_asr_override(request, frontend_settings),
                llm_model=request.model or frontend_settings.model or None,
                llm_reasoning_effort=request.reasoning_effort or frontend_settings.reasoning_effort or None,
                ocr_backend=request.ocr_backend or frontend_settings.ocr_backend or None,
                ocr_model=request.ocr_model or frontend_settings.ocr_model or None,
                ocr_reasoning_effort=request.ocr_reasoning_effort or frontend_settings.ocr_reasoning_effort or None,
                ocr_max_concurrency=request.ocr_max_concurrency,
                ocr_submit_interval_seconds=request.ocr_submit_interval_seconds,
            ),
        )
        if normalized_stage_name == "transcribe":
            snapshot_path = state_path.parent / "asr-settings.yaml"
            snapshot = loaded_settings.settings.model_dump(mode="json")
            snapshot["runtime"]["default_profile"] = loaded_settings.active_profile_name
            snapshot_path.write_text(yaml.safe_dump(snapshot, allow_unicode=True, sort_keys=False), encoding="utf-8")
            loaded_settings = load_settings(settings_path=snapshot_path, project_root=root,
                                            profile_name=loaded_settings.active_profile_name)
        logger = setup_logging(loaded_settings.settings.runtime.log_level)
        progress_items: dict[str, dict[str, object]] = {}

        def reference_progress(progress: ReferenceFileProgress) -> None:
            update_reference_progress_state(
                app=app,
                state_path=state_path,
                progress_items=progress_items,
                progress=progress,
            )

        app.state.update_state(
            state_path,
            status="running",
            current_stage=normalized_stage_name,
            request_payload=request_payload_with_effective_ocr_settings(request, loaded_settings,
                freeze_asr=normalized_stage_name == "transcribe"),
            **({"asr_candidate": loaded_settings.settings.asr.candidate or "whisper-existing"} if normalized_stage_name == "transcribe" else {}),
        )
        with codex_lb_environment(frontend_settings):
            exit_code = run_stage(
                normalized_stage_name,
                loaded_settings,
                logger,
                backend_override=effective_backend,
                prepare_reference_progress_callback=(
                    reference_progress if normalized_stage_name == "prepare-reference" else None
                ),
            )
        if exit_code == STAGE_EXIT_PARTIAL:
            app.state.update_state(
                state_path,
                status="partial",
                current_stage=normalized_stage_name,
                error_message="准备参考 OCR 尚有缺页，请重试缺失页。",
                resumable=True,
            )
            return
        if exit_code != 0:
            raise JobRunnerError(f"stage 运行失败: stage={normalized_stage_name} exit_code={exit_code}")

        app.state.update_state(
            state_path,
            status="success",
            current_stage=normalized_stage_name,
            output_path=stage_output_path(loaded_settings, normalized_stage_name),
            error_message="",
            resumable=False,
        )
    except (ConfigLoadError, JobRunnerError, ValueError, OSError) as exc:
        app.state.update_state(
            state_path,
            status="failed",
            error_message=str(exc),
        )
    finally:
        app.state.active_jobs.discard(run_id)


def execute_stage_file_run(*, app: FastAPI, run_id: str, stage_name: str, payload: dict) -> None:
    state_path = app.state.stage_run_state_path(run_id)
    root = app.state.project_root

    app.state.active_jobs.add(run_id)
    try:
        request = StageFileRunRequest.model_validate(payload)
        if normalize_stage_name(stage_name) == "refine" and request.secondary_asr_candidate:
            raise StageFileRunError("单 TXT 校对文件模式不支持双 ASR；联合校对请使用完整任务或已完成配对的 CLI 工作区")
        frontend_settings = load_frontend_settings(root)
        normalized_stage_name = normalize_stage_name(stage_name)
        effective_profile = first_text(request.profile, frontend_settings.profile)
        effective_backend = first_text(request.backend, frontend_settings.backend)
        base_loaded_settings = load_settings(
            settings_path=request.config,
            profile_name=effective_profile,
            project_root=root,
        )
        workspace = build_stage_file_workspace(root, run_id)
        place_stage_inputs(
            project_root=root,
            workspace=workspace,
            stage_name=normalized_stage_name,
            input_files=request.input_files,
            loaded_settings=base_loaded_settings,
        )
        write_job_settings(
            project_root=root,
            loaded_settings=base_loaded_settings,
            job_paths=workspace.job_paths,
            profile_name=base_loaded_settings.active_profile_name,
            model_overrides=ModelOverrides(
                asr_candidate=request.asr_candidate or frontend_settings.asr_candidate or None,
                # 文件槽仅接收单份 TXT；明确关闭配置/全局继承，不在联合读取层绕过配对校验。
                secondary_asr_candidate="" if normalized_stage_name == "refine" else secondary_asr_override(request, frontend_settings),
                llm_model=request.model or frontend_settings.model or None,
                llm_reasoning_effort=request.reasoning_effort or frontend_settings.reasoning_effort or None,
                ocr_backend=request.ocr_backend or frontend_settings.ocr_backend or None,
                ocr_model=request.ocr_model or frontend_settings.ocr_model or None,
                ocr_reasoning_effort=request.ocr_reasoning_effort or frontend_settings.ocr_reasoning_effort or None,
                ocr_max_concurrency=request.ocr_max_concurrency,
                ocr_submit_interval_seconds=request.ocr_submit_interval_seconds,
            ),
        )
        workspace_loaded_settings = load_settings(
            settings_path=workspace.job_paths.settings_path,
            project_root=root,
        )
        logger = setup_logging(workspace_loaded_settings.settings.runtime.log_level)
        progress_items: dict[str, dict[str, object]] = {}

        def reference_progress(progress: ReferenceFileProgress) -> None:
            update_reference_progress_state(
                app=app,
                state_path=state_path,
                progress_items=progress_items,
                progress=progress,
            )

        app.state.update_state(
            state_path,
            status="running",
            current_stage=normalized_stage_name,
            request_payload=request_payload_with_effective_ocr_settings(
                request,
                workspace_loaded_settings,
                freeze_asr=normalized_stage_name == "transcribe",
            ),
            **({"asr_candidate": workspace_loaded_settings.settings.asr.candidate or "whisper-existing"} if normalized_stage_name == "transcribe" else {}),
        )
        with codex_lb_environment(frontend_settings):
            exit_code = run_stage(
                normalized_stage_name,
                workspace_loaded_settings,
                logger,
                backend_override=effective_backend,
                prepare_reference_progress_callback=(
                    reference_progress if normalized_stage_name == "prepare-reference" else None
                ),
            )
        if exit_code == STAGE_EXIT_PARTIAL:
            app.state.update_state(
                state_path,
                status="partial",
                current_stage=normalized_stage_name,
                error_message="准备参考 OCR 尚有缺页，请重试缺失页。",
                resumable=True,
            )
            return
        if exit_code != 0:
            raise JobRunnerError(f"文件模式 stage 运行失败: stage={normalized_stage_name} exit_code={exit_code}")

        archive_path = build_stage_result_archive(
            workspace=workspace,
            stage_name=normalized_stage_name,
            result_name=request.result_name,
        )
        app.state.update_state(
            state_path,
            status="success",
            current_stage=normalized_stage_name,
            output_path=str(archive_path),
            download_name=archive_path.name,
            error_message="",
            resumable=False,
        )
    except (ConfigLoadError, JobRunnerError, StageFileRunError, ValueError, OSError) as exc:
        app.state.update_state(
            state_path,
            status="failed",
            error_message=str(exc),
        )
    finally:
        app.state.active_jobs.discard(run_id)


def pdf_book_ocr_source_label(source_pdf: Path, input_path: Path) -> str:
    if input_path.is_dir():
        try:
            return source_pdf.relative_to(input_path).as_posix()
        except ValueError:
            pass
    return source_pdf.name


def serialize_pdf_book_ocr_items(task_paths: PDFBookOCRTaskPaths, input_path: Path, summary) -> list[dict[str, object]]:
    serialized_items: list[dict[str, object]] = []
    for item in summary.items:
        serialized_items.append(
            {
                "source_file": pdf_book_ocr_source_label(item.source_pdf, input_path),
                "output_file": relative_pdf_book_ocr_output_path(task_paths, item.output_text_path) if item.success else "",
                "success": item.success,
                "text_length": item.text_length,
                "warnings": item.warnings,
                "error": item.error or "",
                "page_count": item.page_count,
                "completed_pages": item.completed_pages,
                "failed_pages": len(item.failed_page_numbers),
                "failed_page_numbers": list(item.failed_page_numbers),
                "page_errors": {str(page_number): error for page_number, error in item.page_errors.items()},
                "resumable": not item.success and item.page_count > item.completed_pages,
            }
        )
    return serialized_items


def serialize_pdf_book_ocr_progress(input_path: Path, progress: PDFBookOCRProgress) -> dict[str, object]:
    return {
        "source_file": pdf_book_ocr_source_label(progress.source_pdf, input_path),
        "output_file": "",
        "success": False,
        "text_length": 0,
        "warnings": [],
        "error": "",
        "page_count": progress.page_count,
        "completed_pages": progress.completed_pages,
        "failed_pages": len(progress.failed_page_numbers),
        "failed_page_numbers": list(progress.failed_page_numbers),
        "page_errors": {str(page_number): error for page_number, error in progress.page_errors.items()},
        "resumable": progress.completed_pages < progress.page_count,
    }


def execute_pdf_book_ocr(*, app: FastAPI, task_id: str, payload: dict) -> None:
    root = app.state.project_root
    task_paths = build_pdf_book_ocr_task_paths(root, task_id)
    state_path = task_paths.state_path

    app.state.active_jobs.add(task_id)
    try:
        request = PDFBookOCRRequest.model_validate(payload)
        frontend_settings = load_frontend_settings(root)
        loaded_settings = load_settings(
            settings_path=request.config,
            project_root=root,
        )
        apply_model_overrides(
            loaded_settings,
            ModelOverrides(
                ocr_model=first_text(request.ocr_model, frontend_settings.ocr_model),
                ocr_reasoning_effort=first_text(
                    request.ocr_reasoning_effort,
                    frontend_settings.ocr_reasoning_effort,
                ),
                ocr_max_concurrency=request.ocr_max_concurrency,
                ocr_submit_interval_seconds=request.ocr_submit_interval_seconds,
            ),
        )
        input_path = resolve_uploaded_pdf_ocr_input(root, request.input_path)
        app.state.update_state(
            state_path,
            status="running",
            current_stage="ocr",
            request_payload=request_payload_with_effective_ocr_settings(request, loaded_settings),
            error_message="",
        )

        progress_items: dict[str, dict[str, object]] = {}

        def book_progress(progress: PDFBookOCRProgress) -> None:
            item = serialize_pdf_book_ocr_progress(input_path, progress)
            progress_items[str(item["source_file"])] = item
            items = [progress_items[key] for key in sorted(progress_items)]
            app.state.update_state(
                state_path,
                items=items,
                pages_total=sum(int(entry["page_count"]) for entry in items),
                pages_completed=sum(int(entry["completed_pages"]) for entry in items),
                pages_failed=sum(int(entry["failed_pages"]) for entry in items),
            )

        with codex_lb_environment(frontend_settings):
            summary = ocr_pdf_book_batch(
                input_path,
                task_paths.output_dir,
                loaded_settings,
                checkpoint_root=task_paths.checkpoint_dir,
                progress_callback=book_progress,
            )

        items = serialize_pdf_book_ocr_items(task_paths, input_path, summary)
        pages_total = sum(item.page_count for item in summary.items)
        pages_completed = sum(item.completed_pages for item in summary.items)
        pages_failed = sum(len(item.failed_page_numbers) for item in summary.items)
        if summary.failure_count == 0:
            status = "success"
            error_message = ""
        elif any(item.page_count > 0 for item in summary.items):
            status = "partial"
            error_message = f"OCR 已完成 {pages_completed}/{pages_total} 页；仍有 {pages_failed} 页待重试。"
        elif summary.success_count == 0:
            status = "failed"
            error_message = "所有 PDF OCR 均失败，且未生成可恢复的页检查点。"
        else:
            status = "failed"
            error_message = "部分 PDF OCR 失败，请查看每本书的错误详情并下载已完成的 TXT。"
        app.state.update_state(
            state_path,
            status=status,
            current_stage="done",
            output_path=str(task_paths.output_dir),
            total=len(summary.items),
            success=summary.success_count,
            failed=summary.failure_count,
            pages_total=pages_total,
            pages_completed=pages_completed,
            pages_failed=pages_failed,
            resumable=any(not item.success for item in summary.items),
            items=items,
            error_message=error_message,
        )
    except (ConfigLoadError, PDFBookOCRError, ValueError, OSError) as exc:
        app.state.update_state(
            state_path,
            status="failed",
            error_message=str(exc),
        )
    finally:
        app.state.active_jobs.discard(task_id)
