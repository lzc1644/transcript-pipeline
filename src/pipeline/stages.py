from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from src.align_utils import AlignmentError, align_batch, summarize_alignment_results
from src.asr_utils import AsrTranscriptionError, summarize_transcription_results, transcribe_batch
from src.classify_utils import ClassificationError, classify_batch, summarize_classification_results
from src.export_utils import ExportError, export_markdown_batch, summarize_export_results
from src.ffmpeg_utils import AudioExtractionError, extract_audio_batch, summarize_extraction_results
from src.refine_utils import RefinementError, refine_batch, resolve_requested_backends, summarize_refinement_results
from src.reference_utils import (
    ReferencePreparationError,
    prepare_reference_batch,
    summarize_reference_results,
)

STAGE_EXIT_SUCCESS = 0
STAGE_EXIT_FAILED = 1
STAGE_EXIT_PARTIAL = 2


def log_stage_completion(logger, stage_name: str, summary: str, started_at: float) -> None:
    logger.info(
        "流水线完成 | stage=%s | %s | elapsed=%.3fs",
        stage_name,
        summary,
        time.perf_counter() - started_at,
    )


@dataclass(frozen=True)
class StageOperations:
    extract_audio_batch: Callable
    summarize_extraction_results: Callable
    transcribe_batch: Callable
    summarize_transcription_results: Callable
    prepare_reference_batch: Callable
    summarize_reference_results: Callable
    align_batch: Callable
    summarize_alignment_results: Callable
    classify_batch: Callable
    summarize_classification_results: Callable
    resolve_requested_backends: Callable
    refine_batch: Callable
    summarize_refinement_results: Callable
    export_markdown_batch: Callable
    summarize_export_results: Callable


def default_stage_operations() -> StageOperations:
    return StageOperations(
        extract_audio_batch=extract_audio_batch,
        summarize_extraction_results=summarize_extraction_results,
        transcribe_batch=transcribe_batch,
        summarize_transcription_results=summarize_transcription_results,
        prepare_reference_batch=prepare_reference_batch,
        summarize_reference_results=summarize_reference_results,
        align_batch=align_batch,
        summarize_alignment_results=summarize_alignment_results,
        classify_batch=classify_batch,
        summarize_classification_results=summarize_classification_results,
        resolve_requested_backends=resolve_requested_backends,
        refine_batch=refine_batch,
        summarize_refinement_results=summarize_refinement_results,
        export_markdown_batch=export_markdown_batch,
        summarize_export_results=summarize_export_results,
    )


def run_stage(
    stage_name: str,
    loaded_settings,
    logger,
    backend_override: str | None = None,
    prepare_reference_progress_callback=None,
    *,
    gateway_connection=None,
    operations: StageOperations | None = None,
) -> int:
    operations = operations or default_stage_operations()
    started_at = time.perf_counter()

    if stage_name == "extract-audio":
        try:
            results = operations.extract_audio_batch(loaded_settings, logger=logger)
        except AudioExtractionError as exc:
            logger.error("%s", exc)
            return 1

        log_stage_completion(logger, stage_name, operations.summarize_extraction_results(results), started_at)
        return 0

    if stage_name == "transcribe":
        try:
            outputs = operations.transcribe_batch(loaded_settings, logger=logger)
        except AsrTranscriptionError as exc:
            logger.error("%s", exc)
            return 1

        log_stage_completion(logger, stage_name, operations.summarize_transcription_results(outputs), started_at)
        return 0

    if stage_name == "prepare-reference":
        try:
            summary = operations.prepare_reference_batch(
                loaded_settings,
                logger=logger,
                progress_callback=prepare_reference_progress_callback,
                **({"gateway_connection": gateway_connection} if gateway_connection is not None else {}),
            )
        except ReferencePreparationError as exc:
            logger.error("%s", exc)
            return STAGE_EXIT_FAILED

        log_stage_completion(logger, stage_name, operations.summarize_reference_results(summary), started_at)
        if summary.partial:
            return STAGE_EXIT_PARTIAL
        if summary.failed:
            return STAGE_EXIT_FAILED
        return STAGE_EXIT_SUCCESS

    if stage_name == "align":
        try:
            summary = operations.align_batch(loaded_settings, logger=logger)
        except AlignmentError as exc:
            logger.error("%s", exc)
            return 1

        log_stage_completion(logger, stage_name, operations.summarize_alignment_results(summary), started_at)
        return 0

    if stage_name == "classify":
        try:
            summary = operations.classify_batch(loaded_settings, logger=logger)
        except ClassificationError as exc:
            logger.error("%s", exc)
            return 1

        log_stage_completion(logger, stage_name, operations.summarize_classification_results(summary), started_at)
        return 0

    if stage_name == "refine":
        try:
            requested_backends = operations.resolve_requested_backends(backend_override, loaded_settings.settings.llm.backends)
            summary = operations.refine_batch(loaded_settings, requested_backends=requested_backends, logger=logger,
                **({"gateway_connection": gateway_connection} if gateway_connection is not None else {}))
        except RefinementError as exc:
            logger.error("%s", exc)
            return 1

        log_stage_completion(logger, stage_name, operations.summarize_refinement_results(summary), started_at)
        return 0

    if stage_name == "export-markdown":
        try:
            summary = operations.export_markdown_batch(loaded_settings, logger=logger)
        except ExportError as exc:
            logger.error("%s", exc)
            return 1

        log_stage_completion(logger, stage_name, operations.summarize_export_results(summary), started_at)
        return 0

    logger.error("未实现的阶段: %s", stage_name)
    return 1
