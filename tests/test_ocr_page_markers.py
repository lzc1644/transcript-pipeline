from pathlib import Path

import pytest

from src.align_utils import AsrBlock, align_blocks, build_reference_blocks
from src.config_loader import load_settings
from src.ocr_page_markers import format_ocr_page_break, is_ocr_page_break_line, strip_ocr_page_break_markers
from src.reference_utils import run_codex_api_pdf_ocr, write_codex_ocr_page_checkpoint
from src.refine_utils import (
    BackendDocumentRefinementResult,
    PreReplacementSegment,
    RefinementBlock,
    build_minimal_edit_prompt,
    build_pre_replaced_document,
    build_single_pass_refine_prompt,
    calculate_document_score,
    compare_backend_documents,
    load_markdown_assemble_prompt,
    load_refinement_prompt,
    resolve_refinement_input_paths,
)
from src.text_integration import integrate_ocr_texts
from tests.helpers import write_minimal_settings


@pytest.mark.parametrize("page", [1, 9, 99, 602])
def test_page_marker_format_and_exact_recognition(page):
    marker = format_ocr_page_break(page)
    assert marker == f"----- OCR_PAGE_BREAK: {page} -> {page + 1} -----"
    assert is_ocr_page_break_line(marker)
    assert is_ocr_page_break_line(f"  {marker}\r")


@pytest.mark.parametrize("text", [
    "-----", "2026 年 10 月", "① 注释。", "----- 1 -> 2 -----",
    "OCR_PAGE_BREAK: 1 -> 2", "----- OCR_PAGE_BREAK: 0 -> 1 -----",
    "----- OCR_PAGE_BREAK: 1 -> 3 -----", "----- OCR_PAGE_BREAK: 01 -> 02 -----",
    "正文提到了 ----- OCR_PAGE_BREAK: 1 -> 2 ----- 的例子。",
])
def test_page_marker_filter_preserves_book_lines_numbers_and_partial_markers(text):
    assert not is_ocr_page_break_line(text)
    assert strip_ocr_page_break_markers(text) == text


@pytest.mark.parametrize("split_on_empty_line", [False, True])
@pytest.mark.parametrize("sentence_split_enabled", [False, True])
def test_alignment_reference_blocks_and_scores_exclude_page_metadata(tmp_path, split_on_empty_line, sentence_split_enabled):
    write_minimal_settings(tmp_path,
        segmentation_overrides={"split_on_empty_line": split_on_empty_line, "min_chars_per_block": 1},
        reference_overrides={"sentence_split_enabled": sentence_split_enabled},
    )
    settings = load_settings(project_root=tmp_path)
    marker = format_ocr_page_break(1)
    marked = f"1872 年版序言。\n\n{marker}\n\n-----\n\n① 原书注释。"
    original = tmp_path / "reference.txt"
    original.write_text(marked, encoding="utf-8")
    clean = marked.replace(marker + "\n", "")
    blocks = build_reference_blocks(marked, settings)
    expected_blocks = build_reference_blocks(clean, settings)
    assert blocks == expected_blocks
    assert all("OCR_PAGE_BREAK" not in block.reference_text for block in blocks)
    assert [block.ref_block_id for block in blocks] == list(range(1, len(blocks) + 1))
    assert any("1872" in block.reference_text for block in blocks)
    assert any("-----" in block.reference_text for block in blocks)
    asr = [AsrBlock(1, [1], 0.0, 1.0, "1872 年版序言")]
    assert align_blocks(asr, blocks, settings) == align_blocks(asr, expected_blocks, settings)
    assert original.read_text(encoding="utf-8") == marked


def test_refinement_safe_replacement_does_not_match_or_lock_page_metadata(tmp_path):
    write_minimal_settings(tmp_path)
    settings = load_settings(project_root=tmp_path)
    first = "天地玄黄，宇宙洪荒。"
    second = "日月盈昃，辰宿列张。"
    source = "天地玄黄 宇宙洪荒。日月盈昃 辰宿列张。"
    marked = f"{first}\n\n{format_ocr_page_break(1)}\n\n{second}"
    actual = build_pre_replaced_document(asr_full_text=source, reference_full_text=marked, loaded_settings=settings)
    expected = build_pre_replaced_document(asr_full_text=source, reference_full_text=first + second, loaded_settings=settings)
    assert actual == expected
    assert actual[0].segment_type == "locked_quote"
    assert all("OCR_PAGE_BREAK" not in segment.text for segment in actual)


@pytest.mark.parametrize("backend", ["codex_api", "codex_cli", "agy"])
def test_stage_six_default_prompts_explain_marker_and_keep_fulltext_attachment(tmp_path, backend):
    write_minimal_settings(tmp_path)
    prompt_root = Path(__file__).resolve().parents[1] / "config/prompts"
    for name in ["classify_and_correct.md", "final_cleanup.md"]:
        (tmp_path / "config/prompts" / name).write_text((prompt_root / name).read_text(encoding="utf-8"), encoding="utf-8")
    settings = load_settings(project_root=tmp_path)
    asr = tmp_path / "data/intermediate/asr/book.txt"
    reference = tmp_path / "data/intermediate/extracted_text/book.txt"
    asr.parent.mkdir(parents=True)
    reference.parent.mkdir(parents=True)
    asr.write_text("这是讲话内容。", encoding="utf-8")
    marked = f"跨页句子的前半\n\n{format_ocr_page_break(1)}\n\n后半句。"
    reference.write_text(marked, encoding="utf-8")
    paths = resolve_refinement_input_paths(settings, asr)
    prompt = build_single_pass_refine_prompt(load_markdown_assemble_prompt(settings), paths,
        backend=backend, pre_replaced_segments=[PreReplacementSegment("unlocked_text", "这是讲话内容。", "这是讲话内容。", "", 0, 0)],
        reference_full_text=marked,
    )
    assert "物理换页" in prompt
    assert "硬换行" in prompt
    assert "不得保留换页标记" in prompt
    assert "脚注" in prompt and "尾注" in prompt
    assert marked in prompt
    minimal = build_minimal_edit_prompt(load_refinement_prompt(settings), paths,
        RefinementBlock(0, "这是讲话内容。", "", ""), reference_full_text=marked)
    assert "不代表自然段结束" in minimal
    assert "不得复制工程换页标记" in minimal
    assert marked in minimal


@pytest.mark.parametrize("pages", [["", "正文。", ""], ["", ""]])
def test_cached_page_reassembly_marks_blank_boundaries_without_changing_checkpoints(tmp_path, monkeypatch, pages):
    write_minimal_settings(tmp_path)
    settings = load_settings(project_root=tmp_path)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"PDF")
    checkpoints = tmp_path / "pages"
    for number, text in enumerate(pages, 1):
        write_codex_ocr_page_checkpoint(checkpoints, number, text)
    before = {path.name: path.read_bytes() for path in checkpoints.glob("*.txt")}
    monkeypatch.setattr("src.reference_utils.get_pdf_page_count", lambda _: len(pages))

    def reject_remote(*_args):
        pytest.fail("全部页已缓存，不应请求模型或重新渲染")

    monkeypatch.setattr("src.reference_utils.CodexLBClient.responses_stream_text", reject_remote)
    monkeypatch.setattr("src.reference_utils.render_pdf_page_as_png_data_url", reject_remote)
    output = tmp_path / "output/book.txt"
    first, _ = run_codex_api_pdf_ocr(source, settings, sidecar_path=output, checkpoint_dir=checkpoints)
    second, _ = run_codex_api_pdf_ocr(source, settings, sidecar_path=output, checkpoint_dir=checkpoints)
    assert first == second
    assert first.count("OCR_PAGE_BREAK") == len(pages) - 1
    for page in range(1, len(pages)):
        assert first.count(format_ocr_page_break(page)) == 1
    assert {path.name: path.read_bytes() for path in checkpoints.glob("*.txt")} == before


def test_generic_txt_integration_does_not_invent_pdf_pages(tmp_path):
    parts = tmp_path / "chapters"
    parts.mkdir()
    (parts / "chapter-1.txt").write_text("第一章。", encoding="utf-8")
    (parts / "chapter-2.txt").write_text("第二章。", encoding="utf-8")
    output = tmp_path / "book.txt"
    integrate_ocr_texts(parts, output)
    assert output.read_text(encoding="utf-8") == "第一章。\n第二章。"


def test_backend_document_score_and_selection_ignore_reference_page_markers():
    plain = "第一段正文。\n\n第二段正文。"
    marked = f"第一段正文。\n\n{format_ocr_page_break(1)}\n\n第二段正文。"
    correct = BackendDocumentRefinementResult("codex_api", "test", plain, "test", "", [], [])
    polluted = BackendDocumentRefinementResult("agy", "test", marked, "test", "", [], [])
    for result in [correct, polluted]:
        assert calculate_document_score(asr_full_text=plain, reference_full_text=marked, result=result) == calculate_document_score(
            asr_full_text=plain, reference_full_text=plain, result=result)
    assert compare_backend_documents(asr_full_text=plain, reference_full_text=marked, candidates=[polluted, correct]) == compare_backend_documents(
        asr_full_text=plain, reference_full_text=plain, candidates=[polluted, correct])
    selected, _ = compare_backend_documents(asr_full_text=plain, reference_full_text=marked, candidates=[polluted, correct])
    assert selected is correct
    assert calculate_document_score(asr_full_text=plain, reference_full_text=format_ocr_page_break(1), result=correct) == calculate_document_score(
        asr_full_text=plain, reference_full_text="", result=correct)
