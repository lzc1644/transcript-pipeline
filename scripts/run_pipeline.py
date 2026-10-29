from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import ConfigLoadError, load_settings
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
from src.runtime_utils import normalize_stage_name, setup_logging
from src.settings_overrides import ModelOverrides, SettingsOverrideError, apply_model_overrides

from src.pipeline.stages import (STAGE_EXIT_SUCCESS, STAGE_EXIT_FAILED, STAGE_EXIT_PARTIAL,
                                 StageOperations, log_stage_completion)
from src.pipeline.stages import run_stage as _run_stage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行最小流水线入口。")
    parser.add_argument(
        "--stage",
        required=True,
        help="当前支持 extract-audio、transcribe、prepare-reference、align、classify、refine 或 export-markdown",
    )
    parser.add_argument("--config", help="配置文件路径，默认使用 config/settings.yaml")
    parser.add_argument("--profile", help="运行 profile，覆盖配置文件中的默认 profile")
    parser.add_argument("--asr-candidate", help="ASR 候选 ID，仅转录阶段使用")
    parser.add_argument("--secondary-asr-candidate", help="第二 ASR 候选，转录与校对阶段使用；空字符串关闭")
    parser.add_argument("--backend", choices=["codex_api", "agy", "codex_cli", "both"], help="仅 refine 阶段生效，覆盖后端选择")
    parser.add_argument("--model", help="覆盖 refine 阶段使用的模型，例如 gpt-6.1-sol")
    parser.add_argument("--reasoning-effort", help="覆盖 refine 阶段 reasoning effort，例如 low / medium / high")
    parser.add_argument("--ocr-model", help="覆盖 prepare-reference 阶段 Codex API OCR 使用的模型，例如 gpt-6-luna")
    parser.add_argument("--ocr-reasoning-effort", help="覆盖 prepare-reference 阶段 Codex API OCR reasoning effort，例如 low / medium / high")
    parser.add_argument("--ocr-max-concurrency", type=int, help="覆盖 prepare-reference 阶段 PDF OCR 最大在途请求数")
    parser.add_argument("--ocr-submit-interval-seconds", type=float, help="覆盖 prepare-reference 阶段 PDF OCR 页面投递间隔秒数")
    return parser


def run_stage(stage_name, loaded_settings, logger, backend_override=None,
              prepare_reference_progress_callback=None, *, gateway_connection=None):
    """兼容旧导入和显式替换操作的接缝；执行由 src.pipeline 所有。"""
    return _run_stage(
        stage_name, loaded_settings, logger, backend_override,
        prepare_reference_progress_callback,
        gateway_connection=gateway_connection,
        operations=StageOperations(
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
        ),
    )


def main() -> int:
    args = build_parser().parse_args()

    try:
        stage_name = normalize_stage_name(args.stage)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    try:
        loaded_settings = load_settings(
            settings_path=args.config,
            profile_name=args.profile,
            project_root=PROJECT_ROOT,
        )
    except ConfigLoadError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    try:
        apply_model_overrides(
            loaded_settings,
            ModelOverrides(
                asr_candidate=args.asr_candidate,
                secondary_asr_candidate=args.secondary_asr_candidate,
                llm_model=args.model,
                llm_reasoning_effort=args.reasoning_effort,
                ocr_model=args.ocr_model,
                ocr_reasoning_effort=args.ocr_reasoning_effort,
                ocr_max_concurrency=args.ocr_max_concurrency,
                ocr_submit_interval_seconds=args.ocr_submit_interval_seconds,
            ),
        )
    except SettingsOverrideError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(loaded_settings.settings.runtime.log_level)
    logger.info("流水线启动 | stage=%s | profile=%s", stage_name, loaded_settings.active_profile_name)
    return run_stage(stage_name, loaded_settings, logger, backend_override=args.backend)


if __name__ == "__main__":
    raise SystemExit(main())
