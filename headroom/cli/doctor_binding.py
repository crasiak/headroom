"""Non-secret Ledger diagnostic metadata, never an ownership credential."""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit

BINDING_ENV = "LEDGER_HEADROOM_BINDING"


def parse_binding(raw: str) -> dict[str, object]:
    if len(raw.encode("utf-8")) > 4096:
        raise ValueError("binding exceeds 4 KiB")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("binding is not valid JSON") from None
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("unsupported binding schema")
    if value.get("harness") not in ("claude", "codex") or value.get("mode") not in (
        "isolated",
        "legacy-shared",
    ):
        raise ValueError("unsupported binding harness or mode")
    endpoint = value.get("endpoint")
    try:
        if not isinstance(endpoint, str) or any(ord(c) <= 32 for c in endpoint):
            raise ValueError()
        url = urlsplit(endpoint)
        if (
            url.scheme != "http"
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or "?" in endpoint
            or "#" in endpoint
            or url.path not in ("", "/")
            or not url.port
            or not ipaddress.ip_address(url.hostname).is_loopback
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError(
            "binding endpoint must be a literal loopback HTTP origin with a port"
        ) from None
    # Project only the documented fields: unknown strings must not leak into output.
    return {key: value[key] for key in ("schema", "harness", "mode", "endpoint")}
