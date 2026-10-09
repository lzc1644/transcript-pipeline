from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ArtifactKind = Literal["file", "json-field"]


@dataclass(frozen=True)
class ArtifactEntry:
    id: str
    stage: str
    label: str
    path: Path
    kind: ArtifactKind
    content_type: str
    json_field: str = ""


def _artifact_payload(entry: ArtifactEntry) -> dict[str, object]:
    exists = entry.path.exists()
    return {
        "id": entry.id,
        "stage": entry.stage,
        "label": entry.label,
        "path": str(entry.path),
        "exists": exists,
        "size": entry.path.stat().st_size if exists and entry.path.is_file() else 0,
        "content_type": entry.content_type,
    }


def _main_artifacts(job_root: Path) -> list[ArtifactEntry]:
    return [
        ArtifactEntry(
            id="transcribe-text",
            stage="transcribe",
            label="语音转写文本",
            path=job_root / "intermediate/asr/source.txt",
            kind="file",
            content_type="text",
        ),
        ArtifactEntry(
            id="transcribe-json",
            stage="transcribe",
            label="语音转写 JSON",
            path=job_root / "intermediate/asr/source.json",
            kind="file",
            content_type="json",
        ),
        ArtifactEntry(
            id="reference-text",
            stage="prepare-reference",
            label="参考文本提取结果",
            path=job_root / "intermediate/extracted_text/source.txt",
            kind="file",
            content_type="text",
        ),
        ArtifactEntry(
            id="reference-json",
            stage="prepare-reference",
            label="参考文本提取 JSON",
            path=job_root / "intermediate/extracted_text/source.json",
            kind="file",
            content_type="json",
        ),
        ArtifactEntry(
            id="refine-markdown",
            stage="refine",
            label="校对后 Markdown",
            path=job_root / "intermediate/refined/source.json",
            kind="json-field",
            content_type="markdown",
            json_field="final_markdown",
        ),
        ArtifactEntry(
            id="refine-text",
            stage="refine",
            label="校对后纯文本",
            path=job_root / "intermediate/refined/source.json",
            kind="json-field",
            content_type="text",
            json_field="refined_full_text",
        ),
        ArtifactEntry(
            id="refine-json",
            stage="refine",
            label="校对润色 JSON",
            path=job_root / "intermediate/refined/source.json",
            kind="file",
            content_type="json",
        ),
        ArtifactEntry(
            id="refine-diagnostics",
            stage="refine",
            label="阶段 6 调用诊断",
            path=job_root / "output/logs/refine/diagnostics.json",
            kind="file",
            content_type="json",
        ),
        ArtifactEntry(
            id="final-markdown",
            stage="export-markdown",
            label="最终 Markdown",
            path=job_root / "output/final/source.md",
            kind="file",
            content_type="markdown",
        ),
    ]


def _debug_artifacts(job_root: Path) -> list[ArtifactEntry]:
    return [
        ArtifactEntry(
            id="align-json",
            stage="align",
            label="文本对齐 JSON",
            path=job_root / "intermediate/aligned/source.json",
            kind="file",
            content_type="json",
        ),
        ArtifactEntry(
            id="classify-json",
            stage="classify",
            label="段落分类 JSON",
            path=job_root / "intermediate/classified/source.json",
            kind="file",
            content_type="json",
        ),
    ]


def _ocr_artifacts(job_root: Path) -> list[ArtifactEntry]:
    ocr_dir = job_root / "intermediate/ocr"
    if not ocr_dir.exists():
        return []
    entries: list[ArtifactEntry] = []
    for index, path in enumerate(sorted(ocr_dir.glob("*.txt")), start=1):
        entries.append(
            ArtifactEntry(
                id=f"ocr-text-{index}",
                stage="prepare-reference",
                label=f"OCR 文本 {path.name}",
                path=path,
                kind="file",
                content_type="text",
            )
        )
    return entries


def _secondary_asr_artifacts(job_root: Path) -> list[ArtifactEntry]:
    entries = []
    asr_dir = job_root / "intermediate/asr"
    for path in sorted((asr_dir / "secondary").glob("*/source.*")):
        if path.suffix not in {".json", ".txt"}:
            continue
        candidate = path.parent.name
        kind = "json" if path.suffix == ".json" else "text"
        entries.append(ArtifactEntry(
            id=f"transcribe-secondary-{candidate}-{kind}", stage="transcribe",
            label=f"第二 ASR {candidate} {'JSON' if kind == 'json' else '文本'}",
            path=path, kind="file", content_type=kind,
        ))
    manifest = asr_dir / ".pairs/source.json"
    if manifest.exists():
        entries.append(ArtifactEntry(id="transcribe-pair", stage="transcribe", label="双 ASR 配对状态",
                                     path=manifest, kind="file", content_type="json"))
    return entries


def collect_job_artifacts(project_root: Path, job_id: str) -> list[dict[str, object]]:
    job_root = project_root / "data/jobs" / job_id
    entries = _main_artifacts(job_root) + _ocr_artifacts(job_root) + _secondary_asr_artifacts(job_root)
    return [_artifact_payload(entry) for entry in entries]


def _find_artifact(project_root: Path, job_id: str, artifact_id: str) -> ArtifactEntry | None:
    job_root = project_root / "data/jobs" / job_id
    for entry in _main_artifacts(job_root) + _debug_artifacts(job_root) + _ocr_artifacts(job_root) + _secondary_asr_artifacts(job_root):
        if entry.id == artifact_id:
            return entry
    return None


def read_job_artifact(project_root: Path, job_id: str, artifact_id: str) -> dict[str, object] | None:
    entry = _find_artifact(project_root, job_id, artifact_id)
    if entry is None or not entry.path.exists() or not entry.path.is_file():
        return None

    if entry.kind == "file":
        content = entry.path.read_text(encoding="utf-8")
    else:
        payload = json.loads(entry.path.read_text(encoding="utf-8"))
        raw_content = payload.get(entry.json_field, "") if isinstance(payload, dict) else ""
        content = raw_content if isinstance(raw_content, str) else json.dumps(raw_content, ensure_ascii=False, indent=2)

    return {
        **_artifact_payload(entry),
        "content": content,
    }
