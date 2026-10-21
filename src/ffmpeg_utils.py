from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.runtime_utils import ensure_directory
from src.schemas import LoadedSettings


class AudioExtractionError(RuntimeError):
    """Raised when audio extraction fails."""


class FfmpegNotFoundError(AudioExtractionError):
    """Raised when ffmpeg is not installed."""


class InputDirectoryEmptyError(AudioExtractionError):
    """Raised when there are no matching video files to process."""


@dataclass(frozen=True)
class AudioExtractionResult:
    video_path: Path
    audio_path: Path
    status: str


def normalize_extension(extension: str) -> str:
    normalized = extension.strip().lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized


def supported_media_extensions(loaded_settings: LoadedSettings) -> set[str]:
    audio = loaded_settings.settings.audio
    return set(audio.supported_video_ext) | set(audio.supported_audio_ext)


def iter_video_files(video_dir: Path, allowed_extensions: Iterable[str]) -> list[Path]:
    normalized_extensions = {normalize_extension(extension) for extension in allowed_extensions}
    if not video_dir.exists():
        return []

    return sorted(
        path
        for path in video_dir.iterdir()
        if path.is_file() and path.suffix.lower() in normalized_extensions
    )


def find_ffmpeg_binary() -> str:
    ffmpeg_binary = shutil.which("ffmpeg")
    if ffmpeg_binary is None:
        raise FfmpegNotFoundError(
            "未找到 ffmpeg。请先在系统中安装 ffmpeg，并确保命令 ffmpeg 可在终端直接执行。"
        )
    return ffmpeg_binary


def build_audio_output_path(video_path: Path, output_dir: Path, output_format: str) -> Path:
    return output_dir / f"{video_path.stem}.{output_format.lstrip('.')}"


def extract_audio(
    video_path: Path,
    output_path: Path,
    *,
    sample_rate: int,
    channels: int,
    overwrite: bool,
    ffmpeg_binary: str,
    logger: logging.Logger | None = None,
) -> AudioExtractionResult:
    if output_path.exists() and not overwrite:
        if logger:
            logger.info("跳过已有音频 | %s", output_path.name)
        return AudioExtractionResult(video_path=video_path, audio_path=output_path, status="skipped")

    ensure_directory(output_path.parent)

    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        error_message = result.stderr.strip() or "未知 ffmpeg 错误"
        raise AudioExtractionError(f"音频抽取失败: {video_path.name} | {error_message}")

    if not output_path.exists():
        raise AudioExtractionError(f"音频抽取失败: {video_path.name} | ffmpeg 未生成输出文件。")

    if logger:
        logger.info("音频抽取完成 | %s -> %s", video_path.name, output_path.name)
    return AudioExtractionResult(video_path=video_path, audio_path=output_path, status="created")


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
