"""Compatibility adapters from historical project settings to ASR operations."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.asr import file as file_asr
from src.asr.errors import (
    AsrTranscriptionError, AsrDependencyError, UnsupportedAsrEngineError,
    InvalidAsrDeviceError, CudaUnavailableError, AsrModelLoadError, AudioInputEmptyError,
)
from src.asr.options import AsrResources, FileAsrOptions
from src.asr.pairing import (
    AsrPairError, begin_asr_pair, complete_asr_pair, file_sha256,
    pair_manifest_path, write_pair_manifest,
)
from src.asr.results import (
    AsrBatchItem, AsrFileResult, AsrOutputPaths, AsrSegmentResult, bind_source_audio,
    build_asr_output_paths, check_output_candidate, publish_asr_pair, validate_asr_result, write_asr_result,
)
from src.asr.file import (
    CUDA_RUNTIME_LIBRARY_FILENAMES, CUDA_RUNTIME_PACKAGE_NAMES,
    asr_runtime_versions, asr_workspace_lock, build_cuda_runtime_fix_hint,
    configure_cuda_runtime_from_venv, discover_cuda_runtime_library_dirs,
    find_python_package_dirs, import_faster_whisper_download_model, import_whisper_model_class,
    iter_audio_files, looks_like_cuda_error, normalize_extension, prepend_ld_library_path,
    preload_cuda_runtime_libraries, resolve_cached_faster_whisper_model_path,
    summarize_transcription_results,
)
from src.runtime_utils import ensure_directory
from src.schemas import LoadedSettings


def asr_options_from_settings(loaded: LoadedSettings) -> FileAsrOptions:
    settings, profile = loaded.settings.asr, loaded.active_profile
    return FileAsrOptions(
        model_size=profile.asr_model_size, device=profile.device, compute_type=profile.asr_compute_type,
        candidate=settings.candidate, secondary_candidate=settings.secondary_candidate,
        engine=settings.engine, language=settings.language,
        beam_size=profile.beam_size if profile.beam_size is not None else settings.beam_size,
        vad_filter=settings.vad_filter, condition_on_previous_text=settings.condition_on_previous_text,
        word_timestamps=settings.word_timestamps, initial_prompt=settings.initial_prompt,
        max_new_tokens=settings.max_new_tokens, chunk_seconds=settings.chunk_seconds, terms=tuple(settings.terms),
    )


def asr_resources_from_settings(loaded: LoadedSettings) -> AsrResources:
    from src.asr.registry import worker_python
    return AsrResources(
        cache_dir=loaded.resolve_path(loaded.active_profile.cache_dir), worker_python=worker_python(loaded),
        gpu_lock_path=loaded.project_root / "data/jobs/_asr-gpu.lock", source_root=loaded.project_root,
        whisper_cache_subdir=loaded.settings.asr.model_cache_subdir,
        backend_cache_subdir=loaded.settings.asr.backend_cache_subdir,
    )


def validate_asr_runtime(loaded_settings: LoadedSettings) -> None:
    file_asr.validate_asr_runtime(asr_options_from_settings(loaded_settings))


def load_faster_whisper_model(loaded_settings: LoadedSettings) -> Any:
    return file_asr.load_faster_whisper_model(
        asr_options_from_settings(loaded_settings), asr_resources_from_settings(loaded_settings))


def build_source_file_label(audio_path: Path, loaded_settings: LoadedSettings) -> str:
    return file_asr.build_source_file_label(audio_path, loaded_settings.project_root)


def transcribe_audio_file(audio_path: Path, model: Any, loaded_settings: LoadedSettings,
                          logger: logging.Logger | None = None) -> AsrFileResult:
    return file_asr.transcribe_audio_file(
        audio_path, model, asr_options_from_settings(loaded_settings),
        asr_resources_from_settings(loaded_settings), logger=logger)


def gpu_asr_lock(loaded: LoadedSettings, logger: logging.Logger | None = None):
    return file_asr.gpu_asr_lock(loaded.active_profile.device,
                               loaded.project_root / "data/jobs/_asr-gpu.lock", logger)


def transcribe_optional_backend(audio_files: list[Path], output_dir: Path,
                                loaded: LoadedSettings, logger: logging.Logger | None,
                                lock_fds: tuple[int, ...] = ()) -> list[AsrBatchItem]:
    return file_asr.transcribe_optional_backend(
        audio_files, output_dir, asr_options_from_settings(loaded),
        asr_resources_from_settings(loaded), logger, lock_fds)


def transcribe_candidate_batch(audio_files: list[Path], output_dir: Path,
                               loaded: LoadedSettings, logger: logging.Logger | None,
                               lock_fds: tuple[int, ...]) -> list[AsrBatchItem]:
    return file_asr.transcribe_candidate_batch(
        audio_files, output_dir, asr_options_from_settings(loaded),
        asr_resources_from_settings(loaded), logger, lock_fds)


def transcribe_batch(loaded_settings: LoadedSettings, logger: logging.Logger | None = None) -> list[AsrBatchItem]:
    audio_dir = loaded_settings.path_for("audio_dir")
    output_dir = ensure_directory(loaded_settings.path_for("asr_dir"))
    audio_files = iter_audio_files(audio_dir, loaded_settings.settings.audio.supported_audio_ext)
    if not audio_files:
        supported_ext = ", ".join(loaded_settings.settings.audio.supported_audio_ext)
        raise AudioInputEmptyError(
            f"输入目录中没有可处理的音频文件: {audio_dir}。支持扩展名: {supported_ext}")
    return file_asr.transcribe_files(
        audio_files, output_dir, options=asr_options_from_settings(loaded_settings),
        resources=asr_resources_from_settings(loaded_settings), logger=logger)
