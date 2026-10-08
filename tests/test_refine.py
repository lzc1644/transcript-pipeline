from __future__ import annotations

import json
import logging
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from src.codex_lb_client import CodexLBClientError, extract_event_stream_text
from src.config_loader import load_settings
from src.refine_utils import (
    BACKEND_CODEX_API,
    BACKEND_CODEX,
    BACKEND_FALLBACK,
    BACKEND_AGY,
    BackendDocumentRefinementResult,
    PreReplacementSegment,
    CLIBackendError,
    CLIBackendRetryableError,
    RefinementOutputValidationError,
    RefinementBlock,
    RefinementInputEmptyError,
    build_pre_replaced_document,
    build_fallback_document_result,
    build_markdown_assemble_prompt,
    build_minimal_edit_prompt,
    build_refinement_blocks,
    build_single_pass_refine_prompt,
    build_fulltext_refine_prompt,
    build_refinement_output_path,
    compare_backend_documents,
    load_refinement_prompt,
    parse_backend_document_result,
    refine_batch,
    resolve_requested_backends,
    run_codex_api,
    resolve_refinement_input_paths,
    run_codex_cli,
    run_agy,
    validate_final_markdown_contract,
    validate_minimal_edit_result,
)
from tests.helpers import write_minimal_settings


def write_refine_inputs(tmp_path: Path, *, basename: str = "demo") -> Path:
    asr_dir = tmp_path / "data/intermediate/asr"
    reference_dir = tmp_path / "data/intermediate/extracted_text"
    asr_dir.mkdir(parents=True, exist_ok=True)
    reference_dir.mkdir(parents=True, exist_ok=True)
    asr_path = asr_dir / f"{basename}.txt"
    asr_path.write_text(
        "九月零云至 崇尚敬江山\n这里 的意思 是 作者在说明情绪变化",
        encoding="utf-8",
    )
    (reference_dir / f"{basename}.txt").write_text(
        "久有凌云志，重上井冈山。\n\n三十八年过去，弹指一挥间。",
        encoding="utf-8",
    )
    return asr_path


def test_refine_batch_raises_when_asr_dir_empty(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    (tmp_path / "data/intermediate/asr").mkdir(parents=True, exist_ok=True)
    loaded_settings = load_settings(project_root=tmp_path)

    with pytest.raises(RefinementInputEmptyError):
        refine_batch(loaded_settings)


def test_resolve_refinement_input_paths_uses_same_basename(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    asr_path = write_refine_inputs(tmp_path, basename="same-name")
    loaded_settings = load_settings(project_root=tmp_path)

    paths = resolve_refinement_input_paths(loaded_settings, asr_path)

    assert paths.basename == "same-name"
    assert paths.asr_text_path.name == "same-name.txt"
    assert paths.reference_text_path.name == "same-name.txt"


def test_load_refinement_prompt_reads_prompt_file(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    prompt = load_refinement_prompt(loaded_settings)

    assert prompt == "# test prompt"


def test_build_fulltext_refine_prompt_contains_fulltext_context(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    prompt_text = load_refinement_prompt(loaded_settings)
    input_paths = resolve_refinement_input_paths(loaded_settings, asr_path)

    prompt = build_fulltext_refine_prompt(
        prompt_text,
        input_paths,
        asr_full_text="整篇 ASR 文本",
        reference_full_text="整篇参考原文",
    )

    assert "# test prompt" in prompt
    assert "直接按要求输出最终 Markdown 的 JSON 结果。" in prompt
    assert "录音转写文本：" in prompt
    assert "参考原文：" in prompt
    assert "当前文件: demo.txt" in prompt
    assert "分类摘要" not in prompt
    assert "高置信原文提示" not in prompt
    assert "重点复核块提示" not in prompt


def test_resolve_requested_backends_supports_single_and_dual_mode() -> None:
    assert resolve_requested_backends(None, [BACKEND_CODEX_API]) == [BACKEND_CODEX_API]
    assert resolve_requested_backends("codex_api", [BACKEND_AGY]) == [BACKEND_CODEX_API]
    assert resolve_requested_backends("codex_cli", [BACKEND_AGY]) == [BACKEND_CODEX]
    assert resolve_requested_backends("agy", [BACKEND_CODEX]) == [BACKEND_AGY]
    assert resolve_requested_backends("both", [BACKEND_CODEX]) == [BACKEND_CODEX, BACKEND_AGY]


def test_build_refinement_blocks_includes_adjacent_context() -> None:
    blocks = build_refinement_blocks(
        "第一段。\n\n第二段。\n\n第三段。",
        chunk_paragraphs=1,
        anchor_paragraphs=1,
    )

    assert len(blocks) == 3
    assert blocks[1] == RefinementBlock(
        index=1,
        current_text="第二段。",
        previous_anchor="第一段。",
        next_anchor="第三段。",
    )


def test_build_refinement_blocks_scales_down_long_line_based_asr_to_about_twenty_calls() -> None:
    line_based_text = "\n".join(f"第{i:04d}行转写文本" for i in range(200))

    blocks = build_refinement_blocks(
        line_based_text,
        chunk_paragraphs=2,
        anchor_paragraphs=1,
    )

    assert len(blocks) == 20
    assert blocks[0].current_text.count("\n\n") == 9
    assert blocks[-1].current_text.count("\n\n") == 9


def test_build_minimal_edit_prompt_contains_context_and_hard_guards(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    prompt_text = load_refinement_prompt(loaded_settings)
    input_paths = resolve_refinement_input_paths(loaded_settings, asr_path)

    prompt = build_minimal_edit_prompt(
        prompt_text,
        input_paths,
        RefinementBlock(
            index=0,
            current_text="久有零云志 重上敬岗山",
            previous_anchor="前文锚点",
            next_anchor="后文锚点",
        ),
        reference_full_text="久有凌云志，重上井冈山。",
    )

    assert "你现在只允许编辑“当前块”文本" in prompt
    assert "当前块正文" in prompt
    assert "前文锚点" in prompt
    assert "后文锚点" in prompt
    assert "绝对禁止" in prompt
    assert "不得总结压缩" in prompt
    assert "删除噪音必须记录" in prompt
    assert "参考原文" in prompt


def test_validate_minimal_edit_result_rejects_summary_style_compression() -> None:
    report = validate_minimal_edit_result(
        source_text="这个地方他其实不是单纯抒情，而是说革命在低潮以后重新起来，所以情绪往上走。",
        edited_text="这里主要是表达革命重新高涨。",
        deletion_candidates=[],
    )

    assert report["passed"] is False
    assert "summary_phrase_detected" in report["reasons"]
    assert "length_ratio_too_low" in report["reasons"]


def test_build_markdown_assemble_prompt_restricts_to_structure_only(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    input_paths = resolve_refinement_input_paths(loaded_settings, asr_path)

    prompt = build_markdown_assemble_prompt(
        "# test final cleanup",
        input_paths,
        edited_plain_text="久有凌云志，重上井冈山。\n\n这里的意思是作者在说明情绪变化。",
        reference_full_text="久有凌云志，重上井冈山。",
    )

    assert "只负责 Markdown 结构整理" in prompt
    assert "不得改写 edited_plain_text 的措辞" in prompt
    assert "edited_plain_text" in prompt
    assert "参考原文" in prompt


def test_build_pre_replaced_document_locks_only_high_confidence_reference_runs(tmp_path: Path) -> None:
    write_minimal_settings(
        tmp_path,
        llm_overrides={
            "safe_replace_min_score": 80,
            "safe_replace_min_margin": 5,
            "safe_replace_length_ratio_min": 0.8,
            "safe_replace_length_ratio_max": 1.2,
            "safe_replace_max_extra_content_ratio": 0.12,
            "safe_replace_min_run_length": 2,
        },
    )
    loaded_settings = load_settings(project_root=tmp_path)

    segments = build_pre_replaced_document(
        asr_full_text="天地玄黄 宇宙洪荒。\n\n日月盈昃 辰宿列张。\n\n这里是在解释宇宙观。",
        reference_full_text="天地玄黄，宇宙洪荒。日月盈昃，辰宿列张。",
        loaded_settings=loaded_settings,
    )

    assert segments == [
        PreReplacementSegment(
            segment_type="locked_quote",
            text="天地玄黄，宇宙洪荒。\n\n日月盈昃，辰宿列张。",
            source_text="天地玄黄 宇宙洪荒。\n\n日月盈昃 辰宿列张。",
            reference_text="天地玄黄，宇宙洪荒。日月盈昃，辰宿列张。",
            start_sentence_index=0,
            end_sentence_index=1,
        ),
        PreReplacementSegment(
            segment_type="unlocked_text",
            text="这里是在解释宇宙观。",
            source_text="这里是在解释宇宙观。",
            reference_text="",
            start_sentence_index=2,
            end_sentence_index=2,
        ),
    ]


def test_build_pre_replaced_document_does_not_lock_sentence_followed_by_explanation(tmp_path: Path) -> None:
    write_minimal_settings(
        tmp_path,
        llm_overrides={
            "safe_replace_min_score": 80,
            "safe_replace_min_margin": 5,
            "safe_replace_length_ratio_min": 0.8,
            "safe_replace_length_ratio_max": 1.2,
            "safe_replace_max_extra_content_ratio": 0.12,
            "safe_replace_min_run_length": 2,
        },
    )
    loaded_settings = load_settings(project_root=tmp_path)

    segments = build_pre_replaced_document(
        asr_full_text="天地玄黄 宇宙洪荒。\n\n这里是在解释为什么先讲宇宙。",
        reference_full_text="天地玄黄，宇宙洪荒。日月盈昃，辰宿列张。",
        loaded_settings=loaded_settings,
    )

    assert segments == [
        PreReplacementSegment(
            segment_type="unlocked_text",
            text="天地玄黄 宇宙洪荒。\n\n这里是在解释为什么先讲宇宙。",
            source_text="天地玄黄 宇宙洪荒。\n\n这里是在解释为什么先讲宇宙。",
            reference_text="",
            start_sentence_index=0,
            end_sentence_index=1,
        )
    ]


def test_build_pre_replaced_document_splits_long_unlocked_text_by_existing_units(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"block_batch_size": 1})
    loaded_settings = load_settings(project_root=tmp_path)

    segments = build_pre_replaced_document(
        asr_full_text="第一行讲解。\n第二行补充。\n第三行继续说明。",
        reference_full_text="完全不同的参考原文。",
        loaded_settings=loaded_settings,
    )

    assert [segment.segment_type for segment in segments] == ["unlocked_text", "unlocked_text", "unlocked_text"]
    assert [segment.text for segment in segments] == ["第一行讲解。", "第二行补充。", "第三行继续说明。"]
    assert [(segment.start_sentence_index, segment.end_sentence_index) for segment in segments] == [(0, 0), (1, 1), (2, 2)]


def test_build_single_pass_refine_prompt_includes_reference_and_locking_rules(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    input_paths = resolve_refinement_input_paths(loaded_settings, asr_path)

    prompt = build_single_pass_refine_prompt(
        "# test final cleanup",
        input_paths,
        backend=BACKEND_CODEX,
        pre_replaced_segments=[
            PreReplacementSegment(
                segment_type="locked_quote",
                text="天地玄黄，宇宙洪荒。",
                source_text="天地玄黄 宇宙洪荒。",
                reference_text="天地玄黄，宇宙洪荒。",
                start_sentence_index=0,
                end_sentence_index=0,
            ),
            PreReplacementSegment(
                segment_type="unlocked_text",
                text="这里是在解释宇宙观。",
                source_text="这里是在解释宇宙观。",
                reference_text="",
                start_sentence_index=1,
                end_sentence_index=1,
            ),
        ],
        reference_full_text="天地玄黄，宇宙洪荒。",
    )

    assert "预替换全文" in prompt
    assert "整篇参考原文" in prompt
    assert prompt.index("预替换全文：") < prompt.index("整篇参考原文：")
    assert "[SEGMENT 01][locked_quote]" in prompt
    assert "[SEGMENT 02][unlocked_text]" in prompt
    assert "预替换全文是主输入" in prompt
    assert "deletion_candidates" in prompt
    assert "不得改写 locked_quote 的实词内容" in prompt
    assert "unlocked_text 中的讲解、串场、例子、重复强调和讨论内容必须保留" in prompt


def test_build_single_pass_refine_prompt_without_reference_uses_conversation_rules(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path, reference_overrides={"enabled": False})
    asr_dir = tmp_path / "data/intermediate/asr"
    asr_dir.mkdir(parents=True, exist_ok=True)
    asr_path = asr_dir / "conversation-demo.txt"
    asr_path.write_text("今天我们先聊这个问题。对，我补充一点。", encoding="utf-8")
    loaded_settings = load_settings(project_root=tmp_path)
    input_paths = resolve_refinement_input_paths(loaded_settings, asr_path)

    prompt = build_single_pass_refine_prompt(
        "# test conversation cleanup",
        input_paths,
        backend=BACKEND_CODEX,
        pre_replaced_segments=[
            PreReplacementSegment(
                segment_type="unlocked_text",
                text="今天我们先聊这个问题。对，我补充一点。",
                source_text="今天我们先聊这个问题。对，我补充一点。",
                reference_text="",
                start_sentence_index=0,
                end_sentence_index=1,
            )
        ],
        reference_full_text="",
    )

    assert input_paths.reference_text_path is None
    assert "对谈录屏保真转录整理" in prompt
    assert "录音转写全文是唯一主输入" in prompt
    assert "整篇参考原文" not in prompt
    assert "locked_quote" not in prompt
    assert "引用原文" not in prompt
    assert "[SEGMENT 01][unlocked_text]" not in prompt
    assert "[SEGMENT 01]" in prompt


def test_refine_batch_uses_single_codex_call_with_reference_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(
        tmp_path,
        llm_overrides={
            "block_batch_size": 1,
            "safe_replace_min_score": 80,
            "safe_replace_min_margin": 5,
            "safe_replace_length_ratio_min": 0.8,
            "safe_replace_length_ratio_max": 1.2,
            "safe_replace_max_extra_content_ratio": 0.12,
            "safe_replace_min_run_length": 2,
        },
    )
    asr_dir = tmp_path / "data/intermediate/asr"
    reference_dir = tmp_path / "data/intermediate/extracted_text"
    asr_dir.mkdir(parents=True, exist_ok=True)
    reference_dir.mkdir(parents=True, exist_ok=True)
    (asr_dir / "single-pass-demo.txt").write_text(
        "天地玄黄 宇宙洪荒。\n\n日月盈昃 辰宿列张。\n\n这里是在解释宇宙观。",
        encoding="utf-8",
    )
    (reference_dir / "single-pass-demo.txt").write_text(
        "天地玄黄，宇宙洪荒。日月盈昃，辰宿列张。",
        encoding="utf-8",
    )
    loaded_settings = load_settings(project_root=tmp_path)

    prompts: list[str] = []

    def fake_run_codex_payload(prompt: str, _loaded_settings, **_kwargs) -> dict[str, object]:
        prompts.append(prompt)
        if "当前块正文" in prompt:
            current_text = prompt.split("当前块正文：\n", 1)[1].split("\n\n后文锚点：", 1)[0].strip()
            return {
                "edited_text": current_text,
                "deletion_candidates": [],
                "edit_notes": [],
                "needs_review_sections": [],
            }

        return {
            "final_markdown": "# single-pass-demo\n\n> 天地玄黄，宇宙洪荒。\n>\n> 日月盈昃，辰宿列张。\n\n这里是在解释宇宙观。",
            "section_map": [],
            "refinement_notes": [],
            "needs_review_sections": [],
            "refinement_strategy": "single_pass_with_safe_replace",
            "refinement_reason": "single_pass_codex",
            "model_name": "gpt-5.4",
        }

    monkeypatch.setattr("src.refine_utils.run_codex_api_payload", fake_run_codex_payload)

    refine_batch(loaded_settings)

    assert len(prompts) == 1
    assert "整篇参考原文" in prompts[0]
    assert "[SEGMENT 01][locked_quote]" in prompts[0]
    assert "[SEGMENT 02][unlocked_text]" in prompts[0]


def test_refine_batch_without_reference_writes_null_source_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, reference_overrides={"enabled": False})
    asr_dir = tmp_path / "data/intermediate/asr"
    asr_dir.mkdir(parents=True, exist_ok=True)
    (asr_dir / "conversation-demo.txt").write_text(
        "今天我们先聊这个问题。\n\n对，我补充一点。",
        encoding="utf-8",
    )
    loaded_settings = load_settings(project_root=tmp_path)

    prompts: list[str] = []

    def fake_run_codex_payload(prompt: str, _loaded_settings, **_kwargs) -> dict[str, object]:
        prompts.append(prompt)
        return {
            "final_markdown": "# conversation-demo\n\n今天我们先聊这个问题。\n\n对，我补充一点。",
            "section_map": [],
            "refinement_notes": [],
            "needs_review_sections": [],
            "deletion_candidates": [],
            "refinement_strategy": "single_pass_conversation_cleanup",
            "refinement_reason": "test",
            "model_name": "gpt-test",
        }

    monkeypatch.setattr("src.refine_utils.run_codex_api_payload", fake_run_codex_payload)

    summary = refine_batch(loaded_settings)

    assert summary.success == 1
    assert len(prompts) == 1
    assert "整篇参考原文" not in prompts[0]
    assert "locked_quote" not in prompts[0]
    output_payload = json.loads(
        (tmp_path / "data/intermediate/refined/conversation-demo.json").read_text(encoding="utf-8")
    )
    assert output_payload["source_reference_file"] is None
    assert output_payload["final_markdown"].startswith("# conversation-demo")


def test_parse_backend_document_result_requires_fulltext() -> None:
    payload = {
        "final_markdown": "# 标题\n\n> 完整精修文本",
        "refinement_strategy": "final_markdown_cleanup",
        "refinement_reason": "test",
        "needs_review_sections": [{"excerpt": "片段", "reason": "test"}],
        "refinement_notes": ["note"],
    }

    result = parse_backend_document_result(BACKEND_AGY, payload)

    assert result.backend == BACKEND_AGY
    assert result.model_name == ""
    assert result.final_markdown == "# 标题\n\n> 完整精修文本"
    assert result.needs_review_sections[0]["excerpt"] == "片段"
    assert result.refinement_notes == ["note"]


def test_parse_backend_document_result_rejects_empty_final_markdown() -> None:
    with pytest.raises(CLIBackendError, match="缺少 final_markdown"):
        parse_backend_document_result(BACKEND_CODEX_API, {"final_markdown": ""})


def test_extract_event_stream_text_raises_on_failed_event() -> None:
    failed_event = json.dumps(
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {"code": "stream_idle_timeout", "message": "Upstream stream idle timeout"},
            },
        },
        ensure_ascii=False,
    )
    stream_text = f"event: response.failed\ndata: {failed_event}\n\n"

    with pytest.raises(CodexLBClientError, match="stream_idle_timeout"):
        extract_event_stream_text(stream_text)


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("response.output_text.done", {"text": "完成事件中的正文。"}),
        (
            "response.content_part.done",
            {"part": {"type": "output_text", "text": "内容分段完成事件中的正文。"}},
        ),
        (
            "response.output_item.done",
            {"item": {"content": [{"type": "output_text", "text": "输出项完成事件中的正文。"}]}},
        ),
    ],
)
def test_extract_event_stream_text_supports_official_done_events(
    event_type: str,
    payload: dict[str, object],
) -> None:
    event_payload = json.dumps({"type": event_type, **payload}, ensure_ascii=False)

    assert extract_event_stream_text(f"event: {event_type}\ndata: {event_payload}\n\n").endswith("正文。")


def test_extract_event_stream_text_reads_nested_output_from_completed_response() -> None:
    completed_event = json.dumps(
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "嵌套 output 中的完整正文。"}],
                    }
                ],
            },
        },
        ensure_ascii=False,
    )

    assert extract_event_stream_text(f"event: response.completed\ndata: {completed_event}\n\n") == "嵌套 output 中的完整正文。"


def test_extract_event_stream_text_uses_complete_done_text_when_deltas_miss_prefix() -> None:
    full_text = '{"final_markdown":"完整正文。"}'
    delta = json.dumps({"type": "response.output_text.delta", "delta": full_text[18:]}, ensure_ascii=False)
    done = json.dumps({"type": "response.output_text.done", "text": full_text}, ensure_ascii=False)
    stream_text = (
        f"event: response.output_text.delta\ndata: {delta}\n\n"
        f"event: response.output_text.done\ndata: {done}\n\n"
    )

    assert extract_event_stream_text(stream_text) == full_text


def test_extract_event_stream_text_does_not_duplicate_done_text() -> None:
    delta = json.dumps({"type": "response.output_text.delta", "delta": "完整正文。"}, ensure_ascii=False)
    done = json.dumps({"type": "response.output_text.done", "text": "完整正文。"}, ensure_ascii=False)
    stream_text = (
        f"event: response.output_text.delta\ndata: {delta}\n\n"
        f"event: response.output_text.done\ndata: {done}\n\n"
    )

    assert extract_event_stream_text(stream_text) == "完整正文。"


def test_extract_event_stream_text_accepts_explicit_empty_output_text_for_blank_page() -> None:
    done = json.dumps({"type": "response.output_text.done", "text": ""}, ensure_ascii=False)
    completed = json.dumps(
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": ""}],
                    }
                ],
            },
        },
        ensure_ascii=False,
    )
    stream_text = (
        f"event: response.output_text.done\ndata: {done}\n\n"
        f"event: response.completed\ndata: {completed}\n\n"
    )

    assert extract_event_stream_text(stream_text) == ""


def test_extract_event_stream_text_still_rejects_completed_response_without_output_text() -> None:
    completed = json.dumps(
        {
            "type": "response.completed",
            "response": {"status": "completed", "output": [{"type": "reasoning", "content": []}]},
        },
        ensure_ascii=False,
    )

    with pytest.raises(CodexLBClientError, match="未找到 output_text"):
        extract_event_stream_text(f"event: response.completed\ndata: {completed}\n\n")


def test_run_agy_uses_configured_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"gemini_model": "Gemini 3.1 Pro (High)", "timeout_seconds": 1800})
    loaded_settings = load_settings(project_root=tmp_path)
    seen: dict[str, object] = {}

    def fake_run_subprocess(command: list[str], *, prompt: str, cwd: Path, timeout_seconds: int) -> str:
        seen["command"] = command
        seen["prompt"] = prompt
        seen["cwd"] = cwd
        seen["timeout_seconds"] = timeout_seconds
        return json.dumps(
            {
                "final_markdown": "# 标题\n\n完整精修文本",
                "refinement_strategy": "final_markdown_cleanup",
                "refinement_reason": "ok",
                "needs_review_sections": [],
                "refinement_notes": [],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("src.refine_utils.run_subprocess", fake_run_subprocess)

    result = run_agy("prompt", loaded_settings)

    assert "完整精修文本" in result.final_markdown
    assert result.model_name == "Gemini 3.1 Pro (High)"
    assert seen["command"] == [
        "agy",
        "--model",
        "Gemini 3.1 Pro (High)",
        "--print",
        "prompt",
        "--print-timeout",
        "1800s",
    ]
    assert seen["prompt"] == ""
    assert seen["cwd"] == tmp_path
    assert seen["timeout_seconds"] == 1800


def test_run_codex_cli_uses_configured_model_and_reasoning_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(
        tmp_path,
        llm_overrides={"model": "gpt-5.4", "reasoning_effort": "medium", "timeout_seconds": 1800},
    )
    loaded_settings = load_settings(project_root=tmp_path)
    seen: dict[str, object] = {}

    def fake_run_subprocess(command: list[str], *, prompt: str, cwd: Path, timeout_seconds: int) -> str:
        seen["command"] = command
        seen["prompt"] = prompt
        seen["cwd"] = cwd
        seen["timeout_seconds"] = timeout_seconds
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "final_markdown": "# 标题\n\n完整精修文本",
                    "refinement_strategy": "final_markdown_cleanup",
                    "refinement_reason": "ok",
                    "needs_review_sections": [],
                    "refinement_notes": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return ""

    monkeypatch.setattr("src.refine_utils.run_subprocess", fake_run_subprocess)

    result = run_codex_cli("prompt", loaded_settings)
    command = seen["command"]
    assert isinstance(command, list)
    output_index = command.index("-o") + 1

    assert "完整精修文本" in result.final_markdown
    assert result.model_name == "gpt-5.4"
    assert command[:10] == [
        "codex",
        "exec",
        "-C",
        str(tmp_path),
        "-s",
        "read-only",
        "-m",
        "gpt-5.4",
        "-c",
        'model_reasoning_effort="medium"',
    ]
    assert command[output_index - 1] == "-o"
    assert command[-1] == "-"
    assert seen["prompt"] == "prompt"
    assert seen["cwd"] == tmp_path
    assert seen["timeout_seconds"] == 1800


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:2455", "http://127.0.0.1:2455/v1/"])
def test_run_codex_api_uses_responses_api_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    write_minimal_settings(
        tmp_path,
        llm_overrides={
            "model": "gpt-5.6-sol",
            "reasoning_effort": "max",
            "temperature": 0.9,
            "max_output_tokens": 99,
            "timeout_seconds": 1800,
        },
    )
    loaded_settings = load_settings(project_root=tmp_path)
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-key")
    monkeypatch.setenv("CODEX_LB_BASE_URL", base_url)
    seen: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb

        def read(self) -> bytes:
            payload = json.dumps(
                {
                    "type": "response.output_text.delta",
                    "delta": json.dumps(
                        {
                            "final_markdown": "# 标题\n\n完整精修文本",
                            "refinement_strategy": "final_markdown_cleanup",
                            "refinement_reason": "ok",
                            "needs_review_sections": [],
                            "refinement_notes": [],
                        },
                        ensure_ascii=False,
                    ),
                },
                ensure_ascii=False,
            )
            return f"event: response.output_text.delta\ndata: {payload}\n\n".encode("utf-8")

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["headers"] = dict(request.header_items())
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("src.codex_lb_client.urlopen", fake_urlopen)

    result = run_codex_api("prompt", loaded_settings)

    assert result.backend == BACKEND_CODEX_API
    assert result.model_name == "gpt-5.6-sol"
    assert "完整精修文本" in result.final_markdown
    assert seen["url"] == "http://127.0.0.1:2455/v1/responses"
    assert seen["method"] == "POST"
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["timeout"] == 1800
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt-5.6-sol"
    assert payload["instructions"]
    assert payload["input"] == "prompt"
    assert payload["reasoning"] == {"effort": "max"}
    assert "text" not in payload
    assert payload["stream"] is True
    assert payload["store"] is False
    assert "temperature" not in payload
    assert "max_output_tokens" not in payload


def test_run_codex_api_retries_with_curl_on_cloudflare_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"model": "gpt-5.4", "reasoning_effort": "high"})
    loaded_settings = load_settings(project_root=tmp_path)
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-key")
    seen: dict[str, object] = {}

    def fake_urlopen(_request, timeout=None):
        raise HTTPError(
            "https://api.redworker.org/v1/responses",
            403,
            "Forbidden",
            {},
            BytesIO(b'{"cloudflare_error":true,"error_name":"browser_signature_banned"}'),
        )

    class Completed:
        returncode = 0
        stderr = ""
        payload = json.dumps(
            {
                "type": "response.output_text.delta",
                "delta": json.dumps(
                    {"final_markdown": "# 测试\n\n正文", "section_map": [], "refinement_notes": [], "needs_review_sections": []},
                    ensure_ascii=False,
                ),
            },
            ensure_ascii=False,
        )
        stdout = (
            f"event: response.output_text.delta\ndata: {payload}\n\n"
            "\n200"
        )

    def fake_run(command, *, input, text, capture_output, check):
        seen["command"] = command
        seen["input"] = input
        return Completed()

    monkeypatch.setattr("src.codex_lb_client.urlopen", fake_urlopen)
    monkeypatch.setattr("src.codex_lb_client.shutil.which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    monkeypatch.setattr("src.codex_lb_client.subprocess.run", fake_run)

    result = run_codex_api("prompt", loaded_settings)

    assert result.backend == BACKEND_CODEX_API
    assert result.final_markdown == "# 测试\n\n正文"
    command = seen["command"]
    assert isinstance(command, list)
    assert command[0] == "curl"
    assert "test-key" not in command
    assert "Authorization: Bearer test-key" in str(seen["input"])


def test_run_agy_retries_with_fallback_model_on_capacity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(
        tmp_path,
        llm_overrides={
            "gemini_model": "Gemini 3.1 Pro (High)",
            "gemini_fallback_model": "Gemini 3.5 Flash (High)",
            "timeout_seconds": 1800,
        },
    )
    loaded_settings = load_settings(project_root=tmp_path)
    seen_commands: list[list[str]] = []

    def fake_run_subprocess(command: list[str], *, prompt: str, cwd: Path, timeout_seconds: int) -> str:
        seen_commands.append(command)
        if command[2] == "Gemini 3.1 Pro (High)":
            raise CLIBackendRetryableError(
                "CLI 命令执行失败: agy --model 'Gemini 3.1 Pro (High)' --print prompt | 429 MODEL_CAPACITY_EXHAUSTED"
            )
        return json.dumps(
            {
                "final_markdown": "# 标题\n\n备用模型结果",
                "refinement_strategy": "final_markdown_cleanup",
                "refinement_reason": "fallback_model",
                "needs_review_sections": [],
                "refinement_notes": [],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("src.refine_utils.run_subprocess", fake_run_subprocess)

    result = run_agy("prompt", loaded_settings)

    assert result.final_markdown == "# 标题\n\n备用模型结果"
    assert result.model_name == "Gemini 3.5 Flash (High)"
    assert seen_commands == [
        ["agy", "--model", "Gemini 3.1 Pro (High)", "--print", "prompt", "--print-timeout", "1800s"],
        ["agy", "--model", "Gemini 3.5 Flash (High)", "--print", "prompt", "--print-timeout", "1800s"],
    ]


def test_compare_backend_documents_prefers_reference_closer_result_for_quote_heavy() -> None:
    codex_result = BackendDocumentRefinementResult(
        backend=BACKEND_CODEX,
        model_name="codex_default",
        final_markdown="# 文稿\n\n> 久有凌云志，重上井冈山。\n\n> 风雷动，旌旗奋，是人寰。",
        refinement_strategy="final_markdown_reference_restore",
        refinement_reason="codex",
        needs_review_sections=[],
        refinement_notes=[],
    )
    gemini_result = BackendDocumentRefinementResult(
        backend=BACKEND_AGY,
        model_name="Gemini 3.1 Pro (High)",
        final_markdown="# 文稿\n\n九月零云至 崇尚敬江山。\n\n兵起奋是人还 三十八年过去。",
        refinement_strategy="final_markdown_cleanup",
        refinement_reason="gemini",
        needs_review_sections=[{"excerpt": "兵起奋是人还", "reason": "uncertain"}],
        refinement_notes=[],
    )

    selected, summary = compare_backend_documents(
        asr_full_text="九月零云至 崇尚敬江山\n兵起奋是人还 三十八年过去",
        reference_full_text="久有凌云志，重上井冈山。\n\n风雷动，旌旗奋，是人寰。",
        candidates=[codex_result, gemini_result],
    )

    assert selected.backend == BACKEND_CODEX
    assert "selected=codex_cli" in summary


def test_build_fallback_document_result_uses_full_asr_text() -> None:
    result = build_fallback_document_result("demo", "九月零云至 崇尚敬江山\n\n这里 的意思 是 作者在说明情绪变化")

    assert result.backend == BACKEND_FALLBACK
    assert result.model_name == BACKEND_FALLBACK
    assert "# demo" in result.final_markdown
    assert "九月零云至 崇尚敬江山" in result.final_markdown
    assert result.refinement_notes == ["all_cli_backends_failed_use_fallback"]
    assert len(result.needs_review_sections) == 2


def test_refine_batch_writes_expected_fulltext_output_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"block_batch_size": 1})
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    def fake_run_codex_payload(prompt: str, _loaded_settings, **_kwargs) -> dict[str, object]:
        return {
            "final_markdown": "# demo\n\n> 久有凌云志，重上井冈山。\n\n这里 的意思 是 作者在说明情绪变化。",
            "section_map": [{"section": "quote", "source_blocks": [0]}],
            "refinement_notes": ["codex_note"],
            "needs_review_sections": [],
            "refinement_strategy": "single_pass_with_safe_replace",
            "refinement_reason": "single_pass_codex",
            "model_name": "gpt-5.4",
        }

    monkeypatch.setattr("src.refine_utils.run_codex_api_payload", fake_run_codex_payload)

    summary = refine_batch(loaded_settings)
    output_path = build_refinement_output_path(asr_path, tmp_path / "data/intermediate/refined")
    result = json.loads(output_path.json_path.read_text(encoding="utf-8"))
    backend_path = output_path.json_path.with_name("demo.codex_api.json")
    backend_result = json.loads(backend_path.read_text(encoding="utf-8"))

    assert summary.success == 1
    assert result["prompt_mode"] == "single_pass_safe_replace"
    assert result["source_asr_file"] == "data/intermediate/asr/demo.txt"
    assert result["source_reference_file"] == "data/intermediate/extracted_text/demo.txt"
    assert result["refinement_backends"] == [BACKEND_CODEX_API]
    assert result["backend_status"]["codex_api"] == "returned_single_pass:model=gpt-5.4"
    assert result["selected_backend"] == BACKEND_CODEX_API
    assert "final_markdown" in result
    assert result["final_markdown"].startswith("# demo")
    assert result["edited_plain_text"].startswith("九月零云至 崇尚敬江山")
    assert "这里 的意思 是 作者在说明情绪变化" in result["edited_plain_text"]
    assert result["deletion_candidates"] == []
    assert result["fidelity_report"]["passed"] is True
    assert result["refinement_notes"] == ["codex_note"]
    assert "classification_summary" not in result
    assert backend_path.exists()
    assert backend_result["selected_backend"] == BACKEND_CODEX_API
    assert backend_result["backend_status"]["codex_api"] == "returned_single_pass:model=gpt-5.4"
    assert backend_result["final_markdown"].startswith("# demo")


def test_build_single_pass_refine_prompt_adds_backend_specific_review_warning(tmp_path: Path) -> None:
    write_minimal_settings(tmp_path)
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    prompt_text = load_refinement_prompt(loaded_settings)
    input_paths = resolve_refinement_input_paths(loaded_settings, asr_path)
    segments = [
        PreReplacementSegment(
            segment_type="locked_quote",
            text="久有凌云志，重上井冈山。",
            source_text="九月零云至 崇尚敬江山",
            reference_text="久有凌云志，重上井冈山。",
            start_sentence_index=0,
            end_sentence_index=0,
        ),
        PreReplacementSegment(
            segment_type="unlocked_text",
            text="这里 的意思 是 作者在说明情绪变化",
            source_text="这里 的意思 是 作者在说明情绪变化",
            reference_text="",
            start_sentence_index=1,
            end_sentence_index=1,
        ),
    ]

    codex_prompt = build_single_pass_refine_prompt(
        prompt_text,
        input_paths,
        backend=BACKEND_CODEX,
        pre_replaced_segments=segments,
        reference_full_text="久有凌云志，重上井冈山。",
    )
    gemini_prompt = build_single_pass_refine_prompt(
        prompt_text,
        input_paths,
        backend=BACKEND_AGY,
        pre_replaced_segments=segments,
        reference_full_text="久有凌云志，重上井冈山。",
    )

    assert "结果会交给 Gemini 和 Claude 审核" in codex_prompt
    assert "结果会交给 Codex 和 Claude 审核" in gemini_prompt


def test_refine_batch_writes_dual_backend_outputs_without_selected_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"backends": [BACKEND_CODEX, BACKEND_AGY]})
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    def fake_run_single_pass_backend_refinement(**kwargs) -> BackendDocumentRefinementResult:
        backend = kwargs["backend"]
        return BackendDocumentRefinementResult(
            backend=backend,
            model_name="gpt-5.4" if backend == BACKEND_CODEX else "Gemini 3.1 Pro (High)",
            final_markdown=f"# demo\n\n{backend} 结果",
            refinement_strategy="single_pass_safe_replace",
            refinement_reason=f"{backend}_done",
            needs_review_sections=[],
            refinement_notes=[f"{backend}_note"],
            edited_plain_text=f"{backend} plain text",
            fidelity_report={"passed": True},
            section_map=[{"backend": backend}],
        )

    monkeypatch.setattr(
        "src.refine_utils.run_single_pass_backend_refinement",
        fake_run_single_pass_backend_refinement,
    )

    summary = refine_batch(loaded_settings, requested_backends=[BACKEND_CODEX, BACKEND_AGY])
    output_path = build_refinement_output_path(asr_path, tmp_path / "data/intermediate/refined")
    result = json.loads(output_path.json_path.read_text(encoding="utf-8"))
    codex_path = output_path.json_path.with_name("demo.codex_cli.json")
    agy_path = output_path.json_path.with_name("demo.agy.json")

    assert summary.success == 1
    assert summary.items[0].selected_backends == [BACKEND_CODEX, BACKEND_AGY]
    assert result["refinement_backends"] == [BACKEND_CODEX, BACKEND_AGY]
    assert result["selected_backend"] is None
    assert result["comparison_summary"] == "manual_selection_required"
    assert result["final_markdown"] == ""
    assert result["model_results"][BACKEND_CODEX]["final_markdown"] == "# demo\n\ncodex_cli 结果"
    assert result["model_results"][BACKEND_AGY]["final_markdown"] == "# demo\n\nagy 结果"
    assert codex_path.exists()
    assert agy_path.exists()


def test_refine_batch_supports_single_agy_backend_via_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"backends": [BACKEND_CODEX]})
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    def fake_run_single_pass_backend_refinement(**kwargs) -> BackendDocumentRefinementResult:
        backend = kwargs["backend"]
        return BackendDocumentRefinementResult(
            backend=backend,
            model_name="Gemini 3.1 Pro (High)",
            final_markdown="# demo\n\nagy 单跑结果",
            refinement_strategy="single_pass_safe_replace",
            refinement_reason="agy_done",
            needs_review_sections=[],
            refinement_notes=["agy_note"],
            edited_plain_text="agy plain text",
            fidelity_report={"passed": True},
            section_map=[{"backend": backend}],
        )

    monkeypatch.setattr("src.refine_utils.run_single_pass_backend_refinement", fake_run_single_pass_backend_refinement)

    summary = refine_batch(loaded_settings, requested_backends=[BACKEND_AGY])
    output_path = build_refinement_output_path(asr_path, tmp_path / "data/intermediate/refined")
    result = json.loads(output_path.json_path.read_text(encoding="utf-8"))

    assert summary.success == 1
    assert summary.backends == [BACKEND_AGY]
    assert result["refinement_backends"] == [BACKEND_AGY]
    assert result["selected_backend"] == BACKEND_AGY
    assert result["final_markdown"] == "# demo\n\nagy 单跑结果"
    assert result["model_results"][BACKEND_AGY]["model_name"] == "Gemini 3.1 Pro (High)"


def test_validate_final_markdown_contract_accepts_content_without_h1_or_blank_line() -> None:
    result = BackendDocumentRefinementResult(
        backend=BACKEND_CODEX_API,
        model_name="gpt-5.6-terra",
        final_markdown="哲学是世界观的理论形态。",
        refinement_strategy="single_pass_safe_replace",
        refinement_reason="single_pass_codex_api",
        needs_review_sections=[],
        refinement_notes=[],
    )

    assert validate_final_markdown_contract(result) == []


def test_validate_final_markdown_contract_keeps_known_damage_checks() -> None:
    fallback_result = BackendDocumentRefinementResult(
        backend=BACKEND_CODEX_API,
        model_name=BACKEND_CODEX_API,
        final_markdown="# 正常标题\n\n程序回退稿",
        refinement_strategy="programmatic_markdown_fallback",
        refinement_reason="single_pass_backend_failed",
        needs_review_sections=[],
        refinement_notes=[],
    )
    damaged_result = BackendDocumentRefinementResult(
        backend=BACKEND_CODEX_API,
        model_name="gpt-5.6-terra",
        final_markdown="# source\n\n仍然包含损坏字符 �",
        refinement_strategy="single_pass_safe_replace",
        refinement_reason="single_pass_codex_api",
        needs_review_sections=[],
        refinement_notes=[],
    )

    assert validate_final_markdown_contract(fallback_result) == ["programmatic_markdown_fallback"]
    assert validate_final_markdown_contract(damaged_result) == [
        "contains_unicode_replacement_character",
        "uses_canonical_source_placeholder_title",
    ]


def test_refine_batch_accepts_backend_result_without_h1_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)
    calls = 0
    final_markdown = "哲学是世界观的理论形态。\n\n## 历史语境\n\n这里继续展开正文。"

    def fake_run_single_pass_backend_refinement(**_kwargs) -> BackendDocumentRefinementResult:
        nonlocal calls
        calls += 1
        return BackendDocumentRefinementResult(
            backend=BACKEND_CODEX_API,
            model_name="gpt-5.6-terra",
            final_markdown=final_markdown,
            refinement_strategy="single_pass_safe_replace",
            refinement_reason="single_pass_codex_api",
            needs_review_sections=[],
            refinement_notes=[],
            edited_plain_text="哲学是世界观的理论形态。\n\n历史语境\n\n这里继续展开正文。",
        )

    monkeypatch.setattr(
        "src.refine_utils.run_single_pass_backend_refinement",
        fake_run_single_pass_backend_refinement,
    )

    summary = refine_batch(loaded_settings)
    output_path = build_refinement_output_path(asr_path, tmp_path / "data/intermediate/refined")
    persisted = json.loads(output_path.json_path.read_text(encoding="utf-8"))
    diagnostics = json.loads(
        (loaded_settings.path_for("logs_dir") / "refine/diagnostics.json").read_text(encoding="utf-8")
    )

    assert summary.success == 1
    assert calls == 1
    assert persisted["final_markdown"] == final_markdown
    assert persisted["model_results"][BACKEND_CODEX_API]["validation_retry_count"] == 0
    assert diagnostics["attempts"][0]["status"] == "accepted"
    assert diagnostics["attempts"][0]["validation_reasons"] == []


def test_refine_batch_retries_programmatic_fallback_and_persists_validation_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    calls = 0

    def fake_run_single_pass_backend_refinement(**_kwargs) -> BackendDocumentRefinementResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return BackendDocumentRefinementResult(
                backend=BACKEND_CODEX_API,
                model_name=BACKEND_CODEX_API,
                final_markdown="# demo\n\n降级稿",
                refinement_strategy="programmatic_markdown_fallback",
                refinement_reason="single_pass_backend_failed",
                needs_review_sections=[],
                refinement_notes=["single_pass_backend_failed_use_programmatic_fallback"],
                edited_plain_text="降级稿",
            )
        return BackendDocumentRefinementResult(
            backend=BACKEND_CODEX_API,
            model_name=BACKEND_CODEX_API,
            final_markdown="# 可交付章节\n\n这是重新润色后的正文。",
            refinement_strategy="single_pass_safe_replace",
            refinement_reason="retry_succeeded",
            needs_review_sections=[],
            refinement_notes=[],
            edited_plain_text="这是重新润色后的正文。",
        )

    monkeypatch.setattr("src.refine_utils.run_single_pass_backend_refinement", fake_run_single_pass_backend_refinement)

    refine_batch(loaded_settings)
    output_path = build_refinement_output_path(asr_path, tmp_path / "data/intermediate/refined")
    result = json.loads(output_path.json_path.read_text(encoding="utf-8"))

    assert calls == 2
    assert result["prompt_mode"] == "single_pass_safe_replace"
    assert result["final_markdown"] == "# 可交付章节\n\n这是重新润色后的正文。"
    assert result["model_results"]["codex_api"]["validation_retry_count"] == 1
    assert "programmatic_markdown_fallback" in result["model_results"]["codex_api"]["validation_failure_reasons"]
    diagnostics_path = loaded_settings.path_for("logs_dir") / "refine/diagnostics.json"
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    attempts = diagnostics["attempts"]
    assert [item["status"] for item in attempts] == ["rejected", "accepted"]
    assert attempts[0]["validation_reasons"] == ["programmatic_markdown_fallback"]
    assert attempts[1]["validation_reasons"] == []
    assert attempts[0]["run_id"] == attempts[1]["run_id"]


def test_refine_batch_rejects_result_that_stays_invalid_after_configured_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"refinement_validation_retry_count": 1})
    write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    def fake_run_single_pass_backend_refinement(**_kwargs) -> BackendDocumentRefinementResult:
        return BackendDocumentRefinementResult(
            backend=BACKEND_CODEX_API,
            model_name=BACKEND_CODEX_API,
            final_markdown="# source\n\n仍然包含损坏字符 �",
            refinement_strategy="single_pass_safe_replace",
            refinement_reason="invalid",
            needs_review_sections=[],
            refinement_notes=[],
        )

    monkeypatch.setattr("src.refine_utils.run_single_pass_backend_refinement", fake_run_single_pass_backend_refinement)

    with pytest.raises(RefinementOutputValidationError, match="contains_unicode_replacement_character"):
        refine_batch(loaded_settings)

    diagnostics_path = loaded_settings.path_for("logs_dir") / "refine/diagnostics.json"
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert [item["status"] for item in diagnostics["attempts"]] == ["rejected", "rejected"]
    for attempt in diagnostics["attempts"]:
        assert attempt["failure_category"] == "content_validation"
        assert "需要人工复核" in attempt["failure_summary"]
        candidate_path = loaded_settings.path_for("logs_dir") / attempt["diagnostic_directory"] / attempt["candidate_file"]
        assert candidate_path.read_text(encoding="utf-8") == "# source\n\n仍然包含损坏字符 �"
    assert diagnostics["attempts"][-1]["validation_reasons"] == [
        "contains_unicode_replacement_character",
        "uses_canonical_source_placeholder_title",
    ]


def test_refine_batch_falls_back_to_codex_when_single_agy_backend_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"backends": [BACKEND_CODEX]})
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    def fake_run_agy_payload(_prompt: str, _loaded_settings) -> dict[str, object]:
        raise CLIBackendError("agy failed")

    def fake_run_codex_payload(_prompt: str, _loaded_settings, **_kwargs) -> dict[str, object]:
        return {
            "model_name": BACKEND_CODEX,
            "final_markdown": "# demo\n\nCodex 回退结果",
            "section_map": [{"section": "lecture", "source_blocks": [0]}],
            "refinement_notes": ["codex_fallback_note"],
            "needs_review_sections": [],
            "refinement_strategy": "single_pass_with_safe_replace",
            "refinement_reason": "single_pass_codex",
        }

    monkeypatch.setattr("src.refine_utils.run_agy_payload", fake_run_agy_payload)
    monkeypatch.setattr("src.refine_utils.run_codex_cli_payload", fake_run_codex_payload)

    refine_batch(loaded_settings, requested_backends=[BACKEND_AGY])
    output_path = build_refinement_output_path(asr_path, tmp_path / "data/intermediate/refined")
    result = json.loads(output_path.json_path.read_text(encoding="utf-8"))

    assert result["refinement_backends"] == [BACKEND_AGY]
    assert result["selected_backend"] == BACKEND_CODEX
    assert result["final_markdown"] == "# demo\n\nCodex 回退结果"
    assert result["backend_status"]["agy"] == "failed_on_file"
    assert result["backend_status"]["codex_cli"] == "returned_single_pass:model=codex_cli:fallback_from=agy"
    assert result["model_results"][BACKEND_CODEX]["final_markdown"] == "# demo\n\nCodex 回退结果"


def test_refine_batch_uses_single_codex_api_call_for_long_line_based_asr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"block_batch_size": 2, "block_concurrency": 6})
    asr_dir = tmp_path / "data/intermediate/asr"
    reference_dir = tmp_path / "data/intermediate/extracted_text"
    asr_dir.mkdir(parents=True, exist_ok=True)
    reference_dir.mkdir(parents=True, exist_ok=True)
    asr_path = asr_dir / "long-demo.txt"
    line_count = 200
    asr_path.write_text("\n".join(f"第{i:04d}行转写文本" for i in range(line_count)), encoding="utf-8")
    (reference_dir / "long-demo.txt").write_text("参考原文", encoding="utf-8")
    loaded_settings = load_settings(project_root=tmp_path)

    prompts: list[str] = []

    def fake_run_codex_payload(prompt: str, _loaded_settings, **_kwargs) -> dict[str, object]:
        prompts.append(prompt)
        return {
            "final_markdown": "# long-demo\n\n" + "\n\n".join(f"第{i:04d}行转写文本" for i in range(line_count)),
            "section_map": [],
            "refinement_notes": [],
            "needs_review_sections": [],
            "refinement_strategy": "single_pass_with_safe_replace",
            "refinement_reason": "single_pass_codex",
            "model_name": "gpt-5.4",
        }

    monkeypatch.setattr("src.refine_utils.run_codex_api_payload", fake_run_codex_payload)

    refine_batch(loaded_settings)
    output_path = build_refinement_output_path(asr_path, tmp_path / "data/intermediate/refined")
    result = json.loads(output_path.json_path.read_text(encoding="utf-8"))
    sidecar = json.loads(output_path.json_path.with_name("long-demo.codex_api.json").read_text(encoding="utf-8"))

    assert len(prompts) == 1
    assert "整篇参考原文" in prompts[0]
    assert "预替换全文" in prompts[0]
    assert "当前块正文" not in prompts[0]
    assert result["prompt_mode"] == "single_pass_safe_replace"
    assert result["backend_status"]["codex_api"] == "returned_single_pass:model=gpt-5.4"
    assert "第0000行转写文本" in result["final_markdown"]
    assert "第0199行转写文本" in result["final_markdown"]
    assert sidecar["prompt_mode"] == "single_pass_safe_replace"


def test_refine_batch_does_not_use_block_concurrency_in_single_pass_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"block_batch_size": 1, "block_concurrency": 6})
    asr_dir = tmp_path / "data/intermediate/asr"
    reference_dir = tmp_path / "data/intermediate/extracted_text"
    asr_dir.mkdir(parents=True, exist_ok=True)
    reference_dir.mkdir(parents=True, exist_ok=True)
    (asr_dir / "parallel-demo.txt").write_text("\n".join(f"第{i:04d}行转写文本" for i in range(12)), encoding="utf-8")
    (reference_dir / "parallel-demo.txt").write_text("参考原文", encoding="utf-8")
    loaded_settings = load_settings(project_root=tmp_path)

    seen: dict[str, object] = {}

    class FakeExecutor:
        def __init__(self, *, max_workers: int) -> None:
            seen["max_workers"] = max_workers

        def __enter__(self) -> "FakeExecutor":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb

    def fake_run_codex_payload(prompt: str, _loaded_settings, **_kwargs) -> dict[str, object]:
        return {
            "final_markdown": "# parallel-demo\n\n正文",
            "section_map": [],
            "refinement_notes": [],
            "needs_review_sections": [],
            "refinement_strategy": "single_pass_with_safe_replace",
            "refinement_reason": "single_pass_codex",
        }

    monkeypatch.setattr("src.refine_utils.ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr("src.refine_utils.run_codex_api_payload", fake_run_codex_payload)

    refine_batch(loaded_settings)

    assert "max_workers" not in seen


def test_refine_batch_logs_single_pass_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    write_minimal_settings(tmp_path, llm_overrides={"block_batch_size": 2, "block_concurrency": 6})
    asr_dir = tmp_path / "data/intermediate/asr"
    reference_dir = tmp_path / "data/intermediate/extracted_text"
    asr_dir.mkdir(parents=True, exist_ok=True)
    reference_dir.mkdir(parents=True, exist_ok=True)
    (asr_dir / "progress-demo.txt").write_text("\n".join(f"第{i:04d}行转写文本" for i in range(30)), encoding="utf-8")
    (reference_dir / "progress-demo.txt").write_text("参考原文", encoding="utf-8")
    loaded_settings = load_settings(project_root=tmp_path)

    def fake_run_codex_payload(prompt: str, _loaded_settings, **_kwargs) -> dict[str, object]:
        return {
            "final_markdown": "# progress-demo\n\n正文",
            "section_map": [],
            "refinement_notes": [],
            "needs_review_sections": [],
            "refinement_strategy": "single_pass_with_safe_replace",
            "refinement_reason": "single_pass_codex",
        }

    monkeypatch.setattr("src.refine_utils.run_codex_api_payload", fake_run_codex_payload)

    with caplog.at_level("INFO"):
        refine_batch(loaded_settings, logger=logging.getLogger("test-refine-progress"))

    progress_messages = [record.message for record in caplog.records if "阶段 6 单次调用" in record.message]
    assert len(progress_messages) == 1
    assert "file=progress-demo" in progress_messages[0]
    assert "backend=codex_api" in progress_messages[0]
    assert "locked_segments=" in progress_messages[0]
    assert "total_segments=" in progress_messages[0]


def test_refine_batch_uses_fallback_when_all_backends_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_settings(tmp_path)
    asr_path = write_refine_inputs(tmp_path)
    loaded_settings = load_settings(project_root=tmp_path)

    def fail_backend(**_kwargs) -> BackendDocumentRefinementResult:
        raise CLIBackendError("backend failed")

    monkeypatch.setattr("src.refine_utils.run_single_pass_backend_refinement", fail_backend)

    summary = refine_batch(loaded_settings)
    output_path = build_refinement_output_path(asr_path, tmp_path / "data/intermediate/refined")
    result = json.loads(output_path.json_path.read_text(encoding="utf-8"))

    assert summary.success == 1
    assert result["refinement_backends"] == [BACKEND_CODEX_API]
    assert result["backend_status"]["codex_api"] == "failed_on_file"
    assert "agy" not in result["backend_status"]
    assert result["backend_status"]["fallback"] == "used"
    assert result["selected_backend"] == BACKEND_FALLBACK
