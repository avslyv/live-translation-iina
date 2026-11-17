"""Official model downloads: host boundaries, exact ranges and atomic commits."""

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from unittest import mock

import pytest

from live_translation import model_download as download

CDN = "dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com"


def artifact(data):
    return {"digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data)}


def manifest(data):
    config = b'{"architecture":"fixture"}'
    model, settings = artifact(data), artifact(config)
    raw = json.dumps({"schemaVersion": 2, "layers": [model], "config": settings}).encode()
    return raw, [model, settings], {model["digest"]: data, settings["digest"]: config}


def fake_curl(contents, *, status=206, called=None):
    def run(command, **kwargs):
        assert "--location" not in command and "-L" not in command
        first, last = map(int, command[command.index("--range") + 1].split("-"))
        payload = contents[command[-1].rsplit("/", 1)[-1]]
        if called is not None:
            called.append((first, last))
        Path(command[command.index("--output") + 1]).write_bytes(payload[first : last + 1])
        Path(command[command.index("--dump-header") + 1]).write_text(
            f"HTTP/2 {status}\r\nContent-Range: bytes {first}-{last}/{len(payload)}\r\n\r\n"
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    return run


def redirected(url):
    return url.replace(download.REGISTRY, CDN)


@pytest.mark.parametrize(
    "url",
    [
        "http://registry.ollama.ai/v2/library/model",
        "https://127.0.0.1/model",
        "https://registry.ollama.ai.evil.invalid/model",
        "https://other.r2.cloudflarestorage.com/model",
        "https://user:password@registry.ollama.ai/model",
        "https://registry.ollama.ai:8443/model",
        "https://registry.ollama.ai/model#fragment",
    ],
)
def test_unapproved_url_is_rejected_before_network_access(url):
    with (
        mock.patch.object(download.subprocess, "run") as request,
        pytest.raises(RuntimeError, match="unapproved"),
    ):
        download._resolve_url(url)
    request.assert_not_called()


def test_signed_get_redirect_is_validated_using_a_bounded_registry_get_only():
    signed = f"https://{CDN}/objects/blob?X-Amz-Signature=private-signature"
    response = subprocess.CompletedProcess([], 0, f"HTTP/2 307\r\nLocation: {signed}\r\n\r\n", "")
    with mock.patch.object(download.subprocess, "run", return_value=response) as request:
        assert (
            download._resolve_url(
                "https://registry.ollama.ai/v2/library/model/blobs/sha256:fixture"
            )
            == signed
        )
    assert request.call_count == 1
    assert "--head" not in request.call_args.args[0]
    assert request.call_args.args[0][request.call_args.args[0].index("--max-filesize") + 1] == "1"
    assert "--location" not in request.call_args.args[0]


def test_registry_redirect_to_another_host_is_blocked_before_following_it():
    response = subprocess.CompletedProcess(
        [], 0, "HTTP/2 307\r\nLocation: https://localhost/private\r\n\r\n", ""
    )
    with (
        mock.patch.object(download.subprocess, "run", return_value=response) as request,
        pytest.raises(RuntimeError, match="unapproved"),
    ):
        download._resolve_url("https://registry.ollama.ai/v2/library/model/blobs/sha256:fixture")
    assert request.call_count == 1


def test_signed_urls_do_not_appear_in_network_errors():
    response = subprocess.CompletedProcess([], 7, "", "https://private.invalid/?secret=do-not-log")
    with (
        mock.patch.object(download.subprocess, "run", return_value=response),
        pytest.raises(RuntimeError, match="curl 7") as raised,
    ):
        download._resolve_url("https://registry.ollama.ai/v2/library/model/manifests/tag")
    assert "secret" not in str(raised.value)


def test_curl_cannot_inherit_insecure_or_redirect_settings_from_user_configuration():
    command = download._curl()
    assert command[:2] == ["/usr/bin/curl", "--disable"]
    assert "--insecure" not in command and "-k" not in command
    assert command[command.index("--proto") + 1] == "=https"


@pytest.mark.parametrize(
    "name",
    [
        "../../file",
        "https://evil.invalid/model",
        "evil.invalid/ns/model",
        "name:../../file",
        "name:tag:extra",
    ],
)
def test_model_names_cannot_select_external_urls_or_escape_the_store(tmp_path, name):
    with mock.patch.object(download.subprocess, "run") as request, pytest.raises(ValueError):
        download.download_ollama_model(name, tmp_path, lambda _: None)
    request.assert_not_called()


def test_manifest_requires_valid_artifact_digests_sizes_and_configuration():
    head = subprocess.CompletedProcess([], 0, "HTTP/2 200\r\n\r\n", "")
    bad = subprocess.CompletedProcess(
        [], 0, b'{"layers":[{"digest":"https://evil.invalid/weights","size":4}],"config":{}}', b""
    )
    with (
        mock.patch.object(download.subprocess, "run", side_effect=[head, bad]),
        pytest.raises(RuntimeError, match="manifest is invalid"),
    ):
        download._manifest("https://registry.ollama.ai/v2/library/model/manifests/tag")


def test_parallel_download_verifies_every_blob_and_commits_exact_manifest_last(
    tmp_path, monkeypatch
):
    raw, artifacts, contents = manifest(b"fixture-model-weights")
    events = []
    monkeypatch.setattr(download, "CHUNK_BYTES", 4)
    with (
        mock.patch.object(download, "_manifest", return_value=(raw, artifacts)),
        mock.patch.object(download, "_resolve_url", side_effect=redirected),
        mock.patch.object(download.subprocess, "run", side_effect=fake_curl(contents)),
    ):
        result = download.download_ollama_model("gemma4:12b", tmp_path, events.append)
    assert result == tmp_path / "manifests/registry.ollama.ai/library/gemma4/12b"
    assert result.read_bytes() == raw
    for item in artifacts:
        blob = tmp_path / "blobs" / item["digest"].replace(":", "-")
        assert blob.read_bytes() == contents[item["digest"]]
        assert blob.stat().st_mode & 0o777 == 0o600
    progress = [event["progress"] for event in events if "progress" in event]
    assert progress == sorted(progress)
    assert progress[-1] == pytest.approx(0.7)
    assert all("https://" not in json.dumps(event) for event in events)


def test_verified_blobs_are_reused_without_any_data_request(tmp_path):
    raw, artifacts, contents = manifest(b"already present")
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    for item in artifacts:
        (blobs / item["digest"].replace(":", "-")).write_bytes(contents[item["digest"]])
    before = {path: path.stat().st_mtime_ns for path in blobs.iterdir()}
    with (
        mock.patch.object(download, "_manifest", return_value=(raw, artifacts)),
        mock.patch.object(
            download, "_resolve_url", side_effect=AssertionError("Unexpected data request")
        ),
    ):
        download.download_ollama_model("gemma4:12b", tmp_path, lambda _: None)
    assert before == {path: path.stat().st_mtime_ns for path in blobs.iterdir()}


def test_wrong_range_never_commits_or_overwrites_an_existing_manifest(tmp_path, monkeypatch):
    raw, artifacts, contents = manifest(b"model-weights")
    existing = tmp_path / "manifests/registry.ollama.ai/library/gemma4/12b"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"previous manifest")
    monkeypatch.setattr(download, "CHUNK_BYTES", 4)
    monkeypatch.setattr(download, "MAX_ATTEMPTS", 1)
    with (
        mock.patch.object(download, "_manifest", return_value=(raw, artifacts)),
        mock.patch.object(download, "_resolve_url", side_effect=redirected),
        mock.patch.object(download.subprocess, "run", side_effect=fake_curl(contents, status=200)),
        mock.patch.object(threading.Event, "wait", return_value=False),
        pytest.raises(RuntimeError, match="range download failed"),
    ):
        download.download_ollama_model("gemma4:12b", tmp_path, lambda _: None)
    assert existing.read_bytes() == b"previous manifest"
    assert not list((tmp_path / "blobs").glob("sha256-*")) or all(
        path.stat().st_size == 0 for path in (tmp_path / "blobs").iterdir()
    )


def test_complete_small_blob_accepts_http_200_only_with_full_size_and_hash(tmp_path):
    raw, artifacts, contents = manifest(b"small weights")
    with (
        mock.patch.object(download, "_manifest", return_value=(raw, artifacts)),
        mock.patch.object(download, "_resolve_url", side_effect=redirected),
        mock.patch.object(download.subprocess, "run", side_effect=fake_curl(contents, status=200)),
    ):
        result = download.download_ollama_model("gemma4:12b", tmp_path, lambda _: None)
    assert result.read_bytes() == raw


def test_completed_ranges_survive_a_failure_and_are_not_fetched_again(tmp_path, monkeypatch):
    data = b"abcdefghijkl"
    item = artifact(data)
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    calls = []
    real_download = fake_curl({item["digest"]: data}, called=calls)

    def interrupted(command, **kwargs):
        if command[command.index("--range") + 1] == "4-7":
            return subprocess.CompletedProcess(command, 7, b"", b"")
        return real_download(command, **kwargs)

    monkeypatch.setattr(download, "CHUNK_BYTES", 4)
    monkeypatch.setattr(download, "WORKERS", 1)
    monkeypatch.setattr(download, "MAX_ATTEMPTS", 1)
    with (
        mock.patch.object(download, "_resolve_url", side_effect=redirected),
        mock.patch.object(download.subprocess, "run", side_effect=interrupted),
        mock.patch.object(threading.Event, "wait", return_value=False),
        pytest.raises(RuntimeError),
    ):
        download._fetch_blob(
            "https://registry.ollama.ai/v2/library/gemma4",
            item,
            blobs,
            work,
            lambda _: None,
            threading.Event(),
        )
    saved = json.loads((work / item["digest"].split(":")[1] / "progress.json").read_text())
    assert 0 in saved["completed"]
    calls.clear()
    with (
        mock.patch.object(download, "_resolve_url", side_effect=redirected),
        mock.patch.object(download.subprocess, "run", side_effect=real_download),
    ):
        download._fetch_blob(
            "https://registry.ollama.ai/v2/library/gemma4",
            item,
            blobs,
            work,
            lambda _: None,
            threading.Event(),
        )
    assert (0, 3) not in calls
    assert (blobs / item["digest"].replace(":", "-")).read_bytes() == data


def test_partial_range_bytes_resume_only_after_a_valid_content_range(tmp_path):
    calls = []

    def interrupted_then_resumed(command, **kwargs):
        requested = command[command.index("--range") + 1]
        calls.append(requested)
        first, last = map(int, requested.split("-"))
        body = b"ab" if first == 0 else b"cd"
        Path(command[command.index("--output") + 1]).write_bytes(body)
        Path(command[command.index("--dump-header") + 1]).write_text(
            f"HTTP/2 206\r\nContent-Range: bytes {first}-{last}/12\r\n\r\n"
        )
        return subprocess.CompletedProcess(command, 18 if first == 0 else 0, b"", b"")

    with (
        mock.patch.object(download.subprocess, "run", side_effect=interrupted_then_resumed),
        mock.patch.object(threading.Event, "wait", return_value=False),
    ):
        result = download._download_range(
            f"https://{CDN}/blob", 0, 3, 12, tmp_path, threading.Event()
        )
    assert result.read_bytes() == b"abcd"
    assert calls == ["0-3", "2-3"]


def test_digest_mismatch_never_promotes_unverified_bytes(tmp_path):
    raw, artifacts, contents = manifest(b"expected")
    contents[artifacts[0]["digest"]] = b"unwanted"
    with (
        mock.patch.object(download, "_manifest", return_value=(raw, artifacts)),
        mock.patch.object(download, "_resolve_url", side_effect=redirected),
        mock.patch.object(download.subprocess, "run", side_effect=fake_curl(contents)),
        pytest.raises(RuntimeError, match="checksum"),
    ):
        download.download_ollama_model("gemma4:12b", tmp_path, lambda _: None)
    assert not (tmp_path / "manifests/registry.ollama.ai/library/gemma4/12b").exists()
    assert not list((tmp_path / "blobs").iterdir())


def test_main_thread_cancellation_sets_worker_stop_before_joining_the_pool(tmp_path):
    class Cancelled(BaseException):
        pass

    blobs = tmp_path / "blobs"
    work = tmp_path / "work"
    blobs.mkdir()
    work.mkdir()
    stop = threading.Event()
    pool = mock.Mock()
    pool.shutdown.side_effect = lambda **_: (
        None if stop.is_set() else pytest.fail("Joined workers before cancellation")
    )
    with (
        mock.patch.object(download, "_resolve_url", side_effect=redirected),
        mock.patch.object(download.concurrent.futures, "ThreadPoolExecutor", return_value=pool),
        mock.patch.object(download.concurrent.futures, "as_completed", side_effect=Cancelled),
        pytest.raises(Cancelled),
    ):
        download._fetch_blob(
            "https://registry.ollama.ai/v2/library/gemma4",
            artifact(b"model"),
            blobs,
            work,
            lambda _: None,
            stop,
        )
    assert stop.is_set()
    pool.shutdown.assert_called_once_with(wait=True, cancel_futures=True)


def test_cancelled_worker_never_starts_another_curl_attempt(tmp_path):
    stop = threading.Event()
    stop.set()
    with (
        mock.patch.object(download.subprocess, "run") as request,
        pytest.raises(InterruptedError, match="cancelled"),
    ):
        download._download_range(f"https://{CDN}/blob", 0, 3, 4, tmp_path, stop)
    request.assert_not_called()
