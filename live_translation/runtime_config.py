"""Explicit per-instance locations and loopback endpoints for local runtimes.

Defaults preserve the normal installation. A separate instance can use private
ports, a separate home, and a native sandbox without sharing service labels or
letting launchd silently resolve paths back to the login user's home.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


def _port(value: str, name: str) -> int:
    if not re.fullmatch(r"[0-9]{1,5}", value):
        raise ValueError(f"{name} must be a decimal port number")
    result = int(value)
    if not 1 <= result <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return result


def _absolute_path(value: str, name: str) -> Path:
    if not value or "\x00" in value:
        raise ValueError(f"{name} must be an absolute path")
    result = Path(value)
    if not result.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return result


@dataclass(frozen=True)
class RuntimeConfig:
    bridge_port: int
    ollama_port: int
    instance_id: str
    home: Path
    cf_home: Path
    hf_home: Path
    hf_hub_cache: Path
    ollama_models: Path
    sandbox_profile: Path | None = None

    @property
    def bridge_url(self) -> str:
        return f"http://127.0.0.1:{self.bridge_port}"

    @property
    def ollama_url(self) -> str:
        return f"http://127.0.0.1:{self.ollama_port}"

    @property
    def bridge_label(self) -> str:
        return self.instance_id + ".iina"

    @property
    def ollama_label(self) -> str:
        return self.instance_id + ".ollama"

    def environment(self) -> dict[str, str]:
        result = {
            "HOME": str(self.home),
            "CFFIXED_USER_HOME": str(self.cf_home),
            "HF_HOME": str(self.hf_home),
            "HF_HUB_CACHE": str(self.hf_hub_cache),
            "OLLAMA_MODELS": str(self.ollama_models),
            "LT_BRIDGE_PORT": str(self.bridge_port),
            "LT_OLLAMA_PORT": str(self.ollama_port),
            "LT_INSTANCE_ID": self.instance_id,
        }
        if self.sandbox_profile is not None:
            result["LT_SANDBOX_PROFILE"] = str(self.sandbox_profile)
        return result

    def wrap_arguments(self, arguments: Sequence[str]) -> list[str]:
        if self.sandbox_profile is None:
            return list(arguments)
        return ["/usr/bin/sandbox-exec", "-f", str(self.sandbox_profile), *arguments]


def load_runtime_config(environ: Mapping[str, str] | None = None) -> RuntimeConfig:
    values = os.environ if environ is None else environ
    bridge_port = _port(values.get("LT_BRIDGE_PORT", "18743"), "LT_BRIDGE_PORT")
    ollama_port = _port(values.get("LT_OLLAMA_PORT", "11434"), "LT_OLLAMA_PORT")
    if bridge_port == ollama_port:
        raise ValueError("LT_BRIDGE_PORT and LT_OLLAMA_PORT must differ")
    instance_id = values.get("LT_INSTANCE_ID", "local.live-translation")
    if len(instance_id) > 128 or not re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", instance_id):
        raise ValueError("LT_INSTANCE_ID must be a nonempty lowercase service identifier")
    home_value = str(Path.home()) if environ is None else values.get("HOME", str(Path.home()))
    home = _absolute_path(home_value, "HOME")
    cf_home = _absolute_path(values.get("CFFIXED_USER_HOME", str(home)), "CFFIXED_USER_HOME")
    hf_home = _absolute_path(values.get("HF_HOME", str(home / ".cache/huggingface")), "HF_HOME")
    hf_hub_cache = _absolute_path(values.get("HF_HUB_CACHE", str(hf_home / "hub")), "HF_HUB_CACHE")
    ollama_models = _absolute_path(
        values.get("OLLAMA_MODELS", str(home / ".ollama/models")), "OLLAMA_MODELS"
    )
    sandbox_profile = None
    if "LT_SANDBOX_PROFILE" in values:
        sandbox_profile = _absolute_path(values["LT_SANDBOX_PROFILE"], "LT_SANDBOX_PROFILE")
        if not sandbox_profile.is_file():
            raise ValueError("LT_SANDBOX_PROFILE must name an existing profile file")
    return RuntimeConfig(
        bridge_port,
        ollama_port,
        instance_id,
        home,
        cf_home,
        hf_home,
        hf_hub_cache,
        ollama_models,
        sandbox_profile,
    )
