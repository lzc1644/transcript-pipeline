"""File/long-audio ASR, with explicit parameters and resource ownership.

A batch keeps one Whisper model (or one optional-backend worker) per candidate.
Workspace and GPU lock descriptors are inherited by optional workers.
"""
from __future__ import annotations

import ctypes
import importlib.util
import importlib.metadata
import json
import logging
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from src.asr.errors import (
    AsrTranscriptionError, AsrDependencyError, UnsupportedAsrEngineError,
    InvalidAsrDeviceError, CudaUnavailableError, AsrModelLoadError, AudioInputEmptyError,
)
from src.asr.options import AsrResources, FileAsrOptions
from src.asr.pairing import AsrPairError, begin_asr_pair, complete_asr_pair, pair_manifest_path, write_pair_manifest
from src.asr.results import (
    AsrSegmentResult, AsrFileResult, AsrOutputPaths, AsrBatchItem, bind_source_audio,
    build_asr_output_paths, check_output_candidate, validate_asr_result, write_asr_result,
)
from src.runtime_utils import ensure_directory

CUDA_RUNTIME_PACKAGE_NAMES = ("nvidia.cublas.lib", "nvidia.cudnn.lib")
CUDA_RUNTIME_LIBRARY_FILENAMES = ("libcublas.so.12", "libcublasLt.so.12", "libcudnn.so.9")


def normalize_extension(extension: str) -> str:
    normalized = extension.strip().lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized


def iter_audio_files(audio_dir: Path, allowed_extensions: Iterable[str]) -> list[Path]:
    normalized_extensions = {normalize_extension(extension) for extension in allowed_extensions}
    if not audio_dir.exists():
        return []

    return sorted(
        path
        for path in audio_dir.iterdir()
        if path.is_file() and path.suffix.lower() in normalized_extensions
    )


def validate_asr_runtime(options: FileAsrOptions) -> None:
    engine = options.engine.strip().lower()
    device = options.device.strip().lower()

    if engine != "faster-whisper":
        raise UnsupportedAsrEngineError(
            f"当前阶段仅支持 faster-whisper，配置值为: {options.engine}"
        )

    if device not in {"cpu", "cuda"}:
        raise InvalidAsrDeviceError(
            f"无效的 ASR device 配置: {options.device}. 仅支持 cpu 或 cuda。"
        )


def import_whisper_model_class() -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AsrDependencyError(
            "未安装 faster-whisper。请先执行 `pip install -r requirements.txt`。"
        ) from exc

    return WhisperModel


def import_faster_whisper_download_model() -> Any:
    try:
        from faster_whisper.utils import download_model
    except ImportError as exc:
        raise AsrDependencyError(
            "未安装 faster-whisper。请先执行 `pip install -r requirements.txt`。"
        ) from exc

    return download_model


def resolve_cached_faster_whisper_model_path(model_size: str, download_root: Path) -> Path | None:
    download_model = import_faster_whisper_download_model()

    try:
        model_path = download_model(
            model_size,
            cache_dir=str(download_root),
            local_files_only=True,
        )
    except Exception:
        return None

    return Path(model_path)


def find_python_package_dirs(package_name: str) -> list[Path]:
    try:
        spec = importlib.util.find_spec(package_name)
    except ModuleNotFoundError:
        return []
    if spec is None:
        return []

    if spec.submodule_search_locations:
        return [Path(location).resolve() for location in spec.submodule_search_locations]

    if spec.origin:
        return [Path(spec.origin).resolve().parent]

    return []


def discover_cuda_runtime_library_dirs() -> list[Path]:
    library_dirs: list[Path] = []
    seen: set[Path] = set()

    for package_name in CUDA_RUNTIME_PACKAGE_NAMES:
        for directory in find_python_package_dirs(package_name):
            if directory.exists() and directory not in seen:
                library_dirs.append(directory)
                seen.add(directory)

    return library_dirs


def prepend_ld_library_path(library_dirs: Iterable[Path]) -> str:
    existing_value = os.environ.get("LD_LIBRARY_PATH", "")
    existing_parts = [part for part in existing_value.split(":") if part]
    combined_parts: list[str] = []

    for directory in library_dirs:
        candidate = str(directory)
        if candidate not in combined_parts and candidate not in existing_parts:
            combined_parts.append(candidate)

    combined_parts.extend(existing_parts)
    if combined_parts:
        os.environ["LD_LIBRARY_PATH"] = ":".join(combined_parts)

    return os.environ.get("LD_LIBRARY_PATH", "")


def preload_cuda_runtime_libraries(library_dirs: Iterable[Path]) -> None:
    for library_name in CUDA_RUNTIME_LIBRARY_FILENAMES:
        for directory in library_dirs:
            candidate = directory / library_name
            if not candidate.exists():
                continue
            ctypes.CDLL(str(candidate), mode=ctypes.RTLD_GLOBAL)
            break


def configure_cuda_runtime_from_venv() -> list[Path]:
    library_dirs = discover_cuda_runtime_library_dirs()
    if not library_dirs:
        return []

    prepend_ld_library_path(library_dirs)
    preload_cuda_runtime_libraries(library_dirs)
    return library_dirs


def build_cuda_runtime_fix_hint() -> str:
    library_dirs = discover_cuda_runtime_library_dirs()
    if library_dirs:
        joined_dirs = ":".join(str(path) for path in library_dirs)
        return (
            "检测到当前 .venv 已安装 NVIDIA CUDA runtime wheels。"
            f" 若仍失败，请先执行 `export LD_LIBRARY_PATH=\"{joined_dirs}"
            "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\"` 后重试；"
            "如果当前机器没有可用 GPU，也可以改用 `--profile local_cpu_high_accuracy`。"
        )

    return (
        "当前未发现可用的 NVIDIA CUDA runtime wheels。"
        " 如需继续使用 GPU，请在 .venv 中安装 "
        "`nvidia-cublas-cu12` 和 `nvidia-cudnn-cu12==9.*`，"
        "或改用 `--profile local_cpu_high_accuracy`。"
    )


def looks_like_cuda_error(message: str) -> bool:
    normalized = message.lower()
    keywords = ("cuda", "cublas", "cudnn", "driver", "gpu", "curand")
    return any(keyword in normalized for keyword in keywords)


def load_faster_whisper_model(options: FileAsrOptions, resources: AsrResources) -> Any:
    validate_asr_runtime(options)
    model_size = options.model_size
    device = options.device.lower()
    compute_type = options.compute_type
    download_root = resources.whisper_cache_dir
    if device == "cuda":
        cuda_runtime_hint = build_cuda_runtime_fix_hint()
        try:
            configure_cuda_runtime_from_venv()
        except OSError as exc:
            raise CudaUnavailableError(
                "CUDA runtime 预加载失败。"
                f" model_size={model_size}, compute_type={compute_type} | {exc}. "
                f"{cuda_runtime_hint}"
            ) from exc

    WhisperModel = import_whisper_model_class()
    cached_model_path = resolve_cached_faster_whisper_model_path(model_size, download_root)

    try:
        model_ref = model_size
        model_kwargs: dict[str, Any] = {
            "device": device,
            "compute_type": compute_type,
        }
        if cached_model_path is not None:
            model_ref = str(cached_model_path)
        else:
            model_kwargs["download_root"] = str(download_root)

        model = WhisperModel(
            model_ref,
            **model_kwargs,
        )
        model._asr_pipeline_model_reference = str(model_ref)
        return model
    except Exception as exc:
        message = str(exc)
        if device == "cuda" and looks_like_cuda_error(message):
            cuda_runtime_hint = build_cuda_runtime_fix_hint()
            raise CudaUnavailableError(
                "当前 profile 配置为 cuda，但运行环境不可用。"
                f" model_size={model_size}, compute_type={compute_type} | {message}. "
                f"{cuda_runtime_hint}"
            ) from exc

        raise AsrModelLoadError(
            "加载 faster-whisper 模型失败。"
            f" model_size={model_size}, device={device}, compute_type={compute_type} | {message}"
        ) from exc


def build_source_file_label(audio_path: Path, source_root: Path | None = None) -> str:
    if source_root is None:
        return str(audio_path.resolve())
    try:
        return str(audio_path.resolve().relative_to(source_root.resolve()))
    except ValueError:
        return str(audio_path.resolve())


def transcribe_audio_file(
    audio_path: Path,
    model: Any,
    options: FileAsrOptions,
    resources: AsrResources,
    logger: logging.Logger | None = None,
) -> AsrFileResult:
    beam_size = options.beam_size

    try:
        raw_segments, info = model.transcribe(
            str(audio_path),
            language=options.language,
            beam_size=beam_size,
            vad_filter=options.vad_filter,
            condition_on_previous_text=options.condition_on_previous_text,
            word_timestamps=options.word_timestamps,
            initial_prompt=options.initial_prompt or None,
        )
        segments = [
            AsrSegmentResult(
                id=int(segment.id),
                start=float(segment.start),
                end=float(segment.end),
                text=segment.text.strip(),
            )
            for segment in list(raw_segments)
        ]
    except Exception as exc:
        if options.device.lower() == "cuda" and looks_like_cuda_error(str(exc)):
            raise CudaUnavailableError(
                f"CUDA 转录失败: {audio_path.name} | {exc}. {build_cuda_runtime_fix_hint()}"
            ) from exc
        raise AsrTranscriptionError(f"转录失败: {audio_path.name} | {exc}") from exc

    language = getattr(info, "language", options.language) or options.language
    full_text = "\n".join(segment.text for segment in segments if segment.text).strip()
    duration = getattr(info, "duration", None)
    voiced_duration = getattr(info, "duration_after_vad", None)
    if not full_text and options.vad_filter and voiced_duration is not None and voiced_duration > 0:
        raise AsrTranscriptionError("Whisper VAD 检测到语音但转录为空，拒绝覆盖已有成功产物")
    audio_kind = "speech" if full_text else (
        "empty_audio" if duration == 0 else "no_speech_detected" if voiced_duration == 0 else "unclassified_empty_result")

    if logger:
        logger.info("转录完成 | %s | segments=%s", audio_path.name, len(segments))

    return AsrFileResult(
        source_file=build_source_file_label(audio_path, resources.source_root),
        engine=options.engine,
        model_size=options.model_size,
        device=options.device,
        compute_type=options.compute_type,
        language=language,
        segments=segments,
        full_text=full_text,
        metadata={"candidate": "whisper-existing", "model_id": options.model_size,
                  "model_reference": getattr(model, "_asr_pipeline_model_reference", options.model_size),
                  "runtime_versions": asr_runtime_versions(),
                  "timestamp_source": "faster-whisper_segments", "timestamp_granularity": "segment",
                  "duration_seconds": duration, "audio_kind": audio_kind,
                  "duration_after_vad": voiced_duration,
                  "resolved_parameters": {"compute_type": options.compute_type, "beam_size": beam_size,
                      "vad_filter": options.vad_filter, "word_timestamps": options.word_timestamps,
                      "condition_on_previous_text": options.condition_on_previous_text,
                      "initial_prompt": options.initial_prompt},
                  "warnings": (["Whisper 原生尾段略超出音频时长；保留旧时间戳，需人工关注尾部重复/幻觉"]
                      if getattr(info, "duration", None) is not None and segments
                      and max(s.end for s in segments) > info.duration + 0.1 else [])},
    )


def asr_runtime_versions() -> dict[str, str]:
    versions = {"python": sys.version.split()[0]}
    for name in ("faster-whisper", "ctranslate2", "numpy"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions


@contextmanager
def asr_workspace_lock(output_dir: Path):
    import fcntl
    with (output_dir.parent / f"._{output_dir.name}-asr.lock").open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        # Closing releases our reference; an inherited worker reference keeps
        # the lock alive if the parent is killed/cancelled during inference.
        yield file.fileno()


@contextmanager
def gpu_asr_lock(device: str, lock_path: Path | None, logger: logging.Logger | None = None):
    if device.lower() != "cuda":
        yield None
        return
    if lock_path is None:
        raise AsrTranscriptionError("CUDA 文件识别必须明确提供共享 GPU 锁路径 gpu_lock_path")
    import fcntl
    ensure_directory(lock_path.parent)
    with lock_path.open("a") as file:
        if logger:
            logger.info("等待 GPU ASR 进程锁")
        fcntl.flock(file, fcntl.LOCK_EX)
        yield file.fileno()


def transcribe_optional_backend(audio_files: list[Path], output_dir: Path,
                                options: FileAsrOptions, resources: AsrResources, logger: logging.Logger | None,
                                lock_fds: tuple[int, ...] = ()) -> list[AsrBatchItem]:
    from src.asr.registry import get_candidate
    candidate = get_candidate(options.candidate)
    device = options.device.lower()
    if device not in candidate.supported_devices:
        raise InvalidAsrDeviceError(f"候选 {candidate.id} 不支持设备: {device}")
    if options.language != "zh":
        raise AsrTranscriptionError("新增候选首版仅验证中文配置 language=zh；不静默改变语言")
    from src.asr.registry import required_modules
    # The worker runs from the repository root, not the caller's working
    # directory. Anchor its resources here, without resolving the venv symlink.
    interpreter = str(Path(resources.worker_python).expanduser().absolute())
    if not Path(interpreter).is_file():
        raise AsrDependencyError(f"ASR worker Python 不存在: {interpreter}")
    try:
        check = subprocess.run([interpreter, "-c", "import importlib.util,sys; "
                                f"sys.exit(0 if all(importlib.util.find_spec(n) for n in {required_modules(candidate)!r}) else 1)"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AsrDependencyError(f"无法检查 ASR worker Python: {interpreter} | {exc}") from exc
    if check.returncode:
        group = "qwen" if candidate.engine == "qwen-asr" else "funasr"
        raise AsrDependencyError(f"{interpreter} 缺少 {candidate.package} 或其必要依赖；请安装 requirements-asr-{group}.txt，"
                                 "或配置 asr.worker_python 指向已安装依赖的隔离环境")
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import get_speech_timestamps
    import numpy as np
    inputs = []
    for audio_path in audio_files:
        try:
            pcm = decode_audio(str(audio_path), sampling_rate=16000)
            if not len(pcm):
                raise AsrTranscriptionError(f"空音频: {audio_path}")
            if not np.isfinite(pcm).all():
                raise AsrTranscriptionError(f"音频包含非有限 PCM 数值: {audio_path}")
            speech_regions = get_speech_timestamps(pcm)
            voiced = bool(speech_regions)
            kind = "speech" if voiced else ("digital_silence" if not bool(pcm.any()) else "no_speech_detected")
        except Exception as exc:
            raise AsrTranscriptionError(f"ASR 音频/VAD 检查失败: {audio_path.name} | {exc}") from exc
        inputs.append({"path": str(audio_path.absolute()), "source_file": build_source_file_label(audio_path, resources.source_root),
                       "speech_detected": voiced, "audio_kind": kind,
                       "speech_regions_samples": speech_regions})
    terms = list(dict.fromkeys(term.strip() for term in options.terms if term.strip()))
    if sum(len(term) for term in terms) > 4096:
        raise AsrTranscriptionError("结构化 ASR 术语超过首版 4096 字符工程上限；不静默截断")
    warnings = []
    if options.initial_prompt:
        warnings.append("Whisper initial_prompt 不跨模型复制；新候选使用 asr.terms 结构化术语")
    request = {"candidate": candidate.id, "device": device,
               "cache": str(resources.backend_cache_dir.absolute()),
               "parameters": {"max_new_tokens": options.max_new_tokens, "chunk_seconds": options.chunk_seconds},
               "terms": terms, "warnings": warnings, "audio": inputs}
    with tempfile.TemporaryDirectory(prefix="asr-worker-", dir=output_dir) as temporary:
        root = Path(temporary).absolute()
        request_path = root / "request.json"
        response_path = root / "response.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        environment = os.environ.copy()
        whisper_libraries = {str(path) for path in discover_cuda_runtime_library_dirs()}
        environment["LD_LIBRARY_PATH"] = ":".join(
            part for part in environment.get("LD_LIBRARY_PATH", "").split(":")
            if part and part not in whisper_libraries
        )
        process = subprocess.run([interpreter, "-m", "src.asr.worker", "--request", str(request_path),
                                  "--response", str(response_path)], cwd=Path(__file__).resolve().parents[2],
                                 capture_output=True, text=True, env=environment, pass_fds=lock_fds)
        if logger and process.stderr:
            logger.info("ASR worker | %s", process.stderr[-5000:])
        if process.returncode:
            raise AsrTranscriptionError(f"{candidate.id} worker 失败（无模型回退）: {process.stderr[-5000:]}")
        try:
            payloads = json.loads(response_path.read_text(encoding="utf-8"))
            if len(payloads) != len(audio_files):
                raise ValueError("worker 输出数量与输入不一致")
            results = []
            for payload in payloads:
                payload["segments"] = [AsrSegmentResult(**segment) for segment in payload["segments"]]
                result = AsrFileResult(**payload)
                validate_asr_result(result)
                results.append(result)
        except (OSError, ValueError, TypeError) as exc:
            raise AsrTranscriptionError(f"无效 ASR worker 结果: {exc}") from exc
    outputs = []
    for audio_path, result in zip(audio_files, results):
        paths = build_asr_output_paths(audio_path, output_dir)
        write_asr_result(bind_source_audio(result, audio_path), paths)
        outputs.append(AsrBatchItem(audio_path, paths, len(result.segments)))
    return outputs


def transcribe_candidate_batch(audio_files: list[Path], output_dir: Path,
                               options: FileAsrOptions, resources: AsrResources, logger: logging.Logger | None,
                               lock_fds: tuple[int, ...]) -> list[AsrBatchItem]:
    """锁由批处理入口持有；候选推理仍复用 registry 和既有 worker。"""
    from src.asr.registry import get_candidate
    if get_candidate(options.candidate).id != "whisper-existing":
        return transcribe_optional_backend(audio_files, output_dir, options, resources, logger, lock_fds)
    model = load_faster_whisper_model(options, resources)
    try:
        outputs: list[AsrBatchItem] = []
        for audio_path in audio_files:
            result = transcribe_audio_file(audio_path, model, options, resources, logger=logger)
            paths = build_asr_output_paths(audio_path, output_dir)
            write_asr_result(bind_source_audio(result, audio_path), paths)
            outputs.append(AsrBatchItem(audio_path, paths, len(result.segments)))
        return outputs
    finally:
        del model


def summarize_transcription_results(output_files: list[AsrBatchItem]) -> str:
    total = len(output_files)
    total_segments = sum(item.segment_count for item in output_files)
    summary = f"total={total}, json={total}, txt={total}, segments={total_segments}"
    secondary_count = sum(item.secondary_output_paths is not None for item in output_files)
    return f"{summary}, secondary_pairs={secondary_count}" if secondary_count else summary


def transcribe_files(
    audio_paths: Iterable[Path],
    output_dir: Path,
    *,
    options: FileAsrOptions,
    resources: AsrResources,
    logger: logging.Logger | None = None,
) -> list[AsrBatchItem]:
    audio_files = list(audio_paths)
    output_dir = ensure_directory(output_dir)
    if not audio_files:
        raise AudioInputEmptyError("没有可处理的音频文件")

    from src.asr.registry import get_candidate
    try:
        candidate = get_candidate(options.candidate)
        if options.secondary_candidate:
            get_candidate(options.secondary_candidate)
    except ValueError as exc:
        raise UnsupportedAsrEngineError(str(exc)) from exc
    secondary = options.secondary_candidate
    if secondary == candidate.id:
        raise AsrTranscriptionError("第二 ASR 候选必须与主候选不同")
    with asr_workspace_lock(output_dir) as workspace_fd, gpu_asr_lock(options.device, resources.gpu_lock_path, logger) as gpu_fd:
        lock_fds = tuple(fd for fd in [workspace_fd, gpu_fd] if fd is not None)
        if not secondary:
            check_output_candidate(audio_files, output_dir, candidate.id)
            return transcribe_candidate_batch(audio_files, output_dir, options, resources, logger, lock_fds)

        # 一张 GPU 串行运行，两个候选使用独立工作区；主 TXT 仍兼容原下游。
        secondary_options = replace(options, candidate=secondary, secondary_candidate=None)
        secondary_dir = ensure_directory(output_dir / "secondary" / secondary)
        with asr_workspace_lock(secondary_dir) as secondary_fd:
            # 先使旧完成标记失效；辅助失败后即使旧文件仍在也不能用于新配对。
            pairs: dict[Path, dict[str, Any]] = {}
            try:
                for audio in audio_files:
                    pairs[audio] = begin_asr_pair(audio, output_dir, candidate.id, secondary)
                check_output_candidate(audio_files, output_dir, candidate.id)
                check_output_candidate(audio_files, secondary_dir, secondary)
                primary_outputs = transcribe_candidate_batch(
                    audio_files, output_dir, options, resources, logger, lock_fds)
                if logger:
                    logger.info("双 ASR 第二候选开始 | primary=%s | secondary=%s", candidate.id, secondary)
                secondary_outputs = transcribe_candidate_batch(
                    audio_files, secondary_dir, secondary_options, resources, logger, (*lock_fds, secondary_fd))
                if len(primary_outputs) != len(secondary_outputs):
                    raise AsrTranscriptionError("双 ASR 输出数量不一致，拒绝宣称成功")
                for audio in audio_files:
                    complete_asr_pair(audio, output_dir, pairs[audio])
            except (AsrTranscriptionError, AsrPairError, OSError, ValueError) as exc:
                for audio, pair in pairs.items():
                    write_pair_manifest(pair_manifest_path(output_dir, audio.stem),
                                        {**pair, "status": "failed", "error": str(exc)})
                raise AsrTranscriptionError(f"双 ASR 未完成: {exc}") from exc
            return [replace(primary, secondary_output_paths=aux.output_paths)
                    for primary, aux in zip(primary_outputs, secondary_outputs)]


def transcribe_file(
    audio_path: Path, output_dir: Path, *, options: FileAsrOptions,
    resources: AsrResources, logger: logging.Logger | None = None,
) -> AsrBatchItem:
    """Recognize and publish one audio file without a project YAML or job."""
    return transcribe_files([audio_path], output_dir, options=options, resources=resources, logger=logger)[0]
