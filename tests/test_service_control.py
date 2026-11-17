"""Service readiness uses loaded source identity; no real launchd or HTTP calls."""

import io
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from live_translation import service_control as control
from live_translation.runtime_config import load_runtime_config
from live_translation.runtime_lifecycle import begin_runtime_update, finish_runtime_update
from live_translation.service_contract import SERVICE_NAME, service_identity


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unit tests must not touch launchd or local HTTP services")

    monkeypatch.setattr(control.subprocess, "run", forbidden)
    monkeypatch.setattr(control, "local_urlopen", forbidden)


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "Runtime"
    (project / "live_translation").mkdir(parents=True)
    (project / "iina_bridge.py").write_text("# worker fixture\n")
    (project / "live_translation/__init__.py").write_text("")
    (project / "requirements-iina.txt").write_text("# requirements fixture\n")
    return project


@pytest.fixture
def config(tmp_path):
    config = load_runtime_config(
        {
            "HOME": str(tmp_path / "Private Home"),
            "LT_INSTANCE_ID": "local.live-translation.unit-test",
            "LT_BRIDGE_PORT": "19143",
            "LT_OLLAMA_PORT": "19134",
        }
    )
    agent = config.home / "Library/LaunchAgents" / (config.bridge_label + ".plist")
    agent.parent.mkdir(parents=True)
    agent.write_text("registered fixture, never loaded")
    return config


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


def healthy(project):
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        **service_identity(control.runtime_revision(project)),
        "process_id": 200,
    }


def test_current_service_returns_immediately_without_restart_or_sleep(project, config):
    clock = Clock()
    body = healthy(project)
    probe, restart, sleep = mock.Mock(return_value=body), mock.Mock(), mock.Mock()
    assert (
        control.ensure_service(
            project, config=config, probe=probe, restart=restart, clock=clock, sleep=sleep
        )
        == body
    )
    probe.assert_called_once()
    restart.assert_not_called()
    sleep.assert_not_called()


@pytest.mark.parametrize("stale_kind", ["legacy", "runtime", "protocol", "capabilities"])
def test_old_healthy_process_is_replaced_once_then_new_identity_is_awaited(
    project, config, stale_kind
):
    clock = Clock()
    current = healthy(project)
    old = dict(current)
    old["process_id"] = 100
    if stale_kind == "legacy":
        old = {"status": "ok", "service": SERVICE_NAME}
    elif stale_kind == "runtime":
        old["runtime_id"] = "0" * 64
    elif stale_kind == "protocol":
        old["protocol"] = 0
    else:
        old["capabilities"] = ["state_form", "native_srt"]
    probe = mock.Mock(side_effect=[old, old, current])
    restart = mock.Mock()
    assert (
        control.ensure_service(
            project, config=config, probe=probe, restart=restart, clock=clock, sleep=clock.sleep
        )
        == current
    )
    restart.assert_called_once_with(
        config.home / "Library/LaunchAgents" / (config.bridge_label + ".plist"),
        config.bridge_label,
    )
    assert probe.call_count == 3


def test_forced_installer_restart_requires_a_new_pid_even_with_identical_source(project, config):
    clock = Clock()
    current = healthy(project)
    old = {**current, "process_id": 100}
    probe = mock.Mock(side_effect=[old, old, current])
    restart = mock.Mock()
    assert (
        control.ensure_service(
            project,
            config=config,
            force_restart=True,
            probe=probe,
            restart=restart,
            clock=clock,
            sleep=clock.sleep,
        )
        == current
    )
    restart.assert_called_once()
    assert probe.call_count == 3


def test_forced_restart_never_accepts_draining_old_process(project, config):
    clock, restart = Clock(), mock.Mock()
    with pytest.raises(RuntimeError, match="did not load"):
        control.ensure_service(
            project,
            config=config,
            force_restart=True,
            timeout=0.5,
            probe=lambda remaining: healthy(project),
            restart=restart,
            clock=clock,
            sleep=clock.sleep,
        )
    restart.assert_called_once()


def test_missing_listener_bootstraps_registered_job_once(project, config):
    clock = Clock()
    probe = mock.Mock(side_effect=[None, None, healthy(project)])
    restart = mock.Mock()
    control.ensure_service(
        project, config=config, probe=probe, restart=restart, clock=clock, sleep=clock.sleep
    )
    restart.assert_called_once()


def test_persistently_old_listener_times_out_without_restart_loop(project, config):
    clock, restart = Clock(), mock.Mock()
    with pytest.raises(RuntimeError, match="did not load the installed runtime"):
        control.ensure_service(
            project,
            config=config,
            timeout=1,
            probe=lambda remaining: {"status": "ok"},
            restart=restart,
            clock=clock,
            sleep=clock.sleep,
        )
    assert clock.now == 1
    restart.assert_called_once()


def test_missing_agent_is_actionable_and_never_launches_another_instance(project, config):
    for agent in (config.home / "Library/LaunchAgents").iterdir():
        agent.unlink()
    restart = mock.Mock()
    with pytest.raises(RuntimeError, match="LaunchAgent is missing"):
        control.ensure_service(
            project, config=config, probe=lambda remaining: None, restart=restart
        )
    restart.assert_not_called()


def test_unfinished_update_waits_without_http_or_launchd(project, config):
    clock, probe, restart = Clock(), mock.Mock(), mock.Mock()
    begin_runtime_update(project)
    with pytest.raises(RuntimeError, match="did not load"):
        control.ensure_service(
            project,
            config=config,
            timeout=0.5,
            probe=probe,
            restart=restart,
            clock=clock,
            sleep=clock.sleep,
        )
    probe.assert_not_called()
    restart.assert_not_called()


def test_completed_update_is_checked_before_starting(project, config):
    clock = Clock()
    begin_runtime_update(project)
    restart = mock.Mock()

    def complete(delay):
        clock.sleep(delay)
        finish_runtime_update(project)

    control.ensure_service(
        project,
        config=config,
        probe=lambda remaining: healthy(project),
        restart=restart,
        clock=clock,
        sleep=complete,
    )
    assert clock.now == 0.2
    restart.assert_not_called()


def test_update_started_during_probe_blocks_restart_until_complete(project, config):
    clock = Clock()
    probes = 0

    def probe(remaining):
        nonlocal probes
        probes += 1
        if probes == 1:
            begin_runtime_update(project)
            (project / "iina_bridge.py").write_text("# updated worker\n")
            return None
        return None if probes == 2 else healthy(project)

    def complete(delay):
        clock.sleep(delay)
        finish_runtime_update(project)

    def restart_job(agent, label):
        assert not control.runtime_update_pending(project)
        assert clock.now > 0

    restart = mock.Mock(side_effect=restart_job)
    result = control.ensure_service(
        project,
        config=config,
        probe=probe,
        restart=restart,
        clock=clock,
        sleep=complete,
    )
    assert result == healthy(project)
    restart.assert_called_once()
    assert probes == 3


def test_runtime_changed_during_probe_is_not_restarted_under_old_installer_identity(
    project, config
):
    original = control.runtime_revision(project)

    def probe(remaining):
        (project / "iina_bridge.py").write_text("# another completed update\n")
        return None

    restart = mock.Mock()
    with pytest.raises(RuntimeError, match="runtime changed"):
        control.ensure_service(
            project,
            config=config,
            expected_runtime_id=original,
            probe=probe,
            restart=restart,
        )
    restart.assert_not_called()


def test_expected_installer_revision_cannot_be_satisfied_by_different_files(project, config):
    probe, restart = mock.Mock(), mock.Mock()
    with pytest.raises(RuntimeError, match="runtime changed"):
        control.ensure_service(
            project, config=config, expected_runtime_id="0" * 64, probe=probe, restart=restart
        )
    probe.assert_not_called()
    restart.assert_not_called()


def test_restart_errors_are_not_hidden_or_retried(project, config):
    restart = mock.Mock(side_effect=OSError("fixture bootstrap error"))
    with pytest.raises(OSError, match="fixture bootstrap error"):
        control.ensure_service(
            project, config=config, probe=lambda remaining: None, restart=restart
        )
    restart.assert_called_once()


def test_default_restart_loads_only_namespaced_agent_with_shared_deadline(project, config):
    clock = Clock()
    probe = mock.Mock(side_effect=[None, healthy(project)])
    with mock.patch.object(control, "start_agent") as start:
        control.ensure_service(project, config=config, probe=probe, clock=clock, sleep=clock.sleep)
    start.assert_called_once_with(
        config.home / "Library/LaunchAgents" / (config.bridge_label + ".plist"),
        config.bridge_label,
        deadline=30,
    )


def test_start_agent_handles_unloaded_job_and_bootstraps_once(config):
    missing = subprocess.CompletedProcess([], 3)
    ok = subprocess.CompletedProcess([], 0)
    with mock.patch.object(control.subprocess, "run", side_effect=[missing, ok, ok]) as process:
        control.start_agent(Path("/fixture/agent.plist"), config.bridge_label)
    commands = [call.args[0] for call in process.call_args_list]
    assert [command[1] for command in commands] == ["bootout", "enable", "bootstrap"]
    assert commands[0][2] == f"gui/{control.os.getuid()}/{config.bridge_label}"
    assert commands[-1][-1] == "/fixture/agent.plist"
    assert all(call.kwargs["timeout"] <= 10 for call in process.call_args_list)


def test_start_agent_deadline_prevents_launchctl_after_timeout():
    with (
        mock.patch.object(control.time, "monotonic", return_value=4),
        mock.patch.object(control.subprocess, "run") as process,
        pytest.raises(RuntimeError, match="Timed out"),
    ):
        control.start_agent(Path("/fixture/agent.plist"), "local.test.iina", deadline=3)
    process.assert_not_called()


def test_health_reads_only_explicit_private_loopback_origin(config):
    body = {"status": "ok"}
    with mock.patch.object(
        control, "local_urlopen", return_value=io.BytesIO(json.dumps(body).encode())
    ) as request:
        assert control.read_health(config, 0.25) == body
    request.assert_called_once_with("http://127.0.0.1:19143/health", timeout=0.25)


@pytest.mark.parametrize("response", [b"not json", b"{", b"\xff"])
def test_invalid_health_is_not_ready(config, response):
    with mock.patch.object(control, "local_urlopen", return_value=io.BytesIO(response)):
        assert control.read_health(config) is None


def test_connection_refusal_is_not_ready(config):
    with mock.patch.object(control, "local_urlopen", side_effect=OSError("refused fixture")):
        assert control.read_health(config) is None


@pytest.mark.parametrize("timeout,interval", [(0, 1), (float("inf"), 1), (1, 0)])
def test_invalid_wait_is_rejected_before_side_effects(project, config, timeout, interval):
    with pytest.raises(ValueError, match="wait settings"):
        control.ensure_service(project, config=config, timeout=timeout, interval=interval)


def test_cli_propagates_failure_without_opening_iina(capsys):
    with mock.patch.object(control, "ensure_service", side_effect=RuntimeError("fixture timeout")):
        assert control.main(["--ensure"]) == 1
    assert "fixture timeout" in capsys.readouterr().err
