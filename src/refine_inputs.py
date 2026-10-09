"""阶段 6 的同源双 ASR 输入解析；不执行推理，也不预先选择/合并正确措辞。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.asr.pairing import AsrPairError, pair_manifest_path, validate_asr_pair
from src.asr_utils import AsrFileResult, AsrSegmentResult, AsrTranscriptionError, validate_asr_result
from src.runtime_utils import relativize_path
from src.schemas import LoadedSettings


class DualAsrInputError(ValueError):
    pass


def _read_document(json_path: Path, candidate: str) -> dict[str, Any]:
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        metadata = payload.get("metadata") or {}
        actual = metadata.get("candidate")
        if actual is None and payload.get("engine") == "faster-whisper":
            actual = "whisper-existing"
        if actual != candidate:
            raise ValueError(f"候选身份不符: expected={candidate}, actual={actual}")
        result = AsrFileResult(
            **{key: payload[key] for key in ("source_file", "engine", "model_size", "device", "compute_type", "language", "full_text")},
            segments=[AsrSegmentResult(**segment) for segment in payload["segments"]],
            metadata=metadata,
        )
        validate_asr_result(result)
        text = json_path.with_suffix(".txt").read_text(encoding="utf-8").strip()
        if not text or text != result.full_text.strip():
            raise ValueError("JSON/TXT 内容不一致或转录为空")
        if not result.source_file.strip():
            raise ValueError("ASR 缺少来源音频身份")
        return payload
    except (OSError, ValueError, KeyError, TypeError, AttributeError, AsrTranscriptionError) as exc:
        raise DualAsrInputError(f"双 ASR 输入无效或缺失: {json_path} | {exc}") from exc


def _time_label(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


@dataclass(frozen=True)
class DualAsrInput:
    primary_path: Path
    secondary_path: Path
    primary_candidate: str
    secondary_candidate: str
    primary: dict[str, Any]
    secondary: dict[str, Any]
    pair_run_id: str

    def render(self) -> str:
        # 按 start 放入共同分钟窗，每个原始段恰好出现一次；不按行号或字数硬对齐。
        windows: dict[int, dict[str, list[str]]] = {}
        for label, payload in (("A", self.primary), ("B", self.secondary)):
            for segment in payload["segments"]:
                index = int(segment["start"] // 60)
                windows.setdefault(index, {}).setdefault(label, []).append(segment["text"])
        lines = [
            f"转录 A：{self.primary_candidate} / {self.primary['model_size']}",
            f"转录 B：{self.secondary_candidate} / {self.secondary['model_size']}",
            "时间窗只作粗定位，段落按起点归入窗口，可能跨窗；两份段号和时间戳不必一致。",
        ]
        for index, sources in sorted(windows.items()):
            lines.append(f"\n[WINDOW {index + 1:03d}] {_time_label(index * 60)}–{_time_label((index + 1) * 60)}")
            for label in ("A", "B"):
                lines.append(f"[ASR {label}]\n" + ("\n".join(sources.get(label, [])) or "（该候选此窗没有转录，不能据此认定无讲话）"))
        return "\n".join(lines)

    def provenance(self, project_root: Path) -> dict[str, Any]:
        entries = []
        for candidate, path, payload in (
            (self.primary_candidate, self.primary_path, self.primary),
            (self.secondary_candidate, self.secondary_path, self.secondary),
        ):
            text = payload["full_text"]
            entries.append({
                "candidate": candidate,
                "json_file": relativize_path(path, project_root),
                "text_file": relativize_path(path.with_suffix(".txt"), project_root),
                "text_chars": len(text),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "source_audio_sha256": (payload.get("metadata") or {}).get("source_audio_sha256"),
            })
        return {"asr_input_mode": "dual_asr", "source_asr_files": entries,
                "asr_pair_run_id": self.pair_run_id,
                "asr_pair_manifest": relativize_path(pair_manifest_path(self.primary_path.parent, self.primary_path.stem), project_root)}


def load_dual_asr_input(loaded: LoadedSettings, asr_text_path: Path) -> DualAsrInput | None:
    secondary = loaded.settings.asr.secondary_candidate
    if not secondary:
        return None
    primary = loaded.settings.asr.candidate or "whisper-existing"
    if primary == secondary:
        raise DualAsrInputError("第二 ASR 候选必须与主候选不同")
    primary_path = asr_text_path.with_suffix(".json")
    secondary_path = asr_text_path.parent / "secondary" / secondary / primary_path.name
    try:
        pair = validate_asr_pair(asr_text_path.parent, asr_text_path.stem, primary, secondary)
        first = _read_document(primary_path, primary)
        second = _read_document(secondary_path, secondary)
        if any((payload.get("metadata") or {}).get("source_audio_sha256") != pair["source_audio_sha256"]
               for payload in (first, second)):
            raise DualAsrInputError("双 ASR 来源音频 SHA256 与完成配对不一致")
        current_pair = validate_asr_pair(asr_text_path.parent, asr_text_path.stem, primary, secondary)
        if current_pair["run_id"] != pair["run_id"]:
            raise DualAsrInputError("读取期间双 ASR 配对发生变化，请稍后重试")
    except AsrPairError as exc:
        raise DualAsrInputError(str(exc)) from exc
    return DualAsrInput(primary_path, secondary_path, primary, secondary, first, second, pair["run_id"])
