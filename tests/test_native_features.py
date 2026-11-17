"""Exercise the actual native pipeline with deterministic model doubles."""

import copy
import threading
import time
from unittest import mock

import numpy as np
import pytest

from iina_bridge import Bridge
from live_translation.translation_quality import stable_cue_id


class NativeBridge(Bridge):
    def _probe(self, path):
        return 75.0

    def _decode_audio(self, path, start, seconds):
        assert self.active is not None
        self.decoded_map = self.active.spec.audio_map
        return np.ones(16000, dtype=np.float32) * 0.1

    def _transcribe(self, audio):
        self.transcriptions = getattr(self, "transcriptions", 0) + 1
        return {"segments": [{"words": [{"start": 1.0, "end": 2.0, "word": "Hello."}]}]}

    def _unload(self):
        pass

    def _release_whisper(self):
        self._whisper_resident = False

    def _release_translation(self, force=False):
        self._translation_resident = False


@pytest.fixture
def native(tmp_path, monkeypatch):
    import live_translation.deterministic as deterministic
    import live_translation.iina_features as features
    from live_translation.translation_quality import OllamaBatchTranslator

    monkeypatch.setattr(features, "local_translation_identity", lambda *_: "fixture-digest")
    monkeypatch.setattr(
        deterministic,
        "local_model_identities",
        lambda *_: {"whisper_revision": "fixture-whisper", "translation_digest": "fixture-digest"},
    )
    monkeypatch.setattr("iina_bridge.ollama_version", lambda: "fixture-ollama")

    def translate(self, cues, *, on_cue=None, **kwargs):
        result = {}
        for cue in cues:
            result[stable_cue_id(cue)] = "Перевод: " + cue["source"]
            if on_cue:
                on_cue(stable_cue_id(cue), result[stable_cue_id(cue)])
        return result

    monkeypatch.setattr(OllamaBatchTranslator, "translate", translate)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"controlled-fixture-not-a-real-container")
    subtitle = tmp_path / "video.en.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nThis is an English lesson.\n\n2\n00:00:29,000 --> 00:00:32,000\nA sentence crossing a chunk boundary.\n\n3\n00:00:40,000 --> 00:00:43,000\nThe final English sentence.\n"
    )
    selection = {
        "mode": "subtitles",
        "subtitle_track_id": 7,
        "audio_track_id": 2,
        "tracks": [
            {"id": 1, "type": "audio", "ff-index": 0, "lang": "es"},
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
    bridge = NativeBridge(tmp_path / "cache", start_worker=False)
    yield bridge, video, subtitle, selection
    bridge.close()


def activate(native, position=0, selection=None):
    bridge, video, _, original = native
    return bridge.update(
        {
            "path": str(video),
            "position": position,
            "enabled": True,
            "selection": selection or original,
            "seek_generation": position,
        }
    )


def finish(bridge):
    while (index := bridge._next_chunk(bridge.active)) is not None:
        bridge._process(bridge.active, index, bridge.generation)


def test_existing_english_subtitles_skip_whisper_and_keep_native_timing(native):
    bridge, _, _, _ = native
    with mock.patch.object(
        bridge, "_transcribe", side_effect=AssertionError("Text mode must never run Whisper")
    ):
        state = activate(native)
        assert state["source"]["kind"] == "text"
        assert state["source"]["audio_label"] == "0:1"
        finish(bridge)
    assert bridge._feature_response()["done"]
    assert "00:00:29,000 --> 00:00:32,000" in bridge.active.srt_path.read_text()
    assert bridge.active.original_path.is_file()


def test_text_path_does_not_resolve_whisper_artifacts(native, monkeypatch):
    monkeypatch.setattr(
        "live_translation.deterministic.local_model_identities",
        mock.Mock(side_effect=AssertionError("No Whisper dependency")),
    )
    assert activate(native)["source"]["kind"] == "text"


def test_selected_audio_and_change_invalidate_old_translation(native):
    bridge, _, _, selection = native
    selection = copy.deepcopy(selection)
    selection["mode"] = "audio"
    activate(native, selection=selection)
    bridge._process(bridge.active, 0, bridge.generation)
    first = bridge.active.key
    assert bridge.decoded_map == "0:1"
    selection["audio_track_id"] = 1
    state = activate(native, selection=selection)
    assert state["path"] is None
    assert bridge.active.key != first
    bridge._process(bridge.active, 0, bridge.generation)
    assert bridge.decoded_map == "0:0"


def test_cached_text_loads_without_running_ollama_or_models(native, monkeypatch):
    bridge, _, _, _ = native
    activate(native)
    finish(bridge)
    before = bridge.active.srt_path.read_bytes()
    monkeypatch.setattr(
        "iina_bridge.ollama_version", mock.Mock(side_effect=RuntimeError("stopped"))
    )
    monkeypatch.setattr(
        "live_translation.iina_features.local_translation_identity",
        mock.Mock(side_effect=RuntimeError("weights removed")),
    )
    bridge._request_signature = None
    state = activate(native, position=40)
    assert state["current_ready"] is True
    assert state["cache_only"] is True
    assert bridge.active.srt_path.read_bytes() == before


def test_delay_is_applied_once_by_native_subtitle_renderer(native):
    bridge, _, _, selection = native
    selection = {**selection, "subtitle_delay": 2}
    activate(native, selection=selection)
    finish(bridge)
    assert bridge.active.chunks["0"][0]["start"] == 3
    assert "00:00:01,000 --> 00:00:03,000" in bridge.active.srt_path.read_text()


def test_seek_order_keeps_identical_source_context(native):
    bridge, _, _, _ = native
    activate(native, position=40)
    bridge._process(bridge.active, 1, bridge.generation)
    context = copy.deepcopy(bridge.active.contexts[1])
    bridge._process(bridge.active, 0, bridge.generation)
    bridge._prepare_feature_context(bridge.active, 1, bridge.generation)
    assert bridge.active.contexts[1] == context


def test_async_hash_does_not_block_state_and_seek_during_initialization_recovers(
    native, monkeypatch
):
    bridge, video, _, selection = native
    import live_translation.iina_features as features

    entered, release = threading.Event(), threading.Event()
    original = features.fingerprint_file

    def slow_hash(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(features, "fingerprint_file", slow_hash)
    bridge._asynchronous = True
    started = time.monotonic()
    result = activate(native)
    assert result["status"] == "initializing"
    assert time.monotonic() - started < 0.2
    thread = threading.Thread(target=bridge._initialize_pending)
    thread.start()
    assert entered.wait(1)
    bridge.update(
        {"path": str(video), "position": 40, "selection": selection, "seek_generation": 1}
    )
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert bridge.active is not None
    assert bridge.position == 40


def test_settings_validation_and_cache_deletion_are_scoped(native):
    bridge, _, _, _ = native
    activate(native)
    finish(bridge)
    key = bridge.active.key
    bridge.describe = lambda: {"settings": bridge.settings}
    with pytest.raises(ValueError):
        bridge.control("settings", {"target_language": "invalid"})
    with pytest.raises(ValueError):
        bridge.control("cache_delete", {"key": "../../outside"})
    bridge.control("cache_delete", {"key": key})
    assert not (bridge.cache_dir / f"{key}.json").exists()
    assert bridge.active is None


def test_native_cross_boundary_cues_survive_a_cache_reload(native):
    bridge, _, _, _ = native
    activate(native)
    finish(bridge)
    bridge._request_signature = None
    state = activate(native, position=31)
    assert state["done"]
    assert bridge.active.chunks["0"][-1]["end"] == 32


def test_later_pages_of_a_boundary_cue_keep_source_chunk_ownership(native, monkeypatch):
    from live_translation.translation_quality import OllamaBatchTranslator

    bridge, _, _, _ = native

    def long_translation(self, cues, *, on_cue, **kwargs):
        for cue in cues:
            on_cue(
                stable_cue_id(cue),
                "Достаточно длинный перевод, который занимает несколько страниц субтитров и переходит через границу тридцатисекундного участка.",
            )

    monkeypatch.setattr(OllamaBatchTranslator, "translate", long_translation)
    activate(native)
    finish(bridge)
    assert any(cue["start"] >= 30 for cue in bridge.active.chunks["0"])
    before = bridge.active.srt_path.read_bytes()
    bridge._request_signature = None
    assert activate(native, position=31)["done"]
    assert bridge.active.srt_path.read_bytes() == before


def test_explicit_empty_cache_choice_clears_a_previous_override(native):
    bridge, video, _, selection = native
    activate(native)
    finish(bridge)
    bridge._preferred_cache_key = bridge.active.key
    bridge.update({"path": str(video), "selection": selection, "cache_key": ""})
    assert bridge._preferred_cache_key is None


def test_audio_and_subtitle_delays_each_apply_once_on_the_native_timeline(native):
    bridge, _, _, selection = native
    selection = {**selection, "mode": "audio", "audio_delay": 2, "subtitle_delay": 3}
    activate(native, selection=selection)
    bridge._process(bridge.active, 0, bridge.generation)
    assert bridge.active.recognized["0"][0]["start"] == 6
    assert "00:00:03,000 --> 00:00:04,000" in bridge.active.srt_path.read_text()


def test_pre_upgrade_cache_is_discovered_and_selectable_without_models(native, monkeypatch):
    import hashlib
    import json

    from live_translation.deterministic import descriptor_hash
    from live_translation.media_cache import key_for

    bridge, video, _, selection = native
    selection = {**selection, "audio_track_id": 1}
    activate(native, selection=selection)
    profile = {
        "source_language": "en",
        "target_language": "ru",
        "translation": {"model": "gemma4:12b"},
        "segmentation": {"audio_track": "0:a:0"},
    }
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    key = key_for(digest, descriptor_hash(profile))
    saved = {
        "version": 4,
        "key": key,
        "source": str(video),
        "duration": 75,
        "source_sha256": digest,
        "profile": profile,
        "profile_sha256": descriptor_hash(profile),
        "revision": 1,
        "chunks": {
            "0": [
                {
                    "start": 1.0,
                    "end": 2.0,
                    "source": "Legacy English.",
                    "translated": "Сохранённый перевод.",
                }
            ],
            "1": [],
            "2": [],
        },
        "complete": True,
    }
    path = bridge.cache_dir / f"{key}.json"
    path.write_text(json.dumps(saved))
    original_json = path.read_bytes()
    assert any(entry["key"] == key and entry["legacy"] for entry in bridge.describe()["cache"])
    monkeypatch.setattr(
        "iina_bridge.ollama_version", mock.Mock(side_effect=RuntimeError("offline"))
    )
    bridge.control("cache_use", {"key": key})
    response = bridge.update({"path": str(video), "selection": selection, "cache_key": key})
    assert response["read_only"]
    assert response["done"]
    assert bridge._missing_chunks(bridge.active) == []
    assert "Сохранённый перевод." in bridge.active.srt_path.read_text()
    assert path.read_bytes() == original_json


def test_explicit_saved_prompt_version_opens_read_only_after_pipeline_upgrade(native, monkeypatch):
    import live_translation.translation_quality as quality

    bridge, _, _, _ = native
    activate(native)
    finish(bridge)
    key = bridge.active.key
    before = bridge.active.srt_path.read_bytes()
    original = quality.quality_descriptor
    monkeypatch.setattr(
        quality,
        "quality_descriptor",
        lambda glossary=(): {**original(glossary), "quality_version": 999},
    )
    bridge.control("cache_use", {"key": key})
    result = activate(native)
    assert result["read_only"]
    assert result["cache_key"] == key
    assert bridge.active.srt_path.read_bytes() == before


def test_bilingual_original_can_be_rebuilt_from_committed_json_without_asr_artifact(native):
    bridge, _, _, selection = native
    activate(native, selection={**selection, "mode": "audio"})
    finish(bridge)
    assert bridge.active.asr_path is not None
    original_bytes = bridge.active.original_path.read_bytes()
    bridge.active.asr_path.unlink()
    bridge.active.original_path.unlink()
    bridge._request_signature = None
    result = activate(native, selection={**selection, "mode": "audio"})
    assert result["done"]
    assert bridge.active.original_path.is_file()
    assert bridge.active.original_path.read_bytes() == original_bytes
