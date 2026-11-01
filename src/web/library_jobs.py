"""网页单任务的书库快照准备；旧上传、CLI 和批量入口不经过这里。"""
from __future__ import annotations

from pathlib import Path

from src.config_loader import load_settings
from src.library_reference import LIBRARY_REFERENCE_RULES
from src.web.frontend_settings import load_frontend_settings
from src.web.state_store import read_json_file, write_json_file


def prepare_library_job(app, job_id: str, request) -> tuple[dict, dict]:
    root = app.state.project_root
    frontend = load_frontend_settings(root)
    profile = (request.profile or "").strip() or frontend.profile or None
    loaded = load_settings(settings_path=request.config, profile_name=profile, project_root=root)
    original_prompt = (request.refine_prompt or "").strip()
    if not original_prompt:
        prompt_path = loaded.resolve_path(loaded.settings.prompts.final_cleanup)
        original_prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not original_prompt:
        raise ValueError("本任务的校对提示词为空。")

    job_root = root / "data/jobs" / job_id
    snapshot = app.state.library.snapshot_for_job(request.library_material_revision_id, job_root)
    # 文件由服务端固定命名，后续执行和重跑不再查询书库。
    attachment_root = job_root / "attachments/library"
    snapshot = {**snapshot, "revision_id": request.library_material_revision_id,
                "txt_path": str(attachment_root / "reference.txt"),
                "pdf_path": str(attachment_root / "reference.pdf"),
                "sources_path": str(attachment_root / "sources.json")}
    write_json_file(attachment_root / "snapshot.json", snapshot)
    payload = request.model_dump()
    payload.update(profile=loaded.active_profile_name, config=str(loaded.settings_path),
                   refine_prompt=f"{original_prompt}\n\n{LIBRARY_REFERENCE_RULES}")
    return payload, snapshot


def library_job_snapshot(job_root: Path) -> dict:
    return read_json_file(job_root / "attachments/library/snapshot.json")
