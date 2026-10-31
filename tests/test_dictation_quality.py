from __future__ import annotations

import asyncio
from pathlib import Path
import struct
import time

import pytest
from fastapi.testclient import TestClient

from scripts.score_dictation import edit_counts, literal_present, normalize, score
from tests.test_dictation import root
from tests.test_dictation_pause import PauseWorker


def test_cer_edit_counts_and_normalization_do_not_hide_numbers_or_negation():
    assert normalize("HTTP，API 1500。不是15000！") == "httpapi1500不是15000"
    assert normalize("一千五") != normalize("一万五")
    assert normalize("1500") != normalize("15000")
    assert normalize("1.5") != normalize("15")
    assert normalize("-15") != normalize("15")
    assert not literal_present({"kind": "number", "expected": "1500"}, "-1500")
    assert not literal_present({"kind": "number", "expected": "15"}, "15%")
    assert edit_counts("预算1500", "预算15000")["insertions"] == 1
    assert edit_counts("不能", "能")["deletions"] == 1
    assert edit_counts("甲乙", "甲丙")["substitutions"] == 1
    assert edit_counts("", "幻觉")["silence_false_chars"] == 2
    assert edit_counts("", "幻觉")["cer"] is None


def manifest():
    return [{"id": "a", "path": "speech.wav", "reference": "不是1500", "recording_group": "one", "split": "tuning",
             "checks": [{"kind": "negation", "expected": "不是"}, {"kind": "number", "expected": "1500"}]},
            {"id": "b", "path": "missing.wav", "reference": "保留失败", "recording_group": "two", "split": "holdout"}]


def test_offline_score_keeps_missing_failures_raw_clean_document_and_no_fake_latency():
    reports = [{"sample_id": "a", "realtime": False, "result": {"status": "completed", "config": {"qwen_revision": "fixed"},
        "document_text": "不对齐全文", "utterances": [{"id": "1", "status": "cleaned", "raw_text": "不是15000", "cleaned_text": "是1500",
        "final_latency_seconds": -1, "asr_seconds": .3}]}}]
    result = score(manifest(), reports)
    assert result["summary"]["samples"] == 2
    assert result["summary"]["scored_samples"] == 2
    assert result["summary"]["missing_reports"] == 1
    assert result["samples"][1]["raw_score"]["deletions"] == 4
    assert result["samples"][0]["raw_score"]["insertions"] == 1
    assert result["samples"][0]["checks"][0]["raw_present"]
    assert not result["samples"][0]["checks"][1]["raw_present"]
    assert not result["samples"][0]["checks"][0]["cleaned_present"]
    assert result["samples"][0]["document_cleanup"] == "不对齐全文"
    assert result["summary"]["human_semantic_reviewed"] == 0
    assert result["summary"]["latency"]["final_latency_seconds"]["count"] == 0
    assert result["summary"]["latency"]["asr_seconds"]["p50"] == .3


def test_offline_score_rejects_split_leak_duplicate_artifact_and_unlisted_failures():
    items = manifest()
    items[1]["recording_group"] = "one"
    with pytest.raises(ValueError, match="不能分散"):
        score(items, [])
    with pytest.raises(ValueError, match="多份产物"):
        score(manifest(), [{"sample_id": "a"}, {"sample_id": "a"}])
    with pytest.raises(ValueError, match="不在清单"):
        score(manifest(), [{"sample_id": "unlisted", "result": {"status": "failed"}}])
    items = manifest()
    items[0]["reference"] = None
    result = score(items, [])
    assert result["samples"][0]["raw_score"] is None
    assert result["summary"]["scored_samples"] == 1


def test_sentence_audio_requires_owner_existing_uid_and_non_symlink_file(root):
    from api_server import create_app
    app = create_app(project_root=root)
    app.state.dictation.worker_factory = PauseWorker
    headers = {"Origin": "http://testserver"}
    with TestClient(app) as client:
        sid = client.post("/api/dictation/sessions", json={"cleanup": False}, headers=headers).json()["id"]
        with client.websocket_connect(f"/api/dictation/sessions/{sid}/stream", headers=headers) as ws:
            while ws.receive_json().get("snapshot", {}).get("status") != "listening":
                pass
            pcm = b"\1\0" * 997
            ws.send_bytes(struct.pack("<IQ", 0, 0) + pcm)
            ws.send_json({"type": "stop"})
            while ws.receive_json().get("snapshot", {}).get("status") != "completed":
                pass
        endpoint = f"/api/dictation/sessions/{sid}/utterances/1/audio"
        response = client.get(endpoint)
        assert response.status_code == 200 and response.content[:4] == b"RIFF"
        assert response.content[44:] == pcm and response.headers["cache-control"] == "no-store"
        partial = client.get(endpoint, headers={"Range": "bytes=44-47"})
        assert partial.status_code == 206 and partial.content == pcm[:4]
        assert client.get(f"/api/dictation/sessions/{sid}/utterances/999/audio").status_code == 404
        assert client.get(endpoint, params={"path": "/etc/passwd"}).content == response.content
        assert client.get(endpoint, headers={"Cookie": "dictation_owner=not-owner"}).status_code == 404
        path = app.state.dictation.storage.path(sid) / "audio/1.wav"
        path.unlink(); path.symlink_to(root / "config/settings.yaml")
        assert client.get(endpoint).status_code == 404
