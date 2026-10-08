from __future__ import annotations

from pathlib import Path

import yaml


def write_minimal_settings(
    project_root: Path,
    *,
    supported_audio_ext: list[str] | None = None,
    reference_overrides: dict | None = None,
    segmentation_overrides: dict | None = None,
    alignment_overrides: dict | None = None,
    llm_overrides: dict | None = None,
    output_overrides: dict | None = None,
) -> Path:
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir = config_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "project": {
            "name": "transcript-pipeline",
            "version": "0.1.0",
            "description": "test",
        },
        "runtime": {
            "profile": "local_cpu",
            "environment": "test",
            "log_level": "INFO",
        },
        "profiles": {
            "local_cpu": {
                "device": "cpu",
                "asr_compute_type": "int8",
                "asr_model_size": "small",
                "batch_size": 1,
                "beam_size": 5,
                "temp_dir": "/tmp/transcript-pipeline",
                "cache_dir": "~/.cache/transcript-pipeline",
            }
        },
        "paths": {
            "videos_dir": "data/input/videos",
            "audio_dir": "data/input/audio",
            "reference_dir": "data/input/reference",
            "asr_dir": "data/intermediate/asr",
            "ocr_dir": "data/intermediate/ocr",
            "extracted_text_dir": "data/intermediate/extracted_text",
            "chunks_dir": "data/intermediate/chunks",
            "aligned_dir": "data/intermediate/aligned",
            "classified_dir": "data/intermediate/classified",
            "refined_dir": "data/intermediate/refined",
            "review_dir": "data/output/review",
            "final_dir": "data/output/final",
            "logs_dir": "data/output/logs",
        },
        "audio": {
            "output_format": "wav",
            "sample_rate": 16000,
            "channels": 1,
            "overwrite": False,
            "supported_video_ext": [".mp4", ".mkv", ".mov", ".webm"],
            "supported_audio_ext": supported_audio_ext or [".wav", ".mp3", ".m4a", ".flac"],
        },
        "asr": {
            "engine": "faster-whisper",
            "language": "zh",
            "beam_size": 5,
            "vad_filter": True,
            "condition_on_previous_text": True,
            "word_timestamps": False,
            "initial_prompt": "",
            "model_cache_subdir": "faster-whisper",
        },
        "reference": {
            "enabled": True,
            "allow_pdf": True,
            "allow_txt": True,
            "allow_md": True,
            "allow_docx": False,
            "prefer_existing_text": True,
            "run_ocr_when_needed": False,
            "sentence_split_enabled": True,
            "ai_ocr_backend": "codex_api",
            "gemini_ocr_model": "Gemini 3.5 Flash (High)",
            "gemini_ocr_fallback_model": "",
            "codex_ocr_model": "gpt-6-luna",
            "codex_ocr_reasoning_effort": "high",
            "codex_ocr_max_concurrency": 40,
            "codex_ocr_submit_interval_seconds": 5.0,
            "ocr_timeout_seconds": 480,
            "ocr_languages": ["chi_sim", "eng"],
        },
        "segmentation": {
            "enabled": True,
            "min_chars_per_block": 60,
            "max_chars_per_block": 500,
            "max_seconds_per_block": 30,
            "split_on_empty_line": True,
            "merge_short_lines": True,
        },
        "alignment": {
            "method": "rapidfuzz_ratio",
            "top_k": 3,
            "matched_threshold": 80,
            "weak_match_threshold": 55,
            "use_normalization": True,
        },
        "classification": {
            "enabled": True,
            "allow_types": ["quote", "lecture", "qa"],
            "default_type": "lecture",
            "qa_section_title": "提问环节",
            "enable_intro_candidate": True,
            "quote_score_threshold": 85,
            "quote_like_min_score": 40,
            "mixed_score_threshold": 60,
            "quote_margin_threshold": 8,
            "reference_focus_margin": 4,
            "qa_keywords": ["为什么", "怎么", "请问", "是不是", "有没有", "能不能", "如何", "哪一个"],
            "intro_keywords": ["现在播送", "中央人民广播电台", "下面播送", "标题", "作者", "今天我们", "今天继续"],
            "lecture_markers": ["就是说", "我们看", "你看", "意思是", "说明", "比如", "所以", "这个地方", "这里"],
        },
        "llm": {
            "enabled": True,
            "provider": "local_cli",
            "model": "gpt-6.1-sol",
            "gemini_model": "Gemini 3.1 Pro (High)",
            "gemini_fallback_model": "",
            "backends": ["codex_api"],
            "enable_fallback": True,
            "block_batch_size": 2,
            "block_concurrency": 6,
            "prompt_style": "web_like",
            "top_matches_for_prompt": 3,
            "max_asr_chars_for_prompt": 120,
            "max_reference_chars_for_prompt": 120,
            "reasoning_effort": "high",
            "temperature": 0.1,
            "max_output_tokens": 4000,
            "timeout_seconds": 1800,
            "refinement_validation_retry_count": 1,
            "safe_replace_min_score": 88.0,
            "safe_replace_min_margin": 6.0,
            "safe_replace_length_ratio_min": 0.8,
            "safe_replace_length_ratio_max": 1.2,
            "safe_replace_max_extra_content_ratio": 0.12,
            "safe_replace_min_run_length": 2,
        },
        "codex_lb": {
            "base_url": "http://127.0.0.1:2455",
            "base_url_env": "CODEX_LB_BASE_URL",
            "api_key_env": "CODEX_LB_API_KEY",
            "responses_path": "/v1/responses",
            "codex_responses_path": "/v1/responses",
            "files_create_path": "/backend-api/files",
            "files_finalize_path_template": "/backend-api/files/{file_id}/uploaded",
        },
        "prompts": {
            "classify_and_correct": "config/prompts/classify_and_correct.md",
            "final_cleanup": "config/prompts/final_cleanup.md",
            "conversation_cleanup": "config/prompts/conversation_cleanup.md",
        },
        "output": {
            "write_review_json": True,
            "write_final_markdown": True,
            "final_markdown_filename": "final.md",
            "review_json_filename": "review.json",
            "include_timestamps_in_final": False,
            "include_reference_in_final": False,
            "include_notes_in_final": False,
        },
        "pipeline": {
            "stop_on_error": True,
            "stages": ["extract_audio", "transcribe", "prepare_reference", "refine", "export_markdown"],
        },
    }

    if reference_overrides:
        payload["reference"].update(reference_overrides)
    if segmentation_overrides:
        payload["segmentation"].update(segmentation_overrides)
    if alignment_overrides:
        payload["alignment"].update(alignment_overrides)
    if llm_overrides:
        payload["llm"].update(llm_overrides)
    if output_overrides:
        payload["output"].update(output_overrides)

    settings_path = config_dir / "settings.yaml"
    with settings_path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(payload, file, allow_unicode=True, sort_keys=False)
    (prompts_dir / "classify_and_correct.md").write_text("# test prompt\n", encoding="utf-8")
    (prompts_dir / "final_cleanup.md").write_text("# test final cleanup\n", encoding="utf-8")
    (prompts_dir / "conversation_cleanup.md").write_text("# test conversation cleanup\n", encoding="utf-8")
    return settings_path
