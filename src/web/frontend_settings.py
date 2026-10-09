from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from src.asr.registry import WEB_CANDIDATE_IDS
from src.config_loader import ConfigLoadError, load_settings
from src.schemas import AsrCandidateName

SETTINGS_RELATIVE_PATH = Path("data/jobs/frontend-settings.json")
RETIRED_MODEL_PREFIXES = ("gpt-5.4", "gpt-5.5", "gpt-5.6")


def is_retired_model(value: str) -> bool:
    return any(value == prefix or value.startswith(prefix + "-") for prefix in RETIRED_MODEL_PREFIXES)


class FrontendSettings(BaseModel):
    codex_lb_base_url: str = ""
    codex_lb_api_key: str = ""
    codex_lb_bypass_proxy: bool = False
    profile: str = ""
    asr_candidate: AsrCandidateName | Literal[""] = ""
    secondary_asr_candidate: AsrCandidateName | Literal[""] | None = None
    backend: str = ""
    remote_concurrency: int = Field(default=2, ge=1)
    book_name: str = ""
    chapter: str = ""
    glossary_file: str = ""
    model: str = ""
    reasoning_effort: str = ""
    ocr_backend: str = ""
    ocr_model: str = ""
    ocr_reasoning_effort: str = ""


class FrontendSettingsUpdate(BaseModel):
    codex_lb_base_url: str | None = None
    codex_lb_api_key: str | None = None
    clear_codex_lb_api_key: bool = False
    codex_lb_bypass_proxy: bool | None = None
    profile: str | None = None
    asr_candidate: AsrCandidateName | Literal[""] | None = None
    secondary_asr_candidate: AsrCandidateName | Literal[""] | None = None
    backend: str | None = None
    remote_concurrency: int | None = Field(default=None, ge=1)
    book_name: str | None = None
    chapter: str | None = None
    glossary_file: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    ocr_backend: str | None = None
    ocr_model: str | None = None
    ocr_reasoning_effort: str | None = None


def frontend_settings_path(project_root: Path) -> Path:
    return project_root / SETTINGS_RELATIVE_PATH


def normalize_setting_value(value: str | None) -> str:
    return (value or "").strip()


def load_frontend_settings(project_root: Path) -> FrontendSettings:
    path = frontend_settings_path(project_root)
    if not path.exists():
        return FrontendSettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return FrontendSettings()
    if not isinstance(payload, dict):
        return FrontendSettings()
    settings = FrontendSettings.model_validate(payload)
    # 仅迁移 Web 默认值的读取视图，不改磁盘设置或历史任务快照。
    for field_name in ("model", "ocr_model"):
        if getattr(settings, field_name) == "gpt-6-sol":
            setattr(settings, field_name, "gpt-6.1-sol")
        elif is_retired_model(getattr(settings, field_name)):
            setattr(settings, field_name, "")
    if settings.asr_candidate and settings.asr_candidate not in WEB_CANDIDATE_IDS:
        settings.asr_candidate = ""
    if settings.secondary_asr_candidate and settings.secondary_asr_candidate not in WEB_CANDIDATE_IDS:
        settings.secondary_asr_candidate = ""
    return settings


def save_frontend_settings(project_root: Path, update: FrontendSettingsUpdate) -> FrontendSettings:
    current = load_frontend_settings(project_root)
    payload = current.model_dump()

    for field_name in (
        "codex_lb_base_url",
        "profile",
        "asr_candidate",
        "secondary_asr_candidate",
        "backend",
        "book_name",
        "chapter",
        "glossary_file",
        "model",
        "reasoning_effort",
        "ocr_backend",
        "ocr_model",
        "ocr_reasoning_effort",
    ):
        raw_value = getattr(update, field_name)
        if raw_value is not None:
            payload[field_name] = normalize_setting_value(raw_value)
    if update.remote_concurrency is not None:
        payload["remote_concurrency"] = update.remote_concurrency
    if update.codex_lb_bypass_proxy is not None:
        payload["codex_lb_bypass_proxy"] = update.codex_lb_bypass_proxy

    if update.clear_codex_lb_api_key:
        payload["codex_lb_api_key"] = ""
    elif update.codex_lb_api_key is not None:
        normalized_api_key = normalize_setting_value(update.codex_lb_api_key)
        if normalized_api_key:
            payload["codex_lb_api_key"] = normalized_api_key

    settings = FrontendSettings.model_validate(payload)
    path = frontend_settings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def frontend_settings_response(project_root: Path) -> dict[str, object]:
    settings = load_frontend_settings(project_root)
    try:
        loaded_settings = load_settings(project_root=project_root)
        codex_lb = loaded_settings.settings.codex_lb
        default_base_url = os.environ.get(codex_lb.base_url_env, "").strip() or codex_lb.base_url
        default_profile = loaded_settings.active_profile_name
        configured_asr_candidate = loaded_settings.settings.asr.candidate or "whisper-existing"
        default_asr_candidate = (configured_asr_candidate if configured_asr_candidate in WEB_CANDIDATE_IDS
                                 else "whisper-existing")
        default_secondary_asr_candidate = loaded_settings.settings.asr.secondary_candidate or ""
        configured_backends = loaded_settings.settings.llm.backends
        default_backend = configured_backends[0] if configured_backends else ""
        default_model = loaded_settings.settings.llm.model
        default_reasoning_effort = loaded_settings.settings.llm.reasoning_effort
        default_ocr_backend = loaded_settings.settings.reference.ai_ocr_backend
        default_ocr_model = loaded_settings.settings.reference.codex_ocr_model
        default_ocr_reasoning_effort = loaded_settings.settings.reference.codex_ocr_reasoning_effort
        default_ocr_max_concurrency = loaded_settings.settings.reference.codex_ocr_max_concurrency
        default_ocr_submit_interval_seconds = loaded_settings.settings.reference.codex_ocr_submit_interval_seconds
        api_key_env = codex_lb.api_key_env
        has_env_api_key = bool(os.environ.get(api_key_env, "").strip())
    except ConfigLoadError:
        # 前端设置接口沿用既有稳定响应结构；配置暂时不可读时使用产品明确指定的 OCR 调度默认值。
        default_base_url = ""
        default_profile = ""
        default_asr_candidate = "whisper-existing"
        default_secondary_asr_candidate = ""
        default_backend = ""
        default_model = ""
        default_reasoning_effort = ""
        default_ocr_backend = ""
        default_ocr_model = ""
        default_ocr_reasoning_effort = ""
        default_ocr_max_concurrency = 40
        default_ocr_submit_interval_seconds = 5.0
        api_key_env = "CODEX_LB_API_KEY"
        has_env_api_key = bool(os.environ.get(api_key_env, "").strip())

    return {
        "codex_lb_base_url": settings.codex_lb_base_url or default_base_url,
        "codex_lb_api_key": "",
        "has_codex_lb_api_key": bool(settings.codex_lb_api_key or has_env_api_key),
        "codex_lb_bypass_proxy": settings.codex_lb_bypass_proxy,
        "profile": settings.profile or default_profile,
        "asr_candidate": settings.asr_candidate or default_asr_candidate,
        "secondary_asr_candidate": settings.secondary_asr_candidate if settings.secondary_asr_candidate is not None else default_secondary_asr_candidate,
        "backend": settings.backend or default_backend,
        "remote_concurrency": settings.remote_concurrency,
        "book_name": settings.book_name,
        "chapter": settings.chapter,
        "glossary_file": settings.glossary_file,
        "model": settings.model or default_model,
        "reasoning_effort": settings.reasoning_effort or default_reasoning_effort,
        "ocr_backend": settings.ocr_backend or default_ocr_backend,
        "ocr_model": settings.ocr_model or default_ocr_model,
        "ocr_reasoning_effort": settings.ocr_reasoning_effort or default_ocr_reasoning_effort,
        "ocr_max_concurrency": default_ocr_max_concurrency,
        "ocr_submit_interval_seconds": default_ocr_submit_interval_seconds,
        "api_key_env": api_key_env,
        "settings_path": str(frontend_settings_path(project_root)),
    }


@contextmanager
def codex_lb_environment(settings: FrontendSettings) -> Iterator[None]:
    keys = ("CODEX_LB_BASE_URL", "CODEX_LB_API_KEY", "NO_PROXY", "no_proxy")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        if settings.codex_lb_base_url:
            os.environ["CODEX_LB_BASE_URL"] = settings.codex_lb_base_url
        if settings.codex_lb_api_key:
            os.environ["CODEX_LB_API_KEY"] = settings.codex_lb_api_key
        if settings.codex_lb_bypass_proxy:
            effective_base_url = os.environ.get("CODEX_LB_BASE_URL", "").strip()
            bypass_host = urlparse(effective_base_url).hostname or ""
            if bypass_host:
                entries = [
                    entry.strip()
                    for value in (os.environ.get("NO_PROXY", ""), os.environ.get("no_proxy", ""))
                    for entry in value.split(",")
                    if entry.strip()
                ]
                if bypass_host not in entries:
                    entries.append(bypass_host)
                merged_no_proxy = ",".join(dict.fromkeys(entries))
                os.environ["NO_PROXY"] = merged_no_proxy
                os.environ["no_proxy"] = merged_no_proxy
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
