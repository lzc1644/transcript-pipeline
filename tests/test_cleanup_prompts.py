"""默认提示词的装配与工程契约回归；不把静态断言当作模型质量验收。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config_loader import load_settings
from src.refine_inputs import DualAsrInput
from src.refine_utils import (
    PreReplacementSegment,
    RefinementInputPaths,
    build_single_pass_refine_prompt,
    load_markdown_assemble_prompt,
    locked_quotes_preserved,
)
from tests.helpers import write_minimal_settings


PROMPT_ROOT = Path(__file__).resolve().parents[1] / "config/prompts"
JSON_FIELDS = (
    "final_markdown", "section_map", "refinement_notes",
    "needs_review_sections", "deletion_candidates",
)
POLICY_FRAGMENTS = (
    "以下纠错与轻度清理仅作用于未锁定输入",
    "不能改写 `locked_quote` 的实词或顺序",
    "不能自行解锁",
    "证据共同支持一个明显更可信的候选时，应主动采用",
    "不必等到可以逐字唯一还原才纠错",
    "否定、条件和因果关系以讲话证据为准",
    "不能为了让观点更合理而增删",
    "不能只因某个名字更著名",
    "不按多数投票",
    "可以清理纯口吃与无意义填充",
    "删除紧邻且没有新增含义的机械重复",
    "也保留有意义的重复强调、自我修正",
    "保持原顺序及",
    "同一话题被问答或另一话轮打断，不能跨过它合并",
    "玩笑中有意义的特殊措辞要保留",
    "已有较可信候选时，可在正文采用",
    "证据相当、不同选择会改变含义时",
    "不用大片省略号代替有效讲话",
    "不编造时间戳",
)


def segment(text: str, *, locked: bool = False, index: int = 0) -> PreReplacementSegment:
    return PreReplacementSegment(
        segment_type="locked_quote" if locked else "unlocked_text",
        text=text,
        source_text=text,
        reference_text=text if locked else "",
        start_sentence_index=index,
        end_sentence_index=index,
    )


@pytest.mark.parametrize("content_type", ["book_club", "conversation"])
@pytest.mark.parametrize("mode", ["single_no_reference", "single_reference", "dual_reference"])
@pytest.mark.parametrize("backend", ["codex_api", "agy", "codex_cli"])
def test_default_editing_policy_reaches_each_prompt_without_conflicting_tail(
    tmp_path: Path, content_type: str, mode: str, backend: str,
) -> None:
    write_minimal_settings(tmp_path)
    filename = "conversation_cleanup.md" if content_type == "conversation" else "final_cleanup.md"
    policy = (PROMPT_ROOT / filename).read_text(encoding="utf-8").strip()
    (tmp_path / "config/prompts" / filename).write_text(policy, encoding="utf-8")
    settings = load_settings(project_root=tmp_path)
    assert load_markdown_assemble_prompt(settings, content_type=content_type) == policy

    asr_path = settings.path_for("asr_dir") / "demo.txt"
    reference_path = None if mode == "single_no_reference" else settings.path_for("extracted_text_dir") / "demo.txt"
    quote = "万物各有其理。"
    speech = "嗯，我，我不是说两个条件一样。"
    reference = "参考附件：万物各有其理。" if reference_path else ""
    parts = [segment(quote, locked=mode == "single_reference"), segment(speech, index=1)]
    dual = None
    if mode == "dual_reference":
        primary = {"model_size": "large-v3-turbo", "segments": [
            {"start": 0.0, "text": quote}, {"start": 61.0, "text": speech},
        ]}
        secondary = {"model_size": "Qwen/Qwen3-ASR-0.6B", "segments": [
            {"start": 0.5, "text": quote}, {"start": 62.0, "text": "我不是说两个条件一样。"},
        ]}
        dual = DualAsrInput(
            primary_path=asr_path.with_suffix(".json"),
            secondary_path=asr_path.parent / "secondary/qwen3-asr-0.6b/demo.json",
            primary_candidate="whisper-existing", secondary_candidate="qwen3-asr-0.6b",
            primary=primary, secondary=secondary, pair_run_id="prompt-fixture",
        )
        parts = []
    paths = RefinementInputPaths("demo", asr_path, reference_path, dual)
    prompt = build_single_pass_refine_prompt(
        policy, paths, backend=backend, pre_replaced_segments=parts, reference_full_text=reference,
    )

    assert prompt.startswith(policy) and prompt.count(policy) == 1
    for fragment in POLICY_FRAGMENTS:
        assert fragment in policy
    for field in JSON_FIELDS:
        assert policy.count(f"- `{field}`：") == 1
    assert prompt.count("字段必须包含") == 1
    for obsolete in ("只允许添加标点", "所有有效讲话、追问、回答、停顿后的补充", "结果会交给"):
        assert obsolete not in prompt
    # 装配不能代替模型做清理、改否定或重排证据。
    assert speech in prompt and prompt.index(quote) < prompt.index(speech)
    if mode == "dual_reference":
        assert dual.render() in prompt
        assert "[ASR A]" in prompt and "[ASR B]" in prompt
        assert "工程契约：不得改写 locked_quote" not in prompt
    elif mode == "single_reference":
        assert "[SEGMENT 01][locked_quote]" in prompt
        assert "[SEGMENT 02][unlocked_text]" in prompt
        assert "工程契约：不得改写 locked_quote 的实词内容" in prompt
    else:
        assert "[SEGMENT 01]" in prompt and "本任务没有参考附件" in prompt
    if reference_path:
        assert reference in prompt
    else:
        assert "参考附件：" not in prompt


@pytest.mark.parametrize("filename", ["final_cleanup.md", "conversation_cleanup.md"])
def test_policies_do_not_encode_answers_from_the_evaluation_recording(filename: str) -> None:
    policy = (PROMPT_ROOT / filename).read_text(encoding="utf-8")
    for sample_answer in ("杨献珍", "咱毛爷爷", "大汗", "伊比纳", "恣意横行", "宇宙主佛"):
        assert sample_answer not in policy
    for job_id in ("fcb933b77a18", "90ed921097cc"):
        assert job_id not in policy


@pytest.mark.parametrize("content_type", ["book_club", "conversation"])
def test_new_editorial_autonomy_still_respects_locked_quote_validation(
    tmp_path: Path, content_type: str,
) -> None:
    write_minimal_settings(tmp_path)
    filename = "conversation_cleanup.md" if content_type == "conversation" else "final_cleanup.md"
    policy = (PROMPT_ROOT / filename).read_text(encoding="utf-8").strip()
    parts = [segment("万物各有其理。", locked=True), segment("嗯，我，我不是说两个条件一样。", index=1)]
    paths = RefinementInputPaths("demo", tmp_path / "demo.txt", tmp_path / "reference.txt")
    prompt = build_single_pass_refine_prompt(
        policy, paths, backend="codex_api", pre_replaced_segments=parts, reference_full_text="万物各有其理。",
    )
    assert "不能改写 `locked_quote` 的实词或顺序" in prompt
    assert "不能自行解锁" in prompt
    assert "参考/OCR 纠错" in prompt
    assert locked_quotes_preserved(
        "> 万物各有其理。\n\n我不是说两个条件一样。", parts,
    )
    assert not locked_quotes_preserved(
        "> 万物没有规律。\n\n我不是说两个条件一样。", parts,
    )
