"""Checks for the isolated experiment, not a replacement stage-6 contract."""
import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/probe_fulltext_refinement.py"
spec = importlib.util.spec_from_file_location("fulltext_probe", MODULE_PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def block(kind="speech", source=None, reference=None, text="讲话内容。", **extra):
    return {"kind": kind, "source_lines": source or [1, 1], "reference_lines": reference,
            "text": text, **extra}


def payload(*blocks, omissions=None):
    return {"title": "真实题名", "blocks": list(blocks), "omissions": omissions or [], "needs_review": []}


def test_prompt_preserves_raw_fulltext_without_pre_replacement():
    text = "明显的错别字。\n讲解和插话。"
    result = probe.build_prompt(text, "对应原文。")
    assert "[ASR 1] 明显的错别字。\n[ASR 2] 讲解和插话。" in result
    assert "[REF 1] 对应原文。" in result
    assert "locked_quote" not in result
    assert "SEGMENT" not in result
    # The sample's answers must never enter the generic policy.
    assert "突厥" not in probe.POLICY
    assert "李世民" not in probe.POLICY


def test_programmatic_render_preserves_interruption_and_heading_semantics():
    data = payload(block("heading", [1, 1], text="第一节", level=2),
                   block("quote", [2, 2], [1, 1], "朗读前半段。\n\n另一个自然段。"),
                   block(source=[3, 3], text="插话。"),
                   block("quote", [4, 4], [1, 1], "继续朗读。"))
    md, report = probe.validate_and_render(data, "第一节\n朗读\n插话\n继续朗读", "书籍正文")
    assert report["structural_passed"]
    assert "\n\n## 第一节\n\n> 朗读前半段。\n>\n> 另一个自然段。\n\n插话。\n\n> 继续朗读。" in md
    assert report["semantic_acceptance"].startswith("requires_independent")


def test_shared_source_line_is_allowed_for_inline_reading_interruption():
    _, report = probe.validate_and_render(payload(block("quote", [1, 1], [1, 1]), block()), "混合一句。", "参考。")
    assert report["structural_passed"]


@pytest.mark.parametrize("value", [[0, 1], [1, 3], [2, 1], [True, 1], [1], "1-2"])
def test_invalid_ranges_rejected(value):
    _, report = probe.validate_and_render(payload(block(source=value)), "甲\n乙", "参考。")
    assert not report["structural_passed"]
    assert "block_1:invalid_source_lines" in report["errors"]


def test_reordered_source_ranges_rejected():
    _, report = probe.validate_and_render(payload(block(source=[2, 2]), block(source=[1, 1])), "甲\n乙", "参考。")
    assert "block_2:source_order_or_overlap" in report["errors"]


def test_missing_source_lines_not_silently_accepted():
    _, report = probe.validate_and_render(payload(block()), "甲\n不可漏掉\n乙", "参考。")
    assert report["unaccounted_source_lines"] == [2, 3]
    assert not report["structural_passed"]


def test_omission_has_to_name_actual_source_text():
    omitted = {"source_lines": [2, 2], "source_text": "原始噪音", "reason": "无法恢复"}
    _, report = probe.validate_and_render(payload(block(), omissions=[omitted]), "甲\n原始噪音", "参考。")
    assert report["structural_passed"]
    assert report["claimed_omitted_lines"] == 1
    omitted["source_text"] = "编造的文字"
    _, report = probe.validate_and_render(payload(block(), omissions=[omitted]), "甲\n原始噪音", "参考。")
    assert "omission_1:source_text_not_in_claimed_range" in report["errors"]


@pytest.mark.parametrize("data,reason", [
    (block("quote", reference=[1, 9]), "invalid_reference_lines"),
    (block(reference=[1, 1]), "unexpected_reference_lines"),
    (block(text="> 模型自行标记"), "embedded_structure_marker"),
    (block(text="[ASR 1] 泄漏行号"), "embedded_structure_marker"),
    (block(text="OCR_PAGE_BREAK: 1 -> 2"), "leaked_or_damaged_text"),
    (block("heading", text="多行\n标题"), "invalid_heading"),
    (block("unknown"), "invalid_kind"),
])
def test_invalid_output_shapes_are_diagnostic(data, reason):
    _, report = probe.validate_and_render(payload(data), "甲", "参考。")
    assert f"block_1:{reason}" in report["errors"]


def test_written_style_policy_preserves_content_humor_and_separate_fact_checking():
    policy = probe.POLICY
    for phrase in ["只修改表达形式，不改变思想内容", "同义重复", "必要重复", "生动比喻", "不刻意学术化",
                   "不把核查建议回流替换正文", "不能根据回答倒推出未出现的问题", "编辑性标题"]:
        assert phrase in policy


def test_interaction_heading_and_roles_render_without_inventing_questions():
    data = payload(block("heading", [1, 1], text="问答与交流", level=2, heading_type="interaction"),
                   block(source=[1, 1], text="我补充一个例子。", interaction_role="comment"),
                   block(source=[2, 2], text="这个例子说明了同一问题。", interaction_role="answer"),
                   block(source=[3, 3], text="为什么？", interaction_role="question"))
    md, report = probe.validate_and_render(data, "补充\n回应\n问题", "参考")
    assert report["structural_passed"]
    assert "## 问答与交流" in md
    assert "**交流发言：** 我补充一个例子。" in md
    assert "**回应：** 这个例子说明了同一问题。" in md
    assert "**提问：** 为什么？" in md
    assert md.index("**交流发言") < md.index("**回应") < md.index("**提问")


@pytest.mark.parametrize("data,reason", [
    (block(interaction_role="listener"), "invalid_interaction_role"),
    (block("quote", reference=[1, 1], interaction_role="question"), "interaction_role_on_non_speech"),
    (block(interaction_role="answer", text="回答：重复标签"), "embedded_interaction_label"),
    (block("heading", text="问答与交流", level=2, heading_type="interaction"),
     "interaction_heading_without_following_interaction"),
])
def test_invalid_interaction_protocol_is_rejected(data, reason):
    _, report = probe.validate_and_render(payload(data), "转录", "参考")
    assert f"block_1:{reason}" in report["errors"]


def test_unknown_interaction_role_remains_unlabelled_and_markdown_is_preserved():
    text = "**重点**如下：\n\n- 一个例子\n- 另一个例子"
    md, report = probe.validate_and_render(payload(block(text=text)), "转录", "参考")
    assert report["structural_passed"]
    assert text in md
    assert "**提问：**" not in md and "**回应：**" not in md


def test_quote_without_reference_is_reported_not_invented():
    md, report = probe.validate_and_render(payload(block("quote")), "朗读。", "另一本书。")
    assert "> 讲话内容。" in md
    assert report["warnings"] == ["block_1:quote_without_reference"]


def test_trial_policy_comes_from_its_own_markdown_resource():
    assert probe.POLICY_PATH == MODULE_PATH.parents[1] / "config/prompts/fulltext_refinement_experiment.md"
    assert probe.POLICY == probe.POLICY_PATH.read_text(encoding="utf-8")
    assert probe.build_prompt("转录", "附件").startswith(probe.POLICY)


# These guard the editing instructions, not the semantic quality of an LLM response.
@pytest.mark.parametrize("fragments", [
    ("忠实表达先于通顺", "不能只因某个候选符合历史知识", "已经发生／差点发生", "后文修正前文时"),
    ("俚语、粗口和鲜明贬称", "不另加更强烈的贬称", "理论标签误解成人格评价"),
    ("短句不等于可删除的填充语", "quote / speech / quote", "行号覆盖不是内容已保留的证明"),
    ("quote 表示实际在朗读，不表示参考匹配成功", "参考缺失不能成为改标 speech 的理由", "reference_lines=null"),
    ("不能按段落交替猜角色", "不填角色，原文原位保留", "不能把听众评论改写成问题"),
    ("不要靠编出一句通顺的话来选答案", "局部用“〔此处转写不清〕”", "不能当作既定原话"),
])
def test_v3_policy_addresses_observed_failure_modes(fragments):
    for fragment in fragments:
        assert fragment in probe.POLICY


def test_prompt_does_not_normalize_claims_even_when_reference_disagrees():
    asr = "他在652年任职。后来被处死了。\n这句话真够离谱的，已经发生，不是差点发生。"
    reference = "他在655年任职。"
    prompt = probe.build_prompt(asr, reference)
    assert "[ASR 1] 他在652年任职。后来被处死了。" in prompt
    assert "[ASR 2] 这句话真够离谱的，已经发生，不是差点发生。" in prompt
    assert "[REF 1] 他在655年任职。" in prompt


def test_reference_gap_does_not_swallow_inline_comment_or_force_role():
    data = payload(block("quote", text="减轻负担。"),
                   block(text="就把人牢牢拴住了。"),
                   block("quote", text="秩序稳定。"),
                   block(source=[2, 2], text="我再补一句。", interaction_role=None))
    data["needs_review"] = [{"source_lines": [1, 1], "excerpt": "书上说减轻负担",
                             "reason": "有朗读导语，但当前附件缺对应文字。"}]
    md, report = probe.validate_and_render(data, "书上说减轻负担，就把人牢牢拴住了，然后说秩序稳定。\n我再补一句。", "另一处参考。")
    assert report["structural_passed"]
    assert report["warnings"] == ["block_1:quote_without_reference", "block_3:quote_without_reference"]
    assert "> 减轻负担。\n\n就把人牢牢拴住了。\n\n> 秩序稳定。\n\n我再补一句。" in md
    assert "**交流发言：**" not in md and "**回应：**" not in md


def test_claimed_coverage_does_not_certify_content_retention():
    _, report = probe.validate_and_render(payload(block(source=[1, 2], text="只有概括。")),
                                          "具体例子。\n有意义的反例。", "参考。")
    assert report["structural_passed"]
    assert report["unaccounted_source_lines"] == []
    assert report["semantic_acceptance"] == "requires_independent_reading_of_actual_source_and_candidate"
