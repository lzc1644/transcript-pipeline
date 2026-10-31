"""Historical stage-6 adapters; text processing is owned by src.llm.tasks.refinement."""
from __future__ import annotations

import json
import logging
from dataclasses import fields
from pathlib import Path
from typing import Any

from src.llm.tasks import refinement as _refinement
from src.llm.gateway import GatewayClient, GatewayConnection, connection_from_settings
from src.refine_inputs import DualAsrInputError, load_dual_asr_input
from src.runtime_utils import ensure_directory, relativize_path
from src.schemas import LoadedSettings
from dataclasses import dataclass

from src.llm.tasks.refinement import (
    BACKEND_AGY,
    BACKEND_CODEX,
    BACKEND_CODEX_API,
    BACKEND_FALLBACK,
    BackendDocumentRefinementResult,
    CLIBackendError,
    CLIBackendRetryableError,
    DocumentRefinementResult,
    LEGACY_DUAL_REFINEMENT_BACKENDS,
    MinimalEditBlockResult,
    PROMPT_MISSING_ERROR,
    PreReplacementSegment,
    PromptLoadError,
    QuoteMatchingOptions,
    REFINE_JSON_CONTRACT,
    RefinementBlock,
    RefinementError,
    RefinementInputEmptyError,
    RefinementInputPaths,
    RefinementOptions,
    RefinementOutputValidationError,
    RefinementRuntime,
    TARGET_MINIMAL_EDIT_BLOCKS,
    VALID_REFINEMENT_BACKENDS,
    _split_sentences_with_paragraph_ids,
    build_fallback_document_result,
    build_fulltext_refine_prompt,
    build_markdown_assemble_prompt,
    build_minimal_edit_prompt,
    build_pre_replaced_plain_text,
    build_refinement_blocks,
    build_result_backend_status,
    build_simple_markdown,
    build_single_pass_refine_prompt,
    build_validation_retry_prompt,
    calculate_document_score,
    compare_backend_documents,
    compute_extra_content_ratio,
    contains_any_keyword,
    extract_json_payload,
    find_best_reference_match,
    is_gemini_capacity_error,
    join_sentence_paragraphs,
    load_text_file,
    locked_quotes_preserved,
    map_reference_sentence_paragraphs,
    markdown_to_plain_text,
    normalize_deletion_candidates,
    normalize_for_match,
    normalize_inline_text,
    normalize_multiline_text,
    parse_backend_document_result,
    parse_minimal_edit_result,
    refine_document,
    resolve_backend_statuses,
    resolve_chunk_paragraphs,
    resolve_requested_backends,
    run_subprocess,
    serialize_backend_result,
    split_text_into_paragraphs,
    split_text_into_sentences,
    strip_markdown_fence,
    truncate_for_prompt,
    validate_final_markdown_contract,
    validate_minimal_edit_result,
)


def runtime_from_settings(loaded_settings: LoadedSettings, *, gateway_connection: GatewayConnection | None = None) -> RefinementRuntime:
    if isinstance(loaded_settings, RefinementRuntime):
        return loaded_settings
    llm = loaded_settings.settings.llm
    options = RefinementOptions(**{item.name: (tuple(getattr(llm, item.name)) if item.name == "backends" else getattr(llm, item.name))
                                  for item in fields(RefinementOptions)})
    classification = loaded_settings.settings.classification
    quote_options = QuoteMatchingOptions(**{item.name: tuple(getattr(classification, item.name))
                                           for item in fields(QuoteMatchingOptions)})
    connection = gateway_connection or connection_from_settings(loaded_settings.settings.codex_lb)
    return RefinementRuntime(options, quote_options, loaded_settings.project_root,
                             loaded_settings.path_for("logs_dir"), GatewayClient(connection, timeout_seconds=options.timeout_seconds))


@dataclass(frozen=True)
class RefinementOutputPath:
    json_path: Path


@dataclass(frozen=True)
class RefinementBatchItem:
    basename: str
    output_path: Path | None
    success: bool
    skipped: bool
    selected_backends: list[str]
    reason: str | None = None


@dataclass(frozen=True)
class RefinementBatchSummary:
    total: int
    success: int
    skipped: int
    failed: int
    backends: list[str]
    items: list[RefinementBatchItem]


def iter_asr_text_files(asr_dir: Path) -> list[Path]:
    if not asr_dir.exists():
        return []
    return sorted(path for path in asr_dir.iterdir() if path.is_file() and path.suffix.lower() == ".txt")


def build_refinement_output_path(asr_text_path: Path, output_dir: Path) -> RefinementOutputPath:
    return RefinementOutputPath(json_path=output_dir / f"{asr_text_path.stem}.json")


def build_backend_output_json_path(output_path: RefinementOutputPath, backend: str) -> Path:
    return output_path.json_path.with_name(f"{output_path.json_path.stem}.{backend}.json")


def load_refinement_prompt(loaded_settings: LoadedSettings) -> str:
    prompts = loaded_settings.settings.prompts
    if prompts is None or not prompts.classify_and_correct:
        raise PromptLoadError(PROMPT_MISSING_ERROR)

    prompt_path = loaded_settings.resolve_path(prompts.classify_and_correct)
    if not prompt_path.exists():
        raise PromptLoadError(f"阶段 6 提示词文件不存在: {prompt_path}")

    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PromptLoadError(f"无法读取阶段 6 提示词文件: {prompt_path} | {exc}") from exc


def resolve_refinement_input_paths(loaded_settings: LoadedSettings, asr_text_path: Path) -> RefinementInputPaths:
    basename = asr_text_path.stem
    asr_text_path = loaded_settings.path_for("asr_dir") / f"{basename}.txt"
    reference_text_path = loaded_settings.path_for("extracted_text_dir") / f"{basename}.txt"
    reference_required = loaded_settings.settings.reference.enabled

    if not asr_text_path.exists():
        raise RefinementError(f"阶段 6 缺少对应 ASR 文本文件: {asr_text_path}")
    if reference_required and not reference_text_path.exists():
        raise RefinementError(f"阶段 6 缺少对应参考原文文件: {reference_text_path}")

    try:
        dual_input = load_dual_asr_input(loaded_settings, asr_text_path)
    except DualAsrInputError as exc:
        raise RefinementError(str(exc)) from exc
    return RefinementInputPaths(
        basename=basename,
        asr_text_path=asr_text_path,
        reference_text_path=reference_text_path if reference_required else None,
        dual_asr_input=dual_input,
    )


def load_markdown_assemble_prompt(loaded_settings: LoadedSettings, *, content_type: str | None = None) -> str:
    prompts = loaded_settings.settings.prompts
    prompt_field = "conversation_cleanup" if (content_type or "").strip() == "conversation" else "final_cleanup"
    prompt_value = getattr(prompts, prompt_field, None) if prompts is not None else None
    if prompts is None or not prompt_value:
        raise PromptLoadError(f"缺少阶段 6 Markdown 组装提示词配置，请检查 prompts.{prompt_field}")

    prompt_path = loaded_settings.resolve_path(prompt_value)
    if not prompt_path.exists():
        raise PromptLoadError(f"阶段 6 Markdown 组装提示词文件不存在: {prompt_path}")

    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PromptLoadError(f"无法读取阶段 6 Markdown 组装提示词文件: {prompt_path} | {exc}") from exc


def write_backend_result_file(
    *,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    backend_status: str,
    result: BackendDocumentRefinementResult,
    output_path: Path,
) -> None:
    payload = {
        "source_asr_file": relativize_path(input_paths.asr_text_path, loaded_settings.project_root),
        "source_reference_file": relativize_optional_reference(input_paths, loaded_settings),
        **asr_input_metadata(input_paths, loaded_settings),
        "refinement_backends": [result.backend],
        "backend_status": {result.backend: backend_status},
        "prompt_mode": result.refinement_strategy,
        "selected_backend": result.backend,
        "comparison_summary": "single_backend_result",
        **serialize_backend_result(result),
    }
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def write_refinement_result(
    *,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    active_backends: list[str],
    backend_status: dict[str, str],
    backend_results: list[BackendDocumentRefinementResult],
    selected_result: BackendDocumentRefinementResult | None,
    comparison_summary: str,
    output_path: RefinementOutputPath,
) -> None:
    ensure_directory(output_path.json_path.parent)
    model_results = {result.backend: serialize_backend_result(result) for result in backend_results}
    base_result = selected_result or backend_results[0]
    final_markdown = selected_result.final_markdown if selected_result else ""
    payload = {
        "source_asr_file": relativize_path(input_paths.asr_text_path, loaded_settings.project_root),
        "source_reference_file": relativize_optional_reference(input_paths, loaded_settings),
        **asr_input_metadata(input_paths, loaded_settings),
        "refinement_backends": list(active_backends),
        "backend_status": backend_status,
        "prompt_mode": selected_result.refinement_strategy if selected_result else "manual_selection_required",
        "selected_backend": selected_result.backend if selected_result else None,
        "comparison_summary": comparison_summary,
        "final_markdown": final_markdown,
        "edited_plain_text": base_result.edited_plain_text,
        "refined_full_text": markdown_to_plain_text(final_markdown),
        "refinement_strategy": selected_result.refinement_strategy if selected_result else "manual_selection_required",
        "refinement_reason": selected_result.refinement_reason if selected_result else "manual_selection_required",
        "needs_review_sections": selected_result.needs_review_sections if selected_result else [],
        "refinement_notes": selected_result.refinement_notes if selected_result else ["manual_selection_required"],
        "edit_operations": selected_result.edit_operations if selected_result else [],
        "deletion_candidates": selected_result.deletion_candidates if selected_result else [],
        "fidelity_report": selected_result.fidelity_report if selected_result else {},
        "section_map": selected_result.section_map if selected_result else [],
        "model_results": model_results,
    }
    with output_path.json_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)

    for result in backend_results:
        if result.backend == BACKEND_FALLBACK:
            continue
        write_backend_result_file(
            input_paths=input_paths,
            loaded_settings=loaded_settings,
            backend_status=backend_status.get(result.backend, build_result_backend_status(result)),
            result=result,
            output_path=build_backend_output_json_path(output_path, result.backend),
        )


def refine_batch(
    loaded_settings: LoadedSettings,
    requested_backends: list[str] | None = None,
    logger: logging.Logger | None = None,
    *,
    gateway_connection: GatewayConnection | None = None,
) -> RefinementBatchSummary:
    asr_dir = loaded_settings.path_for("asr_dir")
    output_dir = ensure_directory(loaded_settings.path_for("refined_dir"))
    asr_files = iter_asr_text_files(asr_dir)
    active_backends = list(requested_backends) if requested_backends is not None else list(loaded_settings.settings.llm.backends)

    if not asr_files:
        raise RefinementInputEmptyError(f"ASR 输入目录中没有可处理的 TXT 文件: {asr_dir}")

    markdown_prompt_text = load_markdown_assemble_prompt(loaded_settings)
    runtime = runtime_from_settings(loaded_settings, gateway_connection=gateway_connection)
    items: list[RefinementBatchItem] = []
    success_count = 0

    for asr_text_path in asr_files:
        input_paths = resolve_refinement_input_paths(loaded_settings, asr_text_path)
        asr_full_text = load_text_file(input_paths.asr_text_path, "ASR")
        reference_full_text = (
            load_text_file(input_paths.reference_text_path, "参考原文")
            if input_paths.reference_text_path is not None
            else ""
        )
        document = refine_document(
            asr_full_text, title=input_paths.basename, prompt=markdown_prompt_text,
            logs_dir=runtime.logs_dir, workdir=runtime.workdir, reference_text=reference_full_text,
            options=runtime.options, quote_options=runtime.quote_options, gateway=runtime.gateway,
            backends=active_backends, input_paths=input_paths, logger=logger,
        )
        backend_results, backend_status = document.backend_results, document.backend_status
        selected_result, comparison_summary = document.selected_result, document.comparison_summary
        output_path = build_refinement_output_path(asr_text_path, output_dir)
        write_refinement_result(
            input_paths=input_paths,
            loaded_settings=loaded_settings,
            active_backends=active_backends,
            backend_status=backend_status,
            backend_results=backend_results,
            selected_result=selected_result,
            comparison_summary=comparison_summary,
            output_path=output_path,
        )

        items.append(
            RefinementBatchItem(
                basename=input_paths.basename,
                output_path=output_path.json_path,
                success=True,
                skipped=False,
                selected_backends=[result.backend for result in backend_results],
            )
        )
        success_count += 1
        if logger:
            logger.info(
                "精修完成 | %s | prompt_mode=%s | needs_review=%s | selected_backend=%s",
                input_paths.basename,
                selected_result.refinement_strategy if selected_result else "manual_selection_required",
                len(selected_result.needs_review_sections) if selected_result else 0,
                selected_result.backend if selected_result else "manual_selection_required",
            )

    return RefinementBatchSummary(
        total=len(asr_files),
        success=success_count,
        skipped=0,
        failed=0,
        backends=active_backends,
        items=items,
    )


def summarize_refinement_results(summary: RefinementBatchSummary) -> str:
    return (
        f"total={summary.total}, success={summary.success}, skipped={summary.skipped}, "
        f"failed={summary.failed}, backends={','.join(summary.backends)}"
    )


def asr_input_metadata(
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
) -> dict[str, Any]:
    return _refinement.asr_input_metadata(
        input_paths,
        runtime_from_settings(loaded_settings),
    )


def get_safe_replace_settings(
    loaded_settings: LoadedSettings,
) -> dict[str, float]:
    return _refinement.get_safe_replace_settings(
        runtime_from_settings(loaded_settings),
    )


def is_safe_replace_candidate(
    sentence_text: str,
    reference_text: str,
    *,
    best_score: float,
    margin: float,
    loaded_settings: LoadedSettings,
) -> bool:
    return _refinement.is_safe_replace_candidate(
        sentence_text,
        reference_text,
        best_score=best_score,
        margin=margin,
        runtime=runtime_from_settings(loaded_settings),
    )


def build_pre_replaced_document(
    *,
    asr_full_text: str,
    reference_full_text: str,
    loaded_settings: LoadedSettings,
) -> list[PreReplacementSegment]:
    return _refinement.build_pre_replaced_document(
        asr_full_text=asr_full_text,
        reference_full_text=reference_full_text,
        runtime=runtime_from_settings(loaded_settings),
    )


def run_codex_cli_payload(
    prompt: str,
    loaded_settings: LoadedSettings,
) -> dict[str, Any]:
    return _refinement.run_codex_cli_payload(
        prompt,
        runtime_from_settings(loaded_settings),
    )


def run_codex_api_payload(
    prompt: str,
    loaded_settings: LoadedSettings,
    *,
    request_trace: RequestTrace | None = None,
) -> dict[str, Any]:
    return _refinement.run_codex_api_payload(
        prompt,
        runtime_from_settings(loaded_settings),
        request_trace=request_trace,
    )


def run_codex_api(
    prompt: str,
    loaded_settings: LoadedSettings,
) -> BackendDocumentRefinementResult:
    return _refinement.run_codex_api(
        prompt,
        runtime_from_settings(loaded_settings),
    )


def run_codex_cli(
    prompt: str,
    loaded_settings: LoadedSettings,
) -> BackendDocumentRefinementResult:
    return _refinement.run_codex_cli(
        prompt,
        runtime_from_settings(loaded_settings),
    )


def run_agy_payload(
    prompt: str,
    loaded_settings: LoadedSettings,
) -> dict[str, Any]:
    return _refinement.run_agy_payload(
        prompt,
        runtime_from_settings(loaded_settings),
    )


def run_agy(
    prompt: str,
    loaded_settings: LoadedSettings,
) -> BackendDocumentRefinementResult:
    return _refinement.run_agy(
        prompt,
        runtime_from_settings(loaded_settings),
    )


def run_backend_cli(
    backend: str,
    prompt: str,
    loaded_settings: LoadedSettings,
) -> BackendDocumentRefinementResult:
    return _refinement.run_backend_cli(
        backend,
        prompt,
        runtime_from_settings(loaded_settings),
    )


def run_backend_payload(
    backend: str,
    prompt: str,
    loaded_settings: LoadedSettings,
    *,
    request_trace: RequestTrace | None = None,
) -> dict[str, Any]:
    return _refinement.run_backend_payload(
        backend,
        prompt,
        runtime_from_settings(loaded_settings),
        request_trace=request_trace,
    )


def process_minimal_edit_block(
    *,
    backend: str,
    block: RefinementBlock,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    minimal_edit_prompt_text: str,
    reference_full_text: str,
) -> MinimalEditBlockResult:
    return _refinement.process_minimal_edit_block(
        backend=backend,
        block=block,
        input_paths=input_paths,
        runtime=runtime_from_settings(loaded_settings),
        minimal_edit_prompt_text=minimal_edit_prompt_text,
        reference_full_text=reference_full_text,
    )


def run_two_step_backend_refinement(
    *,
    backend: str,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    minimal_edit_prompt_text: str,
    markdown_prompt_text: str,
    asr_full_text: str,
    reference_full_text: str,
    logger: logging.Logger | None = None,
) -> BackendDocumentRefinementResult:
    return _refinement.run_two_step_backend_refinement(
        backend=backend,
        input_paths=input_paths,
        runtime=runtime_from_settings(loaded_settings),
        minimal_edit_prompt_text=minimal_edit_prompt_text,
        markdown_prompt_text=markdown_prompt_text,
        asr_full_text=asr_full_text,
        reference_full_text=reference_full_text,
        logger=logger,
    )


def run_single_pass_backend_refinement(
    *,
    backend: str,
    fallback_backend: str | None = None,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    markdown_prompt_text: str,
    asr_full_text: str,
    reference_full_text: str,
    logger: logging.Logger | None = None,
    diagnostic_run_id: str | None = None,
    diagnostic_attempt: int = 1,
) -> BackendDocumentRefinementResult:
    return _refinement.run_single_pass_backend_refinement(
        backend=backend,
        fallback_backend=fallback_backend,
        input_paths=input_paths,
        runtime=runtime_from_settings(loaded_settings),
        markdown_prompt_text=markdown_prompt_text,
        asr_full_text=asr_full_text,
        reference_full_text=reference_full_text,
        logger=logger,
        diagnostic_run_id=diagnostic_run_id,
        diagnostic_attempt=diagnostic_attempt,
    )


def run_validated_single_pass_backend_refinement(
    *,
    backend: str,
    fallback_backend: str | None = None,
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
    markdown_prompt_text: str,
    asr_full_text: str,
    reference_full_text: str,
    logger: logging.Logger | None = None,
) -> BackendDocumentRefinementResult:
    return _refinement.run_validated_single_pass_backend_refinement(
        backend=backend,
        fallback_backend=fallback_backend,
        input_paths=input_paths,
        runtime=runtime_from_settings(loaded_settings),
        markdown_prompt_text=markdown_prompt_text,
        asr_full_text=asr_full_text,
        reference_full_text=reference_full_text,
        logger=logger,
    )


def relativize_optional_reference(
    input_paths: RefinementInputPaths,
    loaded_settings: LoadedSettings,
) -> str | None:
    return _refinement.relativize_optional_reference(
        input_paths,
        runtime_from_settings(loaded_settings),
    )
