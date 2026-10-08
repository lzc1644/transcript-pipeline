from __future__ import annotations

import json

import pytest

from src.codex_lb_client import CodexLBClient, endpoint_url
from src.schemas import CodexLBSettings


@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    [
        ("http://localhost:8317", "/v1/responses", "http://localhost:8317/v1/responses"),
        ("http://localhost:8317/", "v1/responses", "http://localhost:8317/v1/responses"),
        ("http://localhost:8317/v1", "/v1/responses", "http://localhost:8317/v1/responses"),
        ("http://localhost:8317/v1/", "v1/responses", "http://localhost:8317/v1/responses"),
        ("https://gateway.test/cpa", "/v1/responses", "https://gateway.test/cpa/v1/responses"),
        ("https://gateway.test/cpa/v1/", "/v1/responses", "https://gateway.test/cpa/v1/responses"),
        ("https://gateway.test/v10", "/v1/responses", "https://gateway.test/v10/v1/responses"),
        ("http://v1", "/v1/responses", "http://v1/v1/responses"),
        ("http://localhost:8317/v1", "/responses", "http://localhost:8317/v1/responses"),
        ("https://gateway.test", "/backend-api/codex/responses", "https://gateway.test/backend-api/codex/responses"),
        ("https://gateway.test", "https://other.test/custom/responses", "https://other.test/custom/responses"),
    ],
)
def test_endpoint_url_accepts_root_and_versioned_base_urls(
    base_url: str, path: str, expected: str,
) -> None:
    assert endpoint_url(base_url, path) == expected


def test_gateway_defaults_use_standard_responses_api() -> None:
    settings = CodexLBSettings()
    assert settings.base_url == "http://127.0.0.1:8317"
    assert settings.responses_path == settings.codex_responses_path == "/v1/responses"
    assert settings.base_url_env == "CODEX_LB_BASE_URL"
    assert settings.api_key_env == "CODEX_LB_API_KEY"


def test_explicit_legacy_codex_endpoint_is_still_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_LB_BASE_URL", "http://127.0.0.1:2455")
    monkeypatch.setenv("CODEX_LB_API_KEY", "legacy-test-key")
    client = CodexLBClient(CodexLBSettings(codex_responses_path="/backend-api/codex/responses"))
    seen_urls = []

    def fake_read_http_response(request, **kwargs):
        seen_urls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer legacy-test-key"
        assert json.loads(request.data)["stream"] is True
        return 'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'

    monkeypatch.setattr("src.codex_lb_client.read_http_response", fake_read_http_response)
    payload = {"model": "legacy-model", "input": "prompt", "stream": True, "store": False}
    assert client.codex_responses_text(payload) == "ok"
    assert client.responses_stream_text(payload) == "ok"
    assert seen_urls == [
        "http://127.0.0.1:2455/backend-api/codex/responses",
        "http://127.0.0.1:2455/v1/responses",
    ]
