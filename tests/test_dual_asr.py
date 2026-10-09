from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.asr.pairing import begin_asr_pair, complete_asr_pair, pair_manifest_path, validate_asr_pair, AsrPairError
from src.asr_utils import (AsrBatchItem, AsrFileResult, AsrSegmentResult, AsrTranscriptionError,
                           bind_source_audio, build_asr_output_paths, transcribe_batch, write_asr_result)
from src.config_loader import load_settings
from src.refine_inputs import DualAsrInputError, load_dual_asr_input
from src.refine_utils import (RefinementError, build_single_pass_refine_prompt, iter_asr_text_files,
                              refine_batch, resolve_refinement_input_paths)
from src.settings_overrides import ModelOverrides, SettingsOverrideError, apply_model_overrides, apply_model_overrides_to_raw_settings
from src.web.frontend_settings import FrontendSettingsUpdate, frontend_settings_response, save_frontend_settings
from tests.helpers import write_minimal_settings


SECONDARY = "qwen3-asr-0.6b"


def setup_dual(tmp_path, *, reference=False):
    config = write_minimal_settings(tmp_path, reference_overrides={"enabled": reference})
    loaded = load_settings(project_root=tmp_path)
    apply_model_overrides(loaded, ModelOverrides(secondary_asr_candidate=SECONDARY))
    audio_dir = loaded.path_for("audio_dir")
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "source.wav"
    audio.write_bytes(b"same source audio fixture")
    if reference:
        directory = loaded.path_for("extracted_text_dir")
        directory.mkdir(parents=True)
        (directory / "source.txt").write_text("参考朗读。", encoding="utf-8")
    return config, loaded, audio


def fake_candidate_batch(audio_files, output_dir, loaded, logger, lock_fds):
    candidate = loaded.settings.asr.candidate or "whisper-existing"
    outputs = []
    for audio in audio_files:
        text = "托奇拉领导的战争。" if candidate == "whisper-existing" else "孙中山领导的战争。"
        result = AsrFileResult(str(audio), "faster-whisper" if candidate == "whisper-existing" else "qwen-asr",
                              candidate, "cpu", "float32", "zh",
                              [AsrSegmentResult(1, 0.0, 4.0, text)], text,
                              {"candidate": candidate, "duration_seconds": 10.0})
        paths = build_asr_output_paths(audio, output_dir)
        write_asr_result(bind_source_audio(result, audio), paths)
        outputs.append(AsrBatchItem(audio, paths, 1))
    return outputs


def produce_pair(tmp_path, monkeypatch, *, reference=False):
    config, loaded, audio = setup_dual(tmp_path, reference=reference)
    monkeypatch.setattr("src.asr_utils.transcribe_candidate_batch", fake_candidate_batch)
    outputs = transcribe_batch(loaded)
    return config, loaded, audio, outputs


def test_dual_batch_is_serial_isolated_and_backward_compatible(tmp_path, monkeypatch):
    _, loaded, audio = setup_dual(tmp_path)
    calls = []

    def record(audio_files, directory, settings, logger, lock_fds):
        calls.append((settings.settings.asr.candidate or "whisper-existing", directory, bool(lock_fds)))
        return fake_candidate_batch(audio_files, directory, settings, logger, lock_fds)

    monkeypatch.setattr("src.asr_utils.transcribe_candidate_batch", record)
    outputs = transcribe_batch(loaded)
    root = loaded.path_for("asr_dir")
    assert calls == [("whisper-existing", root, True), (SECONDARY, root / "secondary" / SECONDARY, True)]
    assert len(outputs) == 1 and outputs[0].secondary_output_paths is not None
    assert iter_asr_text_files(root) == [root / "source.txt"]
    assert sorted(p.name for p in root.glob("*.json")) == ["source.json"]
    assert loaded.settings.asr.candidate is None and loaded.settings.asr.secondary_candidate == SECONDARY
    manifest = validate_asr_pair(root, audio.stem, "whisper-existing", SECONDARY)
    assert manifest["status"] == "complete"
    evidence = load_dual_asr_input(loaded, root / "source.txt")
    assert evidence and "托奇拉" in evidence.render() and "孙中山" in evidence.render()


def test_same_audio_old_secondary_cannot_survive_failed_new_run(tmp_path, monkeypatch):
    _, loaded, _, _ = produce_pair(tmp_path, monkeypatch)
    root = loaded.path_for("asr_dir")
    old_aux = (root / "secondary" / SECONDARY / "source.json").read_bytes()
    old_run = json.loads(pair_manifest_path(root, "source").read_text())["run_id"]
    calls = []

    def fail_aux(audio_files, directory, settings, logger, lock_fds):
        calls.append(settings.settings.asr.candidate or "whisper-existing")
        if settings.settings.asr.candidate == SECONDARY:
            raise AsrTranscriptionError("auxiliary failed")
        assert json.loads(pair_manifest_path(root, "source").read_text())["status"] == "pending"
        return fake_candidate_batch(audio_files, directory, settings, logger, lock_fds)

    monkeypatch.setattr("src.asr_utils.transcribe_candidate_batch", fail_aux)
    with pytest.raises(AsrTranscriptionError, match="auxiliary failed"):
        transcribe_batch(loaded)
    manifest = json.loads(pair_manifest_path(root, "source").read_text())
    assert calls == ["whisper-existing", SECONDARY]
    assert manifest["status"] == "failed" and manifest["run_id"] != old_run
    assert (root / "secondary" / SECONDARY / "source.json").read_bytes() == old_aux
    with pytest.raises(RefinementError, match="未完成"):
        resolve_refinement_input_paths(loaded, root / "source.txt")


@pytest.mark.parametrize("damage", ["pending", "failed", "missing", "primary_txt", "secondary_json", "candidate", "audio_hash", "run_id"])
def test_invalid_pair_is_rejected_before_any_ai_request(tmp_path, monkeypatch, damage):
    _, loaded, _, _ = produce_pair(tmp_path, monkeypatch)
    root = loaded.path_for("asr_dir")
    path = pair_manifest_path(root, "source")
    payload = json.loads(path.read_text())
    if damage in {"pending", "failed"}:
        payload["status"] = damage
    elif damage == "missing":
        path.unlink()
    elif damage == "primary_txt":
        (root / "source.txt").write_text("modified")
    elif damage == "secondary_json":
        (root / "secondary" / SECONDARY / "source.json").write_text("{}")
    elif damage == "candidate":
        payload["secondary_candidate"] = "fun-asr-nano"
    elif damage == "audio_hash":
        payload["source_audio_sha256"] = "b" * 64
    elif damage == "run_id":
        payload.pop("run_id")
    if path.exists():
        path.write_text(json.dumps(payload))
    monkeypatch.setattr("src.refine_utils.run_backend_payload", lambda *a, **kw: pytest.fail("must not call AI"))
    with pytest.raises(RefinementError, match="双 ASR"):
        refine_batch(loaded)


def test_dual_refine_is_one_request_and_records_both_inputs(tmp_path, monkeypatch):
    _, loaded, _, _ = produce_pair(tmp_path, monkeypatch, reference=True)
    prompts = []

    def backend(prompt, settings, **kwargs):
        prompts.append(prompt)
        return {"final_markdown": "# 章节\n\n托梯拉领导的战争。", "section_map": [],
                "refinement_notes": [], "needs_review_sections": [], "deletion_candidates": []}

    monkeypatch.setattr("src.refine_utils.run_codex_api_payload", backend)
    monkeypatch.setattr("src.refine_utils.build_pre_replaced_document", lambda **kw: pytest.fail("dual must preserve raw evidence"))
    summary = refine_batch(loaded)
    assert summary.total == summary.success == 1 and len(prompts) == 1
    assert "[ASR A]" in prompts[0] and "[ASR B]" in prompts[0]
    assert "托奇拉" in prompts[0] and "孙中山" in prompts[0] and "参考朗读。" in prompts[0]
    for filename in ("source.json", "source.codex_api.json"):
        payload = json.loads((loaded.path_for("refined_dir") / filename).read_text())
        assert payload["asr_input_mode"] == "dual_asr"
        assert payload["refinement_strategy"] == "single_pass_dual_asr"
        assert len(payload["source_asr_files"]) == 2 and payload["asr_pair_run_id"]
        assert payload["source_asr_file"].endswith("asr/source.txt")
    traces = list(loaded.path_for("logs_dir").glob("refine/source/codex_api/*/attempt-001/attempt-meta.json"))
    assert len(traces) == 1 and len(json.loads(traces[0].read_text())["source_asr_files"]) == 2


def test_disabling_secondary_keeps_single_txt_without_json_or_manifest(tmp_path):
    _, loaded, _ = setup_dual(tmp_path)
    apply_model_overrides(loaded, ModelOverrides(secondary_asr_candidate=""))
    root = loaded.path_for("asr_dir")
    root.mkdir(parents=True)
    text = root / "source.txt"
    text.write_text("单份历史转录。")
    assert resolve_refinement_input_paths(loaded, text).dual_asr_input is None


@pytest.mark.parametrize("primary", [None, "qwen3-asr-0.6b"])
def test_equal_candidates_rejected_without_mutating_settings(tmp_path, primary):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    effective = primary or "whisper-existing"
    before = loaded.settings.model_dump()
    with pytest.raises(SettingsOverrideError, match="不同"):
        apply_model_overrides(loaded, ModelOverrides(asr_candidate=primary, secondary_asr_candidate=effective))
    assert loaded.settings.model_dump() == before
    raw = {"asr": {"candidate": primary}}
    with pytest.raises(SettingsOverrideError, match="不同"):
        apply_model_overrides_to_raw_settings(raw, ModelOverrides(secondary_asr_candidate=effective))
    assert raw == {"asr": {"candidate": primary}}


def test_frontend_secondary_default_and_explicit_disable(tmp_path):
    config = write_minimal_settings(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["asr"]["secondary_candidate"] = SECONDARY
    config.write_text(yaml.safe_dump(raw))
    assert frontend_settings_response(tmp_path)["secondary_asr_candidate"] == SECONDARY
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(secondary_asr_candidate=""))
    assert frontend_settings_response(tmp_path)["secondary_asr_candidate"] == ""
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(secondary_asr_candidate=SECONDARY))
    assert frontend_settings_response(tmp_path)["secondary_asr_candidate"] == SECONDARY


def test_job_snapshot_freezes_pair_and_history_does_not_inherit_globals(tmp_path, monkeypatch):
    from api_server import create_app
    from src.job_runner import build_job_paths, write_job_settings
    from src.web.tasks import run_job_rerun
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    paths = build_job_paths(tmp_path, "history")
    snapshot = write_job_settings(project_root=tmp_path, loaded_settings=loaded, job_paths=paths,
                                  profile_name="local_cpu", model_overrides=ModelOverrides(secondary_asr_candidate=SECONDARY))
    raw = yaml.safe_load(snapshot.read_text())
    raw["pipeline"] = {"stages": ["transcribe"]}
    snapshot.write_text(yaml.safe_dump(raw))
    paths.manifest_path.write_text(json.dumps({"profile": "local_cpu"}))
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(secondary_asr_candidate="fun-asr-nano"))
    seen = []
    monkeypatch.setattr("src.web.tasks.run_stage", lambda stage, settings, *a, **kw: seen.append(settings.settings.asr.secondary_candidate) or 0)
    app = create_app(project_root=tmp_path)
    state = run_job_rerun(app=app, job_id="history", payload={"start_stage": "transcribe"}, state_path=paths.job_root / "state.json")
    assert state["status"] == "success" and seen == [SECONDARY]
    state = run_job_rerun(app=app, job_id="history", payload={"start_stage": "refine", "secondary_asr_candidate": ""}, state_path=paths.job_root / "state.json")
    assert state["status"] == "failed" and "快照" in state["error_message"] and seen == [SECONDARY]


@pytest.mark.parametrize("explicit_secondary", [None, "", SECONDARY])
def test_refine_file_mode_has_explicit_single_asr_boundary(tmp_path, monkeypatch, explicit_secondary):
    from api_server import create_app
    from src.web.tasks import execute_stage_file_run
    from src.web.state_store import create_initial_state, read_json_file, write_json_file
    from tests.test_stage_file_runs import staged_file
    config = write_minimal_settings(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["asr"]["secondary_candidate"] = SECONDARY
    config.write_text(yaml.safe_dump(raw))
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(secondary_asr_candidate=SECONDARY))
    asr = staged_file(tmp_path, "refine", "asr_txt", "lecture.txt", "语音转写。")
    ref = staged_file(tmp_path, "refine", "reference_txt", "reference.txt", "原文。")
    app = create_app(project_root=tmp_path)
    path = app.state.stage_run_state_path("files")
    write_json_file(path, create_initial_state("files", "stage"))
    calls = []

    def stage(name, loaded, *a, **kw):
        calls.append(loaded.settings.asr.secondary_candidate)
        assert loaded.settings.asr.secondary_candidate is None
        directory = loaded.path_for("refined_dir")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source.json").write_text(json.dumps({"final_markdown": "正文。"}))
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", stage)
    execute_stage_file_run(app=app, run_id="files", stage_name="refine", payload={
        "input_files": {"asr_txt": str(asr), "reference_txt": str(ref)}, "result_name": "result",
        "secondary_asr_candidate": explicit_secondary,
    })
    state = read_json_file(path)
    if explicit_secondary:
        assert state["status"] == "failed" and "单 TXT" in state["error_message"] and not calls
    else:
        assert state["status"] == "success" and calls == [None]
        assert state["request_payload"]["secondary_asr_candidate"] == ""


def test_prompt_policy_has_single_owner_in_all_input_modes(tmp_path, monkeypatch):
    _, loaded, _, _ = produce_pair(tmp_path, monkeypatch)
    text_path = loaded.path_for("asr_dir") / "source.txt"
    policy = "自定义政策：允许清理纯口吃与无意义填充，合并自然段。"
    dual = resolve_refinement_input_paths(loaded, text_path)
    apply_model_overrides(loaded, ModelOverrides(secondary_asr_candidate=""))
    single = resolve_refinement_input_paths(loaded, text_path)
    for paths in (dual, single):
        prompt = build_single_pass_refine_prompt(policy, paths, backend="codex_api", pre_replaced_segments=[], reference_full_text="")
        assert prompt.count(policy) == 1 and prompt.count("字段必须包含") == 1
        assert "只允许添加标点" not in prompt and "所有有效讲话、追问" not in prompt
        assert "结果会交给" not in prompt
        assert "人物、否定词和因果关系应重点核验" not in prompt


def test_default_markdown_allows_light_cleanup_in_no_reference_single_asr(tmp_path):
    from src.refine_utils import build_pre_replaced_document
    _, loaded, _ = setup_dual(tmp_path)
    apply_model_overrides(loaded, ModelOverrides(secondary_asr_candidate=""))
    text = loaded.path_for("asr_dir") / "source.txt"
    text.parent.mkdir(parents=True)
    speech = "嗯，嗯，这是有意义的讲话。"
    text.write_text(speech)
    paths = resolve_refinement_input_paths(loaded, text)
    assert paths.reference_text_path is None and paths.dual_asr_input is None
    segments = build_pre_replaced_document(asr_full_text=speech, reference_full_text="", loaded_settings=loaded)
    project = Path(__file__).resolve().parents[1]
    for filename in ("final_cleanup.md", "conversation_cleanup.md"):
        policy = (project / "config/prompts" / filename).read_text()
        prompt = build_single_pass_refine_prompt(policy, paths, backend="codex_api", pre_replaced_segments=segments, reference_full_text="")
        assert prompt.startswith(policy.strip()) and speech in prompt
        assert "可以清理纯口吃与无意义填充" in prompt and "合并" in prompt
        assert "只允许添加标点" not in prompt
        assert "所有有效讲话、追问、回答、停顿后的补充、重复强调和口语化表达都应保留" not in prompt


def test_interrupted_pair_stays_pending_and_cannot_be_consumed(tmp_path, monkeypatch):
    _, loaded, _, _ = produce_pair(tmp_path, monkeypatch)

    def interrupt(audio_files, directory, settings, logger, lock_fds):
        if settings.settings.asr.candidate == SECONDARY:
            raise KeyboardInterrupt()
        return fake_candidate_batch(audio_files, directory, settings, logger, lock_fds)

    monkeypatch.setattr("src.asr_utils.transcribe_candidate_batch", interrupt)
    with pytest.raises(KeyboardInterrupt):
        transcribe_batch(loaded)
    root = loaded.path_for("asr_dir")
    assert json.loads(pair_manifest_path(root, "source").read_text())["status"] == "pending"
    with pytest.raises(DualAsrInputError, match="未完成"):
        load_dual_asr_input(loaded, root / "source.txt")


def test_reverse_pair_selection_is_supported(tmp_path, monkeypatch):
    _, loaded, _ = setup_dual(tmp_path)
    apply_model_overrides(loaded, ModelOverrides(asr_candidate=SECONDARY, secondary_asr_candidate="whisper-existing"))
    monkeypatch.setattr("src.asr_utils.transcribe_candidate_batch", fake_candidate_batch)
    transcribe_batch(loaded)
    evidence = load_dual_asr_input(loaded, loaded.path_for("asr_dir") / "source.txt")
    assert evidence.primary_candidate == SECONDARY and evidence.secondary_candidate == "whisper-existing"


def test_new_single_job_uses_saved_dual_default_and_manifest(tmp_path, monkeypatch):
    from api_server import create_app
    from src.web.tasks import execute_single_job
    from src.web.state_store import create_initial_state, read_json_file, write_json_file
    write_minimal_settings(tmp_path)
    save_frontend_settings(tmp_path, FrontendSettingsUpdate(asr_candidate="whisper-existing", secondary_asr_candidate=SECONDARY))
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"video fixture")
    reference = tmp_path / "lecture.txt"
    reference.write_text("参考原文。")
    app = create_app(project_root=tmp_path)
    path = app.state.job_state_path("new-dual")
    write_json_file(path, create_initial_state("new-dual", "job"))
    seen = []

    def stage(name, loaded, *args, **kwargs):
        seen.append((name, loaded.settings.asr.candidate, loaded.settings.asr.secondary_candidate))
        if name == "export-markdown":
            final = loaded.path_for("final_dir")
            final.mkdir(parents=True, exist_ok=True)
            (final / "source.md").write_text("# 测试\n\n正文。")
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", stage)
    execute_single_job(app=app, job_id="new-dual", payload={"video": str(video), "reference": str(reference), "output_dir": str(tmp_path / "final")})
    state = read_json_file(path)
    assert state["status"] == "success" and state["secondary_asr_candidate"] == SECONDARY
    assert all(primary == "whisper-existing" and secondary == SECONDARY for _, primary, secondary in seen)
    manifest = json.loads((tmp_path / "data/jobs/new-dual/manifest.json").read_text())
    assert manifest["asr_candidate"] == "whisper-existing" and manifest["secondary_asr_candidate"] == SECONDARY
