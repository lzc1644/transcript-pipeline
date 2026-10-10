from __future__ import annotations

import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from api_server import create_app
from src.proofreading_memory import MemoryStore, canonical
from src.web.state_store import create_initial_state, write_json_file
from tests.helpers import write_minimal_settings
from tests.test_api_server import request_json, request_raw

BASE = "/api/proofreading-memory"
META = {"dataset_kind": "synthetic", "scope": {"book_id": "test-book", "speaker_id": "test-speaker", "content_type": "reading"}}


@pytest.fixture
def app(tmp_path):
    return create_app(project_root=tmp_path, run_tasks_inline=True)


def prepare(app, *, metadata=None, human="🙂开场。\n劳动有价值。\n"):
    inputs = {}
    for role, text in {"asr": "🙂开场。\n劳动有价值。\n", "system": "🙂开场。\n劳动有假值。\n", "human": human}.items():
        response = request_raw(app, "POST", f"{BASE}/uploads/{role}", content=text.encode(), params={"filename": f"{role}.txt"})
        assert response.status_code == 201, response.text
        inputs[role] = response.json()["token"]
    response = request_json(app, "POST", f"{BASE}/experiments", json_body={"inputs": inputs, "metadata": metadata or META})
    assert response.status_code == 201, response.text
    return response.json()


def proposals(experiment):
    request = experiment["request"]
    evidence = []
    for role, phrase in (("system", "假值"), ("human", "价值")):
        start = request["sources"][role]["text"].index(phrase)
        evidence.append({"source_id": role, "start": start, "end": start + len(phrase), "excerpt": phrase, "pdf_page": None})
    return {"schema_version": 1, "request_id": request["request_id"], "candidates": [{
        "kind": "correction_case", "scope": request["metadata"]["scope"], "text": "只在保真证据支持时复核假值为价值。",
        "reason": "构造编辑，用于验证链路而非校对质量。", "retrieval_keys": ["价值", "假值"],
        "observed_form": "假值", "preferred_form": "价值", "fragment_ids": ["f0001"], "evidence": evidence,
    }]}


def import_proposals(app, experiment, response=None):
    return request_json(app, "POST", f"{BASE}/experiments/{experiment['id']}/import-response", json_body={
        "response_json": canonical(response or proposals(experiment)), "response_mode": "replay",
    })


def entries(app, experiment):
    response = request_json(app, "GET", f"{BASE}/experiments/{experiment['id']}/memories")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def review(app, experiment, entry, action="approve", **overrides):
    return request_json(app, "POST", f"{BASE}/experiments/{experiment['id']}/memories/{entry['memory_id']}/review",
                        json_body={"action": action, "reviewer": "test-reviewer", "reason": "synthetic工程测试，不是语义确认。",
                                   "expected_version": entry["version"], **overrides})


def freeze(app, experiment, **overrides):
    response = request_json(app, "POST", f"{BASE}/experiments/{experiment['id']}/contexts",
                            json_body={"query": "劳动有价值。", **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def test_offline_web_lifecycle_and_immutable_downloads(app, tmp_path, monkeypatch):
    monkeypatch.setattr("src.web.proofreading_memory.load_settings", lambda **_: pytest.fail("offline must not load model settings"))
    experiment = prepare(app)
    request_download = request_json(app, "GET", f"{BASE}/experiments/{experiment['id']}/request")
    assert request_download.json() == experiment["request"]
    assert "attachment" in request_download.headers["content-disposition"]
    assert import_proposals(app, experiment).status_code == 200
    assert entries(app, experiment)[0]["status"] == "pending"
    assert freeze(app, experiment)["context"]["selected"] == []
    assert review(app, experiment, entries(app, experiment)[0]).json()["version"] == 2
    snapshot = freeze(app, experiment)
    assert len(snapshot["context"]["selected"]) == 1
    assert "occurrences" not in snapshot["context"]["selected"][0]
    prefix = f"{BASE}/experiments/{experiment['id']}/contexts/{snapshot['context_id']}"
    original_json = request_json(app, "GET", prefix + "/json").content
    block = request_json(app, "GET", prefix + "/txt").text
    assert "只在保真证据支持" in block
    assert "unreviewed" not in block
    assert review(app, experiment, entries(app, experiment)[0], "disable").status_code == 200
    assert freeze(app, experiment)["context"]["selected"] == []
    assert request_json(app, "GET", prefix + "/json").content == original_json
    assert freeze(app, experiment, max_items=0)["context"]["block_chars"] == 0
    assert not (tmp_path / "data/jobs").exists()
    assert len(request_json(app, "GET", BASE + "/experiments").json()["items"]) == 1
    reloaded_app = create_app(project_root=tmp_path)
    assert entries(reloaded_app, experiment)[0]["status"] == "disabled"


def test_dedup_occurrences_do_not_change_approved_version_or_context(app, tmp_path):
    first = prepare(app)
    assert import_proposals(app, first).status_code == 200
    assert review(app, first, entries(app, first)[0]).status_code == 200
    before = freeze(app, first)["context"]
    with MemoryStore(tmp_path / "data/proofreading-memory/memory.sqlite3") as store:
        audit_before = store.audit_events()
    second = prepare(app, human="🙂开场。\n劳动有价值。\n额外说明。\n")
    response = proposals(second)
    response["candidates"][0]["reason"] = "第二请求的新提案，不是独立审核确认。"
    imported = import_proposals(app, second, response).json()
    assert len(imported["duplicates"]) == len(imported["occurrence_inserted"]) == 1
    entry = entries(app, first)[0]
    assert entry["version"] == 2 and entry["status"] == "approved"
    assert len(entry["occurrences"]) == 2
    assert all(o["review_status"] == "unreviewed" for o in entry["occurrences"])
    assert before == freeze(app, first)["context"] == freeze(app, second)["context"]
    assert not import_proposals(app, second, response).json()["occurrence_inserted"]
    with MemoryStore(tmp_path / "data/proofreading-memory/memory.sqlite3") as store:
        assert store.audit_events() == audit_before


@pytest.mark.parametrize("changes", [
    {"dataset_kind": "real_human"}, {"scope": {**META["scope"], "book_id": "other-book"}},
    {"scope": {**META["scope"], "speaker_id": "other-speaker"}},
    {"scope": {**META["scope"], "content_type": "conversation"}},
])
def test_web_scope_and_dataset_isolation(app, changes):
    experiment = prepare(app)
    import_proposals(app, experiment)
    entry = entries(app, experiment)[0]
    review(app, experiment, entry)
    other = prepare(app, metadata={**META, **changes})
    assert entries(app, other) == []
    assert freeze(app, other)["context"]["selected"] == []
    assert review(app, other, entry).status_code == 404


def test_review_requires_identity_reason_and_current_version(app):
    experiment = prepare(app)
    import_proposals(app, experiment)
    entry = entries(app, experiment)[0]
    assert review(app, experiment, entry, reviewer=" ").status_code == 400
    assert review(app, experiment, entry, reason="").status_code == 422
    assert review(app, experiment, entry).status_code == 200
    assert review(app, experiment, entry).status_code == 400
    assert review(app, experiment, entries(app, experiment)[0]).json()["changed"] is False
    assert review(app, experiment, entries(app, experiment)[0], "reject").status_code == 400


@pytest.mark.parametrize("failure", ["excerpt", "request", "unknown_field", "second_bad_candidate", "duplicate_key"])
def test_invalid_response_never_creates_database(app, tmp_path, failure):
    experiment = prepare(app)
    response = proposals(experiment)
    if failure == "excerpt":
        response["candidates"][0]["evidence"][0]["excerpt"] = "伪造"
    elif failure == "request":
        response["request_id"] = "not-this-request"
    elif failure == "unknown_field":
        response["candidates"][0]["status"] = "approved"
    elif failure == "second_bad_candidate":
        response["candidates"].append({"kind": "term"})
    raw = canonical(response)
    if failure == "duplicate_key":
        raw = raw.replace('"schema_version":1', '"schema_version":1,"schema_version":1')
    result = request_json(app, "POST", f"{BASE}/experiments/{experiment['id']}/import-response",
                          json_body={"response_json": raw, "response_mode": "replay"})
    assert result.status_code == 400, result.text
    assert not (tmp_path / "data/proofreading-memory/memory.sqlite3").exists()


@pytest.mark.parametrize("content,filename,status", [
    (b"", "empty.txt", 400), (b"\xff", "not-utf8.txt", 400),
    (b"x" * (1024 * 1024 + 1), "oversize.txt", 413), (b"binary", "audio.wav", 400),
])
def test_upload_rejections_cleanup(app, tmp_path, content, filename, status):
    result = request_raw(app, "POST", BASE + "/uploads/human", content=content, params={"filename": filename})
    assert result.status_code == status, result.text
    assert not list((tmp_path / "data/proofreading-memory/uploads").glob("*/*"))


def test_upload_tokens_and_symlinks_cannot_read_arbitrary_server_files(app, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("private source", encoding="utf-8")
    token = uuid4().hex + ".txt"
    upload = tmp_path / "data/proofreading-memory/uploads/asr" / token
    upload.parent.mkdir(parents=True)
    upload.symlink_to(outside)
    for invalid in (str(outside), "../secret.txt", token):
        result = request_json(app, "POST", BASE + "/experiments", json_body={
            "inputs": {"asr": invalid, "system": token, "human": token}, "metadata": META,
        })
        assert result.status_code == 400
    assert not (tmp_path / "data/proofreading-memory/experiments").exists()
    assert outside.read_text() == "private source"
    assert request_json(app, "GET", BASE + "/experiments/not-a-valid-id").status_code == 400


def test_reference_upload_is_optional_readonly_evidence(app):
    experiment = prepare(app)
    request = experiment["request"]
    reference = request_raw(app, "POST", BASE + "/uploads/reference", content="已有 OCR 文本".encode(), params={"filename": "book.md"}).json()
    inputs = {role: Path(source["path"]).name for role, source in request["sources"].items()}
    inputs["reference"] = reference["token"]
    result = request_json(app, "POST", BASE + "/experiments", json_body={"inputs": inputs, "metadata": META,
        "reference_metadata": {"source_kind": "ocr_text", "version": "unknown", "book_id": "test-book"}})
    assert result.status_code == 201
    assert result.json()["request"]["sources"]["reference"]["reference_metadata"]["source_kind"] == "ocr_text"


def test_network_opt_in_async_result_and_explicit_pending_import(app, tmp_path, monkeypatch):
    write_minimal_settings(tmp_path)
    experiment = prepare(app)
    called = []
    def fake_analysis(request, **kwargs):
        called.append(kwargs)
        return proposals({"request": request})
    monkeypatch.setattr("src.web.proofreading_memory.analyze_request", fake_analysis)
    url = f"{BASE}/experiments/{experiment['id']}/analyses"
    assert request_json(app, "POST", url, json_body={}).status_code == 400
    assert request_json(app, "POST", url, json_body={"allow_network": "true"}).status_code == 422
    assert called == []
    result = request_json(app, "POST", url, json_body={"allow_network": True, "model": "only-this-experiment"})
    assert result.status_code == 202
    analysis_id = result.json()["analysis_id"]
    state = request_json(app, "GET", url + "/" + analysis_id).json()
    assert state["status"] == "success" and state["candidate_count"] == 1
    assert called[0]["allow_network"] is True and called[0]["model"] == "only-this-experiment"
    assert entries(app, experiment) == []
    assert not (tmp_path / "data/proofreading-memory/memory.sqlite3").exists()
    assert request_json(app, "POST", url + "/" + analysis_id + "/import").status_code == 200
    assert entries(app, experiment)[0]["status"] == "pending"
    assert entries(app, experiment)[0]["occurrences"][0]["response_mode"] == "remote"


def test_async_duplicate_submission_and_explicit_retry_after_failure(tmp_path, monkeypatch):
    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path)
    experiment = prepare(app)
    calls = []
    class CapturingExecutor:
        def submit(self, func, *args):
            calls.append((func, args))
    app.state.executor = CapturingExecutor()
    def fail_analysis(*args, **kwargs):
        raise RuntimeError("secret-api-key-must-not-leak")
    monkeypatch.setattr("src.web.proofreading_memory.analyze_request", fail_analysis)
    url = f"{BASE}/experiments/{experiment['id']}/analyses"
    first = request_json(app, "POST", url, json_body={"allow_network": True}).json()["analysis_id"]
    assert request_json(app, "POST", url, json_body={"allow_network": True}).status_code == 409
    assert len(calls) == 1
    assert request_json(app, "POST", url + "/" + first + "/import").status_code == 400
    calls[0][0](*calls[0][1])
    failed = request_json(app, "GET", url + "/" + first)
    assert failed.json()["status"] == "failed"
    assert "secret-api-key" not in failed.text
    assert entries(app, experiment) == []
    assert request_json(app, "POST", url, json_body={"allow_network": True}).status_code == 202
    assert len(calls) == 2


def test_restart_marks_interrupted_analysis_without_retry(app, tmp_path):
    experiment = prepare(app)
    analysis_id = uuid4().hex
    state = create_initial_state(analysis_id, "proofreading-memory-analysis")
    state["status"] = "running"
    write_json_file(tmp_path / "data/proofreading-memory/experiments" / experiment["id"] / "analyses" / analysis_id / "state.json", state)
    result = request_json(app, "GET", f"{BASE}/experiments/{experiment['id']}/analyses/{analysis_id}")
    assert result.json()["status"] == "failed"
    assert "服务重启" in result.json()["error_message"]
    assert not (tmp_path / "data/proofreading-memory/memory.sqlite3").exists()


def test_web_cannot_silently_migrate_existing_v1_database(app, tmp_path):
    experiment = prepare(app)
    path = tmp_path / "data/proofreading-memory/memory.sqlite3"
    with MemoryStore(path) as store:
        store.db.execute("PRAGMA user_version=1")
    response = request_json(app, "GET", f"{BASE}/experiments/{experiment['id']}/memories")
    assert response.status_code == 400 and "migrate-v1" in response.text
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_real_local_sse_uses_web_settings_without_mutating_environment(app, tmp_path, monkeypatch):
    """Actual Responses transport to localhost, not a remote model or quality test."""
    from src.codex_lb_client import CodexLBClient
    from src.schemas import CodexLBSettings
    from src.web.frontend_settings import FrontendSettingsUpdate, save_frontend_settings

    write_minimal_settings(tmp_path, llm_overrides={"timeout_seconds": 2})
    experiment = prepare(app)
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, self.headers.get("Authorization"), payload))
            answer = canonical(proposals(experiment))
            event = "data: " + json.dumps({"type": "response.output_text.delta", "delta": answer}) + "\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(event.encode())))
            self.end_headers()
            self.wfile.write(event.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        save_frontend_settings(tmp_path, FrontendSettingsUpdate(
            codex_lb_base_url=f"http://127.0.0.1:{server.server_port}/v1",
            codex_lb_api_key="web-secret-not-real", codex_lb_bypass_proxy=True,
            model="web-test-model", reasoning_effort="medium",
        ))
        monkeypatch.setenv("CODEX_LB_BASE_URL", "http://environment-only.invalid")
        monkeypatch.setenv("CODEX_LB_API_KEY", "environment-only-test-key")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        env_keys = ("CODEX_LB_BASE_URL", "CODEX_LB_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")
        environment = {key: os.environ.get(key) for key in env_keys}
        url = f"{BASE}/experiments/{experiment['id']}/analyses"
        result = request_json(app, "POST", url, json_body={"allow_network": True})
        state = request_json(app, "GET", url + "/" + result.json()["analysis_id"])
        assert state.json()["status"] == "success", state.text
        assert captured[0][0] == "/v1/responses"
        assert captured[0][1] == "Bearer web-secret-not-real"
        payload = captured[0][2]
        assert payload["model"] == "web-test-model" and payload["reasoning"] == {"effort": "medium"}
        assert payload["store"] is False
        assert '"path"' not in payload["input"]
        assert {key: os.environ.get(key) for key in env_keys} == environment
        assert CodexLBClient(CodexLBSettings()).api_key == "environment-only-test-key"
        assert "web-secret-not-real" not in repr(CodexLBClient(CodexLBSettings(), api_key_override="web-secret-not-real"))
        assert "web-secret-not-real" not in state.text
        assert entries(app, experiment) == []
        assert request_json(app, "POST", url + "/" + result.json()["analysis_id"] + "/import").status_code == 200
        for path in (tmp_path / "data/proofreading-memory").rglob("*.json"):
            assert "web-secret-not-real" not in path.read_text()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_poll_and_completion_cannot_turn_success_into_interrupted_failure(tmp_path, monkeypatch):
    import src.web.proofreading_memory as memory_web

    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path)
    experiment = prepare(app)
    submitted = []
    class CapturingExecutor:
        def submit(self, func, *args):
            submitted.append((func, args))
    app.state.executor = CapturingExecutor()
    monkeypatch.setattr(memory_web, "analyze_request", lambda request, **kwargs: proposals({"request": request}))
    url = f"{BASE}/experiments/{experiment['id']}/analyses"
    analysis_id = request_json(app, "POST", url, json_body={"allow_network": True}).json()["analysis_id"]
    state_path = tmp_path / "data/proofreading-memory/experiments" / experiment["id"] / "analyses" / analysis_id / "state.json"
    read_ready, release_read, artifact_ready = threading.Event(), threading.Event(), threading.Event()
    original_read, original_write = memory_web.read_json, memory_web.write_artifact

    def paused_read(path):
        result = original_read(path)
        if Path(path) == state_path and threading.current_thread().name == "AnyIO worker thread" and not read_ready.is_set():
            read_ready.set()
            assert release_read.wait(3)
        return result

    def observe_artifact(path, *args, **kwargs):
        original_write(path, *args, **kwargs)
        if Path(path).name == "response.json":
            artifact_ready.set()

    monkeypatch.setattr(memory_web, "read_json", paused_read)
    monkeypatch.setattr(memory_web, "write_artifact", observe_artifact)
    results = []
    reader = threading.Thread(target=lambda: results.append(request_json(app, "GET", url + "/" + analysis_id)), daemon=True)
    worker = threading.Thread(target=lambda: submitted[0][0](*submitted[0][1]), daemon=True)
    reader.start()
    try:
        assert read_ready.wait(3)
        worker.start()
        assert artifact_ready.wait(3)
        # Before the fix the worker finishes while the reader holds a stale
        # pending value, then that reader overwrites success with failed.
        worker.join(timeout=0.2)
    finally:
        release_read.set()
        reader.join(timeout=3)
        if worker.ident is not None:
            worker.join(timeout=3)
    assert not reader.is_alive() and not worker.is_alive()
    assert results[0].status_code == 200 and results[0].json()["status"] != "failed"
    assert request_json(app, "GET", url + "/" + analysis_id).json()["status"] == "success"
