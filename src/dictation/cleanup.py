"""Compatibility adapters for dictation text tasks and legacy settings callers."""
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field, replace

from src.llm.gateway import GatewayClient, GatewayConnection
from src.llm.tasks.dictation import (
    DEFAULT_CLEANUP_TIMEOUT_SECONDS, DOCUMENT_CHUNK_CHARS, DOCUMENT_INSTRUCTIONS,
    INSTRUCTIONS, OUTPUT_FORMAT, UsageTrace, document_chunks, validate_cleanup,
    DictationCleanup,
)


@dataclass(frozen=True)
class SessionGatewayClient(GatewayClient):
    credential: str = field(default="", repr=False)

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, "total_timeout", True)

    def _resolve_connection(self):
        connection = super()._resolve_connection()
        return replace(connection, api_key=self.credential) if self.credential else connection


class Cleanup(DictationCleanup):
    def __init__(self, gateway, model="gpt-6-luna", timeout=DEFAULT_CLEANUP_TIMEOUT_SECONDS,
                 *, project_root: Path | None = None, document_model="gpt-6-luna",
                 document_timeout=DEFAULT_CLEANUP_TIMEOUT_SECONDS, reasoning_effort="low",
                 document_reasoning_effort="high", connection: GatewayConnection | None = None):
        if connection is None and isinstance(gateway, GatewayConnection):
            connection = gateway
        if connection is None:
            credential = os.getenv(gateway.api_key_env, "")
            if project_root is not None:
                from src.web.frontend_settings import load_frontend_settings
                frontend = load_frontend_settings(project_root)
                credential = credential or frontend.codex_lb_api_key
            # Preserve the old adapter's late environment lookup. Product
            # service callers supply an explicit connection before offloading.
            connection = SessionGatewayClient(gateway, timeout_seconds=timeout,
                                              require_stream_completed=True, credential=credential)
        super().__init__(connection, model, timeout, document_model=document_model,
                         document_timeout=document_timeout, reasoning_effort=reasoning_effort,
                         document_reasoning_effort=document_reasoning_effort)
