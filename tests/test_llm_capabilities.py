"""Offline contracts for the independent gateway and text-task boundaries."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.llm.gateway import (
    CodexLBClient, GatewayClient, GatewayConnection, ProxySettings, connection_from_settings,
)
from src.llm.tasks.dictation import DictationCleanup, DOCUMENT_INSTRUCTIONS, INSTRUCTIONS
from src.llm.tasks.refinement import (
    BackendDocumentRefinementResult, RefinementOptions, refine_document,
)
from src.schemas import CodexLBSettings
from src.web.frontend_settings import (
    FrontendSettings, codex_lb_environment, gateway_connection_for_frontend,
)


@contextmanager
def fixed_gateway(label="gateway"):
    """An actual local HTTP/SSE endpoint; can also act as a forwarding proxy."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "payload": payload})
            if not payload.get("stream"):
                body = json.dumps({"output_text": label}).encode()
                content_type = "application/json"
            else:
                if payload.get("instructions") in (INSTRUCTIONS, DOCUMENT_INSTRUCTIONS):
                    output = {"text": json.loads(payload["input"])["current_text"] + "。", "needs_review": False}
                else:
                    output = {"final_markdown": "# 离线读书会\n\n原文与讨论。", "section_map": [],
                              "refinement_notes": [], "needs_review_sections": [], "deletion_candidates": []}
                event = {"type": "response.completed", "response": {"status": "completed", "model": "local-fixed",
                         "usage": {"input_tokens": 7, "output_tokens": 5},
                         "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(output, ensure_ascii=False)}]}]}}
                body = ("data: " + json.dumps(event, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_independent_modules_import_without_product_or_full_settings():
    code = '''
import builtins
original = builtins.__import__
def independent(name, *args, **kwargs):
    if name in {"src.schemas", "src.config_loader", "src.asr_utils"} or name.startswith(("src.web", "src.dictation", "src.job_runner")):
        raise AssertionError("product dependency: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = independent
from src.llm.gateway import GatewayClient, GatewayConnection
from src.llm.tasks.refinement import refine_document, RefinementOptions
from src.llm.tasks.dictation import DictationCleanup
'''
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("transport", ["urllib", "curl"])
def test_interleaved_connections_keep_url_auth_and_proxy_in_threads(monkeypatch, transport):
    # Both requests require a proxy: an accidental bypass would attempt DNS for
    # the reserved .invalid names. Each proxy must see only its own credentials.
    monkeypatch.setattr("src.llm.gateway.should_use_curl_first", lambda _url: transport == "curl")
    with fixed_gateway("A") as (proxy_a, seen_a), fixed_gateway("B") as (proxy_b, seen_b):
        connections = [GatewayConnection(
            f"http://gateway-{name.lower()}.invalid/api", f"local-key-{name}",
            proxy=ProxySettings.capture({"http_proxy": proxy, "no_proxy": ""}),
        ) for name, proxy in (("A", proxy_a), ("B", proxy_b))]
        clients = [GatewayClient(connection, timeout_seconds=3) for connection in connections]
        # Change every ambient value after construction. Neither curl nor urllib
        # may consume these values (including urllib's global proxy bypass).
        monkeypatch.setenv("CODEX_LB_BASE_URL", "http://wrong.invalid")
        monkeypatch.setenv("CODEX_LB_API_KEY", "wrong-key")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "*")
        monkeypatch.setenv("no_proxy", "*")
        before = dict(os.environ)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda index: clients[index % 2].responses_text({"index": index}), range(8)))
        assert results == ["A", "B"] * 4
        assert dict(os.environ) == before
        for name, seen in (("A", seen_a), ("B", seen_b)):
            assert len(seen) == 4
            assert {item["authorization"] for item in seen} == {f"Bearer local-key-{name}"}
            assert {item["path"] for item in seen} == {f"http://gateway-{name.lower()}.invalid/api/v1/responses"}


@pytest.mark.parametrize("transport", ["urllib", "curl"])
def test_explicit_proxy_bypass_is_local_to_one_connection(monkeypatch, transport):
    monkeypatch.setattr("src.llm.gateway.should_use_curl_first", lambda _url: transport == "curl")
    with fixed_gateway("direct") as (url, direct), fixed_gateway("proxy") as (proxy_url, proxied):
        settings = CodexLBSettings(base_url=url, base_url_env="", api_key_env="LOCAL_KEY")
        environ = {"LOCAL_KEY": "offline", "http_proxy": proxy_url, "no_proxy": ""}
        direct_client = GatewayClient(connection_from_settings(settings, environ=environ, bypass_proxy=True))
        proxy_client = GatewayClient(connection_from_settings(settings, environ=environ, bypass_proxy=False))
        assert direct_client.responses_text({}) == "direct"
        assert proxy_client.responses_text({}) == "proxy"
        assert len(direct) == len(proxied) == 1


def test_legacy_client_resolves_late_environment_but_explicit_client_stays_frozen(monkeypatch):
    monkeypatch.delenv("CODEX_LB_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_LB_API_KEY", raising=False)
    legacy = CodexLBClient(CodexLBSettings())
    from src.dictation.cleanup import Cleanup
    legacy_cleanup = Cleanup(CodexLBSettings(), timeout=3)
    with fixed_gateway("first") as (first, seen_first), fixed_gateway("second") as (second, seen_second):
        frozen = GatewayClient(GatewayConnection(first, "frozen-key"))
        frontend = FrontendSettings(codex_lb_base_url=second, codex_lb_api_key="late-key", codex_lb_bypass_proxy=True)
        with codex_lb_environment(frontend):
            assert legacy.responses_text({}) == "second"
            assert frozen.responses_text({}) == "first"
            assert legacy_cleanup("晚绑定的原文", [], [])["text"] == "晚绑定的原文。"
        assert seen_second[0]["authorization"] == "Bearer late-key"
        assert seen_first[0]["authorization"] == "Bearer frozen-key"


def test_web_connection_helper_never_changes_environment(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "legacy.internal")
    monkeypatch.setenv("CODEX_LB_BASE_URL", "http://old.invalid")
    before = dict(os.environ)
    connection = gateway_connection_for_frontend(CodexLBSettings(), FrontendSettings(
        codex_lb_base_url="http://new.invalid", codex_lb_api_key="private-key", codex_lb_bypass_proxy=True))
    assert dict(os.environ) == before
    assert connection.base_url == "http://new.invalid" and connection.api_key == "private-key"
    assert "new.invalid" in dict(connection.proxy.environment)["no_proxy"].split(",")
    assert "private-key" not in repr(connection)


@pytest.mark.parametrize("prompt_name", ["final_cleanup.md", "conversation_cleanup.md"])
def test_standalone_refinement_and_dictation_use_real_local_protocol(tmp_path, prompt_name):
    # There is deliberately no settings YAML, Web root, job, ASR model or PDF.
    prompt = (Path(__file__).resolve().parents[1] / "config" / "prompts" / prompt_name).read_text(encoding="utf-8").strip()
    with fixed_gateway() as (url, seen):
        connection = GatewayConnection(url, "local-fixture-only")
        gateway = GatewayClient(connection, timeout_seconds=3)
        result = refine_document("原文与讨论。", title="离线读书会", prompt=prompt,
                                 logs_dir=tmp_path / "diagnostics", workdir=tmp_path,
                                 gateway=gateway, options=RefinementOptions(model="fixed", timeout_seconds=3))
        assert result.selected_result.final_markdown == "# 离线读书会\n\n原文与讨论。"
        assert result.comparison_summary.startswith("single_backend_selected:codex_api:")
        assert prompt in seen[0]["payload"]["input"]
        assert "max_output_tokens" not in seen[0]["payload"] and "temperature" not in seen[0]["payload"]
        request_meta = next((tmp_path / "diagnostics").rglob("request-meta.json")).read_text()
        assert "local-fixture-only" not in request_meta
        assert list((tmp_path / "diagnostics").rglob("raw-response.sse"))
        before_dictation = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file())
        cleaner = DictationCleanup(connection, model="fixed-sentence", document_model="fixed-document", timeout=3, document_timeout=3)
        sentence = cleaner("原文", ["之前的上下文"], ["API"])
        document = cleaner.document("全文正文", [], ["API"])
        assert sentence["text"] == "原文。" and document["text"] == "全文正文。"
        assert sentence["metrics"]["input_tokens"] == 7 and sentence["metrics"]["output_tokens"] == 5
        assert seen[1]["payload"]["instructions"] == INSTRUCTIONS
        assert seen[2]["payload"]["instructions"] == DOCUMENT_INSTRUCTIONS
        assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()) == before_dictation


def test_standalone_multiple_backends_still_require_human_selection(tmp_path, monkeypatch):
    def backend(**kwargs):
        name = kwargs["backend"]
        return BackendDocumentRefinementResult(name, name, f"# 会话\n\n{name}候选。", "single_pass_safe_replace", "ok", [], [])
    monkeypatch.setattr("src.llm.tasks.refinement.run_single_pass_backend_refinement", backend)
    result = refine_document("原始讨论。", title="会话", prompt="人工指定提示词", logs_dir=tmp_path,
                             backends=["codex_cli", "agy"])
    assert len(result.backend_results) == 2
    assert result.selected_result is None
    assert result.comparison_summary == "manual_selection_required"
