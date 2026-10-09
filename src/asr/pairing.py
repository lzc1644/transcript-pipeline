"""双 ASR 配对生命周期与完整性证据，由 ASR 批处理层发布。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any


class AsrPairError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pair_manifest_path(asr_dir: Path, basename: str) -> Path:
    return asr_dir / ".pairs" / f"{basename}.json"


def write_pair_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def begin_asr_pair(audio_path: Path, asr_dir: Path, primary: str, secondary: str) -> dict[str, Any]:
    manifest = {
        "schema_version": 1, "status": "pending", "run_id": uuid.uuid4().hex,
        "primary_candidate": primary, "secondary_candidate": secondary,
        "source_audio_sha256": None,
    }
    path = pair_manifest_path(asr_dir, audio_path.stem)
    write_pair_manifest(path, manifest)
    manifest["source_audio_sha256"] = file_sha256(audio_path)
    write_pair_manifest(path, manifest)
    return manifest


def complete_asr_pair(audio_path: Path, asr_dir: Path, manifest: dict[str, Any]) -> None:
    records = {}
    for role, candidate in (("primary", manifest["primary_candidate"]), ("secondary", manifest["secondary_candidate"])):
        directory = asr_dir if role == "primary" else asr_dir / "secondary" / candidate
        json_path = directory / f"{audio_path.stem}.json"
        text_path = json_path.with_suffix(".txt")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        metadata = payload.get("metadata") or {}
        if metadata.get("candidate") != candidate or metadata.get("source_audio_sha256") != manifest["source_audio_sha256"]:
            raise AsrPairError("双 ASR 运行期间音频或候选身份变化，拒绝发布完成配对")
        records[role] = {
            "json_file": str(json_path.relative_to(asr_dir)), "json_sha256": file_sha256(json_path),
            "text_file": str(text_path.relative_to(asr_dir)), "text_sha256": file_sha256(text_path),
        }
    write_pair_manifest(pair_manifest_path(asr_dir, audio_path.stem), {**manifest, **records, "status": "complete"})


def validate_asr_pair(asr_dir: Path, basename: str, primary: str, secondary: str) -> dict[str, Any]:
    path = pair_manifest_path(asr_dir, basename)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1 or manifest.get("status") != "complete":
            raise ValueError("当前配对未完成；不能使用失败运行遗留的旧辅助产物")
        if not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]:
            raise ValueError("配对缺少运行身份")
        if manifest.get("primary_candidate") != primary or manifest.get("secondary_candidate") != secondary:
            raise ValueError("当前配对候选与任务配置不一致")
        for role, directory in (("primary", asr_dir), ("secondary", asr_dir / "secondary" / secondary)):
            record = manifest[role]
            for suffix, key in ((".json", "json"), (".txt", "text")):
                target = directory / f"{basename}{suffix}"
                if record[f"{key}_file"] != str(target.relative_to(asr_dir)) or record[f"{key}_sha256"] != file_sha256(target):
                    raise ValueError(f"{role} ASR 文件与完成配对哈希不一致")
        if not manifest.get("source_audio_sha256"):
            raise ValueError("配对缺少音频 SHA256")
        return manifest
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AsrPairError(f"双 ASR 配对无效: {path} | {exc}") from exc
