"""Immutable per-execution gateway configuration; no product or job dependencies."""
from __future__ import annotations

import base64
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import unquote, urlparse
from urllib.request import ProxyHandler, Request, _parse_proxy, build_opener, proxy_bypass_environment


@dataclass(frozen=True)
class ProxySettings:
    # Proxy URLs may contain credentials. Never include them in repr/diagnostics.
    environment: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @classmethod
    def capture(cls, environ: Mapping[str, str] | None = None, *, bypass_host: str = "") -> ProxySettings:
        source = os.environ if environ is None else environ
        values = {name: value for name, value in source.items() if name.lower().endswith("_proxy")}
        if bypass_host:
            entries = [entry.strip() for name in ("NO_PROXY", "no_proxy")
                       for entry in values.get(name, "").split(",") if entry.strip()]
            entries.append(bypass_host)
            merged = ",".join(dict.fromkeys(entries))
            values.update(NO_PROXY=merged, no_proxy=merged)
        # Keep urllib's CGI safeguard in the snapshot, not in later global reads.
        if "REQUEST_METHOD" in source:
            values.pop("HTTP_PROXY", None)
        return cls(tuple(sorted(values.items())))

    def urllib_proxies(self) -> dict[str, str]:
        proxies = {name[:-6].lower(): value for name, value in self.environment if value}
        for name, value in self.environment:
            if name.endswith("_proxy"):
                if value:
                    proxies[name[:-6]] = value
                else:
                    proxies.pop(name[:-6], None)
        return proxies

    def subprocess_environment(self) -> dict[str, str]:
        # Never let another execution's proxy changes leak into a curl process.
        result = {name: value for name, value in os.environ.items() if not name.lower().endswith("_proxy")}
        result.update(self.environment)
        return result


@dataclass(frozen=True)
class GatewayConnection:
    base_url: str
    api_key: str = field(default="", repr=False)
    proxy: ProxySettings = field(default_factory=ProxySettings)
    responses_path: str = "/v1/responses"
    codex_responses_path: str = "/v1/responses"
    files_create_path: str = "/backend-api/files"
    files_finalize_path_template: str = "/backend-api/files/{file_id}/uploaded"
    api_key_env: str = ""


def connection_from_settings(
    settings: Any,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    bypass_proxy: bool = False,
    environ: Mapping[str, str] | None = None,
) -> GatewayConnection:
    """Translate the historical connection settings once, before starting work.

    Explicit values win; None inherits the legacy configured environment. The
    returned connection can be passed through executors and threads unchanged.
    """
    source = dict(os.environ if environ is None else environ)
    url_env = getattr(settings, "base_url_env", "").strip()
    key_env = getattr(settings, "api_key_env", "").strip()
    resolved_url = ((source.get(url_env, "").strip() if url_env else "") or settings.base_url.strip()) if base_url is None else base_url.strip()
    resolved_key = (source.get(key_env, "").strip() if key_env else "") if api_key is None else api_key.strip()
    return GatewayConnection(
        base_url=resolved_url.rstrip("/"), api_key=resolved_key,
        proxy=ProxySettings.capture(source, bypass_host=(urlparse(resolved_url).hostname or "") if bypass_proxy else ""),
        responses_path=settings.responses_path, codex_responses_path=settings.codex_responses_path,
        files_create_path=settings.files_create_path, files_finalize_path_template=settings.files_finalize_path_template,
        api_key_env=key_env,
    )


class _FrozenProxyHandler(ProxyHandler):
    """urllib's proxy handler, with bypass decisions from this request only."""

    def proxy_open(self, req, proxy, type):
        original_type = req.type
        proxy_type, user, password, hostport = _parse_proxy(proxy)
        proxy_type = proxy_type or original_type
        if req.host and proxy_bypass_environment(req.host, self.proxies):
            return None
        if user and password:
            credentials = base64.b64encode(f"{unquote(user)}:{unquote(password)}".encode()).decode("ascii")
            req.add_header("Proxy-authorization", "Basic " + credentials)
        req.set_proxy(unquote(hostport), proxy_type)
        if original_type != proxy_type and original_type != "https":
            return self.parent.open(req, timeout=req.timeout)
        return None


def snapshot_urlopen(request: Request, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    proxy = getattr(request, "gateway_proxy", None)
    if proxy is None:
        proxy = ProxySettings.capture()
    return build_opener(_FrozenProxyHandler(proxy.urllib_proxies())).open(request, timeout=timeout)
