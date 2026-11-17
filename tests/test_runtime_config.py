"""Isolation contracts for a real cold install, without touching login-user jobs."""

import io
import json
import plistlib
import urllib.error
from unittest import mock

import pytest

from iina_bridge import Bridge, ollama_version
from installer import bootstrap
from live_translation.runtime_config import load_runtime_config


def test_default_installation_addresses_and_labels_are_unchanged(tmp_path):
    cfg = load_runtime_config({"HOME": str(tmp_path)})
    assert cfg.bridge_url == "http://127.0.0.1:18743"
    assert cfg.ollama_url == "http://127.0.0.1:11434"
    assert cfg.bridge_label == "local.live-translation.iina"
    assert cfg.ollama_label == "local.live-translation.ollama"
    assert cfg.wrap_arguments(["/private/python", "worker.py"]) == ["/private/python", "worker.py"]
    environment = cfg.environment()
    assert environment["HOME"] == environment["CFFIXED_USER_HOME"] == str(tmp_path)
    assert environment["HF_HOME"] == str(tmp_path / ".cache/huggingface")
    assert environment["HF_HUB_CACHE"] == str(tmp_path / ".cache/huggingface/hub")
    assert environment["OLLAMA_MODELS"] == str(tmp_path / ".ollama/models")
    assert not list(tmp_path.iterdir())


def test_isolated_config_persists_full_context_across_launchd(tmp_path):
    profile = tmp_path / "private profile.sb"
    profile.write_text("(version 1) (allow default)")
    source = {
        "HOME": str(tmp_path / "home"),
        "CFFIXED_USER_HOME": str(tmp_path / "cf-home"),
        "HF_HOME": str(tmp_path / "hf"),
        "HF_HUB_CACHE": str(tmp_path / "hub"),
        "OLLAMA_MODELS": str(tmp_path / "models"),
        "LT_BRIDGE_PORT": "19143",
        "LT_OLLAMA_PORT": "19134",
        "LT_INSTANCE_ID": "local.live-translation.cold-test",
        "LT_SANDBOX_PROFILE": str(profile),
    }
    cfg = load_runtime_config(source)
    assert cfg.environment() == source
    assert cfg.bridge_label == "local.live-translation.cold-test.iina"
    assert cfg.ollama_label == "local.live-translation.cold-test.ollama"
    assert cfg.wrap_arguments(["/private/ollama", "serve"]) == [
        "/usr/bin/sandbox-exec",
        "-f",
        str(profile),
        "/private/ollama",
        "serve",
    ]


@pytest.mark.parametrize("name", ["LT_BRIDGE_PORT", "LT_OLLAMA_PORT"])
@pytest.mark.parametrize("value", ["", "0", "-1", "65536", "one", " 19143", "19143x"])
def test_bad_ports_never_silently_fall_back(name, value, tmp_path):
    with pytest.raises(ValueError, match=name):
        load_runtime_config({"HOME": str(tmp_path), name: value})


def test_distinct_services_cannot_share_a_port(tmp_path):
    with pytest.raises(ValueError, match="must differ"):
        load_runtime_config(
            {"HOME": str(tmp_path), "LT_BRIDGE_PORT": "19143", "LT_OLLAMA_PORT": "19143"}
        )


@pytest.mark.parametrize("value", ["", ".cold", "cold/unsafe", "Cold", "cold..unsafe", "x" * 129])
def test_instance_labels_are_validated(value, tmp_path):
    with pytest.raises(ValueError, match="LT_INSTANCE_ID"):
        load_runtime_config({"HOME": str(tmp_path), "LT_INSTANCE_ID": value})


@pytest.mark.parametrize(
    "name",
    ["HOME", "CFFIXED_USER_HOME", "HF_HOME", "HF_HUB_CACHE", "OLLAMA_MODELS", "LT_SANDBOX_PROFILE"],
)
@pytest.mark.parametrize("value", ["", "relative/path"])
def test_runtime_paths_must_be_explicit_absolute_paths(name, value, tmp_path):
    with pytest.raises(ValueError, match=name):
        load_runtime_config({"HOME": str(tmp_path), name: value})


def test_missing_sandbox_profile_is_an_error_not_an_unsandboxed_fallback(tmp_path):
    with pytest.raises(ValueError, match="existing profile"):
        load_runtime_config(
            {"HOME": str(tmp_path), "LT_SANDBOX_PROFILE": str(tmp_path / "missing.sb")}
        )


@pytest.fixture
def private_instance(monkeypatch, tmp_path):
    for name in [
        "CFFIXED_USER_HOME",
        "HF_HOME",
        "HF_HUB_CACHE",
        "OLLAMA_MODELS",
        "LT_SANDBOX_PROFILE",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LT_BRIDGE_PORT", "19143")
    monkeypatch.setenv("LT_OLLAMA_PORT", "19134")
    monkeypatch.setenv("LT_INSTANCE_ID", "local.live-translation.cold-test")
    return tmp_path


def test_bridge_version_and_model_requests_use_the_private_endpoint(private_instance):
    with mock.patch(
        "iina_bridge.local_urlopen", return_value=io.BytesIO(b'{"version":"test"}')
    ) as request:
        assert ollama_version() == "test"
    request.assert_called_once_with("http://127.0.0.1:19134/api/version", timeout=5)
    bridge = Bridge(start_worker=False, profile={"fixture": True})
    try:
        assert (
            bridge.cache_dir
            == private_instance / "Library/Application Support/LiveTranslate/subtitles"
        )
        with mock.patch("iina_bridge.DeterministicTranslator") as translator:
            translator.return_value.translate.return_value = "Перевод"
            assert bridge._translate("Speech", []) == "Перевод"
            assert translator.call_args.kwargs["url"] == "http://127.0.0.1:19134"
        with mock.patch(
            "iina_bridge.local_urlopen", return_value=io.BytesIO(b'{"done_reason":"unload"}')
        ) as request:
            bridge._release_translation()
        assert request.call_args.args[0].full_url == "http://127.0.0.1:19134/api/generate"
    finally:
        bridge.close()


def test_bootstrap_status_and_model_pull_use_the_private_endpoint(private_instance, monkeypatch):
    with mock.patch(
        "installer.bootstrap.local_urlopen", return_value=io.BytesIO(b'{"version":"test"}')
    ) as request:
        assert bootstrap.local_json("/api/version") == {"version": "test"}
    request.assert_called_once_with("http://127.0.0.1:19134/api/version", timeout=5)
    monkeypatch.setattr(
        bootstrap,
        "local_json",
        mock.Mock(side_effect=[{"models": []}, {"models": [{"name": bootstrap.TRANSLATOR}]}]),
    )
    with mock.patch(
        "installer.bootstrap.local_urlopen", return_value=io.BytesIO(b'{"status":"success"}\n')
    ) as request:
        bootstrap.pull_translation_model(bootstrap.Reporter())
    assert request.call_args.args[0].full_url == "http://127.0.0.1:19134/api/pull"


def test_new_ollama_job_preserves_namespace_home_and_sandbox(private_instance, monkeypatch):
    profile = private_instance / "cold.sb"
    profile.write_text("(version 1) (allow default)")
    monkeypatch.setenv("LT_SANDBOX_PROFILE", str(profile))
    monkeypatch.setattr(
        bootstrap,
        "local_json",
        mock.Mock(side_effect=[urllib.error.URLError("offline"), {"version": "test"}]),
    )
    start = mock.Mock()
    monkeypatch.setattr(bootstrap, "start_agent", start)
    support = private_instance / "Library/Application Support/LiveTranslate"
    support.mkdir(parents=True)
    binary = private_instance / "ollama"
    bootstrap.ensure_ollama_server(binary, support, bootstrap.Reporter())
    agent = private_instance / "Library/LaunchAgents/local.live-translation.cold-test.ollama.plist"
    start.assert_called_once_with(agent, "local.live-translation.cold-test.ollama")
    config = plistlib.loads(agent.read_bytes())
    assert config["ProgramArguments"] == [
        "/usr/bin/sandbox-exec",
        "-f",
        str(profile),
        str(binary),
        "serve",
    ]
    environment = config["EnvironmentVariables"]
    assert environment["HOME"] == environment["CFFIXED_USER_HOME"] == str(private_instance)
    assert environment["OLLAMA_HOST"] == "127.0.0.1:19134"
    assert environment["OLLAMA_MODELS"] == str(private_instance / ".ollama/models")
    assert environment["LT_BRIDGE_PORT"] == "19143"
    assert json.loads(json.dumps(config))["Label"] != "local.live-translation.ollama"
