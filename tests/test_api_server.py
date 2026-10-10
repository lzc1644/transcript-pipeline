from __future__ import annotations

import asyncio
import io
import json
import threading
from pathlib import Path
import zipfile

import httpx
import pytest

from src.web.state_store import create_initial_state, read_json_file, write_json_file
from tests.helpers import write_minimal_settings


def request_json(app, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, json=json_body, params=params)

    return asyncio.run(send_request())


def request_raw(
    app,
    method: str,
    path: str,
    *,
    content: bytes,
    params: dict | None = None,
) -> httpx.Response:
    async def send_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, content=content, params=params)

    return asyncio.run(send_request())


def test_api_health_and_unknown_api_routes(tmp_path: Path) -> None:
    from api_server import create_app

    app = create_app(project_root=tmp_path)

    health_response = request_json(app, "GET", "/api/health")
    unknown_response = request_json(app, "GET", "/api/not-a-route")

    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}
    assert unknown_response.status_code == 404
    assert "<html" not in unknown_response.text.lower()


def test_frontend_static_files_and_deep_links(tmp_path: Path) -> None:
    from api_server import create_app

    dist_dir = tmp_path / "frontend/dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text("<!doctype html><div id=app>frontend</div>", encoding="utf-8")
    (dist_dir / "assets/app.js").write_text("console.log('built')", encoding="utf-8")

    app = create_app(project_root=tmp_path)
    root_response = request_json(app, "GET", "/")
    deep_link_response = request_json(app, "GET", "/jobs")
    asset_response = request_json(app, "GET", "/assets/app.js")
    missing_asset_response = request_json(app, "GET", "/assets/missing.js")
    missing_file_response = request_json(app, "GET", "/missing.txt")
    docs_response = request_json(app, "GET", "/docs")
    unknown_api_response = request_json(app, "GET", "/api/not-a-route")

    assert root_response.status_code == 200
    assert root_response.text == "<!doctype html><div id=app>frontend</div>"
    assert deep_link_response.status_code == 200
    assert deep_link_response.text == root_response.text
    assert asset_response.status_code == 200
    assert asset_response.text == "console.log('built')"
    assert "javascript" in asset_response.headers["content-type"]
    assert missing_asset_response.status_code == 404
    assert "frontend" not in missing_asset_response.text
    assert missing_file_response.status_code == 404
    assert "frontend" not in missing_file_response.text
    assert docs_response.status_code == 200
    assert "swagger-ui" in docs_response.text
    assert unknown_api_response.status_code == 404
    assert "frontend" not in unknown_api_response.text


def test_frontend_static_serving_rejects_paths_outside_dist(tmp_path: Path) -> None:
    from api_server import create_app

    dist_dir = tmp_path / "frontend/dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("frontend", encoding="utf-8")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")
    (dist_dir / "leak.txt").symlink_to(outside_file)

    app = create_app(project_root=tmp_path)
    symlink_response = request_json(app, "GET", "/leak.txt")
    traversal_response = request_json(app, "GET", "/%2e%2e/outside.txt")

    assert symlink_response.status_code == 404
    assert traversal_response.status_code == 404
    assert "secret" not in symlink_response.text
    assert "secret" not in traversal_response.text


def test_missing_frontend_dist_does_not_disable_api(tmp_path: Path) -> None:
    from api_server import create_app

    app = create_app(project_root=tmp_path)

    health_response = request_json(app, "GET", "/api/health")
    frontend_response = request_json(app, "GET", "/single-job")

    assert health_response.status_code == 200
    assert frontend_response.status_code == 404


def test_get_config_returns_profiles_and_backends(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path, llm_overrides={"backends": ["codex_api"]})
    response = request_json(create_app(project_root=tmp_path), "GET", "/api/config")

    assert response.status_code == 200
    payload = response.json()
    candidates = payload.pop("asr_candidates")
    assert [item["id"] for item in candidates] == ["whisper-existing", "qwen3-asr-1.7b", "qwen3-asr-0.6b"]
    assert all(item["runtime_validation"] == "not_checked" for item in candidates)
    assert payload == {
        "profiles": ["local_cpu"],
        "backends": ["codex_api", "agy", "codex_cli", "both"],
        "configured_backends": ["codex_api"],
        "default_backend": "codex_api",
        "default_ocr_backend": "codex_api",
        "active_profile": "local_cpu",
        "video_extensions": [".mkv", ".mov", ".mp4", ".webm"],
        "reference_extensions": [".txt", ".md", ".pdf"],
        "default_output_dir": str(tmp_path / "data/output/final"),
        "upload_dir": str(tmp_path / "data/uploads"),
        "content_types": ["book_club", "conversation"],
    }


def test_get_refine_default_instruction_returns_configured_prompt(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    response = request_json(create_app(project_root=tmp_path), "GET", "/api/refine-default-instruction")

    assert response.status_code == 200
    assert response.json()["prompt"] == "# test final cleanup"


def test_get_refine_default_instruction_returns_conversation_prompt(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    response = request_json(
        create_app(project_root=tmp_path),
        "GET",
        "/api/refine-default-instruction",
        params={"content_type": "conversation"},
    )

    assert response.status_code == 200
    assert response.json()["prompt"] == "# test conversation cleanup"


def test_frontend_settings_roundtrip_keeps_api_key_masked(tmp_path: Path, monkeypatch) -> None:
    from api_server import create_app

    monkeypatch.delenv("CODEX_LB_API_KEY", raising=False)
    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path)

    save_response = request_json(
        app,
        "PUT",
        "/api/frontend-settings",
        json_body={
            "codex_lb_base_url": "https://api.example.test",
            "codex_lb_api_key": "sk-test-secret",
            "codex_lb_bypass_proxy": True,
            "profile": "local_cpu",
            "backend": "agy",
            "remote_concurrency": 4,
            "book_name": "测试书",
            "chapter": "第一章",
            "glossary_file": str(tmp_path / "glossary.txt"),
            "model": "gpt-6.1-sol",
            "reasoning_effort": "max",
            "ocr_backend": "codex_api",
            "ocr_model": "gpt-6-luna",
            "ocr_reasoning_effort": "high",
        },
    )

    assert save_response.status_code == 200
    payload = save_response.json()
    assert payload["codex_lb_base_url"] == "https://api.example.test"
    assert payload["codex_lb_api_key"] == ""
    assert payload["has_codex_lb_api_key"] is True
    assert payload["codex_lb_bypass_proxy"] is True
    assert payload["profile"] == "local_cpu"
    assert payload["backend"] == "agy"
    assert payload["remote_concurrency"] == 4
    assert payload["book_name"] == "测试书"
    assert payload["chapter"] == "第一章"
    assert payload["glossary_file"].endswith("glossary.txt")
    assert payload["model"] == "gpt-6.1-sol"
    assert payload["reasoning_effort"] == "max"
    assert payload["ocr_backend"] == "codex_api"
    assert payload["ocr_model"] == "gpt-6-luna"
    assert payload["ocr_max_concurrency"] == 40
    assert payload["ocr_submit_interval_seconds"] == 5.0

    settings_path = tmp_path / "data/jobs/frontend-settings.json"
    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert persisted["codex_lb_api_key"] == "sk-test-secret"
    assert persisted["codex_lb_bypass_proxy"] is True
    assert persisted["backend"] == "agy"
    assert persisted["remote_concurrency"] == 4

    clear_response = request_json(
        app,
        "PUT",
        "/api/frontend-settings",
        json_body={"clear_codex_lb_api_key": True},
    )

    assert clear_response.status_code == 200
    assert clear_response.json()["has_codex_lb_api_key"] is False

    proxy_response = request_json(
        app,
        "PUT",
        "/api/frontend-settings",
        json_body={"codex_lb_bypass_proxy": False},
    )
    assert proxy_response.status_code == 200
    assert proxy_response.json()["codex_lb_bypass_proxy"] is False
    assert json.loads(settings_path.read_text(encoding="utf-8"))["codex_lb_bypass_proxy"] is False


def test_get_job_status_returns_current_state(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    state_dir = tmp_path / "data/jobs/job-test-001"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "running",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:31:00+08:00",
                "current_stage": "transcribe",
                "error_message": "",
                "output_path": "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    app = create_app(project_root=tmp_path)
    app.state.active_jobs.add("job-test-001")
    response = request_json(app, "GET", "/api/jobs/job-test-001")

    assert response.status_code == 200
    assert response.json()["id"] == "job-test-001"
    assert response.json()["status"] == "running"
    assert response.json()["current_stage"] == "transcribe"


def test_get_job_artifacts_lists_and_reads_text_outputs(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_dir = tmp_path / "data/jobs/job-test-001"
    (job_dir / "intermediate/asr").mkdir(parents=True, exist_ok=True)
    (job_dir / "intermediate/aligned").mkdir(parents=True, exist_ok=True)
    (job_dir / "intermediate/classified").mkdir(parents=True, exist_ok=True)
    (job_dir / "intermediate/refined").mkdir(parents=True, exist_ok=True)
    (job_dir / "output/final").mkdir(parents=True, exist_ok=True)
    (job_dir / "output/logs/refine").mkdir(parents=True, exist_ok=True)
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:31:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(job_dir / "output/final/source.md"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (job_dir / "intermediate/asr/source.txt").write_text("原始转写文本", encoding="utf-8")
    (job_dir / "intermediate/aligned/source.json").write_text("{}", encoding="utf-8")
    (job_dir / "intermediate/classified/source.json").write_text("{}", encoding="utf-8")
    (job_dir / "intermediate/refined/source.json").write_text(
        json.dumps({"final_markdown": "# 校对结果\n\n正文", "refined_full_text": "校对结果 正文"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (job_dir / "output/final/source.md").write_text("# 最终稿\n\n正文", encoding="utf-8")
    (job_dir / "output/logs/refine/diagnostics.json").write_text(
        json.dumps({"schema_version": 1, "attempts": [{"status": "accepted"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    app = create_app(project_root=tmp_path)
    list_response = request_json(app, "GET", "/api/jobs/job-test-001/artifacts")

    assert list_response.status_code == 200
    items = list_response.json()["items"]
    item_ids = {item["id"] for item in items}
    assert any(item["id"] == "transcribe-text" and item["exists"] for item in items)
    assert any(item["id"] == "refine-markdown" and item["exists"] for item in items)
    assert any(item["id"] == "refine-diagnostics" and item["exists"] for item in items)
    assert "align-json" not in item_ids
    assert "classify-json" not in item_ids

    content_response = request_json(app, "GET", "/api/jobs/job-test-001/artifacts/refine-markdown")

    assert content_response.status_code == 200
    assert content_response.json()["content"] == "# 校对结果\n\n正文"

    diagnostics_response = request_json(app, "GET", "/api/jobs/job-test-001/artifacts/refine-diagnostics")
    assert diagnostics_response.status_code == 200
    assert json.loads(diagnostics_response.json()["content"])["attempts"][0]["status"] == "accepted"


def test_get_job_artifact_rejects_unknown_artifact_id(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_dir = tmp_path / "data/jobs/job-test-001"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:31:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/jobs/job-test-001/artifacts/unknown-artifact")

    assert response.status_code == 404


def test_get_batch_item_artifacts_uses_batch_membership_without_child_state_file(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    batch_id = "batch-test-001"
    item_job_id = "batch-child-001"
    job_dir = tmp_path / "data/jobs" / item_job_id
    (job_dir / "intermediate/asr").mkdir(parents=True, exist_ok=True)
    (job_dir / "intermediate/asr/source.txt").write_text("批量子任务转写文本", encoding="utf-8")

    batch_state = create_initial_state(batch_id, "batch")
    batch_state.update(
        {
            "status": "failed",
            "items": [
                {
                    "job_id": item_job_id,
                    "status": "failed",
                    "video_source": "/tmp/video.mp4",
                }
            ],
        }
    )
    write_json_file(tmp_path / "data/jobs/batches" / batch_id / "state.json", batch_state)
    assert not (job_dir / "state.json").exists()

    app = create_app(project_root=tmp_path)
    list_response = request_json(app, "GET", f"/api/batches/{batch_id}/items/{item_job_id}/artifacts")

    assert list_response.status_code == 200
    assert any(
        item["id"] == "transcribe-text" and item["exists"]
        for item in list_response.json()["items"]
    )

    content_response = request_json(
        app,
        "GET",
        f"/api/batches/{batch_id}/items/{item_job_id}/artifacts/transcribe-text",
    )

    assert content_response.status_code == 200
    assert content_response.json()["content"] == "批量子任务转写文本"

    missing_item_response = request_json(
        app,
        "GET",
        f"/api/batches/{batch_id}/items/not-in-batch/artifacts",
    )

    assert missing_item_response.status_code == 404
    assert "批量子任务不存在" in missing_item_response.json()["detail"]


def test_download_job_result_returns_generated_markdown(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_dir = tmp_path / "data/jobs/job-test-001"
    result_path = tmp_path / "data/output/final/lesson.md"
    job_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("# 最终稿\n\n正文", encoding="utf-8")
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:31:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(result_path),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/jobs/job-test-001/result")

    assert response.status_code == 200
    assert response.content == "# 最终稿\n\n正文".encode("utf-8")
    assert "attachment" in response.headers["content-disposition"]
    assert "lesson.md" in response.headers["content-disposition"]


def test_download_job_result_returns_generated_txt_from_markdown(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_dir = tmp_path / "data/jobs/job-test-001"
    result_path = tmp_path / "data/output/final/lesson.md"
    job_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("# 最终稿\n\n正文", encoding="utf-8")
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:31:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(result_path),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/jobs/job-test-001/result?format=txt")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.content == "最终稿\n\n正文\n".encode("utf-8")
    assert "lesson.txt" in response.headers["content-disposition"]


def test_download_job_result_rejects_unknown_format(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_dir = tmp_path / "data/jobs/job-test-001"
    result_path = tmp_path / "data/output/final/lesson.md"
    job_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("# 最终稿\n", encoding="utf-8")
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:31:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(result_path),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/jobs/job-test-001/result?format=pdf")

    assert response.status_code == 400


def test_download_job_result_rejects_unfinished_job(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_dir = tmp_path / "data/jobs/job-test-001"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "running",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:31:00+08:00",
                "current_stage": "refine",
                "error_message": "",
                "output_path": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = create_app(project_root=tmp_path)
    app.state.active_jobs.add("job-test-001")

    response = request_json(app, "GET", "/api/jobs/job-test-001/result")

    assert response.status_code == 400


def test_download_job_result_falls_back_to_job_internal_final_output(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_dir = tmp_path / "data/jobs/job-test-001"
    internal_result_path = job_dir / "output/final/source.md"
    internal_result_path.parent.mkdir(parents=True, exist_ok=True)
    internal_result_path.write_text("# 内部保留结果\n", encoding="utf-8")
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:31:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(tmp_path / "missing/final.md"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/jobs/job-test-001/result")

    assert response.status_code == 200
    assert response.content == "# 内部保留结果\n".encode("utf-8")


def test_get_jobs_lists_persisted_states(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_a = tmp_path / "data/jobs/job-a"
    job_b = tmp_path / "data/jobs/job-b"
    job_a.mkdir(parents=True, exist_ok=True)
    job_b.mkdir(parents=True, exist_ok=True)
    (job_a / "state.json").write_text(
        json.dumps(
            {
                "id": "job-a",
                "kind": "job",
                "status": "success",
                "created_at": "2026-03-16T01:00:00+08:00",
                "updated_at": "2026-03-16T01:10:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": "/tmp/a.md",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (job_a / "manifest.json").write_text(
        json.dumps(
            {
                "video_source": str(tmp_path / "videos/lesson-a.mp4"),
                "reference_source": str(tmp_path / "references/chapter-a.pdf"),
                "output_dir": str(tmp_path / "deliverables"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (job_b / "state.json").write_text(
        json.dumps(
            {
                "id": "job-b",
                "kind": "job",
                "status": "running",
                "created_at": "2026-03-16T02:00:00+08:00",
                "updated_at": "2026-03-16T02:05:00+08:00",
                "current_stage": "refine",
                "error_message": "",
                "output_path": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = create_app(project_root=tmp_path)
    app.state.active_jobs.add("job-b")
    response = request_json(app, "GET", "/api/jobs")

    assert response.status_code == 200
    payload = response.json()["items"]
    assert [item["id"] for item in payload] == ["job-b", "job-a"]
    assert payload[1]["input_summary"] == {
        "video_source": str(tmp_path / "videos/lesson-a.mp4"),
        "reference_source": str(tmp_path / "references/chapter-a.pdf"),
        "output_dir": str(tmp_path / "deliverables"),
    }


def test_get_batch_status_returns_persisted_state(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    batch_dir = tmp_path / "data/jobs/batches/batch-test-001"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "batch-test-001",
                "kind": "batch",
                "status": "running",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "transcribe",
                "error_message": "",
                "output_path": "",
                "items": [{"job_id": "job-1", "status": "running"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = create_app(project_root=tmp_path)
    app.state.active_jobs.add("batch-test-001")
    response = request_json(app, "GET", "/api/batches/batch-test-001")

    assert response.status_code == 200
    assert response.json()["id"] == "batch-test-001"
    assert response.json()["items"][0]["job_id"] == "job-1"


def test_get_batches_lists_persisted_batch_states(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    batch_dir = tmp_path / "data/jobs/batches/batch-test-001"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "batch-test-001",
                "kind": "batch",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": "/tmp/summary.json",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/batches")

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "batch-test-001"


def test_download_batch_item_result_returns_child_markdown(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    batch_dir = tmp_path / "data/jobs/batches/batch-test-001"
    result_path = tmp_path / "data/output/final/lesson-a.md"
    batch_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("# 子任务结果\n", encoding="utf-8")
    (batch_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "batch-test-001",
                "kind": "batch",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(batch_dir / "summary.json"),
                "items": [
                    {
                        "job_id": "job-a",
                        "status": "success",
                        "copied_output_path": str(result_path),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/batches/batch-test-001/items/job-a/result")

    assert response.status_code == 200
    assert response.content == "# 子任务结果\n".encode("utf-8")
    assert "lesson-a.md" in response.headers["content-disposition"]


def test_download_batch_item_result_returns_child_txt(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    batch_dir = tmp_path / "data/jobs/batches/batch-test-001"
    result_path = tmp_path / "data/output/final/lesson-a.md"
    batch_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("# 子任务结果\n", encoding="utf-8")
    (batch_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "batch-test-001",
                "kind": "batch",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(batch_dir / "summary.json"),
                "items": [
                    {
                        "job_id": "job-a",
                        "status": "success",
                        "copied_output_path": str(result_path),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/batches/batch-test-001/items/job-a/result?format=txt")

    assert response.status_code == 200
    assert response.content == "子任务结果\n".encode("utf-8")
    assert "lesson-a.txt" in response.headers["content-disposition"]


def test_download_batch_result_packs_summary_and_successful_outputs(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    batch_dir = tmp_path / "data/jobs/batches/batch-test-001"
    result_path = tmp_path / "data/output/final/lesson-a.md"
    batch_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("# 子任务结果\n", encoding="utf-8")
    (batch_dir / "summary.md").write_text("# Batch Summary\n", encoding="utf-8")
    (batch_dir / "summary.json").write_text('{"total": 1}', encoding="utf-8")
    (batch_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "batch-test-001",
                "kind": "batch",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(batch_dir / "summary.json"),
                "items": [
                    {
                        "job_id": "job-a",
                        "status": "success",
                        "copied_output_path": str(result_path),
                    },
                    {
                        "job_id": "job-b",
                        "status": "failed",
                        "copied_output_path": "",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/batches/batch-test-001/result")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "summary.md" in names
        assert "summary.json" in names
        assert "results/lesson-a.md" in names
        assert archive.read("results/lesson-a.md").decode("utf-8") == "# 子任务结果\n"


def test_download_batch_result_packs_txt_outputs(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    batch_dir = tmp_path / "data/jobs/batches/batch-test-001"
    result_path = tmp_path / "data/output/final/lesson-a.md"
    batch_dir.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("# 子任务结果\n", encoding="utf-8")
    (batch_dir / "summary.md").write_text("# Batch Summary\n", encoding="utf-8")
    (batch_dir / "summary.json").write_text('{"total": 1}', encoding="utf-8")
    (batch_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "batch-test-001",
                "kind": "batch",
                "status": "success",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(batch_dir / "summary.json"),
                "items": [
                    {
                        "job_id": "job-a",
                        "status": "success",
                        "copied_output_path": str(result_path),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/batches/batch-test-001/result?format=txt")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "batch-test-001-results-txt.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "summary.md" in names
        assert "summary.json" in names
        assert "results/lesson-a.txt" in names
        assert archive.read("results/lesson-a.txt").decode("utf-8") == "子任务结果\n"


def test_get_stage_runs_lists_persisted_stage_states(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    run_dir = tmp_path / "data/jobs/stage-runs/run-test-001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "run-test-001",
                "kind": "stage-run",
                "status": "failed",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "refine",
                "error_message": "测试失败",
                "output_path": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", "/api/stage-runs")

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "run-test-001"


def test_get_fs_list_filters_hidden_entries_and_rejects_outside_roots(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    browse_root = tmp_path / "workspace"
    browse_root.mkdir(parents=True, exist_ok=True)
    (browse_root / "visible.txt").write_text("ok", encoding="utf-8")
    (browse_root / ".hidden.txt").write_text("secret", encoding="utf-8")
    (browse_root / "folder").mkdir()

    app = create_app(project_root=tmp_path)
    response = request_json(app, "GET", "/api/fs/list", params={"path": str(browse_root), "type": "all"})

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["folder", "visible.txt"]

    denied = request_json(app, "GET", "/api/fs/list", params={"path": "/tmp", "type": "all"})
    assert denied.status_code == 403


def test_post_upload_writes_video_under_project_upload_dir(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path)

    response = request_raw(
        app,
        "POST",
        "/api/uploads",
        params={"kind": "video", "filename": "../课程:1.MP4"},
        content=b"video-bytes",
    )

    assert response.status_code == 200
    payload = response.json()
    uploaded_path = Path(payload["path"])
    assert payload["kind"] == "video"
    assert payload["name"] == "课程_1.mp4"
    assert payload["size"] == len(b"video-bytes")
    assert payload["directory"] == str(uploaded_path.parent)
    assert uploaded_path.read_bytes() == b"video-bytes"
    assert uploaded_path.is_relative_to(tmp_path / "data/uploads/videos")


def test_post_upload_writes_pdf_ocr_input_under_dedicated_upload_dir(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    response = request_raw(
        create_app(project_root=tmp_path),
        "POST",
        "/api/uploads",
        params={"kind": "pdf_ocr", "filename": "书籍.PDF"},
        content=b"pdf-bytes",
    )

    assert response.status_code == 200
    payload = response.json()
    uploaded_path = Path(payload["path"])
    assert payload["kind"] == "pdf_ocr"
    assert payload["name"] == "书籍.pdf"
    assert uploaded_path.read_bytes() == b"pdf-bytes"
    assert uploaded_path.is_relative_to(tmp_path / "data/uploads/pdf-ocr")


def test_pdf_book_ocr_api_creates_task_and_serves_task_result(tmp_path: Path) -> None:
    from api_server import create_app
    from src.web.pdf_book_ocr import build_pdf_book_ocr_task_paths

    write_minimal_settings(tmp_path)
    input_path = tmp_path / "data/uploads/pdf-ocr/20260713/group-001/book.pdf"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"pdf")
    app = create_app(project_root=tmp_path, run_tasks_inline=True)
    seen: dict[str, object] = {}

    def fake_execute_pdf_book_ocr(*, app, task_id: str, payload: dict) -> None:
        seen["task_id"] = task_id
        seen["payload"] = payload
        task_paths = build_pdf_book_ocr_task_paths(tmp_path, task_id)
        result_path = task_paths.output_dir / "book.txt"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text("OCR 文本", encoding="utf-8")
        app.state.update_state(
            task_paths.state_path,
            status="success",
            current_stage="done",
            output_path=str(task_paths.output_dir),
            total=1,
            success=1,
            failed=0,
            items=[
                {
                    "source_file": "book.pdf",
                    "output_file": "book.txt",
                    "success": True,
                    "text_length": len("OCR 文本"),
                    "warnings": [],
                    "error": "",
                }
            ],
        )
        app.state.active_jobs.discard(task_id)

    app.state.execute_pdf_book_ocr = fake_execute_pdf_book_ocr
    submit_response = request_json(
        app,
        "POST",
        "/api/pdf-book-ocr",
        json_body={"input_path": str(input_path)},
    )

    assert submit_response.status_code == 202
    task_id = submit_response.json()["task_id"]
    assert seen["task_id"] == task_id
    assert seen["payload"] == {
        "input_path": str(input_path),
        "ocr_fast_mode": None,
        "config": None,
        "ocr_model": None,
        "ocr_reasoning_effort": None,
        "ocr_max_concurrency": None,
        "ocr_submit_interval_seconds": None,
    }

    status_response = request_json(app, "GET", f"/api/pdf-book-ocr/{task_id}")
    assert status_response.status_code == 200
    assert status_response.json()["items"][0]["output_file"] == "book.txt"

    list_response = request_json(app, "GET", "/api/pdf-book-ocr")
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()["items"]] == [task_id]

    result_response = request_json(app, "GET", f"/api/pdf-book-ocr/{task_id}/results/book.txt")
    assert result_response.status_code == 200
    assert result_response.content == "OCR 文本".encode("utf-8")
    assert "book.txt" in result_response.headers["content-disposition"]

    epub_response = request_json(
        app,
        "GET",
        f"/api/pdf-book-ocr/{task_id}/results/book.txt",
        params={"format": "epub"},
    )
    assert epub_response.status_code == 422
    assert not (build_pdf_book_ocr_task_paths(tmp_path, task_id).output_dir / "book.epub").exists()

    archive_response = request_json(app, "GET", f"/api/pdf-book-ocr/{task_id}/download")
    assert archive_response.status_code == 200
    assert archive_response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
        assert archive.read("books/book.txt") == "OCR 文本".encode("utf-8")
        assert json.loads(archive.read("summary.json"))["included_books"] == ["book.txt"]


def test_pdf_book_ocr_api_rejects_input_outside_uploaded_pdf_area(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    outside_input = tmp_path / "book.pdf"
    outside_input.write_bytes(b"pdf")

    response = request_json(
        create_app(project_root=tmp_path),
        "POST",
        "/api/pdf-book-ocr",
        json_body={"input_path": str(outside_input)},
    )

    assert response.status_code == 400
    assert "本页面上传" in response.text


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("ocr_max_concurrency", 0),
        ("ocr_submit_interval_seconds", -0.1),
    ],
)
def test_pdf_book_ocr_api_rejects_invalid_scheduling_values(
    tmp_path: Path,
    field_name: str,
    value: float,
) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    input_path = tmp_path / "data/uploads/pdf-ocr/20260713/group-001/book.pdf"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"pdf")

    response = request_json(
        create_app(project_root=tmp_path),
        "POST",
        "/api/pdf-book-ocr",
        json_body={"input_path": str(input_path), field_name: value},
    )

    assert response.status_code == 422


def test_pdf_book_ocr_retry_reuses_task_and_request_payload(tmp_path: Path) -> None:
    from api_server import create_app
    from src.web.pdf_book_ocr import build_pdf_book_ocr_task_paths, create_pdf_book_ocr_task_id
    from src.web.state_store import create_initial_state, read_json_file, write_json_file

    write_minimal_settings(tmp_path)
    input_path = tmp_path / "data/uploads/pdf-ocr/20260713/group-001/book.pdf"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"pdf")
    app = create_app(project_root=tmp_path, run_tasks_inline=True)
    task_id = create_pdf_book_ocr_task_id()
    task_paths = build_pdf_book_ocr_task_paths(tmp_path, task_id)
    state = create_initial_state(task_id, "pdf-ocr")
    state.update(
        {
            "status": "partial",
            "input_summary": {"input_path": str(input_path)},
            "request_payload": {
                "input_path": str(input_path),
                "config": None,
                "ocr_model": "gpt-5.4-mini",
                "ocr_reasoning_effort": "high",
                "ocr_max_concurrency": 12,
                "ocr_submit_interval_seconds": 2.5,
            },
        }
    )
    write_json_file(task_paths.state_path, state)
    seen: dict[str, object] = {}

    def fake_execute_pdf_book_ocr(*, app, task_id: str, payload: dict) -> None:
        seen["task_id"] = task_id
        seen["payload"] = payload
        app.state.active_jobs.discard(task_id)

    app.state.execute_pdf_book_ocr = fake_execute_pdf_book_ocr

    response = request_json(app, "POST", f"/api/pdf-book-ocr/{task_id}/retry")

    assert response.status_code == 202
    assert response.json() == {"task_id": task_id}
    assert seen == {
        "task_id": task_id,
        "payload": {
            "input_path": str(input_path),
            "ocr_fast_mode": False,
            "config": None,
            "ocr_model": "gpt-5.4-mini",
            "ocr_reasoning_effort": "high",
            "ocr_max_concurrency": 12,
            "ocr_submit_interval_seconds": 2.5,
        },
    }
    updated_state = read_json_file(task_paths.state_path)
    assert updated_state["status"] == "pending"
    assert updated_state["current_stage"] == "resume"
    assert updated_state["resume_count"] == 1


def test_pdf_book_ocr_running_task_becomes_resumable_after_service_restart(tmp_path: Path) -> None:
    from api_server import create_app
    from src.web.pdf_book_ocr import build_pdf_book_ocr_task_paths, create_pdf_book_ocr_task_id
    from src.web.state_store import create_initial_state, write_json_file

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)
    task_id = create_pdf_book_ocr_task_id()
    task_paths = build_pdf_book_ocr_task_paths(tmp_path, task_id)
    state = create_initial_state(task_id, "pdf-ocr")
    state.update({"status": "running", "pages_total": 603, "pages_completed": 194})
    write_json_file(task_paths.state_path, state)

    response = request_json(app, "GET", f"/api/pdf-book-ocr/{task_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "partial"
    assert payload["resumable"] is True
    assert "已完成页面仍保留" in payload["error_message"]


def test_post_upload_groups_directory_files(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path)

    first_response = request_raw(
        app,
        "POST",
        "/api/uploads",
        params={
            "kind": "reference",
            "filename": "chapter-01.txt",
            "group_id": "group-001",
            "relative_path": "references/chapter-01.txt",
        },
        content=b"chapter 1",
    )
    second_response = request_raw(
        app,
        "POST",
        "/api/uploads",
        params={
            "kind": "reference",
            "filename": "chapter-02.md",
            "group_id": "group-001",
            "relative_path": "references/chapter-02.md",
        },
        content=b"chapter 2",
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_payload = first_response.json()
    second_payload = second_response.json()
    assert first_payload["directory"] == second_payload["directory"]
    uploaded_dir = Path(first_payload["directory"])
    assert uploaded_dir.is_relative_to(tmp_path / "data/uploads/reference")
    assert (uploaded_dir / "chapter-01.txt").read_bytes() == b"chapter 1"
    assert (uploaded_dir / "chapter-02.md").read_bytes() == b"chapter 2"


def test_post_upload_rejects_unsupported_extension(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path)

    response = request_raw(
        app,
        "POST",
        "/api/uploads",
        params={"kind": "video", "filename": "lesson.txt"},
        content=b"not-video",
    )

    assert response.status_code == 400
    assert "不支持的上传文件类型" in response.text


def test_post_upload_rejects_empty_file(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path)

    response = request_raw(
        app,
        "POST",
        "/api/uploads",
        params={"kind": "reference", "filename": "chapter.txt"},
        content=b"",
    )

    assert response.status_code == 400
    assert "上传文件为空" in response.text


def test_post_stage_input_writes_file_under_stage_input_upload_root(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    response = request_raw(
        create_app(project_root=tmp_path),
        "POST",
        "/api/stage-inputs/transcribe/audio",
        content=b"audio-bytes",
        params={"filename": "lesson.wav"},
    )

    assert response.status_code == 200
    uploaded_path = Path(response.json()["path"])
    assert uploaded_path.is_relative_to(tmp_path / "data/uploads/stage-inputs/transcribe/audio")
    assert uploaded_path.read_bytes() == b"audio-bytes"


def test_post_stage_input_rejects_file_extension_outside_slot_contract(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    response = request_raw(
        create_app(project_root=tmp_path),
        "POST",
        "/api/stage-inputs/transcribe/audio",
        content=b"not-audio",
        params={"filename": "lesson.txt"},
    )

    assert response.status_code == 400
    assert "音频文件不支持 .txt" in response.text


def test_post_stage_file_run_rejects_path_outside_stage_input_upload_root(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    outside_path = tmp_path / "lesson.wav"
    outside_path.write_bytes(b"audio")

    response = request_json(
        create_app(project_root=tmp_path),
        "POST",
        "/api/stages/transcribe/file-run",
        json_body={
            "input_files": {"audio": str(outside_path)},
            "result_name": "lesson-asr",
        },
    )

    assert response.status_code == 400
    assert "暂存文件" in response.text


def test_stage_file_run_uses_isolated_workspace_and_downloads_result(tmp_path: Path, monkeypatch) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)

    def fake_run_stage(
        stage_name,
        loaded_settings,
        logger,
        backend_override=None,
        prepare_reference_progress_callback=None,
    ) -> int:
        _ = logger, backend_override, prepare_reference_progress_callback
        assert stage_name == "transcribe"
        assert (loaded_settings.path_for("audio_dir") / "source.wav").read_bytes() == b"audio-bytes"
        output_path = loaded_settings.path_for("asr_dir") / "source.txt"
        output_path.write_text("转录结果", encoding="utf-8")
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", fake_run_stage)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)
    upload_response = request_raw(
        app,
        "POST",
        "/api/stage-inputs/transcribe/audio",
        content=b"audio-bytes",
        params={"filename": "lesson.wav"},
    )
    assert upload_response.status_code == 200

    submit_response = request_json(
        app,
        "POST",
        "/api/stages/transcribe/file-run",
        json_body={
            "input_files": {"audio": upload_response.json()["path"]},
            "result_name": "lesson-asr",
            "profile": "local_cpu",
        },
    )

    assert submit_response.status_code == 202
    run_id = submit_response.json()["run_id"]
    state = request_json(app, "GET", f"/api/stage-runs/{run_id}").json()
    assert state["status"] == "success"
    assert state["run_mode"] == "file"
    assert state["download_name"] == "lesson-asr.zip"
    assert not (tmp_path / "data/input/audio/source.wav").exists()

    result_response = request_json(app, "GET", f"/api/stage-runs/{run_id}/result")
    assert result_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(result_response.content)) as archive:
        assert archive.namelist() == ["asr/source.txt"]
        assert archive.read("asr/source.txt").decode("utf-8") == "转录结果"


def test_stage_file_run_executes_export_with_uploaded_refinement_json(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)
    upload_response = request_raw(
        app,
        "POST",
        "/api/stage-inputs/export-markdown/refined_json",
        content=json.dumps({"final_markdown": "# 整理稿\n\n正文"}, ensure_ascii=False).encode("utf-8"),
        params={"filename": "refined-result.json"},
    )
    assert upload_response.status_code == 200

    submit_response = request_json(
        app,
        "POST",
        "/api/stages/export-markdown/file-run",
        json_body={
            "input_files": {"refined_json": upload_response.json()["path"]},
            "result_name": "exported-draft",
        },
    )

    assert submit_response.status_code == 202
    run_id = submit_response.json()["run_id"]
    state = request_json(app, "GET", f"/api/stage-runs/{run_id}").json()
    assert state["status"] == "success"
    assert not (tmp_path / "data/intermediate/refined/source.json").exists()

    result_response = request_json(app, "GET", f"/api/stage-runs/{run_id}/result")
    assert result_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(result_response.content)) as archive:
        assert archive.read("final/source.md").decode("utf-8") == "# 整理稿\n\n正文\n"
        assert "final/source.txt" in archive.namelist()


def test_stage_file_result_download_rejects_archive_outside_its_run_directory(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    run_id = "stage-file-outside"
    state_path = tmp_path / "data/jobs/stage-runs" / run_id / "state.json"
    outside_archive = tmp_path / "outside.zip"
    outside_archive.write_bytes(b"not-a-real-archive")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "id": run_id,
                "kind": "stage-run",
                "status": "success",
                "run_mode": "file",
                "output_path": str(outside_archive),
                "download_name": "outside.zip",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = request_json(create_app(project_root=tmp_path), "GET", f"/api/stage-runs/{run_id}/result")

    assert response.status_code == 500
    assert "结果路径无效" in response.text


def test_post_job_returns_job_id_and_persists_state(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    def fake_execute_single_job(*, app, job_id: str, payload: dict) -> None:
        _ = payload
        app.state.update_state(
            app.state.job_state_path(job_id),
            status="success",
            current_stage="done",
            output_path=str(tmp_path / "deliverables/final.md"),
        )

    app.state.execute_single_job = fake_execute_single_job

    response = request_json(
        app,
        "POST",
        "/api/jobs",
        json_body={
            "video": str(tmp_path / "lesson.mp4"),
            "reference": str(tmp_path / "chapter.txt"),
            "output_dir": str(tmp_path / "deliverables"),
        },
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    state = json.loads((tmp_path / "data/jobs" / job_id / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "success"
    assert state["output_path"].endswith("final.md")
    assert state["input_summary"] == {
        "content_type": "book_club",
        "video_source": str(tmp_path / "lesson.mp4"),
        "reference_source": str(tmp_path / "chapter.txt"),
        "output_dir": str(tmp_path / "deliverables"),
    }


def test_post_conversation_job_allows_missing_reference(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    def fake_execute_single_job(*, app, job_id: str, payload: dict) -> None:
        assert payload["content_type"] == "conversation"
        assert payload.get("reference") is None
        app.state.update_state(
            app.state.job_state_path(job_id),
            status="success",
            current_stage="done",
            output_path=str(tmp_path / "deliverables/final.md"),
        )

    app.state.execute_single_job = fake_execute_single_job

    response = request_json(
        app,
        "POST",
        "/api/jobs",
        json_body={
            "video": str(tmp_path / "conversation.mp4"),
            "output_dir": str(tmp_path / "deliverables"),
            "content_type": "conversation",
        },
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    state = json.loads((tmp_path / "data/jobs" / job_id / "state.json").read_text(encoding="utf-8"))
    assert state["input_summary"] == {
        "content_type": "conversation",
        "video_source": str(tmp_path / "conversation.mp4"),
        "output_dir": str(tmp_path / "deliverables"),
    }


def test_post_job_applies_saved_frontend_model_settings(tmp_path: Path, monkeypatch) -> None:
    from api_server import create_app
    from src.refine_utils import load_markdown_assemble_prompt

    monkeypatch.delenv("CODEX_LB_API_KEY", raising=False)
    write_minimal_settings(tmp_path)
    video_path = tmp_path / "lesson.mp4"
    reference_path = tmp_path / "chapter.txt"
    output_dir = tmp_path / "deliverables"
    video_path.write_bytes(b"video")
    reference_path.write_text("参考", encoding="utf-8")
    seen: dict[str, str] = {}

    def fake_run_stage(
        stage_name,
        job_loaded_settings,
        logger,
        backend_override=None,
        prepare_reference_progress_callback=None,
    ) -> int:
        _ = logger, prepare_reference_progress_callback
        if stage_name == "refine":
            seen["backend_override"] = backend_override or ""
            seen["model"] = job_loaded_settings.settings.llm.model
            seen["reasoning_effort"] = job_loaded_settings.settings.llm.reasoning_effort
            seen["ocr_backend"] = job_loaded_settings.settings.reference.ai_ocr_backend
            seen["ocr_model"] = job_loaded_settings.settings.reference.codex_ocr_model
            seen["ocr_reasoning_effort"] = job_loaded_settings.settings.reference.codex_ocr_reasoning_effort
            seen["refine_prompt"] = load_markdown_assemble_prompt(job_loaded_settings)
        if stage_name == "export-markdown":
            final_dir = job_loaded_settings.path_for("final_dir")
            final_dir.mkdir(parents=True, exist_ok=True)
            (final_dir / "source.md").write_text("# 结果\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", fake_run_stage)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    request_json(
        app,
        "PUT",
        "/api/frontend-settings",
        json_body={
            "model": "gpt-6.1-sol",
            "reasoning_effort": "max",
            "backend": "agy",
            "ocr_backend": "agy",
            "ocr_model": "gpt-6-luna",
            "ocr_reasoning_effort": "high",
        },
    )
    response = request_json(
        app,
        "POST",
        "/api/jobs",
        json_body={
            "video": str(video_path),
            "reference": str(reference_path),
            "output_dir": str(output_dir),
            "refine_prompt": "# 单任务自定义阶段六指令\n\n请保留讲解原话。",
        },
    )

    assert response.status_code == 202
    assert seen == {
        "backend_override": "agy",
        "model": "gpt-6.1-sol",
        "reasoning_effort": "max",
        "ocr_backend": "agy",
        "ocr_model": "gpt-6-luna",
        "ocr_reasoning_effort": "high",
        "refine_prompt": "# 单任务自定义阶段六指令\n\n请保留讲解原话。",
    }


def test_post_job_rerun_returns_job_id_and_updates_existing_state(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_dir = tmp_path / "data/jobs/job-test-001"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "failed",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "refine",
                "error_message": "旧错误",
                "output_path": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    def fake_execute_job_rerun(*, app, job_id: str, payload: dict) -> None:
        assert payload["start_stage"] == "refine"
        app.state.update_state(
            app.state.job_state_path(job_id),
            status="success",
            current_stage="done",
            error_message="",
            output_path=str(tmp_path / "deliverables/final.md"),
        )
        app.state.active_jobs.discard(job_id)

    app.state.execute_job_rerun = fake_execute_job_rerun

    response = request_json(
        app,
        "POST",
        "/api/jobs/job-test-001/rerun",
        json_body={"start_stage": "refine"},
    )

    assert response.status_code == 202
    assert response.json()["job_id"] == "job-test-001"
    state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "success"
    assert state["current_stage"] == "done"
    assert state["error_message"] == ""


def test_post_job_rerun_rejects_active_job(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_dir = tmp_path / "data/jobs/job-test-001"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "job-test-001",
                "kind": "job",
                "status": "running",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "transcribe",
                "error_message": "",
                "output_path": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = create_app(project_root=tmp_path, run_tasks_inline=True)
    app.state.active_jobs.add("job-test-001")

    response = request_json(
        app,
        "POST",
        "/api/jobs/job-test-001/rerun",
        json_body={"start_stage": "refine"},
    )

    assert response.status_code == 400
    assert "不能重跑正在运行的任务" in response.text


def test_resolve_rerun_stages_returns_selected_suffix() -> None:
    from src.web.tasks import resolve_rerun_stages

    assert resolve_rerun_stages(
        ["extract_audio", "transcribe", "prepare_reference", "refine", "export_markdown"],
        "prepare-reference",
    ) == ["prepare-reference", "refine", "export-markdown"]


def test_post_job_rerun_executes_selected_stage_suffix(tmp_path: Path, monkeypatch) -> None:
    from api_server import create_app
    from src.config_loader import load_settings
    from src.job_runner import build_job_paths, write_job_settings

    write_minimal_settings(tmp_path)
    base_loaded_settings = load_settings(project_root=tmp_path)
    job_id = "job-test-001"
    job_paths = build_job_paths(tmp_path, job_id)
    write_job_settings(
        project_root=tmp_path,
        loaded_settings=base_loaded_settings,
        job_paths=job_paths,
        profile_name="local_cpu",
    )
    (job_paths.manifest_path).write_text(
        json.dumps(
            {
                "job_id": job_id,
                "profile": "local_cpu",
                "video_source": str(tmp_path / "lesson.mp4"),
                "reference_source": str(tmp_path / "reference.txt"),
                "output_dir": str(tmp_path / "deliverables"),
                "book_name": "",
                "chapter": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (job_paths.job_root / "state.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "kind": "job",
                "status": "failed",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "refine",
                "error_message": "旧错误",
                "output_path": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    called_stages: list[str] = []

    def fake_run_stage(
        stage_name,
        job_loaded_settings,
        logger,
        backend_override=None,
        prepare_reference_progress_callback=None,
    ) -> int:
        _ = logger, backend_override, prepare_reference_progress_callback
        called_stages.append(stage_name)
        if stage_name == "export-markdown":
            final_dir = job_loaded_settings.path_for("final_dir")
            final_dir.mkdir(parents=True, exist_ok=True)
            (final_dir / "source.md").write_text("# 重跑结果\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", fake_run_stage)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    response = request_json(
        app,
        "POST",
        f"/api/jobs/{job_id}/rerun",
        json_body={"start_stage": "prepare-reference"},
    )

    assert response.status_code == 202
    assert called_stages == ["prepare-reference", "refine", "export-markdown"]
    state = json.loads((job_paths.job_root / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "success"
    assert state["current_stage"] == "done"
    assert state["error_message"] == ""
    assert state["output_path"].endswith("lesson.md")
    assert Path(state["output_path"]).exists()


def test_post_batch_item_rerun_executes_suffix_and_updates_batch_state(tmp_path: Path, monkeypatch) -> None:
    from api_server import create_app
    from src.config_loader import load_settings
    from src.job_runner import build_batch_root, build_job_paths, write_job_settings

    write_minimal_settings(tmp_path)
    base_loaded_settings = load_settings(project_root=tmp_path)
    batch_id = "batch-test-001"
    job_id = "job-test-001"
    job_paths = build_job_paths(tmp_path, job_id)
    write_job_settings(
        project_root=tmp_path,
        loaded_settings=base_loaded_settings,
        job_paths=job_paths,
        profile_name="local_cpu",
    )
    (job_paths.manifest_path).write_text(
        json.dumps(
            {
                "job_id": job_id,
                "profile": "local_cpu",
                "video_source": str(tmp_path / "lesson.mp4"),
                "reference_source": str(tmp_path / "reference.txt"),
                "output_dir": str(tmp_path / "deliverables"),
                "book_name": "",
                "chapter": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    batch_root = build_batch_root(tmp_path, batch_id)
    batch_root.mkdir(parents=True, exist_ok=True)
    (batch_root / "state.json").write_text(
        json.dumps(
            {
                "id": batch_id,
                "kind": "batch",
                "status": "failed",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": str(batch_root / "summary.json"),
                "total": 1,
                "success": 0,
                "failed": 1,
                "items": [
                    {
                        "job_id": job_id,
                        "mode": "paired-dir",
                        "video_source": str(tmp_path / "lesson.mp4"),
                        "reference_source": str(tmp_path / "reference.txt"),
                        "output_dir": str(tmp_path / "deliverables"),
                        "book_name": "",
                        "chapter": "",
                        "glossary_file": "",
                        "status": "failed",
                        "failed_stage": "refine",
                        "error_message": "旧错误",
                        "copied_output_path": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    called_stages: list[str] = []

    def fake_run_stage(
        stage_name,
        job_loaded_settings,
        logger,
        backend_override=None,
        prepare_reference_progress_callback=None,
    ) -> int:
        _ = logger, backend_override, prepare_reference_progress_callback
        called_stages.append(stage_name)
        if stage_name == "export-markdown":
            final_dir = job_loaded_settings.path_for("final_dir")
            final_dir.mkdir(parents=True, exist_ok=True)
            (final_dir / "source.md").write_text("# 批量子任务重跑结果\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", fake_run_stage)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    response = request_json(
        app,
        "POST",
        f"/api/batches/{batch_id}/items/{job_id}/rerun",
        json_body={"start_stage": "refine"},
    )

    assert response.status_code == 202
    assert response.json() == {"batch_id": batch_id, "job_id": job_id}
    assert called_stages == ["refine", "export-markdown"]
    state = json.loads((batch_root / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "success"
    assert state["success"] == 1
    assert state["failed"] == 0
    assert state["items"][0]["status"] == "success"
    assert state["items"][0]["failed_stage"] == ""
    assert state["items"][0]["copied_output_path"].endswith("lesson.md")
    assert Path(state["items"][0]["copied_output_path"]).exists()
    summary = json.loads((batch_root / "summary.json").read_text(encoding="utf-8"))
    assert summary["success"] == 1
    assert summary["items"][0]["status"] == "success"
    assert (batch_root / "item-reruns" / job_id / "state.json").exists()
    assert not (job_paths.job_root / "state.json").exists()


def test_post_batch_item_rerun_rejects_active_or_non_terminal_item(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    batch_id = "batch-test-001"
    batch_dir = tmp_path / "data/jobs/batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "state.json").write_text(
        json.dumps(
            {
                "id": batch_id,
                "kind": "batch",
                "status": "failed",
                "created_at": "2026-03-16T01:30:00+08:00",
                "updated_at": "2026-03-16T01:40:00+08:00",
                "current_stage": "done",
                "error_message": "",
                "output_path": "",
                "items": [{"job_id": "job-a", "status": "pending"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    response = request_json(
        app,
        "POST",
        f"/api/batches/{batch_id}/items/job-a/rerun",
        json_body={"start_stage": "refine"},
    )

    assert response.status_code == 400
    assert "只能重跑已结束或等待补页的批量子任务" in response.text

    app.state.active_jobs.add(batch_id)
    response = request_json(
        app,
        "POST",
        f"/api/batches/{batch_id}/items/job-a/rerun",
        json_body={"start_stage": "refine"},
    )

    assert response.status_code == 400
    assert "不能在批量任务运行中重跑子任务" in response.text


def test_single_job_stops_after_partial_reference_ocr_and_keeps_task_resumable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    video_path = tmp_path / "lesson.mp4"
    reference_path = tmp_path / "book.pdf"
    output_dir = tmp_path / "deliverables"
    video_path.write_bytes(b"video")
    reference_path.write_bytes(b"%PDF-1.4 fake")
    called_stages: list[str] = []

    def fake_run_stage(
        stage_name,
        loaded_settings,
        logger,
        backend_override=None,
        prepare_reference_progress_callback=None,
    ) -> int:
        _ = logger, backend_override, prepare_reference_progress_callback
        called_stages.append(stage_name)
        if stage_name == "prepare-reference":
            assert loaded_settings.settings.reference.codex_ocr_max_concurrency == 12
            assert loaded_settings.settings.reference.codex_ocr_submit_interval_seconds == 2.5
            return 2
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", fake_run_stage)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    response = request_json(
        app,
        "POST",
        "/api/jobs",
        json_body={
            "video": str(video_path),
            "reference": str(reference_path),
            "output_dir": str(output_dir),
            "ocr_max_concurrency": 12,
            "ocr_submit_interval_seconds": 2.5,
        },
    )

    assert response.status_code == 202
    state = request_json(app, "GET", f"/api/jobs/{response.json()['job_id']}").json()
    assert state["status"] == "partial"
    assert state["current_stage"] == "prepare-reference"
    assert state["resumable"] is True
    assert called_stages == ["extract-audio", "transcribe", "prepare-reference"]


def test_stage_run_retry_reuses_same_run_and_effective_ocr_payload(
    tmp_path: Path,
) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    run_id = "stage-run-retry"
    state_path = tmp_path / "data/jobs/stage-runs" / run_id / "state.json"
    write_json_file(
        state_path,
        {
            **create_initial_state(run_id, "stage-run"),
            "status": "partial",
            "current_stage": "prepare-reference",
            "run_mode": "directory",
            "resumable": True,
            "request_payload": {
                "ocr_backend": "codex_api",
                "ocr_model": "gpt-5.4-mini",
                "ocr_reasoning_effort": "high",
                "ocr_max_concurrency": 12,
                "ocr_submit_interval_seconds": 2.5,
            },
        },
    )
    captured: dict[str, object] = {}
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    def fake_execute_stage_run(*, app, run_id: str, stage_name: str, payload: dict) -> None:
        _ = app
        captured.update({"run_id": run_id, "stage_name": stage_name, "payload": payload})

    app.state.execute_stage_run = fake_execute_stage_run

    response = request_json(app, "POST", f"/api/stage-runs/{run_id}/retry")

    assert response.status_code == 202
    assert response.json() == {"run_id": run_id}
    assert captured["run_id"] == run_id
    assert captured["stage_name"] == "prepare-reference"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["ocr_fast_mode"] is False
    assert payload["ocr_max_concurrency"] == 12
    assert payload["ocr_submit_interval_seconds"] == 2.5
    state = read_json_file(state_path)
    assert state["resume_count"] == 1


def test_reconcile_running_prepare_reference_job_as_partial_after_restart(
    tmp_path: Path,
) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    job_id = "job-restart-ocr"
    state_path = tmp_path / "data/jobs" / job_id / "state.json"
    write_json_file(
        state_path,
        {
            **create_initial_state(job_id, "job"),
            "status": "running",
            "current_stage": "prepare-reference",
            "pages_total": 603,
            "pages_completed": 194,
        },
    )
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    response = request_json(app, "GET", f"/api/jobs/{job_id}")

    assert response.status_code == 200
    state = response.json()
    assert state["status"] == "partial"
    assert state["resumable"] is True
    assert "已完成页面仍保留" in state["error_message"]

def test_post_batch_jobs_returns_batch_id_and_persists_state(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    def fake_execute_batch_job(*, app, batch_id: str, payload: dict) -> None:
        _ = payload
        app.state.update_state(
            app.state.batch_state_path(batch_id),
            status="success",
            current_stage="done",
            items=[{"job_id": "job-a", "status": "success"}],
        )

    app.state.execute_batch_job = fake_execute_batch_job

    response = request_json(
        app,
        "POST",
        "/api/batch-jobs",
        json_body={
            "manifest": str(tmp_path / "jobs.yaml"),
            "remote_concurrency": 2,
        },
    )

    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    state = json.loads((tmp_path / "data/jobs/batches" / batch_id / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "success"
    assert state["items"][0]["job_id"] == "job-a"
    assert state["input_summary"] == {"content_type": "book_club", "manifest": str(tmp_path / "jobs.yaml")}


def test_execute_batch_job_passes_custom_refine_prompt_to_prepared_jobs(tmp_path: Path, monkeypatch) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    manifest_path = tmp_path / "jobs.yaml"
    manifest_path.write_text(json.dumps({"jobs": []}, ensure_ascii=False), encoding="utf-8")
    seen: dict[str, str | None] = {}

    def fake_prepare_batch_jobs(**kwargs):
        seen["refine_prompt"] = kwargs.get("refine_prompt")
        return []

    monkeypatch.setattr("src.web.tasks.prepare_batch_jobs", fake_prepare_batch_jobs)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    response = request_json(
        app,
        "POST",
        "/api/batch-jobs",
        json_body={
            "manifest": str(manifest_path),
            "remote_concurrency": 2,
            "refine_prompt": "# 批量自定义阶段六指令\n\n所有子任务统一使用。",
        },
    )

    assert response.status_code == 202
    assert seen["refine_prompt"] == "# 批量自定义阶段六指令\n\n所有子任务统一使用。"


def test_web_batch_staggers_remote_pipeline_and_persists_per_item_progress(tmp_path: Path, monkeypatch) -> None:
    from api_server import create_app
    from src.job_runner import CANONICAL_INPUT_BASENAME

    write_minimal_settings(tmp_path)
    output_dir = tmp_path / "deliverables"
    videos_dir = tmp_path / "videos"
    references_dir = tmp_path / "references"
    videos_dir.mkdir()
    references_dir.mkdir()
    for name in ("lesson-a", "lesson-b"):
        (videos_dir / f"{name}.mp4").write_bytes(b"video")
        (references_dir / f"{name}.txt").write_text("参考原文", encoding="utf-8")

    created_job_ids = iter(["job-a", "job-b"])
    monkeypatch.setattr("src.job_runner.create_job_id", lambda: next(created_job_ids))
    prepare_started = threading.Event()

    def fake_run_stage(
        stage_name,
        job_loaded_settings,
        logger,
        backend_override=None,
        prepare_reference_progress_callback=None,
    ) -> int:
        _ = logger, backend_override, prepare_reference_progress_callback
        job_id = job_loaded_settings.path_for("videos_dir").parents[1].name
        if stage_name == "transcribe" and job_id == "job-b":
            assert prepare_started.wait(timeout=1.0), "job-a 转写后没有立即启动远程子流水"
        if stage_name == "prepare-reference" and job_id == "job-a":
            prepare_started.set()
        if stage_name == "export-markdown":
            final_dir = job_loaded_settings.path_for("final_dir")
            final_dir.mkdir(parents=True, exist_ok=True)
            (final_dir / f"{CANONICAL_INPUT_BASENAME}.md").write_text("# 整理稿\n\n正文\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("src.job_runner.run_stage", fake_run_stage)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    response = request_json(
        app,
        "POST",
        "/api/batch-jobs",
        json_body={
            "videos_dir": str(videos_dir),
            "reference_dir": str(references_dir),
            "output_dir": str(output_dir),
            "remote_concurrency": 2,
        },
    )

    assert response.status_code == 202
    state = read_json_file(tmp_path / "data/jobs/batches" / response.json()["batch_id"] / "state.json")
    assert state["status"] == "success"
    assert state["current_stage"] == "done"
    assert {item["current_stage"] for item in state["items"]} == {"done"}
    assert all(item["completed_stages"] == ["extract-audio", "transcribe", "prepare-reference", "refine", "export-markdown"] for item in state["items"])


def test_post_stage_run_returns_run_id(tmp_path: Path) -> None:
    from api_server import create_app

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path, run_tasks_inline=True)

    def fake_execute_stage_run(*, app, run_id: str, stage_name: str, payload: dict) -> None:
        _ = payload
        app.state.update_state(
            app.state.stage_run_state_path(run_id),
            status="success",
            current_stage=stage_name,
        )

    app.state.execute_stage_run = fake_execute_stage_run

    response = request_json(
        app,
        "POST",
        "/api/stages/refine",
        json_body={"profile": "local_cpu", "backend": "codex_cli"},
    )

    assert response.status_code == 202
    run_id = response.json()["run_id"]
    state = json.loads((tmp_path / "data/jobs/stage-runs" / run_id / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "success"
    assert state["current_stage"] == "refine"
