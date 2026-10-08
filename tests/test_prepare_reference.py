from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config_loader import load_settings
from src.codex_lb_client import CodexLBClientError
from src.reference_utils import (
    CodexOCRError,
    CodexOCRPageProgress,
    CodexOCRPagesIncompleteError,
    AgyOCRError,
    ReferenceInputEmptyError,
    build_reference_output_paths,
    is_effectively_empty_text,
    iter_reference_files,
    prepare_reference_batch,
    prepare_reference_file,
    read_text_file,
    render_pdf_page_as_png_data_url,
    run_codex_api_pdf_ocr,
    run_codex_pdf_ocr,
    sanitize_ocrmypdf_text,
    sanitize_gemini_ocr_text,
    run_agy_pdf_ocr,
)
from tests.helpers import write_minimal_settings


def test_prepare_reference_batch_raises_when_reference_dir_empty(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    (tmp_path / "data/input/reference").mkdir(parents=True, exist_ok=True)

    loaded_settings = load_settings(project_root=tmp_path)

    from src.reference_utils import prepare_reference_batch

    with pytest.raises(ReferenceInputEmptyError):
        prepare_reference_batch(loaded_settings)


def test_read_text_file_for_txt(tmp_path: Path) -> None:
    source = tmp_path / "chapter01.txt"
    source.write_text("第一段\n第二段", encoding="utf-8")

    assert read_text_file(source) == "第一段\n第二段"


def test_read_markdown_file_via_prepare_reference_file(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    reference_dir = tmp_path / "data/input/reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    source = reference_dir / "outline.md"
    source.write_text("# 标题\n\n正文", encoding="utf-8")

    loaded_settings = load_settings(project_root=tmp_path)
    result = prepare_reference_file(source, loaded_settings)

    assert result.source_type == "md"
    assert result.success is True
    assert "# 标题" in result.extracted_text


def test_iter_reference_files_filters_supported_extensions(tmp_path: Path) -> None:
    (tmp_path / "book.txt").write_text("txt", encoding="utf-8")
    (tmp_path / "notes.MD").write_text("md", encoding="utf-8")
    (tmp_path / "scan.pdf").write_text("pdf", encoding="utf-8")
    (tmp_path / "image.png").write_text("png", encoding="utf-8")

    files = iter_reference_files(tmp_path, [".txt", ".md", ".pdf"])

    assert [path.name for path in files] == ["book.txt", "notes.MD", "scan.pdf"]


def test_build_reference_output_paths_uses_reference_basename(tmp_path: Path) -> None:
    reference_path = tmp_path / "chapter-03.pdf"
    output_dir = tmp_path / "data/intermediate/extracted_text"

    output_paths = build_reference_output_paths(reference_path, output_dir)

    assert output_paths.txt_path == output_dir / "chapter-03.txt"
    assert output_paths.json_path == output_dir / "chapter-03.json"


def test_is_effectively_empty_text_uses_content_length_threshold() -> None:
    assert is_effectively_empty_text("")
    assert is_effectively_empty_text(" \n ")
    assert not is_effectively_empty_text("这是足够长的可提取文字内容")


def test_prepare_reference_file_uses_codex_page_ocr_with_identity_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    reference_dir = tmp_path / "data/input/reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    source = reference_dir / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    loaded_settings = load_settings(project_root=tmp_path)
    seen: dict[str, object] = {}

    def fake_codex_ocr(
        _path: Path,
        _settings,
        *,
        sidecar_path: Path,
        checkpoint_dir: Path,
        progress_callback,
    ) -> tuple[str, list[str]]:
        seen["sidecar_path"] = sidecar_path
        seen["checkpoint_dir"] = checkpoint_dir
        progress_callback(
            CodexOCRPageProgress(
                page_count=2,
                completed_page_numbers=(1, 2),
                page_errors={},
            )
        )
        return "这是 Codex API OCR 提取出来的中文文本内容。", ["已使用 Codex API OCR。"]

    monkeypatch.setattr("src.reference_utils.run_codex_api_pdf_ocr", fake_codex_ocr)

    result = prepare_reference_file(source, loaded_settings)

    assert result.success is True
    assert result.extraction_method == "codex_api_pdf_ocr"
    assert result.page_count == 2
    assert result.completed_pages == 2
    assert seen["sidecar_path"] == loaded_settings.path_for("extracted_text_dir") / "scan.txt"
    checkpoint_dir = seen["checkpoint_dir"]
    assert isinstance(checkpoint_dir, Path)
    assert checkpoint_dir.parent == (loaded_settings.path_for("ocr_dir") / "pdf-pages").resolve()


def test_prepare_reference_file_prefers_agy_ocr_even_when_pdf_has_text_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, reference_overrides={"run_ocr_when_needed": True, "ai_ocr_backend": "agy"})
    reference_dir = tmp_path / "data/input/reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    source = reference_dir / "book.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    loaded_settings = load_settings(project_root=tmp_path)

    monkeypatch.setattr(
        "src.reference_utils.run_agy_pdf_ocr",
        lambda _path, _settings: ("这是 agy 优先 OCR 的结果。", ["已优先使用 agy OCR。model=Gemini 3.5 Flash (High)"]),
    )
    monkeypatch.setattr(
        "src.reference_utils.extract_pdf_text",
        lambda _path: ("这是 PDF 文字层内容。", []),
    )

    result = prepare_reference_file(source, loaded_settings)

    assert result.success is True
    assert result.extraction_method == "agy_pdf_ocr"
    assert "agy 优先 OCR 的结果" in result.extracted_text
    assert "优先使用 agy OCR" in " ".join(result.warnings)


def test_prepare_reference_file_does_not_fallback_when_selected_agy_ocr_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, reference_overrides={"run_ocr_when_needed": True, "ai_ocr_backend": "agy"})
    reference_dir = tmp_path / "data/input/reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    source = reference_dir / "book.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    loaded_settings = load_settings(project_root=tmp_path)

    def fake_agy_ocr(_path: Path, _settings) -> tuple[str, list[str]]:
        raise AgyOCRError("capacity exhausted")

    monkeypatch.setattr("src.reference_utils.run_agy_pdf_ocr", fake_agy_ocr)
    monkeypatch.setattr("src.reference_utils.extract_pdf_text", lambda _path: pytest.fail("不应回退文字层"))
    monkeypatch.setattr(
        "src.reference_utils.run_codex_api_pdf_ocr",
        lambda *_args, **_kwargs: pytest.fail("不应回退 Codex API"),
    )
    monkeypatch.setattr(
        "src.reference_utils.run_tesseract_pdf_ocr",
        lambda *_args, **_kwargs: pytest.fail("不应回退 ocrmypdf"),
    )

    result = prepare_reference_file(source, loaded_settings)

    assert result.success is False
    assert result.extraction_method == "agy_pdf_ocr"
    assert result.error == "capacity exhausted"
    assert result.resumable is False


def test_prepare_reference_file_returns_partial_page_state_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    reference_dir = tmp_path / "data/input/reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    source = reference_dir / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    loaded_settings = load_settings(project_root=tmp_path)

    def fake_codex_ocr(_path: Path, _settings, **_kwargs) -> tuple[str, list[str]]:
        raise CodexOCRPagesIncompleteError(
            source,
            page_count=3,
            completed_page_numbers=[1, 3],
            page_errors={2: "upstream_unavailable"},
        )

    monkeypatch.setattr("src.reference_utils.run_codex_api_pdf_ocr", fake_codex_ocr)

    result = prepare_reference_file(source, loaded_settings)

    assert result.success is False
    assert result.extraction_method == "codex_api_pdf_ocr"
    assert result.page_count == 3
    assert result.completed_pages == 2
    assert result.failed_page_numbers == (2,)
    assert result.page_errors == {2: "upstream_unavailable"}
    assert result.resumable is True
    assert "页码 2" in str(result.error)


def test_run_agy_pdf_ocr_stages_pdf_into_isolated_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    source = tmp_path / "external" / "scan.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")

    seen: dict[str, object] = {}

    def fake_run(command, *, text, capture_output, cwd, timeout, check):
        seen["command"] = command
        seen["cwd"] = cwd
        seen["timeout"] = timeout

        class Completed:
            returncode = 0
            stdout = "这是 agy OCR 的结果。"
            stderr = ""

        return Completed()

    monkeypatch.setattr("src.reference_utils.shutil.which", lambda _name: "/usr/bin/agy")
    monkeypatch.setattr("src.reference_utils.subprocess.run", fake_run)

    text, warnings = run_agy_pdf_ocr(source, loaded_settings)

    command = seen["command"]
    assert isinstance(command, list)
    assert text == "这是 agy OCR 的结果。"
    assert "agy OCR fallback" in " ".join(warnings)
    assert command[2] == loaded_settings.settings.reference.gemini_ocr_model
    assert seen["cwd"] != str(tmp_path)
    assert seen["timeout"] == loaded_settings.settings.reference.ocr_timeout_seconds
    assert str(seen["cwd"]).startswith(str(loaded_settings.path_for("ocr_dir")))
    staged_pdf = Path(str(seen["cwd"])) / source.name
    assert staged_pdf.exists()
    assert staged_pdf.read_bytes() == source.read_bytes()
    assert f"@{{{source.name}}}" in command[4]
    assert str(source) not in command[4]


def test_run_codex_pdf_ocr_uses_configured_model_prompt_and_reasoning_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(
        tmp_path,
        reference_overrides={
            "codex_ocr_model": "gpt-5.4-mini",
            "codex_ocr_reasoning_effort": "high",
        },
    )
    loaded_settings = load_settings(project_root=tmp_path)

    source = tmp_path / "external" / "scan.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")

    seen: dict[str, object] = {}

    def fake_run(command, *, input, text, capture_output, cwd, timeout, check):
        seen["command"] = command
        seen["input"] = input
        seen["cwd"] = cwd
        seen["timeout"] = timeout
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("这是 Codex OCR 的结果。", encoding="utf-8")

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("src.reference_utils.shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr("src.reference_utils.subprocess.run", fake_run)

    text, warnings = run_codex_pdf_ocr(source, loaded_settings)

    command = seen["command"]
    assert isinstance(command, list)
    assert text == "这是 Codex OCR 的结果。"
    assert "Codex OCR fallback" in " ".join(warnings)
    assert command[:4] == ["codex", "exec", "-C", str(Path(str(seen["cwd"])).resolve())]
    assert ["-m", "gpt-5.4-mini"] == command[6:8]
    assert '-c' in command
    assert 'model_reasoning_effort="high"' in command
    assert seen["timeout"] == loaded_settings.settings.reference.ocr_timeout_seconds
    assert "禁止调用本机的 OCR 工具" in str(seen["input"])
    assert "只使用模型自身的视觉能力" in str(seen["input"])
    assert f"@{{{source.name}}}" in str(seen["input"])


def test_render_pdf_page_as_png_data_url_uses_pdftoppm_for_requested_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    seen: dict[str, object] = {}

    def fake_run(command, *, text, capture_output, check):
        seen["command"] = command
        assert text is True
        assert capture_output is True
        assert check is False
        output_prefix = Path(command[-1])
        output_prefix.parent.mkdir(parents=True, exist_ok=True)
        output_prefix.with_suffix(".png").write_bytes(b"page-two")

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("src.reference_utils.shutil.which", lambda name: "/usr/bin/pdftoppm" if name == "pdftoppm" else None)
    monkeypatch.setattr("src.reference_utils.subprocess.run", fake_run)
    data_url = render_pdf_page_as_png_data_url(source, 2, 10)

    assert data_url == "data:image/png;base64,cGFnZS10d28="
    assert seen["command"][:-2] == ["pdftoppm", "-f", "2", "-l", "2", "-singlefile", "-png"]
    assert seen["command"][-2] == str(source)


def test_render_pdf_page_as_png_data_url_rejects_missing_rendered_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    def fake_run(command, *, text, capture_output, check):
        _ = text, capture_output, check
        _ = command

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("src.reference_utils.shutil.which", lambda _name: "/usr/bin/pdftoppm")
    monkeypatch.setattr("src.reference_utils.subprocess.run", fake_run)
    with pytest.raises(CodexOCRError, match="未生成第 2 页图片"):
        render_pdf_page_as_png_data_url(source, 2, 3)


def test_render_pdf_page_as_png_data_url_rejects_page_out_of_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr("src.reference_utils.shutil.which", lambda _name: "/usr/bin/pdftoppm")

    with pytest.raises(CodexOCRError, match="页面超出范围"):
        render_pdf_page_as_png_data_url(source, 11, 10)


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:2455", "http://127.0.0.1:2455/v1/"])
def test_run_codex_api_pdf_ocr_renders_pages_and_sends_input_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    write_minimal_settings(
        tmp_path,
        reference_overrides={
            "codex_ocr_model": "gpt-5.4-mini",
            "codex_ocr_reasoning_effort": "high",
            "codex_ocr_max_concurrency": 1,
            "codex_ocr_submit_interval_seconds": 0,
        },
    )
    loaded_settings = load_settings(project_root=tmp_path)
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-key")
    monkeypatch.setenv("CODEX_LB_BASE_URL", base_url)

    source = tmp_path / "external" / "scan.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")
    seen: dict[str, object] = {"requests": [], "response_payloads": [], "rendered_pages": []}

    class FakeResponse:
        def __init__(self, payload: dict | str = "") -> None:
            self.payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb

        def read(self) -> bytes:
            if isinstance(self.payload, dict):
                return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
            return self.payload.encode("utf-8")

    def fake_urlopen(request, timeout=None):
        body = request.data or b""
        request_record = {
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {key.lower(): value for key, value in request.header_items()},
            "body": body,
            "timeout": timeout,
        }
        seen["requests"].append(request_record)
        if request.full_url == "http://127.0.0.1:2455/v1/responses":
            response_payload = json.loads(body.decode("utf-8"))
            response_payloads = seen["response_payloads"]
            assert isinstance(response_payloads, list)
            response_payloads.append(response_payload)
            page_number = len(response_payloads)
            delta = "目录" if page_number == 1 else "第2页 OCR 识别出的完整正文内容。"
            event_payload = json.dumps(
                {"type": "response.output_text.delta", "delta": delta},
                ensure_ascii=False,
            )
            return FakeResponse(f"event: response.output_text.delta\ndata: {event_payload}\n\n")
        pytest.fail(f"未预期的请求: {request.full_url}")

    def fake_render_page(_path: Path, page_number: int, page_count: int) -> str:
        assert page_count == 2
        rendered_pages = seen["rendered_pages"]
        assert isinstance(rendered_pages, list)
        rendered_pages.append(page_number)
        return ["data:image/png;base64,Zmlyc3Q=", "data:image/png;base64,c2Vjb25k"][page_number - 1]

    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda _path: 2)
    monkeypatch.setattr("src.reference_utils.render_pdf_page_as_png_data_url", fake_render_page)
    monkeypatch.setattr("src.codex_lb_client.urlopen", fake_urlopen)

    text, warnings = run_codex_api_pdf_ocr(source, loaded_settings)

    assert text == "目录\n\n----- OCR_PAGE_BREAK: 1 -> 2 -----\n\n第2页 OCR 识别出的完整正文内容。"
    assert "Codex API OCR" in " ".join(warnings)
    requests = seen["requests"]
    assert isinstance(requests, list)
    assert len(requests) == 2
    assert seen["rendered_pages"] == [1, 2]
    response_payloads = seen["response_payloads"]
    assert isinstance(response_payloads, list)
    for page_number, (request, response_payload, image_url) in enumerate(
        zip(requests, response_payloads, ["data:image/png;base64,Zmlyc3Q=", "data:image/png;base64,c2Vjb25k"], strict=True),
        start=1,
    ):
        assert request["method"] == "POST"
        assert request["headers"]["authorization"] == "Bearer test-key"
        assert request["headers"]["accept"] == "text/event-stream"
        assert response_payload["model"] == "gpt-5.4-mini"
        assert response_payload["reasoning"] == {"effort": "high"}
        assert response_payload["stream"] is True
        content = response_payload["input"][0]["content"]
        assert len(content) == 2
        assert "单页图片做 OCR 提取" in content[0]["text"]
        assert "脚注" not in content[0]["text"]
        assert "尾注" not in content[0]["text"]
        assert f"当前页：{page_number}" in content[0]["text"]
        assert "PDF 总页数：2" in content[0]["text"]
        assert content[1] == {"type": "input_image", "image_url": image_url}
    sidecar_path = loaded_settings.path_for("ocr_dir") / f"{source.stem}.codex_api_ocr.txt"
    assert sidecar_path.read_text(encoding="utf-8") == text


def test_run_codex_api_pdf_ocr_writes_to_explicit_sidecar_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    source = tmp_path / "external" / "book.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")
    explicit_sidecar_path = tmp_path / "book-ocr" / "part-01" / "book.txt"

    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda _path: 1)
    monkeypatch.setattr(
        "src.reference_utils.render_pdf_page_as_png_data_url",
        lambda _path, _page_number, _page_count: "data:image/png;base64,Zmlyc3Q=",
    )
    monkeypatch.setattr(
        "src.reference_utils.CodexLBClient.responses_stream_text",
        lambda _client, _payload: "这是一段完整的书籍 OCR 正文内容。",
    )

    text, _warnings = run_codex_api_pdf_ocr(
        source,
        loaded_settings,
        sidecar_path=explicit_sidecar_path,
    )

    assert text == "这是一段完整的书籍 OCR 正文内容。"
    assert explicit_sidecar_path.read_text(encoding="utf-8") == text
    default_sidecar_path = loaded_settings.path_for("ocr_dir") / f"{source.stem}.codex_api_ocr.txt"
    assert not default_sidecar_path.exists()


def test_run_codex_api_pdf_ocr_continues_all_pages_without_writing_incomplete_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(
        tmp_path,
        reference_overrides={"codex_ocr_max_concurrency": 1, "codex_ocr_submit_interval_seconds": 0},
    )
    loaded_settings = load_settings(project_root=tmp_path)
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-key")
    source = tmp_path / "external" / "scan.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")
    calls: list[dict[str, object]] = []

    def fake_responses_stream_text(_client, payload: dict[str, object]) -> str:
        calls.append(payload)
        if len(calls) == 2:
            raise CodexLBClientError("context_length_exceeded")
        return "第一页 OCR 识别出的完整正文内容。"

    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda _path: 3)
    monkeypatch.setattr(
        "src.reference_utils.render_pdf_page_as_png_data_url",
        lambda _path, page_number, _page_count: [
            "data:image/png;base64,Zmlyc3Q=",
            "data:image/png;base64,c2Vjb25k",
            "data:image/png;base64,dGhpcmQ=",
        ][page_number - 1],
    )
    monkeypatch.setattr("src.reference_utils.CodexLBClient.responses_stream_text", fake_responses_stream_text)

    with pytest.raises(CodexOCRPagesIncompleteError, match="页码 2") as error:
        run_codex_api_pdf_ocr(source, loaded_settings)

    sidecar_path = loaded_settings.path_for("ocr_dir") / f"{source.stem}.codex_api_ocr.txt"
    assert len(calls) == 3
    assert error.value.completed_page_numbers == (1, 3)
    assert "context_length_exceeded" in error.value.page_errors[2]
    assert not sidecar_path.exists()


def test_run_codex_api_pdf_ocr_resumes_only_missing_checkpoint_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(
        tmp_path,
        reference_overrides={"codex_ocr_max_concurrency": 1, "codex_ocr_submit_interval_seconds": 0},
    )
    loaded_settings = load_settings(project_root=tmp_path)
    source = tmp_path / "external" / "scan.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")
    checkpoint_dir = tmp_path / "checkpoints"
    sidecar_path = tmp_path / "output" / "scan.txt"
    rendered_pages: list[int] = []
    should_fail_page_two = True

    def fake_render(_path: Path, page_number: int, _page_count: int) -> str:
        rendered_pages.append(page_number)
        return f"data:image/png;base64,page-{page_number}"

    def fake_stream(_client, _payload: dict[str, object]) -> str:
        page_number = rendered_pages[-1]
        if page_number == 2 and should_fail_page_two:
            raise CodexLBClientError("upstream_unavailable")
        return f"第{page_number}页 OCR 识别出的完整正文内容。"

    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda _path: 3)
    monkeypatch.setattr("src.reference_utils.render_pdf_page_as_png_data_url", fake_render)
    monkeypatch.setattr("src.reference_utils.CodexLBClient.responses_stream_text", fake_stream)

    with pytest.raises(CodexOCRPagesIncompleteError):
        run_codex_api_pdf_ocr(
            source,
            loaded_settings,
            sidecar_path=sidecar_path,
            checkpoint_dir=checkpoint_dir,
        )

    assert rendered_pages == [1, 2, 3]
    assert (checkpoint_dir / "page-000001.txt").read_text(encoding="utf-8") == "第1页 OCR 识别出的完整正文内容。"
    assert not (checkpoint_dir / "page-000002.txt").exists()
    assert (checkpoint_dir / "page-000003.txt").read_text(encoding="utf-8") == "第3页 OCR 识别出的完整正文内容。"
    assert not sidecar_path.exists()

    should_fail_page_two = False
    rendered_pages.clear()
    text, _warnings = run_codex_api_pdf_ocr(
        source,
        loaded_settings,
        sidecar_path=sidecar_path,
        checkpoint_dir=checkpoint_dir,
    )

    assert rendered_pages == [2]
    assert text == (
        "第1页 OCR 识别出的完整正文内容。\n\n"
        "----- OCR_PAGE_BREAK: 1 -> 2 -----\n\n"
        "第2页 OCR 识别出的完整正文内容。\n\n"
        "----- OCR_PAGE_BREAK: 2 -> 3 -----\n\n"
        "第3页 OCR 识别出的完整正文内容。"
    )
    assert sidecar_path.read_text(encoding="utf-8") == text


def test_prepare_reference_batch_retries_only_missing_pages_and_writes_text_after_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(
        tmp_path,
        reference_overrides={"codex_ocr_max_concurrency": 1, "codex_ocr_submit_interval_seconds": 0},
    )
    loaded_settings = load_settings(project_root=tmp_path)
    reference_dir = loaded_settings.path_for("reference_dir")
    reference_dir.mkdir(parents=True, exist_ok=True)
    source = reference_dir / "book.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    rendered_pages: list[int] = []
    fail_page_two = True

    def fake_render(_path: Path, page_number: int, _page_count: int) -> str:
        rendered_pages.append(page_number)
        return f"data:image/png;base64,page-{page_number}"

    def fake_stream(_client, _payload: dict[str, object]) -> str:
        page_number = rendered_pages[-1]
        if page_number == 2 and fail_page_two:
            raise CodexLBClientError("upstream_unavailable")
        return f"第{page_number}页识别正文。"

    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda _path: 3)
    monkeypatch.setattr("src.reference_utils.render_pdf_page_as_png_data_url", fake_render)
    monkeypatch.setattr("src.reference_utils.CodexLBClient.responses_stream_text", fake_stream)

    first_summary = prepare_reference_batch(loaded_settings)

    output_path = loaded_settings.path_for("extracted_text_dir") / "book.txt"
    metadata_path = loaded_settings.path_for("extracted_text_dir") / "book.json"
    assert first_summary.partial == 1
    assert rendered_pages == [1, 2, 3]
    assert not output_path.exists()
    first_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert first_metadata["resumable"] is True
    assert first_metadata["failed_page_numbers"] == [2]

    fail_page_two = False
    rendered_pages.clear()
    second_summary = prepare_reference_batch(loaded_settings)

    assert second_summary.success == 1
    assert second_summary.partial == 0
    assert rendered_pages == [2]
    assert output_path.read_text(encoding="utf-8") == (
        "第1页识别正文。\n\n"
        "----- OCR_PAGE_BREAK: 1 -> 2 -----\n\n"
        "第2页识别正文。\n\n"
        "----- OCR_PAGE_BREAK: 2 -> 3 -----\n\n"
        "第3页识别正文。"
    )


def test_run_codex_api_pdf_ocr_accepts_blank_page_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(
        tmp_path,
        reference_overrides={"codex_ocr_max_concurrency": 1, "codex_ocr_submit_interval_seconds": 0},
    )
    loaded_settings = load_settings(project_root=tmp_path)
    source = tmp_path / "blank.pdf"
    source.write_bytes(b"%PDF-1.4 blank")
    checkpoint_dir = tmp_path / "pages"
    sidecar_path = tmp_path / "blank.txt"

    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda _path: 1)
    monkeypatch.setattr(
        "src.reference_utils.render_pdf_page_as_png_data_url",
        lambda _path, _page_number, _page_count: "data:image/png;base64,blank",
    )
    monkeypatch.setattr(
        "src.reference_utils.CodexLBClient.responses_stream_text",
        lambda _client, _payload: "",
    )

    text, _warnings = run_codex_api_pdf_ocr(
        source,
        loaded_settings,
        sidecar_path=sidecar_path,
        checkpoint_dir=checkpoint_dir,
    )

    assert text == ""
    assert (checkpoint_dir / "page-000001.txt").is_file()
    assert (checkpoint_dir / "page-000001.txt").read_text(encoding="utf-8") == ""
    assert sidecar_path.is_file()
    assert sidecar_path.read_text(encoding="utf-8") == ""


def test_run_codex_api_pdf_ocr_uses_curl_first_for_remote_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-key")
    source = tmp_path / "external" / "scan.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):
        _ = timeout
        pytest.fail(f"远程 codex-lb 请求应优先使用 curl，不应走 urlopen: {request.full_url}")

    class Completed:
        returncode = 0
        stderr = ""
        payload = json.dumps(
            {"type": "response.output_text.delta", "delta": "远程流式 OCR 结果。"},
            ensure_ascii=False,
        )
        stdout = f"event: response.output_text.delta\ndata: {payload}\n\n\n200"

    def fake_run(command, *, input, text, capture_output, check):
        _ = input, text, capture_output, check
        seen["curl_command"] = command
        return Completed()

    monkeypatch.setenv("CODEX_LB_BASE_URL", "https://api.redworker.org")
    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda _path: 1)
    monkeypatch.setattr(
        "src.reference_utils.render_pdf_page_as_png_data_url",
        lambda _path, _page_number, _page_count: "data:image/png;base64,Zmlyc3Q=",
    )
    monkeypatch.setattr("src.codex_lb_client.urlopen", fake_urlopen)
    monkeypatch.setattr("src.codex_lb_client.shutil.which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    monkeypatch.setattr("src.codex_lb_client.subprocess.run", fake_run)

    text, _warnings = run_codex_api_pdf_ocr(source, loaded_settings)

    assert text == "远程流式 OCR 结果。"
    command = seen["curl_command"]
    assert isinstance(command, list)
    assert command[0] == "curl"
    assert "https://api.redworker.org/v1/responses" in command


def test_run_agy_pdf_ocr_uses_reference_fallback_model_only_for_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(
        tmp_path,
        reference_overrides={"gemini_ocr_model": "Gemini 3.5 Flash (High)", "gemini_ocr_fallback_model": "Gemini 3.5 Flash (Low)"},
        llm_overrides={"gemini_model": "Gemini 3.1 Pro (High)", "gemini_fallback_model": ""},
    )
    loaded_settings = load_settings(project_root=tmp_path)

    source = tmp_path / "external" / "scan.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")

    commands: list[list[str]] = []

    def fake_run(command, *, text, capture_output, cwd, timeout, check):
        commands.append(command)

        class Completed:
            def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if command[2] == "Gemini 3.5 Flash (High)":
            return Completed(1, "", "429 MODEL_CAPACITY_EXHAUSTED")
        return Completed(0, "这是 agy OCR 的结果。", "")

    monkeypatch.setattr("src.reference_utils.shutil.which", lambda _name: "/usr/bin/agy")
    monkeypatch.setattr("src.reference_utils.subprocess.run", fake_run)

    text, warnings = run_agy_pdf_ocr(source, loaded_settings)

    assert text == "这是 agy OCR 的结果。"
    assert commands == [
        ["agy", "--model", "Gemini 3.5 Flash (High)", "--print", commands[0][4], "--print-timeout", "480s"],
        ["agy", "--model", "Gemini 3.5 Flash (Low)", "--print", commands[1][4], "--print-timeout", "480s"],
    ]
    assert "model=Gemini 3.5 Flash (Low)" in " ".join(warnings)


def test_sanitize_gemini_ocr_text_removes_leakage_page_markers_and_tail_repetition() -> None:
    raw_text = """
t>...
CRITICAL INSTRUCTION 2: ...'

Now I have the text content of the PDF.
我将读取 PDF 文件并提取纯文本。

[Page 1]
t第二节 巩固国家统一的重要政策措施

1872 年德文版序言

1882 年俄文版序言

1872 年6月24日于伦敦

秦始皇坚持法家政治路线。
201

[Page 2]
地主阶级专政进一步加强。
202

OK, I will output purely the text from these pages.
Page 1:
第二节 巩固国家统一的重要政策措施
""".strip()

    assert sanitize_gemini_ocr_text(raw_text) == (
        "第二节 巩固国家统一的重要政策措施\n\n"
        "1872 年德文版序言\n\n"
        "1882 年俄文版序言\n\n"
        "1872 年6月24日于伦敦\n\n"
        "秦始皇坚持法家政治路线。\n\n"
        "地主阶级专政进一步加强。"
    )


def test_run_agy_pdf_ocr_sanitizes_model_output_before_return_and_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    source = tmp_path / "external" / "scan.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")

    polluted_output = """
CRITICAL INSTRUCTION 2: ...'
[Page 1]
第二节 巩固国家统一的重要政策措施
201

OK, I will output purely the text from these pages.
""".strip()

    def fake_run(_command, *, text, capture_output, cwd, timeout, check):
        class Completed:
            returncode = 0
            stdout = polluted_output
            stderr = ""

        return Completed()

    monkeypatch.setattr("src.reference_utils.shutil.which", lambda _name: "/usr/bin/agy")
    monkeypatch.setattr("src.reference_utils.subprocess.run", fake_run)

    text, _warnings = run_agy_pdf_ocr(source, loaded_settings)

    sidecar_path = loaded_settings.path_for("ocr_dir") / f"{source.stem}.agy_ocr.txt"
    assert text == "第二节 巩固国家统一的重要政策措施"
    assert sidecar_path.read_text(encoding="utf-8") == "第二节 巩固国家统一的重要政策措施"


def test_sanitize_ocrmypdf_text_removes_repeated_headers_footers_and_page_numbers() -> None:
    raw_text = (
        "附一，中国革命的社会意义 395\n"
        "第一段 正文内容。\n"
        "326\n"
        "\f"
        "附一，中国革命的社会意义 396\n"
        "第二段   正文内容。\n"
        "327\n"
        "\f"
        "附一，中国革命的社会意义 397\n"
        "第三段 正文内容。\n"
        "328\n"
    )

    assert sanitize_ocrmypdf_text(raw_text) == (
        "第一段 正文内容。\n\n"
        "第二段 正文内容。\n\n"
        "第三段 正文内容。"
    )


def test_sanitize_ocrmypdf_text_removes_garbled_lines_and_collapses_cjk_spaces() -> None:
    raw_text = (
        "第一段  正文内容。\n"
        "新兴地主阶级专\n"
        "政，扩大封建制的社会基础。\n"
        "207\n"
        "Be be\n"
        "fis WET GRRE A EL, PMO WES\n"
        "CBR). “PRAT” CK) 。\n"
        "第二段 的 政治、军 事、文 化制度。\n"
        "7}\n"
        "= Il\n"
    )

    assert sanitize_ocrmypdf_text(raw_text) == (
        "第一段 正文内容。\n"
        "新兴地主阶级专政，扩大封建制的社会基础。\n"
        "第二段 的政治、军事、文化制度。"
    )


def test_run_tesseract_pdf_ocr_sanitizes_sidecar_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.reference_utils import run_tesseract_pdf_ocr

    write_minimal_settings(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    source = tmp_path / "external" / "scan.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")

    sidecar_payload = "附一，中国革命的社会意义 395\n正文内容。\n326\n"

    def fake_run(_command, *, text, capture_output, check):
        ocr_dir = loaded_settings.path_for("ocr_dir")
        (ocr_dir / "scan.ocr.txt").write_text(sidecar_payload, encoding="utf-8")

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("src.reference_utils.shutil.which", lambda _name: f"/usr/bin/{_name}")
    monkeypatch.setattr("src.reference_utils.subprocess.run", fake_run)

    text, warnings = run_tesseract_pdf_ocr(source, loaded_settings)

    assert text == "正文内容。"
    assert "ocrmypdf_tesseract" in " ".join(warnings)
