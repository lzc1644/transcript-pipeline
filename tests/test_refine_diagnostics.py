from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config_loader import load_settings
from src.refine_utils import CLIBackendError, run_codex_api_payload
from src.request_trace import RequestTrace, describe_refine_attempt
from tests.helpers import write_minimal_settings


def build_completed_sse(output_text: str, *, response_id: str = "resp_test_trace") -> str:
    delta = json.dumps(
        {"type": "response.output_text.delta", "delta": output_text},
        ensure_ascii=False,
    )
    completed = json.dumps(
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": output_text}],
                    }
                ],
            },
        },
        ensure_ascii=False,
    )
    return (
        f"event: response.output_text.delta\ndata: {delta}\n\n"
        f"event: response.completed\ndata: {completed}\n\n"
    )


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:2455", "http://127.0.0.1:2455/v1/"])
def test_codex_api_trace_persists_request_transport_sse_and_parsed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"model": "gpt-5.6-terra", "reasoning_effort": "xhigh"})
    loaded_settings = load_settings(project_root=tmp_path)
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-secret-key")
    monkeypatch.setenv("CODEX_LB_BASE_URL", base_url)
    output_payload = {
        "final_markdown": "# 可交付标题\n\n完整正文",
        "section_map": [],
        "refinement_notes": [],
        "needs_review_sections": [],
    }
    stream_text = build_completed_sse(json.dumps(output_payload, ensure_ascii=False))

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb

        def read(self) -> bytes:
            return stream_text.encode("utf-8")

        def geturl(self) -> str:
            return "http://127.0.0.1:2455/v1/responses"

    monkeypatch.setattr("src.codex_lb_client.urlopen", lambda _request, timeout=None: FakeResponse())
    trace = RequestTrace(tmp_path / "trace")

    result = run_codex_api_payload("阶段 6 测试 Prompt", loaded_settings, request_trace=trace)

    assert result["final_markdown"] == "# 可交付标题\n\n完整正文"
    request_meta = json.loads((trace.directory / "request-meta.json").read_text(encoding="utf-8"))
    assert request_meta["model"] == "gpt-5.6-terra"
    assert request_meta["prompt_chars"] == len("阶段 6 测试 Prompt")
    assert request_meta["reasoning_effort"] == "xhigh"
    assert request_meta["endpoint"]["host"] == "127.0.0.1"
    assert request_meta["endpoint"]["path"] == "/v1/responses"
    assert "authorization" not in json.dumps(request_meta, ensure_ascii=False).lower()

    transport = json.loads((trace.directory / "transport.json").read_text(encoding="utf-8"))
    assert transport["transports"][0]["transport"] == "urllib"
    assert transport["transports"][0]["result"] == "ok"
    assert transport["transports"][0]["http_status"] == 200
    assert (trace.directory / "raw-response.sse").read_text(encoding="utf-8") == stream_text

    sse_summary = json.loads((trace.directory / "sse-summary.json").read_text(encoding="utf-8"))
    assert sse_summary["terminal_event"] == "response.completed"
    assert sse_summary["response_ids"] == ["resp_test_trace"]
    assert sse_summary["malformed_json_events"] == 0
    assert json.loads((trace.directory / "extracted-output.txt").read_text(encoding="utf-8")) == output_payload
    assert json.loads((trace.directory / "parsed-output.json").read_text(encoding="utf-8")) == output_payload

    all_trace_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in trace.directory.rglob("*")
        if path.is_file()
    )
    assert "test-secret-key" not in all_trace_text


def test_codex_api_trace_keeps_partial_curl_response_and_transport_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"model": "gpt-5.6-terra"})
    loaded_settings = load_settings(project_root=tmp_path)
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-secret-key")
    monkeypatch.setenv("CODEX_LB_BASE_URL", "https://api.redworker.org")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:password@127.0.0.1:10808")
    partial_delta = json.dumps(
        {"type": "response.output_text.delta", "delta": '{"final_markdown":"未完成'},
        ensure_ascii=False,
    )
    partial_stream = f"event: response.output_text.delta\ndata: {partial_delta}\n\n"

    class Completed:
        returncode = 92
        stderr = "curl: (92) HTTP/2 stream was not closed cleanly: INTERNAL_ERROR"
        stdout = (
            partial_stream
            + "\n000\t127.0.0.1\t10808\t172.18.0.1\t43122\t2\t0.001\t0.002\t0.003\t0.100\t5.000\t128"
        )

    monkeypatch.setattr("src.codex_lb_client.shutil.which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    monkeypatch.setattr("src.codex_lb_client.subprocess.run", lambda *args, **kwargs: Completed())
    trace = RequestTrace(tmp_path / "trace")

    with pytest.raises(CLIBackendError, match="curl 请求失败"):
        run_codex_api_payload("阶段 6 测试 Prompt", loaded_settings, request_trace=trace)

    transport = json.loads((trace.directory / "transport.json").read_text(encoding="utf-8"))
    curl_record = transport["transports"][0]
    assert curl_record["transport"] == "curl"
    assert curl_record["result"] == "curl_error"
    assert curl_record["return_code"] == 92
    assert curl_record["metrics"]["remote_ip"] == "127.0.0.1"
    assert curl_record["metrics"]["remote_port"] == "10808"
    assert curl_record["stderr"] == Completed.stderr
    assert (trace.directory / "raw-response.sse").read_text(encoding="utf-8") == partial_stream
    backend_error = json.loads((trace.directory / "backend-error.json").read_text(encoding="utf-8"))
    assert backend_error["error_type"] == "CodexLBClientError"

    request_meta = (trace.directory / "request-meta.json").read_text(encoding="utf-8")
    assert "test-secret-key" not in request_meta
    assert "user:password" not in request_meta
    assert "127.0.0.1:10808" in request_meta


def test_codex_api_trace_keeps_completed_sse_when_model_output_is_not_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"model": "gpt-5.6-terra"})
    loaded_settings = load_settings(project_root=tmp_path)
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-secret-key")
    invalid_output = "# 标题\n\n这次模型直接返回了 Markdown，没有返回约定的 JSON。"
    stream_text = build_completed_sse(invalid_output, response_id="resp_invalid_json")

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb

        def read(self) -> bytes:
            return stream_text.encode("utf-8")

        def geturl(self) -> str:
            return "http://127.0.0.1:2455/v1/responses"

    monkeypatch.setattr("src.codex_lb_client.urlopen", lambda _request, timeout=None: FakeResponse())
    trace = RequestTrace(tmp_path / "trace")

    with pytest.raises(CLIBackendError, match="JSON"):
        run_codex_api_payload("阶段 6 测试 Prompt", loaded_settings, request_trace=trace)

    assert (trace.directory / "raw-response.sse").read_text(encoding="utf-8") == stream_text
    assert (trace.directory / "extracted-output.txt").read_text(encoding="utf-8") == invalid_output
    parse_error = json.loads((trace.directory / "parse-error.json").read_text(encoding="utf-8"))
    assert parse_error["category"] == "model_output_json"
    sse_summary = json.loads((trace.directory / "sse-summary.json").read_text(encoding="utf-8"))
    assert sse_summary["terminal_event"] == "response.completed"
    assert sse_summary["response_ids"] == ["resp_invalid_json"]


@pytest.mark.parametrize(
    ("filename", "category"),
    [("parse-error.json", "model_output_json"),
     ("backend-error.json", "transport_or_protocol"),
     ("result-contract-error.json", "result_contract")],
)
def test_attempt_summary_exposes_backend_cause_not_fallback(
    tmp_path: Path, filename: str, category: str,
) -> None:
    trace = RequestTrace(tmp_path)
    trace.write_json(filename, {"message": "底层错误详情"})
    trace.write_json("request-meta.json", {"model": "configured-model"})
    details = describe_refine_attempt(
        trace, reasons=["programmatic_markdown_fallback", "uses_canonical_source_placeholder_title"],
        markdown="# source\n\n回退稿",
    )
    assert details["failure_category"] == category
    assert "底层错误详情" in details["failure_summary"]
    assert details["requested_model"] == "configured-model"
    assert "candidate_file" not in details
    assert not (tmp_path / "candidate-review.md").exists()


def test_attempt_summary_preserves_candidate_and_locates_damage(tmp_path: Path) -> None:
    trace = RequestTrace(tmp_path)
    markdown = "# 标题\n\n第一处�，第二处�。"
    details = describe_refine_attempt(
        trace, reasons=["contains_unicode_replacement_character"], markdown=markdown,
    )
    assert details["failure_category"] == "content_validation"
    assert details["replacement_character_count"] == 2
    assert details["replacement_character_examples"][0]["line"] == 3
    assert (tmp_path / details["candidate_file"]).read_text() == markdown
    assert details["candidate_status"] == "needs_review_not_accepted"


def test_attempt_summary_does_not_claim_candidate_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RequestTrace, "write_text", lambda *args: False)
    details = describe_refine_attempt(
        RequestTrace(tmp_path), reasons=["contains_unicode_replacement_character"], markdown="�",
    )
    assert "candidate_file" not in details
    assert details["failure_category"] == "content_validation"


def test_codex_api_trace_write_failure_does_not_change_successful_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"model": "gpt-5.6-terra"})
    loaded_settings = load_settings(project_root=tmp_path)
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-secret-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:not-a-port")
    output_payload = {
        "final_markdown": "# 可交付标题\n\n完整正文",
        "section_map": [],
        "refinement_notes": [],
        "needs_review_sections": [],
    }
    stream_text = build_completed_sse(json.dumps(output_payload, ensure_ascii=False))

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb

        def read(self) -> bytes:
            return stream_text.encode("utf-8")

        def geturl(self) -> str:
            return "http://127.0.0.1:2455/v1/responses"

    monkeypatch.setattr("src.codex_lb_client.urlopen", lambda _request, timeout=None: FakeResponse())
    blocked_trace_path = tmp_path / "trace-is-a-file"
    blocked_trace_path.write_text("阻止创建同名诊断目录", encoding="utf-8")
    trace = RequestTrace(blocked_trace_path)
    warning_messages: list[str] = []

    def capture_warning(message: str, *args: object) -> None:
        warning_messages.append(message % args)

    monkeypatch.setattr("src.request_trace.LOGGER.warning", capture_warning)

    result = run_codex_api_payload("阶段 6 测试 Prompt", loaded_settings, request_trace=trace)

    assert result["final_markdown"] == output_payload["final_markdown"]
    assert any("请求诊断文本写入失败" in message for message in warning_messages)
