from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.codex_lb_client import CodexLBClientError
from src.llm.gateway import GatewayClient
from src.dictation.cleanup import Cleanup, DOCUMENT_CHUNK_CHARS, DOCUMENT_INSTRUCTIONS, INSTRUCTIONS, SessionGatewayClient
from src.dictation.schemas import CleanupRequest
from src.dictation.service import CleanupFailed, Conflict, DictationService
from src.dictation.storage import Storage
from src.schemas import CodexLBSettings
from tests.test_dictation import completed, dripping_gateway, fake_cleanup, root


def test_whole_document_payload_uses_current_edited_body_high_and_independent_budget(root, monkeypatch):
    seen = []
    def response(client, payload, **kwargs):
        seen.append((payload, client.timeout_seconds, client.require_stream_completed))
        return json.dumps({"text": json.loads(payload["input"])["current_text"] + "。", "needs_review": False})
    monkeypatch.setattr(GatewayClient, "codex_responses_text", response)
    async def run():
        service = DictationService(root)
        state = await completed(service)
        # Existing sessions retain their frozen Luna/low preset, not today's defaults.
        for config in (state["config"], state["worker_config"]):
            config.update(cleanup_model="gpt-6-luna", reasoning_effort="low")
        state["worker_config"].pop("document_cleanup_model")
        state["worker_config"].pop("document_cleanup_timeout_seconds")
        service.edit(state["id"], "owner", "1", 1, "预算1500元，不是15000元。")
        state["utterances"].append({**state["utterances"][0], "id": "2", "text": "等小王确认后再上线。"})
        service.commit(state)
        original = Storage.render_draft(state)
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="document"))
        assert len(seen) == 1
        payload, timeout, strict = seen[0]
        assert json.loads(payload["input"])["current_text"] == original
        assert payload["model"] == "gpt-6-luna" and payload["reasoning"] == {"effort": "high"}
        assert payload["instructions"] == DOCUMENT_INSTRUCTIONS
        assert payload["max_output_tokens"] >= 16384 and 3599 < timeout <= 3600 and strict
        assert state["document_suggestion"]["metrics"]["strategy"] == "whole"
        assert Storage.render_draft(state) == original  # Preview is not an implicit apply.
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="utterance", utterance_id="1"))
        payload, timeout, _ = seen[-1]
        assert payload["reasoning"] == {"effort": "low"} and payload["instructions"] == INSTRUCTIONS
        assert payload["max_output_tokens"] == 4096 and timeout == 3600
        assert json.loads(payload["input"])["current_text"] == "原稿"
    asyncio.run(run())


def test_default_cleanup_payloads_use_the_settings_preset(root, monkeypatch):
    sent = []
    def response(client, payload, **kwargs):
        sent.append(payload)
        return json.dumps({"text": "整理后文本。", "needs_review": False})
    monkeypatch.setattr(GatewayClient, "codex_responses_text", response)
    async def run():
        service = DictationService(root)
        state = await completed(service)
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="utterance", utterance_id="1"))
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="document"))
        assert [(payload["model"], payload["reasoning"]["effort"]) for payload in sent] == [
            ("gemini-3.5-flash-lite", "medium"), ("gemini-3.8-flash-high", "high")]
        await service.close()
    asyncio.run(run())


def test_sentence_and_document_models_and_efforts_are_independent(monkeypatch):
    sent = []
    def response(client, payload, **kwargs):
        sent.append(payload)
        return json.dumps({"text": "整理后文本。", "needs_review": False})
    monkeypatch.setattr(GatewayClient, "codex_responses_text", response)
    cleaner = Cleanup(CodexLBSettings(base_url="http://testserver"), model="gemini-3.5-flash-lite",
                      document_model="gemini-3.8-flash-high", reasoning_effort="medium",
                      document_reasoning_effort="xhigh")
    sentence = cleaner("识别文本", [], [])
    document = cleaner.document("整篇识别文本", [], [])
    assert [(item["model"], item["reasoning"]["effort"]) for item in sent] == [
        ("gemini-3.5-flash-lite", "medium"), ("gemini-3.8-flash-high", "xhigh")]
    assert sentence["metrics"]["reasoning_effort"] == "medium"
    assert document["metrics"]["reasoning_effort"] == "xhigh"


def test_high_reasoning_incomplete_sse_never_publishes_a_document(root, dripping_gateway, monkeypatch):
    port, *_ = dripping_gateway
    monkeypatch.setenv("DICTATION_DEADLINE_TEST_KEY", "local-test-only")
    async def run():
        service = DictationService(root)
        state = await completed(service)
        state["gateway"] = CodexLBSettings(base_url=f"http://127.0.0.1:{port}", base_url_env="",
            api_key_env="DICTATION_DEADLINE_TEST_KEY", codex_responses_path="/incomplete").model_dump()
        with pytest.raises(CleanupFailed, match="不完整"):
            await service.cleanup(state["id"], "owner", CleanupRequest(scope="document"))
        persisted = service.storage.load(state["id"])
        assert Storage.render_draft(persisted) == "原稿" and "document_suggestion" not in persisted
        assert not list((service.storage.path(state["id"]) / "cleaned").glob("document-*"))
    asyncio.run(run())


@pytest.mark.parametrize("failure,status", [(CodexLBClientError("curl: (28) Operation timed out"), 504),
                                         (CodexLBClientError("HTTP 503"), 502), (ValueError("bad JSON"), 502)])
def test_document_api_explains_failure_preserves_body_and_can_retry(root, failure, status):
    from api_server import create_app
    app = create_app(project_root=root)
    def factory(*args):
        def fail(*args):
            raise failure
        return SimpleNamespace(document=fail)
    app.state.dictation.cleanup_factory = factory
    with TestClient(app) as client:
        state = client.portal.call(completed, app.state.dictation)
        client.cookies.set("dictation_owner", "owner")
        endpoint = f"/api/dictation/sessions/{state['id']}"
        response = client.post(endpoint + "/cleanup", headers={"Origin": "http://testserver"}, json={"scope": "document"})
        assert response.status_code == status and "原稿未改动" in response.json()["detail"]
        assert client.get(endpoint).json()["draft_text"] == client.get(endpoint + "/draft.txt").text == "原稿"
        app.state.dictation.cleanup_factory = fake_cleanup
        data = client.post(endpoint + "/cleanup", headers={"Origin": "http://testserver"}, json={"scope": "document"}).json()
        assert "document_cleanup_error" not in data and data["document_suggestion"]["text"] == "原稿。"


def test_long_document_chunks_share_one_total_deadline_and_do_not_return_partial(monkeypatch):
    import src.llm.tasks.dictation as module
    clock, seen = [0.0], []
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def response(client, payload, **kwargs):
        seen.append(client.timeout_seconds)
        clock[0] += 6
        return '{"text":"一段完整结果","needs_review":false}'
    monkeypatch.setattr(GatewayClient, "codex_responses_text", response)
    cleaner = Cleanup(CodexLBSettings(), document_timeout=10)
    with pytest.raises(TimeoutError, match="未发布不完整建议"):
        cleaner.document("字" * (DOCUMENT_CHUNK_CHARS * 3), [], [])
    assert seen == [10, 4]


def test_long_document_is_explicitly_chunked_in_cleanup_not_service(monkeypatch):
    seen = []
    def response(client, payload, **kwargs):
        data = json.loads(payload["input"])
        seen.append(data)
        return json.dumps({"text": data["current_text"], "needs_review": False})
    monkeypatch.setattr(GatewayClient, "codex_responses_text", response)
    text = "字" * (DOCUMENT_CHUNK_CHARS + 1)
    result = Cleanup(CodexLBSettings()).document(text, [], [])
    assert result["metrics"]["strategy"] == "chunked" and result["metrics"]["chunk_count"] == 2
    assert "".join(p["current_text"] for p in seen) == text and seen[1]["context"] == [seen[0]["current_text"]]


@pytest.mark.parametrize("cancel_old", [False, True])
def test_document_single_flight_blocks_same_session_until_real_old_work_exits(root, cancel_old):
    started, release = threading.Event(), threading.Event()
    calls = [0]
    def factory(*args):
        def document(*args):
            calls[0] += 1
            if calls[0] == 1:
                started.set()
                release.wait(3)
                raise CodexLBClientError("late old failure")
            return {"text": "新建议", "needs_review": False}
        return SimpleNamespace(document=document)
    async def run():
        service = DictationService(root, cleanup_factory=factory)
        first = await completed(service)
        second = await completed(service)
        request = CleanupRequest(scope="document")
        old = asyncio.create_task(service.cleanup(first["id"], "owner", request))
        assert await asyncio.to_thread(started.wait, 1)
        if cancel_old:
            old.cancel()
            await asyncio.sleep(.01)
        with pytest.raises(Conflict, match="本会话已有全文整理"):
            await service.cleanup(first["id"], "owner", request)
        # Another session may finish while the first session is still waiting.
        await service.cleanup(second["id"], "owner", request)
        assert second["document_suggestion"]["text"] == "新建议"
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel_old else CleanupFailed):
            await old
        await service.cleanup(first["id"], "owner", request)
        assert first["document_suggestion"]["text"] == "新建议" and "document_cleanup_error" not in first
        assert "document_cleanup_error" not in second and not service.document_cleanups
    asyncio.run(run())


def test_applied_document_can_be_organized_again_and_apply_clears_identical_suggestion(root):
    def factory(*args):
        return SimpleNamespace(document=lambda text, *args: {"text": text, "needs_review": False})
    async def run():
        service = DictationService(root, cleanup_factory=factory)
        state = await completed(service)
        service.edit_draft(state["id"], "owner", state["revision"], "人工全文")
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="document"))
        suggestion = state["document_suggestion"]
        assert suggestion["original"] == "人工全文"
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="apply", revision=state["revision"], suggestion_id=suggestion["id"]))
        assert "document_suggestion" not in state and Storage.render_draft(state) == "人工全文"
    asyncio.run(run())
