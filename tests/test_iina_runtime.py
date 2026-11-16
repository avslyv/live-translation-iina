"""Portable dependency discovery and actionable service failures, without models."""

import http.client
import io
import json
import threading
from unittest import mock

import pytest

from iina_bridge import ollama_version, resolve_executable, server_for


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


def test_ollama_fingerprints_the_running_server_not_an_app_bundle():
    with mock.patch(
        "iina_bridge.urllib.request.urlopen", return_value=io.BytesIO(b'{"version":"1.2.3"}')
    ) as request:
        assert ollama_version() == "1.2.3"
    request.assert_called_once_with("http://127.0.0.1:11434/api/version", timeout=5)


@pytest.mark.parametrize("response", [b"{}", b"[]", b"not-json", b'{"version":null}'])
def test_invalid_ollama_version_is_actionable(response):
    with (
        mock.patch("iina_bridge.urllib.request.urlopen", return_value=io.BytesIO(response)),
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
