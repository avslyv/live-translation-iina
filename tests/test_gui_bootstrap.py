"""GUI setup contracts: reuse real cache shapes, no model inference or services."""

import io
import json
import ssl
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from installer import bootstrap
from live_translation.runtime_lifecycle import begin_runtime_update, runtime_revision
from live_translation.service_contract import SERVICE_NAME, service_identity


def events(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def runtime_tree(project):
    (project / "live_translation").mkdir(parents=True, exist_ok=True)
    (project / "iina_bridge.py").write_text("# previous worker fixture\n")
    (project / "live_translation/__init__.py").write_text("")
    (project / "requirements-iina.txt").write_text("# previous dependencies fixture\n")
    return project


@pytest.mark.parametrize("kind", ["legacy", "runtime", "capabilities", "process"])
def test_gui_service_readiness_rejects_stale_healthy_listener(tmp_path, monkeypatch, kind):
    project = runtime_tree(tmp_path / "project")
    monkeypatch.setenv("LT_BRIDGE_PORT", "19143")
    body = {
        "status": "ok",
        "service": SERVICE_NAME,
        **service_identity(runtime_revision(project)),
    }
    if kind == "legacy":
        body = {"status": "ok"}
    elif kind == "runtime":
        body["runtime_id"] = "0" * 64
    elif kind == "capabilities":
        body["capabilities"] = ["state_form", "native_srt"]
    else:
        body.pop("process_id", None)
    with mock.patch.object(
        bootstrap, "local_urlopen", return_value=io.BytesIO(json.dumps(body).encode())
    ) as request:
        assert not bootstrap.service_ready(project)
    request.assert_called_once_with("http://127.0.0.1:19143/health", timeout=1)


def test_gui_service_readiness_requires_complete_matching_runtime(tmp_path):
    project = runtime_tree(tmp_path / "project")
    body = {
        "status": "ok",
        "service": SERVICE_NAME,
        **service_identity(runtime_revision(project)),
    }
    with mock.patch.object(
        bootstrap, "local_urlopen", return_value=io.BytesIO(json.dumps(body).encode())
    ):
        assert bootstrap.service_ready(project)
    begin_runtime_update(project)
    with mock.patch.object(bootstrap, "local_urlopen") as request:
        assert not bootstrap.service_ready(project)
    request.assert_not_called()


def speech_cache(tmp_path: Path, monkeypatch, weight_name="weights.safetensors"):
    cache = tmp_path / "hf-cache"
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    repository = cache / ("models--" + bootstrap.WHISPER.replace("/", "--"))
    revision = "a" * 40
    (repository / "refs").mkdir(parents=True)
    (repository / "refs/main").write_text(revision)
    snapshot = repository / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text('{"n_mels":128}')
    (snapshot / weight_name).write_bytes(b"fixture model weights")
    return repository, snapshot


@pytest.mark.parametrize("weight_name", ["weights.safetensors", "model.safetensors", "weights.npz"])
def test_complete_speech_cache_is_reused_without_loading_a_model(
    tmp_path, monkeypatch, weight_name
):
    speech_cache(tmp_path, monkeypatch, weight_name)
    assert bootstrap.whisper_ready() is True


def test_partial_speech_cache_is_not_reported_ready(tmp_path, monkeypatch):
    _, snapshot = speech_cache(tmp_path, monkeypatch)
    (snapshot / "weights.safetensors").unlink()
    assert bootstrap.whisper_ready() is False
    (snapshot / "weights.safetensors").write_bytes(b"restored fixture weights")
    (snapshot / "config.json").unlink()
    assert bootstrap.whisper_ready() is False


@pytest.mark.parametrize("configuration", ["", "{}", "[]", "null", "not-json"])
def test_empty_or_malformed_speech_configuration_is_not_reused(
    tmp_path, monkeypatch, configuration
):
    _, snapshot = speech_cache(tmp_path, monkeypatch)
    (snapshot / "config.json").write_text(configuration)
    assert bootstrap.whisper_ready() is False


@pytest.mark.parametrize("weight_name", ["weights.safetensors", "model.safetensors", "weights.npz"])
def test_zero_byte_speech_weights_are_not_reused(tmp_path, monkeypatch, weight_name):
    _, snapshot = speech_cache(tmp_path, monkeypatch, weight_name)
    (snapshot / weight_name).write_bytes(b"")
    assert bootstrap.whisper_ready() is False


def test_invalid_speech_revision_cannot_escape_snapshot_directory(tmp_path, monkeypatch):
    repository, _ = speech_cache(tmp_path, monkeypatch)
    (repository / "refs/main").write_text("../../unexpected")
    assert bootstrap.whisper_ready() is False


def test_missing_runtime_does_not_launch_python(tmp_path):
    with mock.patch.object(bootstrap.subprocess, "run") as run:
        assert bootstrap.runtime_ready(tmp_path / "missing-python") is False
    run.assert_not_called()


@pytest.mark.parametrize("returncode, expected", [(0, True), (1, False)])
def test_runtime_reuse_checks_pinned_metadata_without_inference(tmp_path, returncode, expected):
    python = tmp_path / "python"
    python.touch()
    with mock.patch.object(
        bootstrap.subprocess, "run", return_value=mock.Mock(returncode=returncode)
    ) as run:
        assert bootstrap.runtime_ready(python) is expected
    args = run.call_args.args[0]
    assert args[:2] == [str(python), "-c"]
    assert "importlib.metadata" in args[2]
    assert "import mlx" not in args[2]
    expected_packages = json.loads(args[3])
    assert {"mlx", "mlx-whisper", "numpy", "huggingface_hub"} <= expected_packages.keys()
    assert run.call_args.kwargs["capture_output"] is True


def test_cached_translation_model_never_pulls_or_runs_inference(capsys):
    with (
        mock.patch.object(
            bootstrap, "local_json", return_value={"models": [{"name": bootstrap.TRANSLATOR}]}
        ) as local_json,
        mock.patch.object(
            bootstrap,
            "local_urlopen",
            side_effect=AssertionError("Unexpected model download"),
        ),
    ):
        bootstrap.pull_translation_model(bootstrap.Reporter())
    local_json.assert_called_once_with("/api/tags")
    assert any("reusing" in event.get("message", "") for event in events(capsys))


def test_translation_download_uses_pull_api_then_verifies_installed_tag(capsys):
    response = io.BytesIO(
        b'{"status":"pulling weights","total":100,"completed":50}\n{"status":"success"}\n'
    )
    with (
        mock.patch.object(
            bootstrap,
            "local_json",
            side_effect=[{"models": []}, {"models": [{"name": bootstrap.TRANSLATOR}]}],
        ) as local_json,
        mock.patch.object(bootstrap, "local_urlopen", return_value=response) as download,
    ):
        bootstrap.pull_translation_model(bootstrap.Reporter())
    request = download.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:11434/api/pull"
    assert request.method == "POST"
    assert json.loads(request.data) == {"name": bootstrap.TRANSLATOR, "stream": True}
    assert local_json.call_args_list == [mock.call("/api/tags"), mock.call("/api/tags")]
    progress = [event["progress"] for event in events(capsys)]
    assert progress == sorted(progress)
    assert all(0 <= item <= 1 for item in progress)


def test_incomplete_translation_download_produces_retry_instruction():
    with (
        mock.patch.object(bootstrap, "local_json", return_value={"models": []}),
        mock.patch.object(
            bootstrap, "local_urlopen", return_value=io.BytesIO(b'{"status":"success"}\n')
        ),
        pytest.raises(RuntimeError, match="Retry to resume"),
    ):
        bootstrap.pull_translation_model(bootstrap.Reporter())


def test_ollama_model_failure_preserves_actionable_reason():
    with (
        mock.patch.object(bootstrap, "local_json", return_value={"models": []}),
        mock.patch.object(
            bootstrap,
            "local_urlopen",
            return_value=io.BytesIO(b'{"error":"not enough disk space"}\n'),
        ),
        pytest.raises(RuntimeError, match="not enough disk space"),
    ):
        bootstrap.pull_translation_model(bootstrap.Reporter())


def test_reporter_progress_never_goes_backwards_and_log_stays_private(tmp_path, capsys):
    log = tmp_path / "logs/setup.log"
    report = bootstrap.Reporter(log)
    for value in (0.4, 0.2, -1, 1.5):
        report.stage("models", "Preparing models", value)
    actual = events(capsys)
    assert [event["progress"] for event in actual] == [0.4, 0.4, 0.4, 1.0]
    assert [json.loads(line) for line in log.read_text().splitlines()] == actual
    assert log.stat().st_mode & 0o777 == 0o600


def test_failed_subprocess_reports_log_and_tool_name(capsys):
    process = mock.Mock()
    process.stdout = io.StringIO('download failed\n{"type":"error","message":"retry connection"}\n')
    process.wait.return_value = 2
    process.poll.return_value = 2
    with (
        mock.patch.object(bootstrap.subprocess, "Popen", return_value=process),
        pytest.raises(RuntimeError, match="python failed.*installation log"),
    ):
        bootstrap.run(["/fixture/bin/python", "-m", "pip"], bootstrap.Reporter())
    emitted = events(capsys)
    assert emitted[0] == {"type": "log", "message": "download failed"}
    assert emitted[1] == {"type": "error", "message": "retry connection"}
    assert process.stdout.closed


def test_running_ollama_server_is_reused_without_launchagent_writes(tmp_path, capsys):
    support = tmp_path / "support"
    with (
        mock.patch.object(bootstrap, "local_json", return_value={"version": "fixture-version"}),
        mock.patch.object(
            bootstrap, "run", side_effect=AssertionError("Unexpected service mutation")
        ),
    ):
        bootstrap.ensure_ollama_server(tmp_path / "ollama", support, bootstrap.Reporter())
    assert not support.exists()
    assert any("running local Ollama" in event.get("message", "") for event in events(capsys))


@pytest.mark.parametrize(
    "system,machine,mac_version", [("Linux", "x86_64", ""), ("Darwin", "arm64", "13.6")]
)
def test_unsupported_platform_fails_before_any_installation(
    tmp_path, monkeypatch, system, machine, mac_version
):
    monkeypatch.setattr(bootstrap.platform, "system", lambda: system)
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: machine)
    monkeypatch.setattr(bootstrap.platform, "mac_ver", lambda: (mac_version, ("", "", ""), machine))
    monkeypatch.setattr(bootstrap.Path, "home", classmethod(lambda cls: tmp_path))
    with (
        mock.patch.object(
            bootstrap,
            "ensure_iina",
            side_effect=AssertionError("Unexpected dependency installation"),
        ),
        pytest.raises(RuntimeError, match="macOS 14"),
    ):
        bootstrap.install(bootstrap.Reporter())
    assert list(tmp_path.iterdir()) == []


def test_dry_run_does_not_install_or_write_private_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap.py", "--dry-run"])
    monkeypatch.setattr(bootstrap.Path, "home", classmethod(lambda cls: tmp_path))
    with mock.patch.object(
        bootstrap, "install", side_effect=AssertionError("Dry run attempted installation")
    ):
        assert bootstrap.main() == 0
    assert list(tmp_path.iterdir()) == []
    assert events(capsys)[-1]["type"] == "complete"


def test_main_returns_an_actionable_error_event(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap.py"])
    monkeypatch.setattr(bootstrap.Path, "home", classmethod(lambda cls: tmp_path))
    with (
        mock.patch.object(bootstrap.signal, "signal"),
        mock.patch.object(
            bootstrap,
            "install",
            side_effect=RuntimeError("Free at least 16 GB of disk space, then retry."),
        ),
    ):
        assert bootstrap.main() == 1
    assert events(capsys)[-1] == {
        "type": "error",
        "message": "Free at least 16 GB of disk space, then retry.",
    }


def test_failed_venv_creation_restores_previous_runtime_even_when_target_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bootstrap.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(bootstrap.platform, "mac_ver", lambda: ("15.0", ("", "", ""), "arm64"))
    monkeypatch.setenv("PATH", "/fixture/bin")
    monkeypatch.setenv("LT_FFMPEG", "/fixture/previous-ffmpeg")
    support = tmp_path / "Library/Application Support/LiveTranslate"
    environment = support / "project/.venv"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").write_text("previous interpreter fixture")
    (environment / "keep.txt").write_text("working previous environment")
    runtime_tree(support / "project")
    agent = tmp_path / "Library/LaunchAgents" / (bootstrap.BRIDGE_LABEL + ".plist")
    agent.parent.mkdir(parents=True)
    agent.write_bytes(b"previous agent fixture")

    def fail_venv(*args, **kwargs):
        marker = support / "project/runtime-revision.json"
        assert json.loads(marker.read_text())["state"] == "updating"
        assert not environment.exists()
        raise RuntimeError("venv creation fixture failed")

    with (
        mock.patch.object(
            bootstrap.shutil, "disk_usage", return_value=mock.Mock(free=100 * 1024**3)
        ),
        mock.patch.object(bootstrap, "whisper_ready", return_value=True),
        mock.patch.object(bootstrap, "runtime_ready", return_value=False),
        mock.patch.object(bootstrap, "ensure_iina", return_value=tmp_path / "IINA.app"),
        mock.patch.object(bootstrap, "ensure_ffmpeg", return_value=tmp_path / "tools/ffmpeg"),
        mock.patch.object(bootstrap, "ensure_ollama", return_value=tmp_path / "tools/ollama"),
        mock.patch.object(bootstrap, "ensure_ollama_server"),
        mock.patch.object(bootstrap.subprocess, "run", return_value=mock.Mock(returncode=0)),
        mock.patch.object(bootstrap, "start_agent") as start_agent,
        mock.patch.object(bootstrap, "run", side_effect=fail_venv) as run,
        pytest.raises(RuntimeError, match="venv creation fixture failed"),
    ):
        bootstrap.install(bootstrap.Reporter())
    assert run.call_args.args[0][1:3] == ["-m", "venv"]
    assert (environment / "keep.txt").read_text() == "working previous environment"
    assert (environment / "bin/python").read_text() == "previous interpreter fixture"
    marker = json.loads((support / "project/runtime-revision.json").read_text())
    assert marker == {
        "schema": 1,
        "state": "ready",
        "revision": runtime_revision(support / "project"),
    }
    start_agent.assert_called_once_with(agent, bootstrap.BRIDGE_LABEL)


@pytest.fixture
def mocked_whisper_download(monkeypatch):
    """A Hub stub that exercises progress without touching TLS or real weights."""
    snapshot = mock.Mock()
    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = snapshot
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    class Tqdm:
        def __init__(self, *args, **kwargs):
            self.total = kwargs.get("total")
            self.desc = kwargs.get("desc", "")
            self.n = 0

        def update(self, n=1):
            self.n += n

    tqdm = types.ModuleType("tqdm")
    auto = types.ModuleType("tqdm.auto")
    auto.tqdm = Tqdm
    tqdm.auto = auto
    monkeypatch.setitem(sys.modules, "tqdm", tqdm)
    monkeypatch.setitem(sys.modules, "tqdm.auto", auto)
    return snapshot


def test_whisper_download_leaves_native_tls_context_unpatched(
    mocked_whisper_download, monkeypatch, capsys
):
    # Hub 2.4/httpcore2 already uses the installed truststore on Darwin.
    # A second, pip-vendored global wrapper was the cold-install regression.
    vendor = types.ModuleType("pip._vendor")
    vendor.truststore = mock.Mock()
    vendor.truststore.inject_into_ssl.side_effect = AssertionError(
        "A second global TLS wrapper must not be installed"
    )
    monkeypatch.setitem(sys.modules, "pip._vendor", vendor)
    original_context = ssl.SSLContext
    monkeypatch.setattr(bootstrap, "whisper_ready", lambda: True)

    def download(*args, **kwargs):
        progress = kwargs["tqdm_class"](total=1_000_002, desc="Verified model weights")
        progress.update(500_001)

    mocked_whisper_download.side_effect = download
    bootstrap.download_whisper(bootstrap.Reporter())
    assert ssl.SSLContext is original_context
    vendor.truststore.inject_into_ssl.assert_not_called()
    assert mocked_whisper_download.call_args.args == (bootstrap.WHISPER,)
    assert mocked_whisper_download.call_args.kwargs["allow_patterns"] == [
        "config.json",
        "*.safetensors",
        "weights.npz",
    ]
    assert events(capsys)[0]["stage"] == "speech-model"


def test_hub_returning_without_complete_whisper_files_is_not_success(
    mocked_whisper_download, monkeypatch
):
    monkeypatch.setattr(bootstrap, "whisper_ready", lambda: False)
    with pytest.raises(RuntimeError, match="Whisper download is incomplete"):
        bootstrap.download_whisper(bootstrap.Reporter())


def test_whisper_cancellation_remains_distinct_from_download_failure(
    mocked_whisper_download, monkeypatch
):
    mocked_whisper_download.side_effect = bootstrap.InstallationCancelled("stopped")
    ready = mock.Mock()
    monkeypatch.setattr(bootstrap, "whisper_ready", ready)
    with pytest.raises(bootstrap.InstallationCancelled, match="stopped"):
        bootstrap.download_whisper(bootstrap.Reporter())
    ready.assert_not_called()


def test_sigterm_handler_returns_cancel_status_without_failed_download_event(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap.py"])
    monkeypatch.setattr(bootstrap.Path, "home", classmethod(lambda cls: tmp_path))
    signal_registration = mock.Mock()
    monkeypatch.setattr(bootstrap.signal, "signal", signal_registration)

    def stop(report):
        handler = signal_registration.call_args.args[1]
        handler(bootstrap.signal.SIGTERM, None)

    monkeypatch.setattr(bootstrap, "install", stop)
    assert bootstrap.main() == 130
    signal_registration.assert_called_once()
    emitted = events(capsys)
    assert emitted[-1]["type"] == "log"
    assert "cancelled" in emitted[-1]["message"]


def test_network_redirect_failure_uses_verified_fallback_without_leaking_signed_url(
    capsys, tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    response = io.BytesIO(
        b'{"error":"redirect target not allowed: https://vendor.example/data?Signature=SECRET resolves to non-public address"}\n'
    )
    with (
        mock.patch.object(
            bootstrap,
            "local_json",
            side_effect=[{"models": []}, {"models": [{"name": bootstrap.TRANSLATOR}]}],
        ),
        mock.patch.object(bootstrap, "local_urlopen", return_value=response),
        mock.patch.object(bootstrap, "download_ollama_model") as fallback,
    ):
        report = bootstrap.Reporter()
        bootstrap.pull_translation_model(report)
    fallback.assert_called_once_with(bootstrap.TRANSLATOR, tmp_path / ".ollama/models", report)
    assert "SECRET" not in capsys.readouterr().out


def test_cancelled_native_model_download_does_not_start_fallback():
    with (
        mock.patch.object(bootstrap, "local_json", return_value={"models": []}),
        mock.patch.object(
            bootstrap, "native_model_pull", side_effect=bootstrap.InstallationCancelled("stop")
        ),
        mock.patch.object(bootstrap, "download_ollama_model") as fallback,
        pytest.raises(bootstrap.InstallationCancelled),
    ):
        bootstrap.pull_translation_model(bootstrap.Reporter())
    fallback.assert_not_called()
