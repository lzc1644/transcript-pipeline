"""Legacy project-settings adapter for the standalone media capability."""
from __future__ import annotations

import logging

from src.media.conversion import (
    AudioExtractionError, AudioExtractionResult, FfmpegNotFoundError, InputDirectoryEmptyError,
    build_audio_output_path, extract_audio, find_ffmpeg_binary, iter_video_files, normalize_extension,
)
from src.runtime_utils import ensure_directory
from src.schemas import LoadedSettings


def supported_media_extensions(loaded_settings: LoadedSettings) -> set[str]:
    audio = loaded_settings.settings.audio
    return set(audio.supported_video_ext) | set(audio.supported_audio_ext)


def extract_audio_batch(
    loaded_settings: LoadedSettings,
    logger: logging.Logger | None = None,
) -> list[AudioExtractionResult]:
    settings = loaded_settings.settings
    video_dir = loaded_settings.path_for("videos_dir")
    audio_dir = ensure_directory(loaded_settings.path_for("audio_dir"))
    media_extensions = supported_media_extensions(loaded_settings)
    video_files = iter_video_files(video_dir, media_extensions)

    if not video_files:
        supported_ext = ", ".join(sorted(media_extensions))
        raise InputDirectoryEmptyError(
            f"输入目录中没有可处理的视频或音频文件: {video_dir}。支持扩展名: {supported_ext}"
        )

    ffmpeg_binary = find_ffmpeg_binary()
    results: list[AudioExtractionResult] = []

    for video_path in video_files:
        output_path = build_audio_output_path(
            video_path=video_path,
            output_dir=audio_dir,
            output_format=settings.audio.output_format,
        )
        extraction_result = extract_audio(
            video_path=video_path,
            output_path=output_path,
            sample_rate=settings.audio.sample_rate,
            channels=settings.audio.channels,
            overwrite=settings.audio.overwrite,
            ffmpeg_binary=ffmpeg_binary,
            logger=logger,
        )
        results.append(extraction_result)

    return results


def summarize_extraction_results(results: list[AudioExtractionResult]) -> str:
    created = sum(1 for item in results if item.status == "created")
    skipped = sum(1 for item in results if item.status == "skipped")
    total = len(results)
    return f"total={total}, created={created}, skipped={skipped}"
