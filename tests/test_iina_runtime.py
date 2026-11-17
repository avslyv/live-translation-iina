"""Portable dependency discovery and actionable service failures, without models."""

import http.client
import io
import json
import subprocess
import threading
from unittest import mock

import pytest

from iina_bridge import Bridge, ollama_version, resolve_executable, server_for


def test_ffmpeg_uses_path_instead_of_a_homebrew_prefix(monkeypatch):
    monkeypatch.delenv("LT_FFMPEG", raising=False)
    with mock.patch("iina_bridge.shutil.which", return_value="/custom/bin/ffmpeg") as which:
        assert resolve_executable("ffmpeg") == "/custom/bin/ffmpeg"
    which.assert_called_once_with("ffmpeg")


def test_configured_ffprobe_is_resolved_and_validated(monkeypatch):
    monkeypatch.setenv("LT_FFPROBE", "/custom/bin/ffprobe")
    with (
        mock.patch("iina_bridge.shutil.which", return_value=None) as which,
        pytest.raises(RuntimeError, match="LT_FFPROBE"),
    ):
        resolve_executable("ffprobe")
    which.assert_called_once_with("/custom/bin/ffprobe")


def test_probe_uses_ffmpeg_when_the_portable_runtime_has_no_ffprobe(monkeypatch, tmp_path):
    monkeypatch.delenv("LT_FFPROBE", raising=False)
    monkeypatch.delenv("LT_FFMPEG", raising=False)
    bridge = Bridge(tmp_path / "cache", start_worker=False, profile={"fixture": True})
    response = subprocess.CompletedProcess(
        [], 1, stdout=b"", stderr=b"Duration: 01:02:03.45, start: 0.0"
    )
    with (
        mock.patch("iina_bridge.shutil.which", side_effect=[None, "/portable/ffmpeg"]),
        mock.patch("iina_bridge.subprocess.run", return_value=response) as process,
    ):
        assert bridge._probe(tmp_path / "movie.mp4") == pytest.approx(3723.45)
    assert process.call_args.args[0][0] == "/portable/ffmpeg"
    assert process.call_args.kwargs["check"] is False
    bridge.close()


@pytest.mark.parametrize("detail", [b"Duration: N/A", b"Duration: 00:00:00.00", b"not media"])
def test_ffmpeg_duration_fallback_rejects_unusable_media(monkeypatch, tmp_path, detail):
    monkeypatch.delenv("LT_FFPROBE", raising=False)
    bridge = Bridge(tmp_path / "cache", start_worker=False, profile={"fixture": True})
    response = subprocess.CompletedProcess([], 1, stdout=b"", stderr=detail)
    with (
        mock.patch("iina_bridge.shutil.which", side_effect=[None, "/portable/ffmpeg"]),
        mock.patch("iina_bridge.subprocess.run", return_value=response),
        pytest.raises(ValueError, match="no usable duration"),
    ):
        bridge._probe(tmp_path / "movie.mp4")
    bridge.close()


def test_ollama_fingerprints_the_running_server_not_an_app_bundle():
    with mock.patch(
        "iina_bridge.local_urlopen", return_value=io.BytesIO(b'{"version":"1.2.3"}')
    ) as request:
        assert ollama_version() == "1.2.3"
    request.assert_called_once_with("http://127.0.0.1:11434/api/version", timeout=5)


@pytest.mark.parametrize("response", [b"{}", b"[]", b"not-json", b'{"version":null}'])
def test_invalid_ollama_version_is_actionable(response):
    with (
        mock.patch("iina_bridge.local_urlopen", return_value=io.BytesIO(response)),
        pytest.raises(RuntimeError, match="Start Ollama"),
    ):
        ollama_version()


def test_missing_models_return_a_structured_error_to_the_plugin():
    bridge = mock.Mock()
    bridge.update.side_effect = RuntimeError("Download the local Whisper model first")
    token = "test-token-only-for-http-fixture"
    server = server_for(bridge, token, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(
            "POST",
            "/state",
            body="path=/tmp/video.mp4&enabled=true",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {"error": "Download the local Whisper model first"}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
