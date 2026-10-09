from pathlib import Path

import pytest

from tests.frontend_workbench_server import build_app
from tests.test_api_server import request_json


def test_browser_fixture_refuses_existing_data(tmp_path: Path) -> None:
    existing = tmp_path / "keep.txt"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="new or empty"):
        build_app(tmp_path)
    assert existing.read_text(encoding="utf-8") == "keep"


def test_browser_fixture_blocks_model_execution(tmp_path: Path) -> None:
    app = build_app(tmp_path)
    marker = request_json(app, "GET", "/api/__workbench_test__")
    assert marker.json()["isolated"] is True
    assert marker.json()["model_execution"] is False
    for path in (
        "/api/jobs",
        "/api/batches",
        "/api/stages/transcribe/file-run",
        "/api/stages/refine/run",
        "/api/pdf-book-ocr",
        "/api/jobs/ui-example-failed/rerun",
    ):
        response = request_json(app, "POST", path, json_body={})
        assert response.status_code == 403
    states = request_json(app, "GET", "/api/jobs").json()["items"]
    assert {state["status"] for state in states} == {"success", "running", "failed"}


def test_browser_fixture_allows_isolated_settings(tmp_path: Path) -> None:
    app = build_app(tmp_path)
    response = request_json(app, "PUT", "/api/frontend-settings", json_body={"codex_lb_base_url": "http://127.0.0.1:2455"})
    assert response.status_code == 200
    assert (tmp_path / "data/jobs/frontend-settings.json").exists()
