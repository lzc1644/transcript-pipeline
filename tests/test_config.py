from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config_loader import load_settings
from src.schemas import ReferenceSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_load_settings_success() -> None:
    loaded_settings = load_settings(project_root=PROJECT_ROOT)

    assert loaded_settings.settings.project.name == "transcript-pipeline"
    assert loaded_settings.settings_path == (PROJECT_ROOT / "config/settings.yaml").resolve()
    assert loaded_settings.active_profile_name == "wsl2_gpu_high_accuracy"
    assert loaded_settings.settings.llm.backends == ["codex_api"]
    assert loaded_settings.settings.llm.model == "gpt-6.1-sol"
    assert loaded_settings.settings.llm.gemini_model == "Gemini 3.1 Pro (High)"
    assert loaded_settings.settings.llm.gemini_fallback_model == ""
    assert loaded_settings.settings.llm.reasoning_effort == "high"
    assert loaded_settings.settings.reference.ocr_timeout_seconds == 480
    assert loaded_settings.settings.reference.ai_ocr_backend == "codex_api"
    assert loaded_settings.settings.reference.gemini_ocr_model == "Gemini 3.5 Flash (High)"
    assert loaded_settings.settings.reference.gemini_ocr_fallback_model == ""
    assert loaded_settings.settings.reference.codex_ocr_model == "gpt-6-luna"
    assert loaded_settings.settings.reference.codex_ocr_reasoning_effort == "high"
    assert loaded_settings.settings.reference.codex_ocr_max_concurrency == 40
    assert loaded_settings.settings.reference.codex_ocr_submit_interval_seconds == 5.0
    assert loaded_settings.settings.codex_lb.base_url == "http://127.0.0.1:8317"
    assert loaded_settings.settings.codex_lb.base_url_env == "CODEX_LB_BASE_URL"
    assert loaded_settings.settings.codex_lb.api_key_env == "CODEX_LB_API_KEY"
    assert loaded_settings.settings.codex_lb.responses_path == "/v1/responses"
    assert loaded_settings.settings.codex_lb.codex_responses_path == "/v1/responses"


@pytest.mark.parametrize(
    "overrides",
    [
        {"codex_ocr_max_concurrency": 0},
        {"codex_ocr_max_concurrency": -1},
        {"codex_ocr_submit_interval_seconds": -0.1},
    ],
)
def test_reference_settings_reject_invalid_ocr_scheduling_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReferenceSettings(**overrides)


def test_load_settings_local_cpu_profile() -> None:
    loaded_settings = load_settings(project_root=PROJECT_ROOT, profile_name="local_cpu")

    assert loaded_settings.active_profile_name == "local_cpu"
    assert loaded_settings.active_profile.device == "cpu"
    assert loaded_settings.active_profile.beam_size == 5


def test_load_settings_local_cpu_high_accuracy_profile() -> None:
    loaded_settings = load_settings(project_root=PROJECT_ROOT, profile_name="local_cpu_high_accuracy")

    assert loaded_settings.active_profile_name == "local_cpu_high_accuracy"
    assert loaded_settings.active_profile.device == "cpu"
    assert loaded_settings.active_profile.asr_model_size == "large-v3-turbo"
    assert loaded_settings.active_profile.beam_size == 8


def test_load_settings_wsl2_gpu_profile() -> None:
    loaded_settings = load_settings(project_root=PROJECT_ROOT, profile_name="wsl2_gpu")

    assert loaded_settings.active_profile_name == "wsl2_gpu"
    assert loaded_settings.active_profile.device == "cuda"
    assert loaded_settings.active_profile.asr_model_size == "medium"
    assert loaded_settings.active_profile.beam_size == 5


def test_load_settings_wsl2_gpu_max_accuracy_profile() -> None:
    loaded_settings = load_settings(project_root=PROJECT_ROOT, profile_name="wsl2_gpu_max_accuracy")

    assert loaded_settings.active_profile_name == "wsl2_gpu_max_accuracy"
    assert loaded_settings.active_profile.device == "cuda"
    assert loaded_settings.active_profile.asr_model_size == "large-v3-turbo"
    assert loaded_settings.active_profile.asr_compute_type == "float16"
    assert loaded_settings.active_profile.beam_size == 10


def test_load_settings_wsl2_gpu_high_accuracy_profile() -> None:
    loaded_settings = load_settings(project_root=PROJECT_ROOT, profile_name="wsl2_gpu_high_accuracy")

    assert loaded_settings.active_profile_name == "wsl2_gpu_high_accuracy"
    assert loaded_settings.active_profile.device == "cuda"
    assert loaded_settings.active_profile.asr_model_size == "large-v3-turbo"
    assert loaded_settings.active_profile.beam_size == 8
