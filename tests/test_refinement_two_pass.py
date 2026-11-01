"""Production two-pass contracts, single-pass compatibility and archived previews."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml
from pydantic import ValidationError

from src.config_loader import load_settings
from src.llm.gateway import GatewayClient, GatewayConnection
from src.llm.tasks.refinement import (
    BackendDocumentRefinementResult, CLIBackendError, PromptLoadError, RefinementError,
    RefinementOptions, RefinementOutputValidationError, refine_document,
)
from src.llm.tasks.refinement_two_pass import TERRA_MODEL
from src.refine_utils import refine_batch, runtime_from_settings
from src.schemas import LLMSettings
from src.web.artifacts import collect_job_artifacts, read_job_artifact
from src.web.frontend_settings import FrontendSettings, FrontendSettingsUpdate, frontend_settings_response, save_frontend_settings
from tests.helpers import write_minimal_settings
from tests.test_llm_capabilities import fixed_gateway

ROOT = Path(__file__).resolve().parents[1]
DRAFT = "# 读书会\n\n> 原文甲。\n\n插话：二十五人，若拒绝则停止。\n\n> 原文乙。"
POLISHED = "# 读书会\n\n> 原文甲。\n\n插话：共有二十五人；若拒绝，则停止。\n\n> 原文乙。"
REVIEW = {"excerpt": "某个名字", "reason": "两份输入不一致，待确认"}


def payload(markdown, review=None):
    return {"final_markdown": markdown, "section_map": [], "needs_review_sections": review or [],
            "refinement_notes": ["本轮编辑说明"],
            "deletion_candidates": [{"source_excerpt": "嗯嗯", "reason": "重复起句"}]}


def configured(root):
    # Old single-pass fixtures stay explicit; exercise the real product default by omission.
    path = write_minimal_settings(root, llm_overrides={"model": "gpt-6.1-sol", "reasoning_effort": "medium", "fast_mode": False})
    config = yaml.safe_load(path.read_text())
    config["llm"].pop("refinement_mode")
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    (root / "config/prompts/refinement_rewrite.md").write_text((ROOT / "config/prompts/refinement_rewrite.md").read_text())
    for directory, text in (("asr", "ASR_ONLY_MARKER 原始讲话。"), ("extracted_text", "OCR_ONLY_MARKER 参考。")):
        file = root / "data/intermediate" / directory / "source.txt"
        file.parent.mkdir(parents=True)
        file.write_text(text)
    return load_settings(project_root=root)


def test_product_and_library_defaults_remain_distinct(tmp_path):
    assert LLMSettings().refinement_mode == "terra_terra"
    assert configured(tmp_path).settings.llm.refinement_mode == "terra_terra"
    assert load_settings(project_root=ROOT).settings.llm.refinement_mode == "terra_terra"
    assert RefinementOptions().refinement_mode == "single_pass"


def test_frontend_mode_roundtrip_preserves_single_pass_choices(tmp_path):
    configured(tmp_path)
    assert frontend_settings_response(tmp_path)["refinement_mode"] == "terra_terra"
    for mode in ("single_pass", "terra_terra", "single_pass"):
        save_frontend_settings(tmp_path, FrontendSettingsUpdate(refinement_mode=mode))
        response = frontend_settings_response(tmp_path)
        assert (response["refinement_mode"], response["model"], response["reasoning_effort"], response["fast_mode"]) == (mode, "gpt-6.1-sol", "medium", False)


def test_old_flash_settings_upgrade_without_retaining_a_new_execution_mode():
    assert LLMSettings(refinement_mode="terra_flash").refinement_mode == "terra_terra"
    assert FrontendSettings(refinement_mode="terra_flash", fast_mode=False).refinement_mode == "terra_terra"
    with pytest.raises(ValidationError):
        FrontendSettingsUpdate(refinement_mode="terra_flash")


def test_default_two_pass_owns_parameters_and_preserves_full_draft_and_audit(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    calls = []

    def backend(_backend, prompt, runtime, **kwargs):
        calls.append((runtime.options.model, runtime.options.reasoning_effort, runtime.options.fast_mode))
        assert runtime.options.refinement_validation_retry_count == 0 and not runtime.options.enable_fallback
        assert runtime.gateway.require_stream_completed and runtime.gateway.total_timeout
        if len(calls) == 1:
            assert "ASR_ONLY_MARKER" in prompt and "OCR_ONLY_MARKER" in prompt
            assert "[SEGMENT " not in prompt
            return payload(DRAFT, [REVIEW])
        assert DRAFT in prompt and REVIEW["reason"] in prompt and "本轮编辑说明" in prompt
        assert (ROOT / "config/prompts/refinement_rewrite.md").read_text().strip() in prompt
        assert "ASR_ONLY_MARKER" not in prompt and "OCR_ONLY_MARKER" not in prompt
        assert "# test final cleanup" not in prompt
        return payload(POLISHED)

    monkeypatch.setattr("src.llm.tasks.refinement.run_backend_payload", backend)
    assert refine_batch(settings).success == 1
    assert calls == [(TERRA_MODEL, "high", True), (TERRA_MODEL, "xhigh", True)]
    llm = settings.settings.llm
    assert (llm.model, llm.reasoning_effort, llm.fast_mode) == ("gpt-6.1-sol", "medium", False)
    result = json.loads((settings.path_for("refined_dir") / "source.json").read_text())
    assert result["final_markdown"] == POLISHED
    assert result["prompt_mode"] == result["comparison_summary"] == "terra_terra_two_pass"
    assert result["refinement_reason"] == "draft_then_prose"
    assert result["needs_review_sections"] == [REVIEW] and len(result["deletion_candidates"]) == 2
    assert result["fidelity_report"]["passed"] is None and result["fidelity_report"]["checked"] is False
    for step, role, text in zip(result["refinement_passes"], ("draft", "polish"), (DRAFT, POLISHED)):
        assert step["role"] == role and step["fast_mode"] is True
        assert (tmp_path / step["markdown_file"]).read_text() == text
        assert (tmp_path / step["json_file"]).exists()
    manifest = json.loads(next(settings.path_for("logs_dir").rglob("experiment.json")).read_text())
    assert manifest["workflow_status"] == "production" and manifest["status"] == "completed"
    assert manifest["pass_options"] == {"draft": {"reasoning_effort": "high", "fast_mode": True}, "polish": {"reasoning_effort": "xhigh", "fast_mode": True}}


@pytest.mark.parametrize("failed_stage", ["draft", "polish"])
@pytest.mark.parametrize("failure", ["transport", "contract", "validation"])
def test_failure_retains_evidence_without_retry_fallback_or_replacing_published_result(tmp_path, monkeypatch, failed_stage, failure):
    settings = configured(tmp_path)
    published = settings.path_for("refined_dir") / "source.json"
    published.parent.mkdir(parents=True)
    published.write_text('{"previous_success": true}')
    calls = []

    def backend(_backend, _prompt, runtime, **kwargs):
        calls.append(runtime.options.model)
        if len(calls) == (1 if failed_stage == "draft" else 2):
            if failure == "transport":
                raise CLIBackendError("offline transport failure")
            return {"section_map": []} if failure == "contract" else payload("# source\n\n占位标题。")
        return payload(DRAFT)

    monkeypatch.setattr("src.llm.tasks.refinement.run_backend_payload", backend)
    with pytest.raises((CLIBackendError, RefinementOutputValidationError)):
        refine_batch(settings)
    assert len(calls) == (1 if failed_stage == "draft" else 2)
    assert json.loads(published.read_text()) == {"previous_success": True}
    manifest = next(settings.path_for("logs_dir").rglob("experiment.json"))
    state = json.loads(manifest.read_text())
    assert state["status"] == "failed" and state["failed_pass"] == failed_stage
    assert (manifest.parent / "draft.md").exists() == (failed_stage == "polish")


def test_missing_second_policy_fails_before_model_calls(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    (tmp_path / "config/prompts/refinement_rewrite.md").unlink()
    def forbidden(*args, **kwargs):
        pytest.fail("missing policy must not trigger a request")
    monkeypatch.setattr("src.llm.tasks.refinement.run_backend_payload", forbidden)
    with pytest.raises(PromptLoadError, match="refinement_rewrite.md"):
        refine_batch(settings)


@pytest.mark.parametrize("backend,policy,error", [("codex_cli", "书面编辑", RefinementError), ("codex_api", None, PromptLoadError)])
def test_two_pass_requires_supported_backend_and_second_policy(tmp_path, monkeypatch, backend, policy, error):
    def forbidden(*args, **kwargs):
        pytest.fail("precondition failure must not call a model")
    monkeypatch.setattr("src.llm.tasks.refinement.run_backend_payload", forbidden)
    with pytest.raises(error):
        refine_document("输入", title="offline", prompt="内容校对", polish_prompt=policy,
                        options=RefinementOptions(refinement_mode="terra_terra"), backends=[backend], logs_dir=tmp_path)


@pytest.mark.parametrize("transport", ["urllib", "curl"])
def test_both_actual_local_requests_send_fixed_priority_and_effort(tmp_path, monkeypatch, transport):
    monkeypatch.setattr("src.llm.gateway.should_use_curl_first", lambda url: transport == "curl")
    with fixed_gateway() as (url, requests):
        refine_document("ASR_ONLY_MARKER。", title="offline", reference_text="OCR_ONLY_MARKER。",
            prompt="内容政策", polish_prompt="书面政策", options=RefinementOptions(refinement_mode="terra_terra", fast_mode=False),
            gateway=GatewayClient(GatewayConnection(url, "local-only")), logs_dir=tmp_path, workdir=tmp_path)
    assert [r["payload"]["model"] for r in requests] == [TERRA_MODEL, TERRA_MODEL]
    assert [r["payload"]["reasoning"]["effort"] for r in requests] == ["high", "xhigh"]
    assert all(r["payload"]["service_tier"] == "priority" and r["payload"]["store"] is False for r in requests)
    assert "ASR_ONLY_MARKER" not in requests[1]["payload"]["input"] and "OCR_ONLY_MARKER" not in requests[1]["payload"]["input"]


@pytest.mark.parametrize("backends", [["codex_cli"], ["codex_api", "agy"]])
def test_independent_library_keeps_single_pass_cli_and_multiple_backends(tmp_path, monkeypatch, backends):
    seen = []
    def backend(*, backend, **kwargs):
        seen.append(backend)
        return BackendDocumentRefinementResult(backend=backend, model_name="caller-model", final_markdown="# 文稿\n\n独立调用。", refinement_strategy="single_pass_raw_asr", refinement_reason="test", needs_review_sections=[], refinement_notes=[])
    monkeypatch.setattr("src.llm.tasks.refinement.run_validated_single_pass_backend_refinement", backend)
    result = refine_document("原始全文", title="文稿", prompt="调用者提示词", logs_dir=tmp_path, backends=backends)
    assert seen == backends and len(result.backend_results) == len(backends)


def test_current_and_archived_flash_artifacts_remain_readable(tmp_path):
    job = tmp_path / "data/jobs/demo"
    for mode in ("terra-terra", "terra-flash"):
        for number in (1, 2):
            directory = job / "output/logs/refine/source" / mode / f"run-{number}"
            directory.mkdir(parents=True)
            for name, content in (("draft.md", f"{mode} draft {number}"), ("polish.md", f"{mode} polish {number}"), ("experiment.json", "{}")):
                (directory / name).write_text(content)
    entries = {e["id"]: e for e in collect_job_artifacts(tmp_path, "demo")}
    for mode, draft_id, polish_id, record_id in [
        ("terra-terra", "refine-terra-terra-draft", "refine-terra-rewrite", "refine-terra-terra-record"),
        ("terra-flash", "refine-terra-draft", "refine-flash-polish", "refine-experiment-record"),
    ]:
        assert entries[record_id]["exists"]
        assert read_job_artifact(tmp_path, "demo", draft_id)["content"] == f"{mode} draft 2"
        assert read_job_artifact(tmp_path, "demo", polish_id)["content"] == f"{mode} polish 2"
        assert (job / "output/logs/refine/source" / mode / "run-1/draft.md").read_text() == f"{mode} draft 1"


def test_stage_file_zip_includes_both_passes_and_record(tmp_path):
    from src.web.stage_file_runs import build_stage_file_workspace, build_stage_result_archive
    workspace = build_stage_file_workspace(tmp_path, "demo")
    (workspace.job_paths.intermediate_refined_dir / "source.json").write_text("{}")
    directory = workspace.job_paths.job_root / "output/logs/refine/source/terra-terra/run-1"
    directory.mkdir(parents=True)
    expected = {"refined/source.json"}
    for name, content in (("draft.md", DRAFT), ("polish.md", POLISHED), ("experiment.json", "{}")):
        (directory / name).write_text(content)
        expected.add(f"terra-terra/run-1/{name}")
    archive = build_stage_result_archive(workspace=workspace, stage_name="refine", result_name="test")
    with ZipFile(archive) as zipped:
        assert set(zipped.namelist()) == expected
        assert zipped.read("terra-terra/run-1/polish.md").decode() == POLISHED


@pytest.mark.parametrize("filename,required", [
    ("06_refine.py", []), ("run_pipeline.py", ["--stage", "refine"]), ("00_run_main_pipeline.py", []),
    ("08_run_job.py", ["--video", "a.mp4", "--reference", "a.txt", "--output-dir", "out"]), ("09_run_batch_jobs.py", []),
])
def test_cli_can_select_single_pass_without_forcing_the_default(filename, required):
    spec = importlib.util.spec_from_file_location("release_cli_" + filename.replace(".", "_"), ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build_parser().parse_args(required).refinement_mode is None
    assert module.build_parser().parse_args([*required, "--refinement-mode", "single_pass"]).refinement_mode == "single_pass"


def test_web_default_rerun_and_explicit_single_pass_do_not_rewrite_parameters(tmp_path, monkeypatch):
    from api_server import create_app
    from tests.test_api_server import request_json
    configured(tmp_path)
    (tmp_path / "sample.mp4").write_bytes(b"offline-placeholder")
    (tmp_path / "reference.txt").write_text("参考")
    seen = []
    def stage(stage, settings, _logger=None, **kwargs):
        if stage == "refine":
            options = runtime_from_settings(settings).options
            seen.append((options.refinement_mode, options.model, options.reasoning_effort, options.fast_mode))
        if stage == "export-markdown":
            final = settings.path_for("final_dir") / "source.md"
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_text("# 离线结果\n\n配置接线检查。")
        return 0
    monkeypatch.setattr("src.web.tasks.run_stage", stage)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)
    response = request_json(app, "POST", "/api/jobs", json_body={"video": str(tmp_path / "sample.mp4"), "reference": str(tmp_path / "reference.txt"), "output_dir": str(tmp_path / "published")})
    assert response.status_code == 202
    job = response.json()["job_id"]
    for mode in (None, "single_pass"):
        response = request_json(app, "POST", f"/api/jobs/{job}/rerun", json_body={"start_stage": "refine", "refinement_mode": mode})
        assert response.status_code == 202
    assert seen == [(mode, "gpt-6.1-sol", "medium", False) for mode in ("terra_terra", "terra_terra", "single_pass")]
