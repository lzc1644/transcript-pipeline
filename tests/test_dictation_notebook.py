from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from src.dictation import document
from src.dictation.schemas import CleanupRequest, DocumentEditRequest, SessionRequest
from src.dictation.service import Conflict, DictationService
from src.dictation.storage import Storage
from tests.test_dictation import completed, fake_cleanup, root


def edit(service, state, start, end, text, *, version=None, op="manual"):
    return service.edit_document(state["id"], "owner", DocumentEditRequest.model_validate({
        "operation_id": op, "base_version": state["document_version"] if version is None else version,
        "changes": [{"from": start, "to": end, "insert": text}]}))


def test_pause_new_anchor_and_late_old_final_use_sample_turn_not_arrival_cursor(root):
    async def run():
        service = DictationService(root, cleanup_factory=fake_cleanup)
        state = await service.create("owner", SessionRequest(editor_version=1, initial_text="甲😀乙", insertion_position=4, cleanup=False), start=False)
        runtime = service.runtimes[state["id"]]
        state.update(status="paused", samples=100, pause_boundaries=[{}])
        runtime.pause_boundary = (0, 100)
        await runtime.resume(0, 100, state["document_version"], 1)
        # The old tail has NOT returned when a new capture turn is confirmed.
        old = dict(id="1", start_sample=0, end_sample=100, text="", raw_text="", edited=False, revision=0, status="finalizing")
        new = dict(old, id="2", start_sample=100, end_sample=200)
        state["utterances"].extend([old, new])
        async def transcribe(_runtime, uid, *args):
            return dict(type="final", text="旧尾句。" if uid == "1" else "新位置。", inference_seconds=0, queue_wait_seconds=0)
        service.recognition.transcribe = transcribe
        for u in (old, new):
            await runtime.queue.put((u["id"], None, 0))
        await runtime.queue.put(None)
        await runtime.final_loop()
        assert Storage.render_draft(state) == "甲新位置。😀乙旧尾句。"
        assert [t["start_sample"] for t in state["dictation_turns"]] == [0, 100]
        assert (service.storage.path(state["id"]) / "draft.txt").read_text() == Storage.render_draft(state)
        await service.close()
    asyncio.run(run())


def test_manual_edit_and_undo_deletion_protect_sources_from_late_cleanup(root):
    async def run():
        service = DictationService(root, cleanup_factory=fake_cleanup)
        state = await completed(service)
        service.enable_document(state["id"], "owner", state["revision"])
        edit(service, state, 0, 1, "手", op="word")
        await service.clean_utterance(state, "1", state["utterances"][0]["revision"])
        assert state["document_text"] == "手稿"
        assert state["utterances"][0]["suggestion"] == "原稿。"
        edit(service, state, 0, 2, "", op="undo-speech")
        await service.clean_utterance(state, "1", state["utterances"][0]["revision"])
        assert state["document_text"] == ""  # A late result cannot resurrect a deleted sentence.
    asyncio.run(run())


def test_new_speech_inside_old_sentence_is_not_overwritten_by_its_late_cleanup(root):
    async def run():
        service = DictationService(root, cleanup_factory=fake_cleanup)
        state = await completed(service)
        service.enable_document(state["id"], "owner", state["revision"])
        # The earlier source is 原稿; resume a different turn between its words.
        document.start_turn(state, 100, 1)
        new = dict(id="2", start_sample=100, end_sample=200, text="新语音", raw_text="新语音",
                   edited=False, revision=1, status="raw")
        state["utterances"].append(new)
        service.commit_document(state, lambda draft: document.insert_utterance(draft, new))
        await service.clean_utterance(state, "1", state["utterances"][0]["revision"])
        assert state["document_text"] == "原新语音稿"
        assert state["utterances"][0]["suggestion"] == "原稿。"
        # The new sentence still owns its exact range and can be cleaned normally.
        await service.clean_utterance(state, "2", new["revision"])
        assert state["document_text"] == "原新语音。稿"
        assert (service.storage.path(state["id"]) / "draft.txt").read_text() == state["document_text"]
    asyncio.run(run())


def test_utf16_rebase_idempotence_conflict_and_legacy_contract(root):
    async def run():
        service = DictationService(root)
        state = await completed(service)
        service.edit_draft(state["id"], "owner", state["revision"], "😀甲乙\n")
        service.enable_document(state["id"], "owner", state["revision"])
        edit(service, state, 2, 3, "一", version=0, op="a")
        edit(service, state, 4, 4, "尾", version=0, op="b")
        assert state["document_text"] == "😀一乙尾\n"
        v = state["document_version"]
        edit(service, state, 4, 4, "尾", version=0, op="b")
        assert state["document_version"] == v
        with pytest.raises(Conflict):
            edit(service, state, 2, 3, "二", version=0, op="overlap")
        with pytest.raises(ValueError, match="emoji"):
            edit(service, state, 1, 1, "!", op="split-surrogate")
        with pytest.raises(Conflict, match="版本化正文"):
            service.edit_draft(state["id"], "owner", state["revision"], "旧客户端覆写")
        assert DictationService(root).get(state["id"], "owner")["document_text"] == "😀一乙尾\n"
    asyncio.run(run())


def test_failed_document_operation_never_moves_text_sources_turns_or_versions(root, monkeypatch):
    async def run():
        service = DictationService(root)
        state = await completed(service)
        service.enable_document(state["id"], "owner", state["revision"])
        document.start_turn(state, 0, 2)
        before = deepcopy(state)
        # All entrances use the same candidate calculation; oversized input fails
        # before publishing mapped source ranges or turn anchors.
        with pytest.raises(ValueError, match="20万"):
            document.apply(state, [{"from": 0, "to": 0, "insert": "字" * 200000}])
        assert state == before
        monkeypatch.setattr(service.storage, "save", lambda _: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            edit(service, state, 0, 0, "插入", op="retryable")
        assert state == before
    asyncio.run(run())


def test_preview_saves_user_candidate_as_one_operation_and_stale_preview_cannot_apply(root, monkeypatch):
    async def run():
        service = DictationService(root, cleanup_factory=fake_cleanup)
        state = await completed(service)
        service.enable_document(state["id"], "owner", state["revision"])
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="document"))
        suggestion = state["document_suggestion"]
        request = CleanupRequest(scope="apply", suggestion_id=suggestion["id"],
            base_content_revision=suggestion["base_content_revision"], text="人工修改后的候选😀", operation_id="apply")
        before = deepcopy(state)
        artifact = service.storage.artifact
        monkeypatch.setattr(service.storage, "artifact", lambda *args: (_ for _ in ()).throw(OSError("artifact full")))
        with pytest.raises(OSError):
            await service.cleanup(state["id"], "owner", request)
        assert state == before
        monkeypatch.setattr(service.storage, "artifact", artifact)
        saves = []
        save = service.storage.save
        monkeypatch.setattr(service.storage, "save", lambda draft: (saves.append(draft["revision"]), save(draft))[-1])
        await service.cleanup(state["id"], "owner", request)
        assert len(saves) == 1
        assert state["revision"] == before["revision"] + 1
        assert state["event_sequence"] == before["event_sequence"] + 1
        assert "document_suggestion" not in state
        assert state["document_text"] == "人工修改后的候选😀"
        assert state["document_operations"][-1]["kind"] == "apply"
        assert state["document_sources"][0]["touched"]
        artifacts = list((service.storage.path(state["id"]) / "cleaned").glob("document-*.json"))
        assert len(artifacts) == 2  # Raw model suggestion and actual applied candidate.
        await service.cleanup(state["id"], "owner", CleanupRequest(scope="document"))
        old = state["document_suggestion"]
        edit(service, state, 0, 1, "新", op="newer")
        with pytest.raises(Conflict):
            await service.cleanup(state["id"], "owner", CleanupRequest(scope="apply", suggestion_id=old["id"],
                base_content_revision=old["base_content_revision"], text="旧候选"))
    asyncio.run(run())


def test_document_api_get_and_txt_share_saved_body_and_historical_newlines(root):
    from api_server import create_app
    app = create_app(project_root=root)
    with TestClient(app) as client:
        state = client.portal.call(completed, app.state.dictation)
        client.cookies.set("dictation_owner", "owner")
        endpoint = f"/api/dictation/sessions/{state['id']}"
        headers = {"Origin": "http://testserver"}
        enabled = client.post(endpoint + "/document", headers=headers, json={"revision": state["revision"]}).json()
        assert enabled["draft_text"] == "原稿"
        result = client.patch(endpoint + "/document", headers=headers, json={"operation_id": "browser", "base_version": 0,
            "changes": [{"from": 2, "to": 2, "insert": "😀\n续写"}]})
        assert result.status_code == 200
        assert client.get(endpoint).json()["draft_text"] == client.get(endpoint + "/draft.txt").text == "原稿😀\n续写"
