"""Terra content correction followed by prose editing; no retries or fallback."""
from __future__ import annotations

import json
from dataclasses import replace

from src.llm.gateway import GatewayClient
from src.llm.tasks import refinement as task
from src.request_trace import (
    build_refine_request_trace, create_trace_run_id, exception_metadata, safe_trace_component,
)
from src.runtime_utils import relativize_path

TERRA_MODEL = "gpt-6.1-terra"


def build_polish_prompt(policy: str, draft: task.BackendDocumentRefinementResult) -> str:
    # The prose pass deliberately gets no raw ASR/OCR or first-pass policy.
    return "\n\n".join([
        policy.strip(), task.REFINE_JSON_CONTRACT,
        "待人工复核的疑点（本轮不裁定）：",
        json.dumps(draft.needs_review_sections, ensure_ascii=False),
        "第一步内容编辑说明：",
        json.dumps(draft.refinement_notes, ensure_ascii=False),
        "待做书面表达编辑的完整底稿：", draft.final_markdown,
    ])


def run_two_pass_refinement(*, runtime: task.RefinementRuntime, input_paths: task.RefinementInputPaths,
                            draft_prompt: str, polish_prompt: str, asr_text: str,
                            reference_text: str, logger=None) -> task.BackendDocumentRefinementResult:
    if runtime.gateway is None:
        raise task.CLIBackendError("双步校对需要显式网关连接。")
    run_id = create_trace_run_id()
    directory = runtime.logs_dir / "refine" / safe_trace_component(input_paths.basename) / "terra-terra" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    gateway = GatewayClient(runtime.gateway.connection, timeout_seconds=runtime.options.timeout_seconds,
                            require_stream_completed=True, total_timeout=True)
    # This runner alone owns the effective model, effort and Fast parameters.
    options = replace(runtime.options, model=TERRA_MODEL, fast_mode=True, refinement_mode="single_pass",
                      enable_fallback=False, refinement_validation_retry_count=0,
                      backends=(task.BACKEND_CODEX_API,))
    pass_options = {"draft": replace(options, reasoning_effort="high"),
                    "polish": replace(options, reasoning_effort="xhigh")}
    passes = []

    def write_json(name, value):
        (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def save_pass(role, result):
        (directory / f"{role}.md").write_text(result.final_markdown, encoding="utf-8")
        write_json(f"{role}.json", task.serialize_backend_result(result))
        effective = pass_options[role]
        passes.append({"role": role, "model_name": result.model_name,
                       "reasoning_effort": effective.reasoning_effort, "fast_mode": effective.fast_mode,
                       "markdown_file": relativize_path(directory / f"{role}.md", runtime.workdir),
                       "json_file": relativize_path(directory / f"{role}.json", runtime.workdir)})

    status = {"mode": "terra_terra", "status": "running", "passes": passes, "workflow_status": "production",
              "models": {role: opts.model for role, opts in pass_options.items()},
              "pass_options": {role: {"reasoning_effort": opts.reasoning_effort, "fast_mode": opts.fast_mode}
                               for role, opts in pass_options.items()},
              "single_pass_model": runtime.options.model, "automatic_retries": 0, "model_fallback": False}
    # Keep the manifest filename for existing artifact readers and old run directories.
    write_json("experiment.json", status)
    (directory / "draft-policy.md").write_text(draft_prompt, encoding="utf-8")
    (directory / "polish-policy.md").write_text(polish_prompt, encoding="utf-8")
    stage = "draft"
    try:
        draft_runtime = replace(runtime, options=pass_options["draft"],
                                logs_dir=directory / "draft-trace", gateway=gateway)
        prepared = task.build_single_pass_refine_prompt(
            draft_prompt, input_paths, backend=task.BACKEND_CODEX_API, pre_replaced_segments=[],
            reference_full_text=reference_text, asr_full_text=asr_text,
        )
        (directory / "draft-prompt.txt").write_text(prepared, encoding="utf-8")
        if logger:
            logger.info("双步校对 1/2 | file=%s | model=%s", input_paths.basename, TERRA_MODEL)
        draft = task.run_validated_single_pass_backend_refinement(
            backend=task.BACKEND_CODEX_API, input_paths=input_paths, runtime=draft_runtime,
            markdown_prompt_text=draft_prompt, asr_full_text=asr_text,
            reference_full_text=reference_text, logger=logger,
        )
        draft = replace(draft, model_name=TERRA_MODEL)
        save_pass("draft", draft)
        write_json("experiment.json", status)

        stage = "polish"
        polish_runtime = replace(runtime, options=pass_options["polish"],
                                 logs_dir=directory / "polish-trace", gateway=gateway)
        prepared = build_polish_prompt(polish_prompt, draft)
        (directory / "polish-prompt.txt").write_text(prepared, encoding="utf-8")
        trace = build_refine_request_trace(polish_runtime.logs_dir, basename=input_paths.basename,
                                          backend=task.BACKEND_CODEX_API, run_id=run_id, attempt=1)
        trace.write_json("attempt-meta.json", {"role": "polish", "input_kind": "terra_draft",
                                               "draft_file": passes[0]["markdown_file"]})
        if logger:
            logger.info("双步校对 2/2 | file=%s | model=%s", input_paths.basename, TERRA_MODEL)
        payload = task.run_backend_payload(task.BACKEND_CODEX_API, prepared, polish_runtime, request_trace=trace)
        polished = replace(task.parse_backend_document_result(task.BACKEND_CODEX_API, payload),
                           model_name=TERRA_MODEL, refinement_strategy="style_polish")
        save_pass("polish", polished)  # Retain rejected candidates, but never publish them.
        reasons = task.validate_final_markdown_contract(polished)
        trace.write_json("validation.json", {"passed": not reasons, "validation_reasons": reasons})
        if reasons:
            raise task.RefinementOutputValidationError("第二步书面编辑未通过交付校验: " + "、".join(reasons))

        review = list(draft.needs_review_sections)
        for item in polished.needs_review_sections:
            if item not in review:
                review.append(item)
        status["status"] = "completed"
        write_json("experiment.json", status)
        return replace(
            polished, refinement_strategy="terra_terra_two_pass", refinement_reason="draft_then_prose",
            edited_plain_text=asr_text, needs_review_sections=review, section_map=draft.section_map,
            refinement_notes=[*draft.refinement_notes, *polished.refinement_notes, "terra_terra_requires_manual_review"],
            deletion_candidates=[*draft.deletion_candidates, *polished.deletion_candidates],
            fidelity_report={"passed": None, "checked": False, "pre_replaced_segments": 0,
                             "semantic_acceptance": "requires_manual_review"},
            refinement_passes=list(passes),
            backend_statuses={task.BACKEND_CODEX_API: f"returned_two_pass:models={TERRA_MODEL}->{TERRA_MODEL}"},
        )
    except Exception as exc:
        status.update(status="failed", failed_pass=stage,
                      error=exception_metadata(exc, category="refinement_two_pass"))
        write_json("experiment.json", status)
        raise
