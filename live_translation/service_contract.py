"""Versioned local-service identity shared by the worker and installation tools."""

from __future__ import annotations

import os
import re

SERVICE_VERSION = "1.4.0"
SERVICE_NAME = "iina-local-translation"
PROTOCOL_VERSION = 1
CAPABILITIES = ("state_form", "native_srt", "auto_restart", "subtitle_sources", "sidebar_control", "bilingual")


def service_identity(runtime_id: str, *, process_id: int | None = None) -> dict:
    """Describe code loaded by this process, rather than files updated on disk."""
    if not isinstance(runtime_id, str) or not re.fullmatch(r"[a-f0-9]{64}", runtime_id):
        raise ValueError("The local runtime identity must be a SHA-256 digest")
    process_id = os.getpid() if process_id is None else process_id
    if type(process_id) is not int or process_id < 1:
        raise ValueError("The local runtime process identity must be positive")
    return {
        "version": SERVICE_VERSION,
        "protocol": PROTOCOL_VERSION,
        "runtime_id": runtime_id,
        "process_id": process_id,
        "capabilities": list(CAPABILITIES),
    }


def compatible_health(body, expected_runtime_id: str | None = None) -> bool:
    """Reject legacy, unrelated and stale healthy-looking listeners."""
    if not isinstance(body, dict):
        return False
    runtime_id = body.get("runtime_id")
    capabilities = body.get("capabilities")
    return bool(
        body.get("status") == "ok"
        and body.get("service") == SERVICE_NAME
        and type(body.get("protocol")) is int
        and body["protocol"] == PROTOCOL_VERSION
        and isinstance(body.get("version"), str)
        and body["version"].strip()
        and isinstance(runtime_id, str)
        and re.fullmatch(r"[a-f0-9]{64}", runtime_id)
        and (expected_runtime_id is None or runtime_id == expected_runtime_id)
        and type(body.get("process_id")) is int
        and body["process_id"] > 0
        and isinstance(capabilities, list)
        and all(capability in capabilities for capability in CAPABILITIES)
    )
