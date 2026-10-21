from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from src.ffmpeg_utils import supported_media_extensions
from src.job_runner import JobPaths, sanitize_filename_stem
from src.runtime_utils import ensure_directory, normalize_stage_name
from src.schemas import LoadedSettings


class StageFileRunError(ValueError):
    """阶段文件模式的输入、工作区或结果归档无效。"""


@dataclass(frozen=True)
class StageInputSlot:
    key: str
    label: str
    extensions: tuple[str, ...]
    workspace_directory: str


@dataclass(frozen=True)
class StageFileWorkspace:
    run_id: str
    run_root: Path
    workspace_root: Path
    job_paths: JobPaths


def stage_input_upload_root(project_root: Path) -> Path:
    return project_root / "data/uploads/stage-inputs"


def _normalized_stage_name(stage_name: str) -> str:
    try:
        return normalize_stage_name(stage_name)
    except ValueError as exc:
        raise StageFileRunError(str(exc)) from exc


def _reference_extensions(loaded_settings: LoadedSettings) -> tuple[str, ...]:
    reference = loaded_settings.settings.reference
    extensions: list[str] = []
    if reference.allow_txt:
        extensions.append(".txt")
    if reference.allow_md:
        extensions.append(".md")
    if reference.allow_pdf:
        extensions.append(".pdf")
    return tuple(extensions)


def stage_input_slots(stage_name: str, loaded_settings: LoadedSettings) -> tuple[StageInputSlot, ...]:
    normalized_stage = _normalized_stage_name(stage_name)
    slots_by_stage = {
        "extract-audio": (
            StageInputSlot(
                key="video",
                label="视频或音频文件",
                extensions=tuple(sorted(supported_media_extensions(loaded_settings))),
                workspace_directory="input/videos",
            ),
        ),
        "transcribe": (
            StageInputSlot(
                key="audio",
                label="音频文件",
                extensions=tuple(sorted(extension.lower() for extension in loaded_settings.settings.audio.supported_audio_ext)),
                workspace_directory="input/audio",
            ),
        ),
        "prepare-reference": (
            StageInputSlot(
                key="reference",
                label="参考文件",
                extensions=_reference_extensions(loaded_settings),
                workspace_directory="input/reference",
            ),
        ),
        "align": (
            StageInputSlot(
                key="asr_json",
                label="ASR JSON",
                extensions=(".json",),
                workspace_directory="intermediate/asr",
            ),
            StageInputSlot(
                key="reference_txt",
                label="参考 TXT",
                extensions=(".txt",),
                workspace_directory="intermediate/extracted_text",
            ),
        ),
        "classify": (
            StageInputSlot(
                key="aligned_json",
                label="对齐 JSON",
                extensions=(".json",),
                workspace_directory="intermediate/aligned",
            ),
        ),
        "refine": (
            StageInputSlot(
                key="asr_txt",
                label="ASR TXT",
                extensions=(".txt",),
                workspace_directory="intermediate/asr",
            ),
            StageInputSlot(
                key="reference_txt",
                label="参考 TXT",
                extensions=(".txt",),
                workspace_directory="intermediate/extracted_text",
            ),
        ),
        "export-markdown": (
            StageInputSlot(
                key="refined_json",
                label="精修 JSON",
                extensions=(".json",),
                workspace_directory="intermediate/refined",
            ),
        ),
    }
    try:
        return slots_by_stage[normalized_stage]
    except KeyError as exc:
        raise StageFileRunError(f"文件模式不支持阶段: {normalized_stage}") from exc


def get_stage_input_slot(stage_name: str, slot_key: str, loaded_settings: LoadedSettings) -> StageInputSlot:
    normalized_slot = slot_key.strip()
    for slot in stage_input_slots(stage_name, loaded_settings):
        if slot.key == normalized_slot:
            return slot
    raise StageFileRunError(f"阶段 {_normalized_stage_name(stage_name)} 不支持输入槽: {slot_key}")


def sanitize_stage_input_filename(filename: str) -> str:
    raw_name = Path(filename).name.strip()
    suffix = Path(raw_name).suffix.lower()
    if not raw_name or not suffix:
        raise StageFileRunError("上传文件必须包含文件名和扩展名。")
    return f"{sanitize_filename_stem(Path(raw_name).stem)}{suffix}"


def validate_stage_input_filename(stage_name: str, slot_key: str, filename: str, loaded_settings: LoadedSettings) -> str:
    slot = get_stage_input_slot(stage_name, slot_key, loaded_settings)
    safe_filename = sanitize_stage_input_filename(filename)
    suffix = Path(safe_filename).suffix.lower()
    if suffix not in slot.extensions:
        supported = "、".join(slot.extensions)
        raise StageFileRunError(f"{slot.label}不支持 {suffix} 文件。当前支持：{supported}")
    return safe_filename


def build_stage_input_destination(
    *,
    project_root: Path,
    stage_name: str,
    slot_key: str,
    filename: str,
    loaded_settings: LoadedSettings,
) -> Path:
    normalized_stage = _normalized_stage_name(stage_name)
    safe_filename = validate_stage_input_filename(normalized_stage, slot_key, filename, loaded_settings)
    return stage_input_upload_root(project_root) / normalized_stage / slot_key / uuid4().hex / safe_filename


def build_stage_file_workspace(project_root: Path, run_id: str) -> StageFileWorkspace:
    run_root = ensure_directory(project_root / "data/jobs/stage-runs" / run_id)
    workspace_root = ensure_directory(run_root / "workspace")
    input_root = ensure_directory(workspace_root / "input")
    intermediate_root = ensure_directory(workspace_root / "intermediate")
    output_root = ensure_directory(workspace_root / "output")

    input_videos_dir = ensure_directory(input_root / "videos")
    input_audio_dir = ensure_directory(input_root / "audio")
    input_reference_dir = ensure_directory(input_root / "reference")
    intermediate_asr_dir = ensure_directory(intermediate_root / "asr")
    intermediate_ocr_dir = ensure_directory(intermediate_root / "ocr")
    intermediate_extracted_text_dir = ensure_directory(intermediate_root / "extracted_text")
    intermediate_refined_dir = ensure_directory(intermediate_root / "refined")
    ensure_directory(intermediate_root / "chunks")
    ensure_directory(intermediate_root / "aligned")
    ensure_directory(intermediate_root / "classified")
    ensure_directory(output_root / "review")
    output_final_dir = ensure_directory(output_root / "final")
    ensure_directory(output_root / "logs")

    return StageFileWorkspace(
        run_id=run_id,
        run_root=run_root,
        workspace_root=workspace_root,
        job_paths=JobPaths(
            job_id=run_id,
            job_root=workspace_root,
            input_videos_dir=input_videos_dir,
            input_reference_dir=input_reference_dir,
            intermediate_audio_dir=input_audio_dir,
            intermediate_asr_dir=intermediate_asr_dir,
            intermediate_ocr_dir=intermediate_ocr_dir,
            intermediate_extracted_text_dir=intermediate_extracted_text_dir,
            intermediate_refined_dir=intermediate_refined_dir,
            output_final_dir=output_final_dir,
            manifest_path=workspace_root / "manifest.json",
            settings_path=workspace_root / "settings.generated.yaml",
        ),
    )


def _ensure_staged_input_path(project_root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser().resolve()
    allowed_root = stage_input_upload_root(project_root).resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise StageFileRunError("文件模式输入必须来自本次页面上传的暂存文件。") from exc
    if not candidate.is_file():
        raise StageFileRunError(f"暂存输入文件不存在: {candidate}")
    return candidate


def validate_stage_input_files(
    *,
    project_root: Path,
    stage_name: str,
    input_files: dict[str, str],
    loaded_settings: LoadedSettings,
) -> dict[str, Path]:
    slots = stage_input_slots(stage_name, loaded_settings)
    expected_keys = {slot.key for slot in slots}
    actual_keys = set(input_files)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        details: list[str] = []
        if missing:
            details.append(f"缺少输入槽: {', '.join(missing)}")
        if unexpected:
            details.append(f"不支持输入槽: {', '.join(unexpected)}")
        raise StageFileRunError("；".join(details))

    resolved: dict[str, Path] = {}
    for slot in slots:
        source_path = _ensure_staged_input_path(project_root, input_files[slot.key])
        validate_stage_input_filename(stage_name, slot.key, source_path.name, loaded_settings)
        resolved[slot.key] = source_path
    return resolved


def place_stage_inputs(
    *,
    project_root: Path,
    workspace: StageFileWorkspace,
    stage_name: str,
    input_files: dict[str, str],
    loaded_settings: LoadedSettings,
) -> dict[str, Path]:
    slots = stage_input_slots(stage_name, loaded_settings)
    resolved = validate_stage_input_files(
        project_root=project_root,
        stage_name=stage_name,
        input_files=input_files,
        loaded_settings=loaded_settings,
    )
    placed: dict[str, Path] = {}
    for slot in slots:
        source_path = resolved[slot.key]
        destination_dir = ensure_directory(workspace.workspace_root / slot.workspace_directory)
        destination_path = destination_dir / f"source{source_path.suffix.lower()}"
        try:
            shutil.copy2(source_path, destination_path)
        except OSError as exc:
            raise StageFileRunError(f"无法准备{slot.label}: {source_path.name} | {exc}") from exc
        placed[slot.key] = destination_path
    return placed


def _stage_result_directories(workspace: StageFileWorkspace, stage_name: str) -> tuple[Path, ...]:
    normalized_stage = _normalized_stage_name(stage_name)
    job_paths = workspace.job_paths
    directory_map = {
        "extract-audio": (job_paths.intermediate_audio_dir,),
        "transcribe": (job_paths.intermediate_asr_dir,),
        "prepare-reference": (job_paths.intermediate_extracted_text_dir,),
        "align": (workspace.workspace_root / "intermediate/aligned",),
        "classify": (workspace.workspace_root / "intermediate/classified",),
        "refine": (job_paths.intermediate_refined_dir,),
        "export-markdown": (job_paths.output_final_dir,),
    }
    return directory_map[normalized_stage]


def normalize_result_name(result_name: str) -> str:
    raw_name = result_name.strip()
    if raw_name.lower().endswith(".zip"):
        raw_name = raw_name[:-4]
    if not raw_name or raw_name in {".", ".."}:
        raise StageFileRunError("结果名称不能为空。")
    return sanitize_filename_stem(Path(raw_name).name)


def _iter_result_files(directories: Iterable[Path]) -> Iterable[tuple[Path, Path]]:
    for directory in directories:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                yield directory, path


def build_stage_result_archive(
    *,
    workspace: StageFileWorkspace,
    stage_name: str,
    result_name: str,
) -> Path:
    archive_name = f"{normalize_result_name(result_name)}.zip"
    archive_path = ensure_directory(workspace.run_root / "result") / archive_name
    result_files = list(_iter_result_files(_stage_result_directories(workspace, stage_name)))
    if not result_files:
        raise StageFileRunError(f"阶段未生成可下载结果: {_normalized_stage_name(stage_name)}")

    try:
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
            for directory, path in result_files:
                archive.write(path, arcname=f"{directory.name}/{path.relative_to(directory)}")
    except OSError as exc:
        raise StageFileRunError(f"无法生成结果归档: {archive_path} | {exc}") from exc
    return archive_path
