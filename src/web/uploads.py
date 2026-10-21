from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal
from uuid import uuid4

from fastapi import HTTPException, Request

from src.config_loader import LoadedSettings
from src.ffmpeg_utils import supported_media_extensions
from src.job_runner import sanitize_filename_stem, supported_reference_extensions

UploadKind = Literal["video", "reference", "manifest", "glossary", "pdf_ocr"]

MANIFEST_EXTENSIONS = {".json", ".yaml", ".yml"}
GLOSSARY_EXTENSIONS = {".txt", ".md"}
PDF_OCR_EXTENSIONS = {".pdf"}
UPLOAD_KIND_DIRS: dict[str, str] = {
    "video": "videos",
    "reference": "reference",
    "manifest": "manifests",
    "glossary": "glossaries",
    "pdf_ocr": "pdf-ocr",
}


def upload_root(project_root: Path) -> Path:
    return project_root / "data/uploads"


def upload_allowed_extensions(kind: UploadKind, loaded_settings: LoadedSettings) -> set[str]:
    if kind == "video":
        return supported_media_extensions(loaded_settings)
    if kind == "reference":
        return set(supported_reference_extensions())
    if kind == "manifest":
        return MANIFEST_EXTENSIONS
    if kind == "glossary":
        return GLOSSARY_EXTENSIONS
    if kind == "pdf_ocr":
        return PDF_OCR_EXTENSIONS
    raise HTTPException(status_code=400, detail=f"不支持的上传类型: {kind}")


def sanitize_upload_filename(filename: str) -> str:
    raw_name = Path(filename).name.strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="上传文件缺少文件名。")

    raw_path = Path(raw_name)
    stem = sanitize_filename_stem(raw_path.stem)
    suffix = raw_path.suffix.lower()
    return f"{stem}{suffix}"


def sanitize_upload_directory_part(part: str) -> str:
    sanitized = sanitize_filename_stem(part)
    return sanitized.strip(". ") or "folder"


def sanitize_relative_upload_path(relative_path: str | None, fallback_filename: str) -> Path:
    if not relative_path:
        return Path(sanitize_upload_filename(fallback_filename))

    parts = [part for part in PurePosixPath(relative_path).parts if part not in {"", ".", ".."}]
    if not parts:
        return Path(sanitize_upload_filename(fallback_filename))

    safe_parts = [sanitize_upload_directory_part(part) for part in parts[:-1]]
    safe_parts.append(sanitize_upload_filename(parts[-1]))
    return Path(*safe_parts)


def build_upload_destination(
    *,
    project_root: Path,
    kind: UploadKind,
    filename: str,
    allowed_extensions: Iterable[str],
    group_id: str | None = None,
    relative_path: str | None = None,
) -> Path:
    if kind not in UPLOAD_KIND_DIRS:
        raise HTTPException(status_code=400, detail=f"不支持的上传类型: {kind}")

    safe_filename = sanitize_upload_filename(filename)
    suffix = Path(safe_filename).suffix.lower()
    normalized_extensions = {extension.lower() for extension in allowed_extensions}
    if suffix not in normalized_extensions:
        supported = "、".join(sorted(normalized_extensions))
        raise HTTPException(status_code=400, detail=f"不支持的上传文件类型: {suffix or '无扩展名'}。当前支持：{supported}")

    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    upload_id = sanitize_upload_directory_part(group_id) if group_id else uuid4().hex
    safe_relative_path = sanitize_relative_upload_path(relative_path, safe_filename)
    return upload_root(project_root) / UPLOAD_KIND_DIRS[kind] / date_part / upload_id / safe_relative_path


def upload_group_path(destination: Path, group_id: str | None, relative_path: str | None) -> Path:
    if not group_id:
        return destination.parent

    if not relative_path:
        return destination.parent

    parts = [part for part in PurePosixPath(relative_path).parts if part not in {"", ".", ".."}]
    if len(parts) <= 1:
        return destination.parent
    return destination.parents[len(parts) - 2]


async def save_upload_request(request: Request, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    try:
        with destination.open("xb") as file:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total_bytes += len(chunk)
                file.write(chunk)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存上传文件失败: {destination} | {exc}") from exc

    if total_bytes == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="上传文件为空，无法作为流水线输入。")
    return total_bytes
