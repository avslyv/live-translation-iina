"""Runtime changes must restart only after a complete, stable update."""

import json
import os
import shutil
import threading
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest

from live_translation import runtime_lifecycle as lifecycle


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "runtime"
    (root / "live_translation").mkdir(parents=True)
    (root / "iina_bridge.py").write_text("BUILD = 'old'\n")
    (root / "live_translation/__init__.py").write_text("")
    (root / "live_translation/service.py").write_text("def translate(): return 'old'\n")
    (root / "requirements-iina.txt").write_text("mlx-whisper==0.4.3\n")
    return root


def change(runtime, value="new"):
    (runtime / "iina_bridge.py").write_text(f"BUILD = {value!r}\n")


def test_identity_is_content_based_and_ignores_private_state(runtime):
    first = lifecycle.runtime_revision(runtime)
    for name, content in [
        ("iina-token", "a private secret"),
        ("subtitles/video.json", "cached translation"),
        (".venv/lib/package.py", "irrelevant = True"),
        (".git/config", "private remote"),
        ("live_translation/__pycache__/ignored.py", "ignored = True"),
    ]:
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    lifecycle.finish_runtime_update(runtime)
    os.utime(runtime / "iina_bridge.py", (1, 1))
    assert lifecycle.runtime_revision(runtime) == first
    change(runtime)
    assert lifecycle.runtime_revision(runtime) != first


def test_identity_matches_a_copy_and_includes_added_library_and_dependency_files(runtime, tmp_path):
    other = tmp_path / "other"
    shutil.copytree(runtime, other)
    assert lifecycle.runtime_revision(runtime) == lifecycle.runtime_revision(other)
    (runtime / "live_translation/service_contract.py").write_text("VERSION = '1.3.2'\n")
    assert lifecycle.runtime_revision(runtime) != lifecycle.runtime_revision(other)
    shutil.copytree(runtime, other, dirs_exist_ok=True)
    (runtime / "requirements-iina.lock.txt").write_text("some-package==1.0\n")
    assert lifecycle.runtime_revision(runtime) != lifecycle.runtime_revision(other)


def test_debounces_every_change_and_notifies_once(runtime):
    calls = []
    watcher = lifecycle.RuntimeWatcher(runtime, calls.append)
    change(runtime)
    assert not watcher.poll(now=0)
    assert not watcher.poll(now=1.9)
    change(runtime, "newer")
    assert not watcher.poll(now=2)
    assert not watcher.poll(now=3.9)
    assert watcher.poll(now=4)
    assert calls == [lifecycle.runtime_revision(runtime)]
    change(runtime, "newest")
    assert not watcher.poll(now=10)
    assert len(calls) == 1


def test_reverting_before_debounce_does_not_restart(runtime):
    calls = []
    watcher = lifecycle.RuntimeWatcher(runtime, calls.append)
    change(runtime)
    watcher.poll(now=0)
    change(runtime, "old")
    assert not watcher.poll(now=10)
    assert not calls


def test_installer_transaction_waits_for_complete_matching_ready_marker(runtime):
    lifecycle.finish_runtime_update(runtime)
    calls = []
    watcher = lifecycle.RuntimeWatcher(runtime, calls.append)
    lifecycle.begin_runtime_update(runtime)
    change(runtime)
    assert not watcher.poll(now=0)
    assert not watcher.poll(now=10)
    marker = runtime / lifecycle.REVISION_MARKER
    marker.write_text(json.dumps({"schema": 1, "state": "ready", "revision": "0" * 64}))
    assert not watcher.poll(now=20)
    assert not watcher.poll(now=30)
    revision = lifecycle.finish_runtime_update(runtime)
    assert not watcher.poll(now=40)
    assert watcher.poll(now=42)
    assert calls == [revision]


def test_unchanged_ready_marker_allows_later_direct_source_updates(runtime):
    lifecycle.finish_runtime_update(runtime)
    calls = []
    watcher = lifecycle.RuntimeWatcher(runtime, calls.append)
    change(runtime)
    assert not watcher.poll(now=0)
    assert watcher.poll(now=2)
    assert calls == [lifecycle.runtime_revision(runtime)]


def test_noop_installer_transaction_does_not_disable_later_direct_updates(runtime):
    lifecycle.finish_runtime_update(runtime)
    calls = []
    watcher = lifecycle.RuntimeWatcher(runtime, calls.append)
    lifecycle.begin_runtime_update(runtime)
    assert not watcher.poll(now=0)
    lifecycle.finish_runtime_update(runtime)
    assert not watcher.poll(now=5)
    change(runtime)
    assert not watcher.poll(now=10)
    assert watcher.poll(now=12)
    assert calls == [lifecycle.runtime_revision(runtime)]


@pytest.mark.parametrize(
    "damage",
    ["invalid_python", "missing_worker", "unreadable_source", "invalid_marker", "changing_source"],
)
def test_incomplete_updates_keep_worker_alive_and_can_recover(runtime, damage):
    calls = []
    watcher = lifecycle.RuntimeWatcher(runtime, calls.append)
    change(runtime)
    if damage == "invalid_python":
        (runtime / "live_translation/service.py").write_text("def unfinished(\n")
    elif damage == "missing_worker":
        (runtime / "iina_bridge.py").unlink()
    elif damage == "invalid_marker":
        (runtime / lifecycle.REVISION_MARKER).write_text("{partial")
    if damage == "unreadable_source":
        patch = mock.patch.object(Path, "read_bytes", side_effect=PermissionError("updating"))
    elif damage == "changing_source":
        patch = mock.patch.object(lifecycle, "_snapshot", side_effect=OSError("concurrent update"))
    else:
        patch = nullcontext()
    with patch:
        assert not watcher.poll(now=0)
        assert not watcher.poll(now=10)
    assert not calls
    (runtime / "live_translation/service.py").write_text("def translate(): return 'new'\n")
    change(runtime)
    (runtime / lifecycle.REVISION_MARKER).unlink(missing_ok=True)
    assert not watcher.poll(now=20)
    assert watcher.poll(now=22)
    assert len(calls) == 1


def test_finish_never_publishes_ready_for_invalid_python(runtime):
    lifecycle.begin_runtime_update(runtime)
    (runtime / "iina_bridge.py").write_text("if (\n")
    with pytest.raises(SyntaxError):
        lifecycle.finish_runtime_update(runtime)
    assert json.loads((runtime / lifecycle.REVISION_MARKER).read_text())["state"] == "updating"


def test_recovery_client_waits_for_pending_or_damaged_marker(runtime):
    assert not lifecycle.runtime_update_pending(runtime)
    lifecycle.begin_runtime_update(runtime)
    assert lifecycle.runtime_update_pending(runtime)
    lifecycle.finish_runtime_update(runtime)
    assert not lifecycle.runtime_update_pending(runtime)
    (runtime / lifecycle.REVISION_MARKER).write_text("{partial")
    assert lifecycle.runtime_update_pending(runtime)


@pytest.mark.parametrize(
    "payload", [{"schema": 1, "state": []}, {"schema": True, "state": "ready"}]
)
def test_wrong_marker_field_types_do_not_kill_the_watcher(runtime, payload):
    calls = []
    watcher = lifecycle.RuntimeWatcher(runtime, calls.append)
    (runtime / lifecycle.REVISION_MARKER).write_text(json.dumps(payload))
    change(runtime)
    assert not watcher.poll(now=0)
    assert not watcher.poll(now=10)
    assert lifecycle.runtime_update_pending(runtime)
    assert not calls


def test_failed_ready_marker_commit_preserves_updating_state(runtime):
    lifecycle.begin_runtime_update(runtime)
    change(runtime)
    with (
        mock.patch.object(lifecycle.os, "replace", side_effect=PermissionError("read-only")),
        pytest.raises(PermissionError),
    ):
        lifecycle.finish_runtime_update(runtime)
    assert lifecycle.runtime_update_pending(runtime)
    assert json.loads((runtime / lifecycle.REVISION_MARKER).read_text())["state"] == "updating"
    assert not list(runtime.glob(".runtime-revision-*.tmp"))


def test_files_changing_during_hash_cannot_be_committed_as_ready(runtime):
    lifecycle.begin_runtime_update(runtime)
    original_read = Path.read_bytes
    changed = False

    def read_while_updating(path):
        nonlocal changed
        result = original_read(path)
        if path.name == "iina_bridge.py" and not changed:
            changed = True
            (runtime / "live_translation/service.py").write_text("VALUE = 'concurrent update'\n")
        return result

    with (
        mock.patch.object(Path, "read_bytes", read_while_updating),
        pytest.raises(OSError, match="changed during inspection"),
    ):
        lifecycle.finish_runtime_update(runtime)
    assert lifecycle.runtime_update_pending(runtime)


def test_unchanged_source_is_not_rehashed_on_every_poll(runtime):
    watcher = lifecycle.RuntimeWatcher(runtime, lambda _: None)
    watcher.poll(now=0)
    with mock.patch.object(lifecycle, "_snapshot", side_effect=AssertionError("unnecessary read")):
        assert not watcher.poll(now=1)
        assert not watcher.poll(now=2)


def test_background_watch_shuts_down_once_and_close_wakes_sleep(runtime):
    notified = threading.Event()
    calls = []

    def on_change(revision):
        calls.append(revision)
        notified.set()

    watcher = lifecycle.RuntimeWatcher(runtime, on_change, poll_interval=0.01, settle_seconds=0.02)
    try:
        watcher.start()
        change(runtime)
        assert notified.wait(timeout=2)
        assert calls == [lifecycle.runtime_revision(runtime)]
    finally:
        watcher.close()
    assert watcher._thread is not None and not watcher._thread.is_alive()
    assert not watcher.poll()


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_bad_poll_interval_is_rejected(runtime, value):
    with pytest.raises(ValueError, match="poll_interval"):
        lifecycle.RuntimeWatcher(runtime, lambda _: None, poll_interval=value)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_bad_settling_interval_is_rejected(runtime, value):
    with pytest.raises(ValueError, match="settle_seconds"):
        lifecycle.RuntimeWatcher(runtime, lambda _: None, settle_seconds=value)
