from __future__ import annotations

import json

import pytest

from api_server import create_app
from src.codex_lb_client import CodexLBClient
from src.config_loader import load_settings
from src.pdf_ocr_workflow import build_pdf_ocr_run_identity
from src.reference_utils import run_codex_api_pdf_ocr
from src.refine_utils import run_codex_api_payload
from src.web.frontend_settings import FrontendSettingsUpdate, frontend_settings_response, save_frontend_settings
from src.web.state_store import create_initial_state, read_json_file, write_json_file
from src.web.tasks import execute_single_job, run_job_rerun
from tests.helpers import write_minimal_settings
from tests.test_api_server import request_json


@pytest.mark.parametrize("fast,ocr_fast", [(False, False), (True, False), (False, True), (True, True)])
def test_request_fast_modes_are_independent(tmp_path, monkeypatch, fast, ocr_fast):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    assert loaded.settings.llm.fast_mode is False
    assert loaded.settings.reference.codex_ocr_fast_mode is False
    loaded.settings.llm.fast_mode = fast
    source = tmp_path / "book.pdf"
    source.write_bytes(b"pdf fixture")
    identity = build_pdf_ocr_run_identity(source, loaded)
    loaded.settings.reference.codex_ocr_fast_mode = ocr_fast
    # 调度档位不使已成功页的内容检查点失效。
    assert build_pdf_ocr_run_identity(source, loaded) == identity
    captured = {}

    def refine_request(self, payload, **kwargs):
        captured["refine"] = payload
        return json.dumps({"final_markdown": "# 校对稿\n\n正文。"})

    def ocr_request(self, payload, **kwargs):
        captured["ocr"] = payload
        return "页面正文。"

    monkeypatch.setattr(CodexLBClient, "codex_responses_text", refine_request)
    monkeypatch.setattr(CodexLBClient, "responses_stream_text", ocr_request)
    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda path: 1)
    monkeypatch.setattr("src.reference_utils.render_pdf_page_as_png_data_url", lambda *args: "data:image/png;base64,fixture")
    run_codex_api_payload("校对正文", loaded)
    run_codex_api_pdf_ocr(source, loaded)
    for stage, enabled, model, effort in (
        ("refine", fast, loaded.settings.llm.model, loaded.settings.llm.reasoning_effort),
        ("ocr", ocr_fast, loaded.settings.reference.codex_ocr_model, loaded.settings.reference.codex_ocr_reasoning_effort),
    ):
        payload = captured[stage]
        assert payload.get("service_tier") == ("priority" if enabled else None)
        assert payload["model"] == model and payload["reasoning"]["effort"] == effort
        assert payload["stream"] is True and payload["store"] is False


def test_saved_defaults_and_explicit_false_override_config(tmp_path):
    write_minimal_settings(tmp_path, llm_overrides={"fast_mode": True}, reference_overrides={"codex_ocr_fast_mode": True})
    defaults = frontend_settings_response(tmp_path)
    assert defaults["fast_mode"] is True and defaults["ocr_fast_mode"] is True
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(fast_mode=False, ocr_fast_mode=True))
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(model="custom-model"))
    defaults = frontend_settings_response(tmp_path)
    assert defaults["fast_mode"] is False and defaults["ocr_fast_mode"] is True


def test_stage_api_inherits_defaults_but_keeps_explicit_false(tmp_path, monkeypatch):
    write_minimal_settings(tmp_path)
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(fast_mode=True, ocr_fast_mode=True))
    app = create_app(project_root=tmp_path, run_tasks_inline=True)
    seen = []

    def stage(name, loaded, *args, **kwargs):
        seen.append((loaded.settings.llm.fast_mode, loaded.settings.reference.codex_ocr_fast_mode))
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", stage)
    for payload, expected in (({}, (True, True)), ({"fast_mode": False, "ocr_fast_mode": False}, (False, False))):
        response = request_json(app, "POST", "/api/stages/refine", json_body=payload)
        assert response.status_code == 202
        state = request_json(app, "GET", f"/api/stage-runs/{response.json()['run_id']}").json()
        assert state["status"] == "success"
        assert (state["request_payload"]["fast_mode"], state["request_payload"]["ocr_fast_mode"]) == expected
    assert seen == [(True, True), (False, False)]


def test_task_snapshot_and_rerun_ignore_changed_global_fast_defaults(tmp_path, monkeypatch):
    write_minimal_settings(tmp_path)
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(fast_mode=True, ocr_fast_mode=False))
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"video fixture")
    reference = tmp_path / "lecture.txt"
    reference.write_text("参考原文。", encoding="utf-8")
    app = create_app(project_root=tmp_path)
    path = app.state.job_state_path("fast-mode-snapshot")
    write_json_file(path, create_initial_state("fast-mode-snapshot", "job"))
    seen = []

    def stage(name, loaded, *args, **kwargs):
        seen.append((loaded.settings.llm.fast_mode, loaded.settings.reference.codex_ocr_fast_mode))
        if name == "export-markdown":
            final = loaded.path_for("final_dir")
            final.mkdir(parents=True, exist_ok=True)
            (final / "source.md").write_text("# 测试\n\n正文。", encoding="utf-8")
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", stage)
    execute_single_job(app=app, job_id="fast-mode-snapshot", payload={
        "video": str(video), "reference": str(reference), "output_dir": str(tmp_path / "final"),
        "fast_mode": False, "ocr_fast_mode": True,
    })
    assert read_json_file(path)["status"] == "success"
    snapshot = load_settings(settings_path=path.parent / "settings.generated.yaml", project_root=tmp_path)
    assert snapshot.settings.llm.fast_mode is False and snapshot.settings.reference.codex_ocr_fast_mode is True
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(fast_mode=True, ocr_fast_mode=False))
    state = run_job_rerun(app=app, job_id="fast-mode-snapshot", payload={"start_stage": "refine"}, state_path=path)
    assert state["status"] == "success"
    assert all(flags == (False, True) for flags in seen)
