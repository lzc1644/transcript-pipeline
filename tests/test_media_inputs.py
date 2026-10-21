from __future__ import annotations

import logging
from pathlib import Path

import pytest

from api_server import create_app
from src.config_loader import load_settings
from src.ffmpeg_utils import AudioExtractionResult, extract_audio_batch
from src.job_runner import (
    JobRunnerError,
    build_job_paths,
    cleanup_completed_job_video,
    load_batch_job_specs,
    run_batch_jobs,
    run_single_job,
    write_job_settings,
)
from src.web.stage_file_runs import stage_input_slots
from tests.helpers import write_minimal_settings
from tests.test_api_server import request_json, request_raw


@pytest.mark.parametrize("extension", [".wav", ".MP3", ".m4a", ".flac"])
def test_audio_upload_and_extraction_use_configured_formats(tmp_path, monkeypatch, extension):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    app = create_app(project_root=tmp_path)
    response = request_raw(app, "POST", "/api/uploads", params={"kind": "video", "filename": f"talk{extension}"}, content=b"audio")
    assert response.status_code == 200
    assert Path(response.json()["path"]).read_bytes() == b"audio"
    assert extension.lower() in stage_input_slots("extract-audio", loaded)[0].extensions

    inputs = tmp_path / "data/input/videos"
    inputs.mkdir(parents=True)
    source = inputs / f"talk{extension}"
    source.write_bytes(b"audio")
    (inputs / "notes.txt").write_text("not media")
    calls = []

    def convert(video_path, output_path, **kwargs):
        calls.append((video_path, kwargs["sample_rate"], kwargs["channels"]))
        output_path.write_bytes(b"normalized wav")
        return AudioExtractionResult(video_path, output_path, "created")

    monkeypatch.setattr("src.ffmpeg_utils.find_ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr("src.ffmpeg_utils.extract_audio", convert)
    results = extract_audio_batch(loaded)
    assert calls == [(source, 16000, 1)]
    assert results[0].audio_path == tmp_path / "data/input/audio/talk.wav"
    assert source.exists()  # A single stage never deletes its input.


def test_batch_directory_accepts_mixed_media_and_pairs_audio_reference(tmp_path):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    inputs, refs = tmp_path / "inputs", tmp_path / "refs"
    inputs.mkdir()
    refs.mkdir()
    for name in ["video.mp4", "recording.MP3"]:
        (inputs / name).write_bytes(b"media")
        (refs / f"{Path(name).stem}.txt").write_text("reference")
    (inputs / "notes.txt").write_text("ignored")
    for mode in ["paired", "shared", "conversation"]:
        kwargs = {"reference_dir": str(refs)} if mode == "paired" else {"shared_reference": str(refs / "video.txt")} if mode == "shared" else {"content_type": "conversation"}
        specs, failures = load_batch_job_specs(base_loaded_settings=loaded, videos_dir=str(inputs), output_dir=str(tmp_path / "output"), **kwargs)
        assert failures == []
        assert {Path(spec.video).name for spec in specs} == {"video.mp4", "recording.MP3"}


def cleanup_fixture(tmp_path, *, source_kind="upload", extension=".mp4", audio=True):
    write_minimal_settings(tmp_path)
    base = load_settings(project_root=tmp_path)
    paths = build_job_paths(tmp_path, "cleanup")
    settings_path = write_job_settings(project_root=tmp_path, loaded_settings=base, job_paths=paths, profile_name="local_cpu")
    loaded = load_settings(settings_path=settings_path, project_root=tmp_path)
    source_dir = tmp_path / ("data/uploads/videos/date/group" if source_kind == "upload" else "external")
    source_dir.mkdir(parents=True)
    source = source_dir / f"talk{extension}"
    source.write_bytes(b"original")
    prepared = paths.input_videos_dir / f"source{extension}"
    prepared.write_bytes(b"copy")
    wav = loaded.path_for("audio_dir") / "source.wav"
    wav.parent.mkdir(parents=True)
    if audio:
        wav.write_bytes(b"retained audio")
    return paths, loaded, source, prepared, wav


@pytest.mark.parametrize("source_kind", ["upload", "external"])
def test_cleanup_removes_only_owned_videos_and_keeps_audio(tmp_path, source_kind):
    paths, loaded, source, prepared, wav = cleanup_fixture(tmp_path, source_kind=source_kind)
    unrelated = prepared.parent / "unrelated.mp4"
    unrelated.write_bytes(b"unrelated")
    for _ in range(2):  # Safe on successful reruns after the first cleanup.
        cleanup_completed_job_video(job_paths=paths, video_source=source, loaded_settings=loaded, logger=logging.getLogger("test"))
    assert not prepared.exists()
    assert source.exists() == (source_kind == "external")
    assert wav.read_bytes() == b"retained audio"
    assert unrelated.exists()


@pytest.mark.parametrize("extension,audio", [(".mp3", True), (".mp4", False)])
def test_cleanup_preserves_audio_inputs_and_videos_without_retained_audio(tmp_path, extension, audio):
    paths, loaded, source, prepared, _ = cleanup_fixture(tmp_path, extension=extension, audio=audio)
    cleanup_completed_job_video(job_paths=paths, video_source=source, loaded_settings=loaded, logger=logging.getLogger("test"))
    assert source.exists()
    assert prepared.exists()


@pytest.mark.parametrize("escape", ["file", "directory", "prepared"])
def test_cleanup_does_not_follow_symlinks_to_external_originals(tmp_path, escape):
    paths, loaded, source, prepared, _ = cleanup_fixture(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    original = external / "talk.mp4"
    original.write_bytes(b"external original")
    if escape == "file":
        source.unlink()
        source.symlink_to(original)
    elif escape == "directory":
        source.unlink()
        upload_root = tmp_path / "data/uploads/videos"
        # Replace only this fixture's empty branch, not any real project data.
        source.parent.rmdir()
        source.parent.parent.rmdir()
        upload_root.rmdir()
        upload_root.symlink_to(external, target_is_directory=True)
        source = upload_root / "talk.mp4"
    else:
        prepared.unlink()
        prepared.symlink_to(original)
    cleanup_completed_job_video(job_paths=paths, video_source=source, loaded_settings=loaded, logger=logging.getLogger("test"))
    assert original.read_bytes() == b"external original"


@pytest.mark.parametrize("entry", ["cli", "web", "batch"])
@pytest.mark.parametrize("outcome", ["success", "failed", "partial", "delivery-failed", "cleanup-warning"])
def test_completed_job_lifecycle_across_entry_points(tmp_path, monkeypatch, entry, outcome):
    write_minimal_settings(tmp_path)
    loaded = load_settings(project_root=tmp_path)
    source = tmp_path / "data/uploads/videos/date/group/talk.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"uploaded video")
    output = tmp_path / "delivered"
    rerunning = False

    def stage(stage_name, settings, logger, **kwargs):
        if not rerunning:
            assert source.exists()  # No deletion while processing.
        if stage_name == "extract-audio":
            audio = settings.path_for("audio_dir") / "source.wav"
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"retained audio")
        if stage_name == "refine" and outcome in {"failed", "partial"}:
            return 1 if outcome == "failed" else 2
        if stage_name == "export-markdown":
            if outcome == "delivery-failed":
                output.write_text("not a directory")
            final = settings.path_for("final_dir")
            final.mkdir(parents=True, exist_ok=True)
            (final / "source.md").write_text("# delivered")
            (final / "source.txt").write_text("delivered")
        return 0

    monkeypatch.setattr("src.job_runner.run_stage", stage)
    monkeypatch.setattr("src.web.tasks.run_stage", stage)
    monkeypatch.setattr("src.job_runner.create_job_id", lambda: "lifecycle")
    successful = outcome in {"success", "cleanup-warning"}
    if outcome == "cleanup-warning":
        unlink = Path.unlink

        def denied(path, *args, **kwargs):
            if path.suffix == ".mp4":
                raise PermissionError("read-only video")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", denied)
    if entry == "cli":
        if successful:
            run_single_job(project_root=tmp_path, base_loaded_settings=loaded, video=str(source), reference=None, output_dir=str(output), content_type="conversation")
        else:
            with pytest.raises((JobRunnerError, OSError)):
                run_single_job(project_root=tmp_path, base_loaded_settings=loaded, video=str(source), reference=None, output_dir=str(output), content_type="conversation")
        job_root = tmp_path / "data/jobs/lifecycle"
    elif entry == "web":
        app = create_app(project_root=tmp_path, run_tasks_inline=True)
        response = request_json(app, "POST", "/api/jobs", json_body={"video": str(source), "output_dir": str(output), "content_type": "conversation"})
        job_id = response.json()["job_id"]
        state = request_json(app, "GET", f"/api/jobs/{job_id}").json()
        assert state["status"] == ("success" if successful else "partial" if outcome == "partial" else "failed")
        job_root = tmp_path / "data/jobs" / job_id
    else:
        specs, _ = load_batch_job_specs(base_loaded_settings=loaded, videos_dir=str(source.parent), output_dir=str(output), content_type="conversation")
        if outcome == "delivery-failed":
            with pytest.raises(OSError):
                run_batch_jobs(project_root=tmp_path, base_loaded_settings=loaded, job_specs=specs)
        else:
            summary = run_batch_jobs(project_root=tmp_path, base_loaded_settings=loaded, job_specs=specs)
            assert summary.items[0].status == ("success" if successful else outcome)
        job_root = tmp_path / "data/jobs/lifecycle"
    assert source.exists() == (outcome != "success")
    assert (job_root / "input/videos/source.mp4").exists() == (outcome != "success")
    assert (job_root / "input/audio/source.wav").read_bytes() == b"retained audio"
    if entry == "web" and outcome == "failed":
        # Recovery from an initial failure cleans up only after successful delivery.
        outcome = "success"
        rerunning = True
        response = request_json(app, "POST", f"/api/jobs/{job_id}/rerun", json_body={"start_stage": "refine"})
        assert response.status_code == 202
        assert request_json(app, "GET", f"/api/jobs/{job_id}").json()["status"] == "success"
        assert not source.exists()
        assert not (job_root / "input/videos/source.mp4").exists()
    if successful or outcome == "success":
        assert (output / "talk.txt").read_text() == "delivered"
        if entry == "web":
            # A successful downstream rerun must not need the deleted video.
            rerunning = True
            response = request_json(app, "POST", f"/api/jobs/{job_id}/rerun", json_body={"start_stage": "export-markdown"})
            assert response.status_code == 202
            assert request_json(app, "GET", f"/api/jobs/{job_id}").json()["status"] == "success"


def test_cleanup_failure_warns_without_preventing_other_cleanup(tmp_path, monkeypatch, caplog):
    paths, loaded, source, prepared, wav = cleanup_fixture(tmp_path)
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == prepared:
            raise PermissionError("read-only")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    cleanup_completed_job_video(job_paths=paths, video_source=source, loaded_settings=loaded, logger=logging.getLogger("test"))
    assert "最终结果已保留" in caplog.text
    assert prepared.exists() and wav.exists()
    assert not source.exists()
