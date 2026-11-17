"""Real loopback HTTP exercises native settings/cache routes without GPU models."""

from __future__ import annotations

import copy
import hashlib
import http.client
import io
import json
import os
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pytest

from iina_bridge import MAX_BODY_BYTES, Bridge, server_for

TOKEN = "native-control-private-fixture-token-123456"
RUNTIME_ID = "a" * 64


class TextFixtureBridge(Bridge):
    """Fake the container/model boundaries, retaining the production state machine."""

    def _probe(self, path):
        return 75.0

    def _decoder_version(self):
        return "fixture-ffmpeg"

    def _transcribe(self, audio):
        raise AssertionError("The native text path must never load a speech model")

    def _unload(self):
        self._whisper_resident = self._translation_resident = False

    def _release_whisper(self):
        self._whisper_resident = False

    def _release_translation(self, force=False):
        self._translation_resident = False


class LocalModelTransport:
    def __init__(self):
        self.online = True
        self.weights_available = True
        self.revisions = {"fixture:baseline": "first"}
        self.engine_version = "fixture-ollama"
        self.translations = []

    def identity(self, model):
        if not self.weights_available:
            raise RuntimeError("Local model weights are unavailable in this fixture")
        revision = self.revisions.setdefault(model, "first")
        return hashlib.sha256((model + revision).encode()).hexdigest()

    def version(self):
        if not self.online:
            raise RuntimeError("Ollama is stopped in this fixture")
        return self.engine_version

    def open(self, request, timeout=30):
        if not self.online:
            raise OSError("Ollama connection refused in this fixture")
        if isinstance(request, str):
            assert request == "http://127.0.0.1:19134/api/tags"
            body = {"models": [{"name": name, "size": 123} for name in sorted(self.revisions)]}
        else:
            assert request.full_url == "http://127.0.0.1:19134/api/chat"
            payload = json.loads(request.data)
            instruction = json.loads(payload["messages"][1]["content"])
            self.translations.append(payload)
            rows = [
                {"id": cue["id"], "translation": "Перевод: " + cue["source"]}
                for cue in instruction["cues"]
            ]
            body = {
                "done": True,
                "message": {"content": json.dumps({"translations": rows}, ensure_ascii=False)},
            }
        return io.BytesIO(json.dumps(body, ensure_ascii=False).encode())


@dataclass
class NativeAPI:
    bridge: TextFixtureBridge
    server: object
    thread: threading.Thread

    def request(
        self, method, path, data=None, *, form=False, authenticated=True, headers=None, raw=None
    ):
        request_headers = {}
        if authenticated:
            request_headers["Authorization"] = "Bearer " + TOKEN
        if raw is None and data is not None:
            if form:
                request_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
                raw = urlencode(
                    {
                        key: json.dumps(value, ensure_ascii=False)
                        if isinstance(value, (dict, list))
                        else value
                        for key, value in data.items()
                    }
                ).encode()
            else:
                request_headers["Content-Type"] = "application/json; charset=utf-8"
                raw = json.dumps(data, ensure_ascii=False).encode()
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=raw, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def control(self, action, data=None, **kwargs):
        return self.request("POST", "/control", {"action": action, "data": data or {}}, **kwargs)


@pytest.fixture
def native_http(tmp_path, monkeypatch):
    import live_translation.deterministic as deterministic
    import live_translation.iina_features as features
    import live_translation.translation_quality as quality

    home = tmp_path / "Private Home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CFFIXED_USER_HOME", str(home))
    monkeypatch.setenv("LT_INSTANCE_ID", "local.live-translation.native-control-test")
    monkeypatch.setenv("LT_OLLAMA_PORT", "19134")
    monkeypatch.setenv("LT_BRIDGE_PORT", "19143")
    monkeypatch.setenv("LT_TRANSLATION_MODEL", "fixture:baseline")
    monkeypatch.delenv("LT_SANDBOX_PROFILE", raising=False)
    transport = LocalModelTransport()
    monkeypatch.setattr(features, "local_translation_identity", transport.identity)
    monkeypatch.setattr(features, "local_urlopen", transport.open)
    monkeypatch.setattr(quality, "local_urlopen", transport.open)
    monkeypatch.setattr("iina_bridge.ollama_version", transport.version)
    monkeypatch.setattr("iina_bridge.resolve_executable", lambda *_: "/fixture/ffmpeg")

    def forbidden_whisper(*args, **kwargs):
        raise AssertionError("Text sources must not inspect or load Whisper artifacts")

    monkeypatch.setattr(deterministic, "local_model_identities", forbidden_whisper)
    video = home / "lesson.mp4"
    video.write_bytes(b"synthetic local media identity, probe is a container-boundary double")
    subtitle = home / "lesson.en.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nThe GPU processes these English numbers.\n\n"
        "2\n00:00:31,000 --> 00:00:34,000\nWe can use the source timeline for context.\n\n"
        "3\n00:01:01,000 --> 00:01:05,000\nThis final sentence is available offline.\n",
        encoding="utf-8",
    )
    selection = {
        "mode": "subtitles",
        "subtitle_track_id": 7,
        "audio_track_id": 2,
        "tracks": [
            {"id": 2, "type": "audio", "ff-index": 1, "lang": "en"},
            {
                "id": 7,
                "type": "sub",
                "external": True,
                "external-filename": str(subtitle),
                "lang": "en",
                "codec": "subrip",
            },
        ],
    }
    cache = home / "Support/subtitles"
    instances = []

    def create():
        bridge = TextFixtureBridge(cache, start_worker=False)
        server = server_for(bridge, TOKEN, port=0, runtime_id=RUNTIME_ID)
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()
        api = NativeAPI(bridge, server, thread)
        instances.append(api)
        return api

    api = create()
    yield api, create, transport, video, subtitle, selection
    for instance in reversed(instances):
        instance.server.shutdown()
        instance.server.server_close()
        instance.thread.join(1)
        instance.bridge.close()
        assert not instance.thread.is_alive()


def activate(api, video, selection, *, position=0, **extra):
    status, body, _ = api.request(
        "POST",
        "/state",
        {
            "path": str(video),
            "position": position,
            "enabled": True,
            "selection": selection,
            **extra,
        },
    )
    assert status == 200, body
    return body


def prepare(api, video, selection):
    activate(api, video, selection)
    bridge = api.bridge
    while (index := bridge._next_chunk(bridge.active)) is not None:
        bridge._process(bridge.active, index, bridge.generation)
    status, body, _ = api.request("GET", "/status")
    assert status == 200 and body["done"], body
    return body


def test_public_health_exposes_protocol_but_never_private_state(native_http):
    api, _, _, video, _, selection = native_http
    activate(api, video, selection)
    status, body, headers = api.request("GET", "/health", authenticated=False)
    assert status == 200 and body["runtime_id"] == RUNTIME_ID
    assert body["protocol"] == 1 and body["process_id"] == os.getpid()
    assert "settings" not in body and str(video) not in json.dumps(body)
    assert headers["Cache-Control"] == "no-store"
    assert api.request("GET", "/status", authenticated=False)[0] == 401
    assert api.control("describe", authenticated=False)[0] == 401


@pytest.mark.parametrize(
    "origin",
    ["https://untrusted.example", "null", "http://127.0.0.1:1", "http://localhost.evil.example"],
)
def test_control_rejects_cross_origin_before_any_settings_mutation(native_http, origin):
    api = native_http[0]
    status, _, headers = api.control("settings", {"bilingual": True}, headers={"Origin": origin})
    assert status == 403
    assert api.bridge.settings["bilingual"] is False
    assert not api.bridge._settings_path.exists()
    assert "Access-Control-Allow-Origin" not in headers


def test_same_origin_form_and_json_settings_persist_into_fresh_bridge(native_http):
    api, create, transport, _, _, _ = native_http
    same_origin = "http://localhost:" + str(api.server.server_port)
    status, description, _ = api.control(
        "settings",
        {"bilingual": True, "wait_for_translation": False},
        form=True,
        headers={"Origin": same_origin},
    )
    assert status == 200 and description["settings"]["bilingual"] is True
    status, description, _ = api.control(
        "settings",
        {"target_language": "de", "glossary": [{"source": " GPU ", "target": " Grafikprozessor "}]},
    )
    assert status == 200
    transport.online = False
    fresh = create()
    status, reopened, _ = fresh.control("describe", form=True)
    assert status == 200 and reopened["settings"] == description["settings"]
    assert reopened["settings"]["glossary"] == [{"source": "GPU", "target": "Grafikprozessor"}]
    assert reopened["models"] == [] and "model_error" in reopened
    assert reopened["runtime_id"] == RUNTIME_ID


def test_describe_reports_defaults_and_actual_model_catalog_without_model_load(native_http):
    api = native_http[0]
    status, body, _ = api.control("describe")
    assert status == 200
    assert body["settings"]["target_language"] == "ru"
    assert body["settings"]["vad_mode"] == "off"
    assert body["models"] == [{"name": "fixture:baseline", "size": 123}]
    assert any(language["code"] == "ru" for language in body["languages"])
    assert body["cache"] == []


@pytest.mark.parametrize(
    "damaged", ["not valid json", '{"bilingual": 1}', '{"unknown_setting": true}']
)
def test_damaged_saved_settings_recover_defaults_without_overwriting_original(native_http, damaged):
    api, create, _, _, _, _ = native_http
    assert api.control("settings", {"target_language": "de"})[0] == 200
    api.bridge._settings_path.write_text(damaged)
    fresh = create()
    status, description, _ = fresh.control("describe")
    assert status == 200
    assert description["settings"]["target_language"] == "ru"
    assert description["settings"]["bilingual"] is False
    assert fresh.bridge._settings_path.read_text() == damaged


@pytest.mark.parametrize(
    "action,data",
    [
        ("unknown", {}),
        ("settings", {"unexpected": True}),
        ("settings", {"bilingual": "true"}),
        ("settings", {"target_language": "unsupported"}),
        ("cache_delete", {"key": "../../outside"}),
        ("cache_use", {"key": "b" * 32}),
    ],
)
def test_invalid_control_inputs_are_rejected_without_persisting_partial_settings(
    native_http, action, data
):
    api = native_http[0]
    previous = copy.deepcopy(api.bridge.settings)
    status, body, _ = api.control(action, data)
    assert status == 400 and body.get("error")
    assert api.bridge.settings == previous
    assert not api.bridge._settings_path.exists()


@pytest.mark.parametrize("size", ["0", "-1", str(MAX_BODY_BYTES + 1)])
def test_control_body_size_is_checked_before_reading_or_mutating(native_http, size):
    api = native_http[0]
    status, _, _ = api.request(
        "POST",
        "/control",
        raw=b"x",
        headers={"Content-Type": "application/json", "Content-Length": size},
    )
    assert status == 413
    assert not api.bridge._settings_path.exists()


def test_control_rejects_bad_auth_media_types_and_nonobject_data(native_http):
    api = native_http[0]
    assert api.control("describe", headers={"Authorization": "Bearer wrong-token"})[0] == 401
    assert (
        api.request("POST", "/control", raw=b"hello", headers={"Content-Type": "text/plain"})[0]
        == 415
    )
    assert (
        api.request("POST", "/control", raw=b"[]", headers={"Content-Type": "application/json"})[0]
        == 400
    )
    assert api.request("POST", "/control", {"action": "settings", "data": []})[0] == 400
    assert (
        api.request("POST", "/control", raw=b"\xff", headers={"Content-Type": "application/json"})[
            0
        ]
        == 400
    )
    assert api.request("POST", "/not-a-route", {"action": "describe"})[0] == 404


@pytest.mark.parametrize("weights_available", [True, False])
def test_fresh_bridge_reopens_native_cache_with_ollama_stopped(native_http, weights_available):
    api, create, transport, video, _, selection = native_http
    initial = prepare(api, video, selection)
    frozen = Path(initial["path"]).read_bytes()
    calls = len(transport.translations)
    transport.online = False
    transport.weights_available = weights_available
    fresh = create()
    reopened = activate(fresh, video, selection, position=62)
    assert reopened["cache_only"] and reopened["current_ready"] and reopened["done"]
    assert reopened["cache_key"] == initial["cache_key"]
    assert Path(reopened["path"]).read_bytes() == frozen
    assert len(transport.translations) == calls
    status, description, _ = fresh.control("describe")
    assert status == 200 and description["cache"][0]["complete"]
    assert description["cache"][0]["current"]


def test_model_variant_can_be_selected_and_explicit_empty_cache_key_returns_to_current_model(
    native_http,
):
    api, _, transport, video, _, selection = native_http
    first = prepare(api, video, selection)
    key_a = first["cache_key"]
    assert api.control("settings", {"translation_model": "fixture:alternative"})[0] == 200
    second = prepare(api, video, selection)
    key_b = second["cache_key"]
    assert key_a != key_b
    status, description, _ = api.control("cache_use", {"key": key_a}, form=True)
    assert status == 200 and description["settings"]["translation_model"] == "fixture:baseline"
    # The user explicitly selected a frozen artifact after its model was updated.
    transport.revisions["fixture:baseline"] = "changed installed weights"
    chosen = activate(api, video, selection, cache_key=key_a, position=31)
    assert chosen["cache_key"] == key_a and chosen["current_ready"] and chosen["cache_only"]
    automatic = activate(api, video, selection, cache_key="", position=31)
    assert automatic["cache_key"] != key_a
    assert not automatic["cache_only"] and not automatic["current_ready"]


def test_cache_use_rejects_another_video_and_keeps_current_settings(native_http):
    api, _, _, video, _, selection = native_http
    first = prepare(api, video, selection)
    other = video.with_name("another.mp4")
    other.write_bytes(b"a genuinely different synthetic media identity")
    active = activate(api, other, selection)
    previous = copy.deepcopy(api.bridge.settings)
    status, body, _ = api.control("cache_use", {"key": first["cache_key"]})
    assert status == 400 and body.get("error")
    assert api.bridge.settings == previous
    status, state, _ = api.request("GET", "/status")
    assert status == 200 and state["cache_key"] == active["cache_key"]


def test_forced_cache_key_cannot_bypass_selected_source_timeline_identity(native_http):
    api, _, transport, video, _, selection = native_http
    frozen = prepare(api, video, selection)
    before = len(transport.translations)
    shifted = {**selection, "subtitle_delay": 1.5}
    rejected = activate(api, video, shifted, cache_key=frozen["cache_key"])
    assert rejected["status"] == "error" and rejected["path"] is None
    assert rejected["current_ready"] is False and rejected["error"]
    assert len(transport.translations) == before
    recovered = activate(api, video, shifted, cache_key="")
    assert recovered["cache_key"] != frozen["cache_key"]
    assert recovered.get("status") != "error"


def test_display_preferences_keep_frozen_cache_but_glossary_changes_invalidate_it(native_http):
    api, _, transport, video, _, selection = native_http
    first = prepare(api, video, selection)
    before = len(transport.translations)
    assert api.control("settings", {"bilingual": True, "wait_for_translation": False})[0] == 200
    visual = activate(api, video, selection)
    assert visual["cache_key"] == first["cache_key"] and visual["done"]
    assert len(transport.translations) == before
    glossary = [
        {"source": "GPU", "target": "графический процессор"},
        {"source": "context", "target": "контекст"},
    ]
    assert api.control("settings", {"glossary": glossary})[0] == 200
    changed = activate(api, video, selection)
    assert changed["cache_key"] != first["cache_key"] and not changed["current_ready"]
    completed = prepare(api, video, selection)
    before = len(transport.translations)
    canonical_equivalent = [
        {"source": " context ", "target": "контекст"},
        {"source": "GPU", "target": "графический   процессор"},
    ]
    assert api.control("settings", {"glossary": canonical_equivalent})[0] == 200
    equivalent = activate(api, video, selection)
    assert equivalent["cache_key"] == completed["cache_key"] and equivalent["done"]
    assert len(transport.translations) == before


def test_subtitle_same_path_content_change_after_retry_invalidates_source_context(native_http):
    api, _, _, video, subtitle, selection = native_http
    first = prepare(api, video, selection)
    original_srt = Path(first["path"]).read_bytes()
    stamp = subtitle.stat()
    subtitle.write_text(subtitle.read_text().replace("English", "Spanish"))
    os.utime(subtitle, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert api.control("retry")[0] == 200
    changed = activate(api, video, selection)
    assert changed["cache_key"] != first["cache_key"] and not changed["current_ready"]
    assert Path(first["path"]).read_bytes() == original_srt


def test_cache_delete_is_scoped_and_does_not_remove_another_model_or_recognition(native_http):
    api, _, _, video, _, selection = native_http
    first = prepare(api, video, selection)
    first_media = api.bridge.active
    recognition = first_media.asr_path
    assert api.control("settings", {"translation_model": "fixture:alternative"})[0] == 200
    second = prepare(api, video, selection)
    assert api.control("cache_delete", {"key": first["cache_key"]})[0] == 200
    assert not Path(first["path"]).exists()
    assert Path(second["path"]).exists() and recognition.is_file()
    status, description, _ = api.control("describe")
    assert status == 200
    assert [entry["key"] for entry in description["cache"]] == [second["cache_key"]]


def test_http_settings_remain_responsive_and_discard_initialization_with_old_settings(
    native_http, monkeypatch
):
    import live_translation.iina_features as features

    api, _, _, video, _, selection = native_http
    entered, release = threading.Event(), threading.Event()
    original = features.fingerprint_file
    first = True

    def slow_first_hash(*args):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(features, "fingerprint_file", slow_first_hash)
    api.bridge._asynchronous = True
    queued = activate(api, video, selection)
    assert queued["status"] == "initializing"
    initialization = threading.Thread(target=api.bridge._initialize_pending, daemon=True)
    initialization.start()
    try:
        assert entered.wait(1)
        status, changed, _ = api.control("settings", {"target_language": "de"})
        assert status == 200 and changed["settings"]["target_language"] == "de"
    finally:
        release.set()
        initialization.join(2)
    assert not initialization.is_alive()
    assert api.bridge.active is None
    activate(api, video, selection)
    assert api.bridge._initialize_pending()
    status, current, _ = api.request("GET", "/status")
    assert status == 200 and current["target_language"] == "de"
    assert api.bridge.active.settings["target_language"] == "de"


def test_partial_cache_is_preserved_when_retry_switches_to_new_engine_profile(native_http):
    api, _, transport, video, _, selection = native_http
    activate(api, video, selection)
    old_media = api.bridge.active
    api.bridge._process(old_media, 0, api.bridge.generation)
    old_json, old_srt = old_media.json_path.read_bytes(), old_media.srt_path.read_bytes()
    assert set(json.loads(old_json)["chunks"]) == {"0"}
    transport.engine_version = "fixture-ollama-upgraded"
    assert api.control("retry")[0] == 200
    replacement = activate(api, video, selection, position=31)
    assert replacement["cache_key"] != old_media.key and not replacement["current_ready"]
    new_media = api.bridge.active
    api.bridge._process(new_media, 1, api.bridge.generation)
    new_cache = json.loads(new_media.json_path.read_text())
    assert new_cache["profile"]["engine_versions"]["ollama"] == transport.engine_version
    assert set(new_cache["chunks"]) == {"1"}
    assert old_media.json_path.read_bytes() == old_json
    assert old_media.srt_path.read_bytes() == old_srt
    status, description, _ = api.control("describe")
    assert status == 200
    assert {old_media.key, new_media.key}.issubset({entry["key"] for entry in description["cache"]})


def test_model_changed_during_inference_cannot_commit_chunk_under_old_digest(
    native_http, monkeypatch
):
    import live_translation.translation_quality as quality

    api, _, transport, video, _, selection = native_http
    activate(api, video, selection)
    media = api.bridge.active
    api.bridge._process(media, 0, api.bridge.generation)
    frozen_json, frozen_srt = media.json_path.read_bytes(), media.srt_path.read_bytes()
    original_digest = media.profile["translation"]["digest"]
    original_open = transport.open

    def replace_weights_while_answering(request, **kwargs):
        answer = original_open(request, **kwargs)
        transport.revisions["fixture:baseline"] = "changed during inference"
        return answer

    monkeypatch.setattr(quality, "local_urlopen", replace_weights_while_answering)
    with pytest.raises(RuntimeError):
        api.bridge._process(media, 1, api.bridge.generation)
    assert transport.identity("fixture:baseline") != original_digest
    assert media.json_path.read_bytes() == frozen_json
    assert media.srt_path.read_bytes() == frozen_srt
    assert set(json.loads(frozen_json)["chunks"]) == {"0"}
    assert "1" not in media.partial_chunks


def test_cache_use_recovers_a_video_after_failed_initialization_with_no_active_media(native_http):
    api, _, transport, video, _, selection = native_http
    saved = prepare(api, video, selection)
    frozen = Path(saved["path"]).read_bytes()
    calls = len(transport.translations)
    assert api.control("settings", {"translation_model": "fixture:unavailable"})[0] == 200
    transport.weights_available = False
    transport.online = False
    failed = activate(api, video, selection)
    assert failed["status"] == "error" and api.bridge.active is None
    status, description, _ = api.control("cache_use", {"key": saved["cache_key"]})
    assert status == 200, description
    assert description["settings"]["translation_model"] == "fixture:baseline"
    recovered = activate(api, video, selection, position=62)
    assert recovered["cache_key"] == saved["cache_key"]
    assert recovered["cache_only"] and recovered["current_ready"] and recovered["done"]
    assert Path(recovered["path"]).read_bytes() == frozen
    assert len(transport.translations) == calls


def test_deleting_unrelated_cache_during_hash_does_not_orphan_initialization(
    native_http, monkeypatch
):
    import live_translation.iina_features as features

    api, _, _, video, _, selection = native_http
    unrelated = prepare(api, video, selection)
    second = video.with_name("second.mp4")
    second.write_bytes(b"another synthetic video with an independent cached identity")
    retained = prepare(api, second, selection)
    current = video.with_name("currently-loading.mp4")
    current.write_bytes(b"a new video which is being hashed while the unrelated cache is removed")
    entered, release = threading.Event(), threading.Event()
    original = features.fingerprint_file

    def slow_current_hash(path, *args):
        if Path(path) == current:
            entered.set()
            assert release.wait(3)
        return original(path, *args)

    monkeypatch.setattr(features, "fingerprint_file", slow_current_hash)
    api.bridge._asynchronous = True
    assert activate(api, current, selection)["status"] == "initializing"
    initializer = threading.Thread(target=api.bridge._initialize_pending, daemon=True)
    initializer.start()
    try:
        assert entered.wait(1)
        status, body, _ = api.control("cache_delete", {"key": unrelated["cache_key"]})
        assert status == 200, body
    finally:
        release.set()
        initializer.join(2)
    assert not initializer.is_alive()
    status, state, _ = api.request("GET", "/status")
    assert status == 200 and state["source_path"] == str(current)
    assert state.get("status") not in {"initializing", "error"}
    assert api.bridge.active is not None and api.bridge.active.source == current
    assert Path(retained["path"]).exists()
    assert not Path(unrelated["path"]).exists()


def test_real_asr_adapter_uses_frozen_media_language_across_http_settings_change(
    native_http, monkeypatch
):
    import live_translation.deterministic as deterministic

    api, _, transport, video, _, selection = native_http
    monkeypatch.setattr(
        deterministic,
        "local_model_identities",
        lambda whisper, model: {
            "whisper_revision": "fixture-whisper",
            "translation_digest": transport.identity(model),
        },
    )
    assert api.control("settings", {"source_language": "es"})[0] == 200
    audio_selection = {**selection, "mode": "audio"}
    state = activate(api, video, audio_selection)
    assert state["source"]["kind"] == "audio"
    media, generation = api.bridge.active, api.bridge.generation
    assert media.settings["source_language"] == "es"
    entered, release = threading.Event(), threading.Event()
    observed = []

    def pause_at_model_boundary(_limit):
        entered.set()
        assert release.wait(3)

    def transcribe(_audio, **options):
        observed.append(options["language"])
        return {"segments": [{"words": [{"start": 1, "end": 2, "word": "Hola."}]}]}

    mlx, core, whisper = (
        types.ModuleType("mlx"),
        types.ModuleType("mlx.core"),
        types.ModuleType("mlx_whisper"),
    )
    mlx.__path__ = []
    mlx.core = core
    core.set_cache_limit = pause_at_model_boundary
    whisper.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(sys.modules, "mlx_whisper", whisper)
    monkeypatch.setattr("iina_bridge.seed_asr", lambda: None)
    monkeypatch.setenv("HF_HUB_OFFLINE", os.environ.get("HF_HUB_OFFLINE", "0"))
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", os.environ.get("TRANSFORMERS_OFFLINE", "0"))
    monkeypatch.setattr(api.bridge, "_decode_audio", lambda *_: [0.1] * 1600)
    monkeypatch.setattr(api.bridge, "_transcribe", types.MethodType(Bridge._transcribe, api.bridge))
    api.bridge._whisper_path = "/fixture/whisper"
    failures = []

    def recognize():
        try:
            api.bridge._prepare_feature_speech(media, 0, generation)
        except Exception as error:
            failures.append(error)

    recognition = threading.Thread(target=recognize, daemon=True)
    recognition.start()
    try:
        assert entered.wait(1)
        status, description, _ = api.control("settings", {"source_language": "de"})
        assert status == 200 and description["settings"]["source_language"] == "de"
    finally:
        release.set()
        recognition.join(2)
    assert not recognition.is_alive()
    assert not failures, failures
    assert observed == ["es"]
    assert (
        media.settings["source_language"] == "es" and api.bridge.settings["source_language"] == "de"
    )
    assert api.bridge.active is None


@pytest.mark.parametrize("complete", [False, True])
def test_legacy_cache_without_index_is_discoverable_and_reopens_first_audio_offline(
    native_http, monkeypatch, complete
):
    import live_translation.deterministic as deterministic
    from iina_bridge import CACHE_VERSION, PAGE_LAYOUT_VERSION, descriptor_hash, key_for
    from live_translation.sessions import export_subtitles

    initial, create, transport, video, _, selection = native_http
    profile = {
        "profile_version": 1,
        "seed": 42,
        "source_language": "en",
        "target_language": "ru",
        "whisper": {"model": "fixture-whisper", "revision": "legacy-asr"},
        "translation": {"model": "fixture:legacy", "digest": "legacy-digest"},
        "segmentation": {"audio_track": "0:a:0", "chunk_seconds": 30, "sample_rate": 16000},
        "engine_versions": {"ollama": "old-ollama", "ffmpeg": "old-ffmpeg"},
    }
    content = hashlib.sha256(video.read_bytes()).hexdigest()
    key = key_for(content, descriptor_hash(profile))
    chunks = {
        str(index): [
            {
                "start": index * 30 + 1.0,
                "end": index * 30 + 3.0,
                "source": f"Legacy sentence {index}.",
                "translated": f"Сохранённый перевод {index}.",
            }
        ]
        for index in range(3 if complete else 1)
    }
    saved = {
        "version": CACHE_VERSION,
        "page_layout_version": PAGE_LAYOUT_VERSION,
        "key": key,
        "source": str(video),
        "duration": 75.0,
        "revision": 4,
        "source_sha256": content,
        "profile_sha256": descriptor_hash(profile),
        "profile": profile,
        "chunks": chunks,
        "complete": complete,
        "completed_chunks": len(chunks),
        "total_chunks": 3,
    }
    json_path = initial.bridge.cache_dir / f"{key}.json"
    json_path.write_text(json.dumps(saved, ensure_ascii=False))
    directory = initial.bridge.cache_dir / key
    directory.mkdir()
    srt_path = directory / "Русский — Live Translation.srt"
    srt_path.write_text(
        export_subtitles({"phrases": [cue for values in chunks.values() for cue in values]})
    )
    frozen_json, frozen_srt = json_path.read_bytes(), srt_path.read_bytes()
    assert not initial.bridge._index_path.exists()
    transport.online = False
    transport.weights_available = False

    def unavailable_models(*args):
        raise RuntimeError("No models are installed in this offline-upgrade fixture")

    monkeypatch.setattr(deterministic, "local_model_identities", unavailable_models)
    fresh = create()
    status, description, _ = fresh.control("describe")
    assert status == 200
    assert [(entry["key"], entry["legacy"]) for entry in description["cache"]] == [(key, True)]
    assert not fresh.bridge._index_path.exists()
    audio_selection = {**selection, "mode": "audio"}
    failed = activate(fresh, video, audio_selection)
    assert failed["status"] == "error" and fresh.bridge.active is None
    status, selected, _ = fresh.control("cache_use", {"key": key})
    assert status == 200, selected
    reopened = activate(fresh, video, audio_selection, position=62 if complete else 1)
    assert reopened["cache_key"] == key and reopened["read_only"] and reopened["cache_only"]
    assert reopened["current_ready"] is True and reopened["done"] is complete
    assert fresh.bridge._next_chunk(fresh.bridge.active) is None
    assert json_path.read_bytes() == frozen_json and srt_path.read_bytes() == frozen_srt
    assert Path(reopened["original_path"]).is_file()
    assert transport.translations == []
