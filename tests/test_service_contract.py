"""A listening legacy worker is not sufficient proof of a successful update."""

import pytest

from live_translation.service_contract import (
    SERVICE_NAME,
    compatible_health,
    service_identity,
)


def health(runtime_id="a" * 64):
    return {"status": "ok", "service": SERVICE_NAME, **service_identity(runtime_id)}


def test_expected_loaded_runtime_is_required_after_update():
    assert compatible_health(health(), "a" * 64)
    assert not compatible_health(health(), "b" * 64)
    assert compatible_health(health())


@pytest.mark.parametrize(
    "change",
    [
        {"status": "starting"},
        {"service": "another-app"},
        {"protocol": True},
        {"protocol": 0},
        {"version": ""},
        {"runtime_id": "legacy"},
        {"process_id": 0},
        {"process_id": True},
        {"process_id": None},
        {"capabilities": ["state_form", "native_srt"]},
        {"capabilities": "state_form,native_srt,auto_restart"},
    ],
)
def test_incomplete_or_unrelated_health_is_rejected(change):
    assert not compatible_health({**health(), **change})


def test_original_protocol_only_health_does_not_pass_update_verification():
    assert not compatible_health({"status": "ok", "service": SERVICE_NAME, "protocol": 1})


@pytest.mark.parametrize("body", [None, [], "ok", 200])
def test_invalid_health_shape_is_rejected(body):
    assert not compatible_health(body)


def test_identity_excludes_private_paths_and_authentication():
    identity = service_identity("a" * 64)
    assert set(identity) == {"version", "protocol", "runtime_id", "process_id", "capabilities"}
    with pytest.raises(ValueError):
        service_identity("not-a-content-digest")


def test_equal_source_revisions_can_distinguish_fresh_worker_processes():
    old = service_identity("a" * 64, process_id=100)
    new = service_identity("a" * 64, process_id=200)
    assert old["runtime_id"] == new["runtime_id"]
    assert old["process_id"] != new["process_id"]
