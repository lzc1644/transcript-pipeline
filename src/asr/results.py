"""ASR data, validation and paired JSON/TXT publication. No models or jobs."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from src.asr.errors import AsrTranscriptionError
from src.asr.pairing import file_sha256
from src.runtime_utils import ensure_directory


@dataclass(frozen=True)
class AsrSegmentResult:
    id: int
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class AsrFileResult:
    source_file: str
    engine: str
    model_size: str
    device: str
    compute_type: str
    language: str
    segments: list[AsrSegmentResult]
    full_text: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class AsrOutputPaths:
    json_path: Path
    txt_path: Path


@dataclass(frozen=True)
class AsrBatchItem:
    source_audio_path: Path
    output_paths: AsrOutputPaths
    segment_count: int
    secondary_output_paths: AsrOutputPaths | None = None


def build_asr_output_paths(audio_path: Path, output_dir: Path) -> AsrOutputPaths:
    return AsrOutputPaths(
        json_path=output_dir / f"{audio_path.stem}.json",
        txt_path=output_dir / f"{audio_path.stem}.txt",
    )


def bind_source_audio(result: AsrFileResult, audio_path: Path) -> AsrFileResult:
    return replace(result, metadata={**(result.metadata or {}), "source_audio_sha256": file_sha256(audio_path)})


def write_asr_result(result: AsrFileResult, output_paths: AsrOutputPaths) -> None:
    ensure_directory(output_paths.json_path.parent)

    payload = {
        "source_file": result.source_file,
        "engine": result.engine,
        "model_size": result.model_size,
        "device": result.device,
        "compute_type": result.compute_type,
        "language": result.language,
        "segments": [asdict(segment) for segment in result.segments],
        "full_text": result.full_text,
    }

    if result.metadata is not None:
        payload["metadata"] = result.metadata
    validate_asr_result(result)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    publish_asr_pair(output_paths, encoded, result.full_text.encode("utf-8"))


def validate_asr_result(result: AsrFileResult) -> None:
    ids: set[int] = set()
    previous_start = -1.0
    duration = (result.metadata or {}).get("duration_seconds")
    for segment in result.segments:
        if segment.id in ids:
            raise AsrTranscriptionError("ASR segment ID 重复")
        ids.add(segment.id)
        if not all(math.isfinite(value) for value in (segment.start, segment.end)):
            raise AsrTranscriptionError("ASR 时间戳不是有限数值")
        if not 0 <= segment.start <= segment.end or segment.start < previous_start:
            raise AsrTranscriptionError("ASR 时间区间或顺序无效")
        tolerance = 2.0 if result.engine == "faster-whisper" else 0.1
        if duration is not None and segment.end > duration + tolerance:
            raise AsrTranscriptionError("ASR 时间戳明显超出音频时长")
        previous_start = segment.start
    if result.full_text != "\n".join(s.text for s in result.segments if s.text).strip():
        raise AsrTranscriptionError("ASR 全文与 segments 不一致")


def publish_asr_pair(paths: AsrOutputPaths, json_bytes: bytes, text_bytes: bytes) -> None:
    targets = [paths.json_path, paths.txt_path]
    temporary: list[Path] = []
    old = [p.read_bytes() if p.exists() else None for p in targets]
    published = 0
    try:
        for target, content in zip(targets, [json_bytes, text_bytes]):
            ensure_directory(target.parent)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as file:
                temporary.append(Path(file.name))
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        for tmp, target in zip(temporary, targets):
            os.replace(tmp, target)
            published += 1
    except OSError as exc:
        for target, previous in zip(targets[:published], old[:published]):
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as restore:
                    restore.write(previous)
                    restore_path = Path(restore.name)
                os.replace(restore_path, target)
        raise AsrTranscriptionError(f"ASR JSON/TXT 发布失败: {exc}") from exc
    finally:
        for tmp in temporary:
            tmp.unlink(missing_ok=True)


def check_output_candidate(audio_files: list[Path], output_dir: Path, candidate: str) -> None:
    for audio in audio_files:
        path = build_asr_output_paths(audio, output_dir).json_path
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            previous = payload.get("metadata", {}).get("candidate")
            if previous is None and payload.get("engine") == "faster-whisper":
                previous = "whisper-existing"
        except (OSError, ValueError, AttributeError) as exc:
            raise AsrTranscriptionError(f"无法确认已有 ASR 产物身份，拒绝覆盖: {path}") from exc
        if previous != candidate:
            raise AsrTranscriptionError("不同 ASR 候选不得覆盖同一输出；请新建任务或独立阶段工作区")
