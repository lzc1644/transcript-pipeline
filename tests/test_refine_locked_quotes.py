from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config_loader import load_settings
from src.refine_utils import (
    PreReplacementSegment,
    RefinementOutputValidationError,
    locked_quotes_preserved,
    markdown_to_plain_text,
    refine_batch,
)
from src.request_trace import RequestTrace, describe_refine_attempt
from tests.helpers import write_minimal_settings


TITLE = "第三节 北方各族人民的文化和科学"
BODY = "石窟艺术就是劳动人民雕塑艺术的独特创造。"


def locked_segment() -> PreReplacementSegment:
    return PreReplacementSegment(
        segment_type="locked_quote",
        text=f"{TITLE}\n\n{BODY}",
        source_text=f"{TITLE}\n\n{BODY}",
        reference_text=f"{TITLE}\n\n{BODY}",
        start_sentence_index=0,
        end_sentence_index=1,
    )


@pytest.mark.parametrize("level", range(1, 7))
def test_locked_quote_heading_format_is_not_a_content_change(level: int) -> None:
    markdown = f"{'#' * level} {TITLE}\n\n> {BODY}"
    assert locked_quotes_preserved(markdown, [locked_segment()])
    # 默认转换保持旧行为，不改变评分或其他保真校验的输入。
    assert TITLE not in markdown_to_plain_text(markdown)
    assert TITLE in markdown_to_plain_text(markdown, preserve_headings=True)


@pytest.mark.parametrize("markdown", [
    f"{TITLE}\n\n{BODY}",
    f"> {TITLE}\n>\n> {BODY}",
    f"## {TITLE}\n\n> 石窟艺术，就是劳动人民雕塑艺术的独特创造！",
])
def test_locked_quote_allows_only_format_and_punctuation_changes(markdown: str) -> None:
    assert locked_quotes_preserved(markdown, [locked_segment()])


@pytest.mark.parametrize("markdown", [
    f"## 北方各族人民的文化和科学\n\n> {BODY}",  # 删除标题中的实词
    f"## {TITLE}\n\n> 石窟艺术就是统治者雕塑艺术的独特创造。",  # 改写正文
    f"## {TITLE}\n\n> 补写内容。{BODY}",
    f"> {BODY}\n\n## {TITLE}",  # 调换顺序
    f"```\n# {TITLE}\n{BODY}\n```",  # 仅在代码块中保留不算正文
])
def test_locked_quote_still_rejects_actual_content_changes(markdown: str) -> None:
    assert not locked_quotes_preserved(markdown, [locked_segment()])


@pytest.mark.parametrize("mode", ["heading", "retry", "reject"])
def test_refine_heading_acceptance_and_real_locked_quote_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    write_minimal_settings(tmp_path)
    for directory in ("asr", "extracted_text"):
        path = tmp_path / "data/intermediate" / directory / "source.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{TITLE}\n\n{BODY}", encoding="utf-8")
    settings = load_settings(project_root=tmp_path)
    prompts: list[str] = []
    valid = f"# {TITLE}\n\n> {BODY}"

    def fake_payload(prompt: str, _settings, **_kwargs):
        prompts.append(prompt)
        markdown = valid if mode == "heading" or (mode == "retry" and len(prompts) == 2) else f"> {BODY}"
        return {"final_markdown": markdown, "section_map": [], "refinement_notes": [], "needs_review_sections": []}

    monkeypatch.setattr("src.refine_utils.run_codex_api_payload", fake_payload)
    if mode == "reject":
        with pytest.raises(RefinementOutputValidationError, match="locked_quote_changed"):
            refine_batch(settings)
    else:
        summary = refine_batch(settings)
        assert summary.success == 1
        result = json.loads((settings.path_for("refined_dir") / "source.json").read_text())
        assert result["final_markdown"] == valid
        assert result["model_results"]["codex_api"]["validation_retry_count"] == (0 if mode == "heading" else 1)

    diagnostics = json.loads((settings.path_for("logs_dir") / "refine/diagnostics.json").read_text())
    attempts = diagnostics["attempts"]
    if mode == "heading":
        assert len(prompts) == 1
        assert attempts[0]["status"] == "accepted"
        assert attempts[0]["validation_reasons"] == []
    else:
        assert len(prompts) == 2
        assert "锁定原文校验失败" in prompts[1]
        assert "locked_quote_changed" in attempts[0]["validation_reasons"]
        assert attempts[0]["failure_category"] == "locked_quote_validation"
        assert "不是后端调用失败" in attempts[0]["failure_summary"]
        evidence_path = settings.path_for("logs_dir") / attempts[0]["diagnostic_directory"] / attempts[0]["failure_evidence_file"]
        assert json.loads(evidence_path.read_text())["reason"] == "locked_quote_changed"
        assert "candidate_file" not in attempts[0]  # 不把程序回退稿当模型候选稿
        if mode == "reject":
            assert [attempt["status"] for attempt in attempts] == ["rejected", "rejected"]
            assert not (settings.path_for("refined_dir") / "source.json").exists()
        else:
            assert attempts[1]["status"] == "accepted"


def test_locked_quote_diagnostic_consumes_refinement_reason(tmp_path: Path) -> None:
    # 即使通用交付 reasons 不含锁定原因，也不能把已知内容失败泛化成后端错误。
    trace = RequestTrace(tmp_path)
    trace.write_json("locked-quote-validation.json", {"passed": False, "reason": "locked_quote_changed"})
    details = describe_refine_attempt(
        trace,
        reasons=["programmatic_markdown_fallback", "uses_canonical_source_placeholder_title"],
        markdown="# source\n\n回退稿",
        refinement_reason="locked_quote_changed",
    )
    assert details["failure_category"] == "locked_quote_validation"
    assert details["failure_evidence_file"] == "locked-quote-validation.json"
    assert "candidate_file" not in details
