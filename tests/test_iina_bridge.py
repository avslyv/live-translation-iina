"""Contracts for timestamp ownership, caching, seeking and authenticated control."""

import http.client
import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import numpy as np

from iina_bridge import (
    HEARTBEAT_SECONDS,
    MODEL_IDLE_SECONDS,
    PAGE_LAYOUT_VERSION,
    Bridge,
    Media,
    recognized_cues,
    reflow_cached_pages,
    server_for,
    translated_pages,
)


class FakeBridge(Bridge):
    def __init__(self, *args, **kwargs):
        self.memory_events = []
        kwargs.setdefault("profile", {"fixture": "deterministic", "seed": 42})
        super().__init__(*args, **kwargs)

    @property
    def media(self) -> Media:
        assert self.active is not None
        return self.active

    def _probe(self, path):
        return 240.0

    def _decode_audio(self, path, start, seconds):
        self.last_decode_start = start
        return np.array([0.1], dtype=np.float32)

    def _transcribe(self, audio):
        self.memory_events.append("recognize")
        self._whisper_resident = True
        return {
            "segments": [
                {
                    "words": [
                        {"start": 1.0, "end": 2.0, "word": "Hello"},
                        {"start": 2.0, "end": 3.0, "word": "there."},
                    ]
                }
            ]
        }

    def _translate(self, source, history):
        self.memory_events.append("translate")
        self._translation_resident = True
        return "Привет." if source else ""

    def _release_translation(self, force=False):
        self.memory_events.append(("release translation", force))
        self._translation_resident = False

    def _release_whisper(self):
        self.memory_events.append("release whisper")
        self._whisper_resident = False


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.video = self.root / "video with spaces.mp4"
        self.video.write_bytes(b"local media")
        self.bridge = FakeBridge(self.root / "cache", start_worker=False)

    def tearDown(self):
        self.bridge.close()
        self.directory.cleanup()

    def state(self, position: float = 0.0, **kwargs):
        return self.bridge.update(
            {"path": str(self.video), "position": position, "enabled": True, **kwargs}
        )

    def process(self):
        media = self.bridge.media
        self.bridge._process(media, self.bridge._next_chunk(media), self.bridge.generation)
        return media

    def test_overlapping_windows_own_each_boundary_word_once(self):
        result = {
            "segments": [
                {
                    "words": [
                        {"start": 29.0, "end": 30.5, "word": "before"},
                        {"start": 30.0, "end": 31.0, "word": "after."},
                    ]
                }
            ]
        }
        before = recognized_cues(result, 0, 0, 30)
        after = recognized_cues(result, 0, 30, 60)
        self.assertEqual([cue["source"] for cue in before], ["before"])
        self.assertEqual([cue["source"].rstrip(".") for cue in after], ["after"])
        self.assertLessEqual(before[-1]["end"], after[0]["start"])

    def test_chunk_offset_is_the_media_timeline(self):
        self.state(position=61)
        media = self.process()
        self.assertEqual(self.bridge.last_decode_start, 59)
        self.assertEqual(media.phrases()[0]["start"], 60)
        self.assertEqual(media.phrases()[0]["end"], 62)
        self.assertIn("00:01:00,000 --> 00:01:02,000", media.srt_path.read_text())

    def test_initial_state_has_no_placeholder_track(self):
        response = self.state()
        self.assertIsNone(response["path"])
        self.assertEqual(response["revision"], 0)
        self.assertFalse(any((self.root / "cache").glob("*.srt")))

    def test_cache_restores_without_transcribing_and_seeking_prioritizes_current_chunk(self):
        self.state()
        media = self.process()
        second = FakeBridge(self.root / "cache", start_worker=False)
        try:
            restored = second.update({"path": str(self.video), "position": 0, "enabled": True})
            self.assertEqual(restored["path"], str(media.srt_path))
            self.assertEqual(restored["buffered_until"], 30)
            second.update({"path": str(self.video), "position": 195, "enabled": True})
            self.assertEqual(second._next_chunk(second.media), 6)
        finally:
            second.close()

    def test_legacy_cache_gets_a_friendly_srt_without_retranslating(self):
        self.state()
        media = self.process()
        legacy_path = self.root / "cache" / f"{media.key}.ru.srt"
        media.srt_path.replace(legacy_path)
        second = FakeBridge(self.root / "cache", start_worker=False)
        try:
            restored = second.update({"path": str(self.video), "position": 0, "enabled": True})
            friendly = Path(restored["path"])
            self.assertEqual(friendly.name, "Русский — Live Translation.srt")
            self.assertEqual(friendly.parent.name, media.key)
            self.assertEqual(restored["revision"], media.revision)
            self.assertEqual(friendly.read_text(), legacy_path.read_text())
            self.assertEqual(second.memory_events, [])
        finally:
            second.close()

    def test_cached_short_tail_reflows_once_without_model_work(self):
        self.state()
        media = self.bridge.media
        old_pages = [
            {
                "source": "one original sentence",
                "translated": " ".join(["пример"] * 14),
                "start": 10.0,
                "end": 15.29,
            },
            {"source": "one original sentence", "translated": "конец", "start": 15.29, "end": 16.0},
        ]
        media.chunks = {"0": old_pages}
        media.revision = 7
        self.bridge._save(media)
        stored = json.loads(media.json_path.read_text())
        stored.pop("page_layout_version")
        media.json_path.write_text(json.dumps(stored), encoding="utf-8")
        second = FakeBridge(self.root / "cache", start_worker=False)
        third = FakeBridge(self.root / "cache", start_worker=False)
        try:
            response = second.update({"path": str(self.video), "position": 0, "enabled": True})
            pages = second.media.chunks["0"]
            self.assertEqual(second.media.key, media.key)
            self.assertEqual(response["revision"], 8)
            self.assertEqual(pages[0]["start"], 10)
            self.assertEqual(pages[-1]["end"], 16)
            self.assertTrue(all(page["end"] - page["start"] >= 1.199 for page in pages))
            old_text = " ".join(page["translated"] for page in old_pages).split()
            new_text = " ".join(page["translated"] for page in pages).split()
            self.assertEqual(old_text, new_text)
            self.assertTrue(all(page["source"] == "one original sentence" for page in pages))
            self.assertEqual(second.memory_events, [])
            self.assertEqual(
                json.loads(media.json_path.read_text())["page_layout_version"], PAGE_LAYOUT_VERSION
            )
            again = third.update({"path": str(self.video), "position": 0, "enabled": True})
            self.assertEqual(again["revision"], 8)
            self.assertEqual(third.media.chunks, second.media.chunks)
        finally:
            second.close()
            third.close()

    def test_page_reflow_does_not_join_different_sentences_or_pauses(self):
        cues = [
            {"source": "first", "translated": "первый", "start": 0, "end": 2},
            {"source": "second", "translated": "второй", "start": 2, "end": 4},
            {"source": "second", "translated": "снова", "start": 5, "end": 7},
        ]
        self.assertEqual(reflow_cached_pages(cues), cues)

    def test_stale_or_disabled_generation_cannot_commit(self):
        self.state()
        media = self.bridge.media
        generation = self.bridge.generation
        self.bridge.last_heartbeat = time.monotonic() - HEARTBEAT_SECONDS - 1
        self.bridge._process(media, 0, generation)
        self.assertEqual(media.chunks, {})
        self.state()
        generation = self.bridge.generation
        self.bridge.update({"enabled": "false"})
        self.bridge._process(media, 0, generation)
        self.assertEqual(media.chunks, {})

    def test_paused_prefetch_continues_through_entire_video(self):
        self.state(paused="true")
        media = self.bridge.media
        media.chunks = {str(index): [] for index in range(6)}
        self.assertEqual(self.bridge._next_chunk(media), 6)
        self.state(position=180, paused=True)
        self.assertEqual(self.bridge._next_chunk(media), 6)
        media.chunks.update({"6": [], "7": []})
        self.assertIsNone(self.bridge._next_chunk(media))

    def test_large_seek_cancels_inflight_translation_before_commit(self):
        self.state()
        media = self.bridge.media
        generation = self.bridge.generation

        def seek_during_translation(*_args):
            self.state(position=121)
            return "Старый перевод."

        self.bridge._translate = seek_during_translation
        self.bridge._process(media, 0, generation)
        self.assertEqual(media.chunks, {})
        self.assertGreater(self.bridge.generation, generation)
        self.assertEqual(self.bridge._next_chunk(media), 4)

    def test_explicit_short_seek_preempts_inflight_translation(self):
        self.state(position=29, seek_generation="1")
        media = self.bridge.media
        generation = self.bridge.generation

        def seek_during_translation(*_args):
            self.state(position=31, seek_generation="2")
            return "Старый перевод."

        self.bridge._translate = seek_during_translation
        self.bridge._process(media, 0, generation)
        self.assertEqual(media.chunks, {})
        self.assertGreater(self.bridge.generation, generation)
        self.assertEqual(self.bridge._next_chunk(media), 1)
        current = self.bridge.generation
        self.state(position=32, seek_generation="2")
        self.assertEqual(self.bridge.generation, current)

    def test_delayed_stop_for_old_file_preserves_new_video(self):
        self.state()
        other_video = self.root / "other.mp4"
        other_video.write_bytes(b"another local media")
        self.bridge.update({"path": str(other_video), "enabled": True, "position": 0})
        generation = self.bridge.generation
        response = self.bridge.update({"path": str(self.video), "enabled": "false"})
        self.assertTrue(self.bridge.enabled)
        self.assertEqual(self.bridge.media.source, other_video.resolve())
        self.assertEqual(self.bridge.generation, generation)
        self.assertNotEqual(response["status"], "disabled")

    def test_music_or_repetitive_hallucinations_create_no_cues(self):
        result = {
            "segments": [
                {"start": 0, "end": 10, "text": "Imagined lyrics", "no_speech_prob": 0.95},
                {"start": 10, "end": 20, "text": "repeated words", "compression_ratio": 3.5},
            ]
        }
        self.assertEqual(recognized_cues(result, 0, 0, 30), [])

    def test_musical_term_crossing_whisper_segments_remains_one_translation(self):
        result = {
            "segments": [
                {
                    "words": [
                        {"start": 12.84, "end": 14.42, "word": "We have an A section which"},
                    ]
                },
                {
                    "words": [
                        {
                            "start": 14.42,
                            "end": 20.24,
                            "word": "is in the key of E minor and a B section which is in the key of F",
                        },
                    ]
                },
                {
                    "words": [
                        {"start": 20.24, "end": 20.84, "word": "sharp minor."},
                    ]
                },
            ]
        }
        cues = recognized_cues(result, 0, 0, 30)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["start"], 12.84)
        self.assertEqual(cues[0]["end"], 20.84)
        self.assertIn("key of F sharp minor", cues[0]["source"])

    def test_empty_chunk_is_cached_without_translating_or_filler_subtitles(self):
        self.state()
        self.bridge._transcribe = lambda *_args: {"segments": []}
        self.bridge._translate = lambda *_args: self.fail("Silence must not be translated")
        media = self.process()
        self.assertEqual(media.chunks, {"0": []})
        self.assertEqual(self.bridge._response()["buffered_until"], 30)
        self.assertIsNone(self.bridge._response()["path"])

    def test_models_handoff_memory_before_loading_the_other_model(self):
        self.state()
        self.process()
        self.assertEqual(
            self.bridge.memory_events,
            [
                ("release translation", True),
                "recognize",
                "release whisper",
                "translate",
            ],
        )
        self.assertFalse(self.bridge._whisper_resident)
        self.assertTrue(self.bridge._translation_resident)
        self.state(position=31)
        self.process()
        self.assertEqual(self.bridge.memory_events[4], ("release translation", True))

    def test_batching_waits_for_a_contiguous_sixty_second_buffer(self):
        self.state()
        media = self.bridge.media
        self.assertEqual(self.bridge._batch_indices(media), [0])
        media.chunks = {"0": []}
        self.assertEqual(self.bridge._batch_indices(media), [1])
        media.chunks["1"] = []
        self.assertEqual(self.bridge._batch_indices(media), [2, 3, 4, 5])
        self.state(position=31)
        self.assertEqual(self.bridge._batch_indices(media), [2])

    def test_phased_batch_recognizes_all_chunks_then_releases_whisper_once(self):
        self.state()
        media = self.bridge.media
        self.bridge._process_batch(media, [0, 1, 2], self.bridge.generation)
        self.assertEqual(
            self.bridge.memory_events,
            [
                ("release translation", True),
                "recognize",
                "recognize",
                "recognize",
                "release whisper",
                "translate",
                "translate",
                "translate",
            ],
        )
        self.assertEqual(set(media.chunks), {"0", "1", "2"})
        self.assertEqual(set(media.recognized), {"0", "1", "2"})

    def test_phased_and_serial_preserve_identical_cues_prompts_and_srt(self):
        self.state()
        serial = FakeBridge(self.root / "serial-cache", start_worker=False)
        calls = {"batch": [], "serial": []}

        def setup(bridge, label):
            original_transcribe = bridge._transcribe

            def transcribe(audio):
                original_transcribe(audio)
                return {
                    "segments": [
                        {
                            "words": [
                                {"start": 1, "end": 2, "word": "Hello."},
                                {"start": 3, "end": 4, "word": "Again."},
                            ]
                        }
                    ]
                }

            def translate(source, history):
                calls[label].append((source, list(history)))
                return "Привет." if source.rstrip(".") == "Hello" else "Снова."

            bridge._transcribe = transcribe
            bridge._translate = translate

        try:
            serial.update({"path": str(self.video), "position": 0, "enabled": True})
            setup(self.bridge, "batch")
            setup(serial, "serial")
            self.bridge._process_batch(self.bridge.media, [0, 1, 2], self.bridge.generation)
            for index in [0, 1, 2]:
                serial._process(serial.media, index, serial.generation)
            self.assertEqual(calls["batch"], calls["serial"])
            self.assertEqual(calls["batch"][1][1], [("Hello", "Привет.")])
            self.assertEqual(self.bridge.media.chunks, serial.media.chunks)
            self.assertEqual(
                self.bridge.media.srt_path.read_bytes(), serial.media.srt_path.read_bytes()
            )
        finally:
            serial.close()

    def test_recognition_survives_translation_failure_and_restart(self):
        self.state()
        media = self.bridge.media
        self.bridge._translate = mock.Mock(side_effect=RuntimeError("translation interrupted"))
        with self.assertRaisesRegex(RuntimeError, "translation interrupted"):
            self.process()
        self.assertEqual(media.chunks, {})
        self.assertIn("0", media.recognized)
        assert media.asr_path is not None
        self.assertTrue(media.asr_path.is_file())
        restarted = FakeBridge(self.root / "cache", start_worker=False)
        try:
            restarted._decode_audio = mock.Mock(
                side_effect=AssertionError("Do not decode cached speech")
            )
            restarted._transcribe = mock.Mock(
                side_effect=AssertionError("Do not recognize cached speech")
            )
            restarted._release_translation = mock.Mock()
            restarted.update({"path": str(self.video), "position": 0, "enabled": True})
            restarted._process(restarted.media, 0, restarted.generation)
            self.assertIn("0", restarted.media.chunks)
            restarted._decode_audio.assert_not_called()
            restarted._transcribe.assert_not_called()
            restarted._release_translation.assert_not_called()
        finally:
            restarted.close()

    def test_translation_profile_changes_reuse_recognition_without_changing_old_cache(self):
        self.state()
        original = self.process()
        frozen = original.json_path.read_bytes()
        changed = FakeBridge(
            self.root / "cache",
            start_worker=False,
            profile={"fixture": "new translation prompt", "seed": 42},
        )
        try:
            changed._transcribe = mock.Mock(side_effect=AssertionError("ASR profile is unchanged"))
            changed.update({"path": str(self.video), "position": 0, "enabled": True})
            self.assertNotEqual(changed.media.key, original.key)
            self.assertEqual(changed.media.asr_key, original.asr_key)
            changed._process(changed.media, 0, changed.generation)
            changed._transcribe.assert_not_called()
            self.assertEqual(original.json_path.read_bytes(), frozen)
        finally:
            changed.close()

    def test_seek_after_asr_preserves_speech_and_preempts_remaining_batch(self):
        self.state()
        media = self.bridge.media
        original = self.bridge._transcribe

        def seek_after_asr(audio):
            result = original(audio)
            self.state(position=121)
            return result

        self.bridge._transcribe = seek_after_asr
        self.bridge._process_batch(media, [0, 1, 2], self.bridge.generation)
        self.assertEqual(media.chunks, {})
        self.assertEqual(set(media.recognized), {"0"})
        self.assertEqual(
            self.bridge.memory_events,
            [
                ("release translation", True),
                "recognize",
                "release whisper",
            ],
        )
        self.assertEqual(self.bridge._next_chunk(media), 4)

    def test_seek_during_translation_does_not_translate_other_prepared_chunks(self):
        self.state()
        media = self.bridge.media
        calls = []

        def seek_during_translation(source, _history):
            calls.append(source)
            self.state(position=121)
            return "Отложенный перевод."

        self.bridge._translate = seek_during_translation
        self.bridge._process_batch(media, [0, 1, 2], self.bridge.generation)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(media.recognized), {"0", "1", "2"})
        self.assertEqual(media.chunks, {})

    def test_asr_failure_releases_batch_model_and_preserves_completed_speech(self):
        self.state()
        media = self.bridge.media
        original = self.bridge._transcribe
        count = 0

        def fail_second(audio):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("second ASR failed")
            return original(audio)

        self.bridge._transcribe = fail_second
        with self.assertRaisesRegex(RuntimeError, "second ASR failed"):
            self.bridge._process_batch(media, [0, 1, 2], self.bridge.generation)
        self.assertEqual(set(media.recognized), {"0"})
        self.assertEqual(media.chunks, {})
        self.assertFalse(self.bridge._whisper_resident)
        self.assertNotIn("translate", self.bridge.memory_events)

    def test_whisper_failure_still_releases_its_memory(self):
        self.state()

        def fail_recognition(_audio):
            self.bridge._whisper_resident = True
            raise RuntimeError("recognition failure")

        self.bridge._transcribe = fail_recognition
        with self.assertRaisesRegex(RuntimeError, "recognition failure"):
            self.process()
        self.assertEqual(
            self.bridge.memory_events,
            [
                ("release translation", True),
                "release whisper",
            ],
        )
        self.assertFalse(self.bridge._whisper_resident)
        self.assertEqual(self.bridge.media.chunks, {})

    def test_failed_translation_unload_prevents_starting_recognition(self):
        self.state()
        self.bridge._release_translation = mock.Mock(side_effect=RuntimeError("release failed"))
        with self.assertRaisesRegex(RuntimeError, "release failed"):
            self.process()
        self.assertNotIn("recognize", self.bridge.memory_events)

    def test_completed_ahead_buffer_releases_models_after_ten_idle_seconds(self):
        self.assertFalse(self.bridge._idle_unload_due(100.0, inactive=False))
        self.assertFalse(
            self.bridge._idle_unload_due(100.0 + MODEL_IDLE_SECONDS - 0.1, inactive=False)
        )
        self.assertTrue(self.bridge._idle_unload_due(100.0 + MODEL_IDLE_SECONDS, inactive=False))
        self.bridge._idle_since = None
        self.assertTrue(self.bridge._idle_unload_due(200.0, inactive=True))

    def test_real_whisper_release_clears_holder_and_allocator_once(self):
        bridge = Bridge(self.root / "release-cache", start_worker=False)
        holder = types.SimpleNamespace(model=object(), model_path="cached-model")
        mx = types.ModuleType("mlx.core")
        mx.synchronize = mock.Mock()
        mx.clear_cache = mock.Mock()
        mlx = types.ModuleType("mlx")
        mlx.core = mx
        transcribe_module = types.ModuleType("mlx_whisper.transcribe")
        transcribe_module.ModelHolder = holder
        bridge._whisper_resident = True
        with mock.patch.dict(
            "sys.modules",
            {
                "mlx": mlx,
                "mlx.core": mx,
                "mlx_whisper.transcribe": transcribe_module,
            },
        ):
            bridge._release_whisper()
            bridge._release_whisper()
        self.assertIsNone(holder.model)
        self.assertIsNone(holder.model_path)
        self.assertFalse(bridge._whisper_resident)
        mx.clear_cache.assert_called_once()
        bridge.close()

    def test_real_translation_release_uses_zero_keepalive_and_does_not_repeat(self):
        bridge = Bridge(self.root / "release-cache", start_worker=False)
        bridge._translation_resident = True
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"done_reason": "unload"}'
        with mock.patch("iina_bridge.local_urlopen", return_value=response) as open_url:
            bridge._release_translation()
            bridge._release_translation()
        open_url.assert_called_once()
        payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(payload["keep_alive"], 0)
        self.assertEqual(payload["model"], bridge.translation_model)
        self.assertFalse(bridge._translation_resident)
        bridge.close()

    def test_translation_failure_does_not_publish_original_as_russian(self):
        self.state()
        self.bridge._translate = lambda *_args: ""
        with self.assertRaisesRegex(RuntimeError, "empty translation"):
            self.process()
        self.assertEqual(self.bridge.media.chunks, {})
        self.assertIsNone(self.bridge._response()["path"])

    def test_updated_file_gets_a_distinct_cache(self):
        self.state()
        original = self.bridge.media
        self.video.write_bytes(b"different media content")
        self.state()
        self.assertNotEqual(original.key, self.bridge.media.key)

    def test_seek_order_does_not_change_translation_context_or_output(self):
        self.state()
        self.process()
        self.state(position=61)
        observed = []

        def translate(source, history):
            observed.append(list(history))
            return "Одинаковый перевод."

        self.bridge._translate = translate
        self.process()
        prepared_after_beginning = self.bridge.media.chunks["2"]
        other = FakeBridge(self.root / "other-cache", start_worker=False)
        try:
            other._translate = translate
            other.update({"path": str(self.video), "position": 61, "enabled": True})
            other._process(other.media, 2, other.generation)
            self.assertEqual(observed, [[], []])
            self.assertEqual(prepared_after_beginning, other.media.chunks["2"])
        finally:
            other.close()

    def test_committed_subtitles_reopen_byte_identically_without_models(self):
        self.state()
        media = self.process()
        before = media.srt_path.read_bytes()
        restarted = FakeBridge(self.root / "cache", start_worker=False)
        try:
            restarted._transcribe = mock.Mock(side_effect=AssertionError("Cache must avoid ASR"))
            restarted._translate = mock.Mock(
                side_effect=AssertionError("Cache must avoid translation")
            )
            restarted.update({"path": str(self.video), "position": 1, "enabled": True})
            restarted._process(restarted.media, 0, restarted.generation)
            self.assertEqual(restarted.media.srt_path.read_bytes(), before)
            self.assertEqual(restarted.media.revision, media.revision)
            restarted._transcribe.assert_not_called()
            restarted._translate.assert_not_called()
        finally:
            restarted.close()

    def test_renamed_video_reuses_cache_and_decodes_new_path(self):
        self.state()
        media = self.process()
        renamed = self.video.with_name("renamed tutorial.mp4")
        self.video.rename(renamed)
        self.bridge.update({"path": str(renamed), "position": 31, "enabled": True})
        self.assertEqual(self.bridge.media.key, media.key)
        self.assertEqual(self.bridge.media.source, renamed.resolve())
        decoded_paths = []
        decode = self.bridge._decode_audio
        self.bridge._decode_audio = lambda path, *args: (
            decoded_paths.append(path) or decode(path, *args)
        )
        self.process()
        self.assertEqual(decoded_paths, [renamed.resolve()])
        self.assertEqual(set(media.chunks), {"0", "1"})

    def test_profile_change_preserves_old_cache_and_creates_a_new_one(self):
        self.state()
        original = self.process()
        old = original.json_path.read_bytes()
        other = FakeBridge(
            self.root / "cache",
            start_worker=False,
            profile={"fixture": "different prompt", "seed": 42},
        )
        try:
            other.update({"path": str(self.video), "position": 0, "enabled": True})
            self.assertNotEqual(other.media.key, original.key)
            self.assertEqual(other.media.chunks, {})
            self.assertEqual(original.json_path.read_bytes(), old)
        finally:
            other.close()

    def test_source_mutation_during_translation_cannot_commit_wrong_video_cache(self):
        self.state()
        media = self.bridge.media

        def change_source(*_args):
            self.video.write_bytes(b"new video bytes while translating")
            return "Этот перевод нельзя сохранить."

        self.bridge._translate = change_source
        with self.assertRaisesRegex(RuntimeError, "изменился"):
            self.process()
        self.assertEqual(media.chunks, {})
        self.assertFalse(media.json_path.exists())

    def test_srt_is_repaired_from_authoritative_json_without_regeneration(self):
        self.state()
        media = self.process()
        expected = media.srt_path.read_bytes()
        media.srt_path.write_text("a newer uncommitted or damaged SRT")
        restarted = FakeBridge(self.root / "cache", start_worker=False)
        try:
            restarted.update({"path": str(self.video), "position": 0, "enabled": True})
            self.assertEqual(restarted.media.srt_path.read_bytes(), expected)
            self.assertEqual(restarted.memory_events, [])
        finally:
            restarted.close()

    def test_cache_repair_write_error_never_retranslates_valid_frozen_chunks(self):
        self.state()
        media = self.process()
        expected = media.json_path.read_bytes()
        restarted = FakeBridge(self.root / "cache", start_worker=False)
        try:
            restarted._save = mock.Mock(side_effect=OSError("disk is read-only"))
            with self.assertRaisesRegex(OSError, "read-only"):
                restarted.update({"path": str(self.video), "position": 0, "enabled": True})
            self.assertEqual(restarted.memory_events, [])
            self.assertEqual(media.json_path.read_bytes(), expected)
        finally:
            restarted.close()

    def test_full_preparation_progress_and_cached_middle_are_reported(self):
        self.state(position=121, paused=True)
        media = self.bridge.media
        media.chunks = {"4": [], "5": []}
        progress = self.bridge._response()
        self.assertTrue(progress["current_ready"])
        self.assertEqual(progress["prepared_percent"], 25)
        self.assertEqual(progress["prepared_until"], 0)
        self.assertEqual(progress["buffered_until"], 180)
        media.chunks.update({str(index): [] for index in range(media.chunk_count)})
        self.bridge._save(media)
        self.assertIsNone(self.bridge._next_chunk(media))
        self.assertTrue(self.bridge._response()["done"])
        self.assertEqual(self.bridge._response()["prepared_percent"], 100)
        self.assertTrue(json.loads(media.json_path.read_text())["complete"])

    def test_pages_preserve_the_original_cue_interval(self):
        pages = translated_pages(
            {"start": 10, "end": 18, "source": "original"},
            "Это длинная строка перевода с дополнительными словами. " * 4,
        )
        self.assertEqual(pages[0]["start"], 10)
        self.assertEqual(pages[-1]["end"], 18)
        self.assertTrue(all(a["end"] == b["start"] for a, b in zip(pages, pages[1:], strict=False)))

    def test_short_final_page_does_not_flash_for_less_than_a_second(self):
        translated = " ".join(["перевод"] * 13 + ["конец"])
        pages = translated_pages({"start": 10, "end": 16, "source": "original"}, translated)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(page["end"] - page["start"] >= 1.199 for page in pages))
        self.assertEqual(pages[0]["start"], 10)
        self.assertEqual(pages[-1]["end"], 16)

    def test_invalid_position_and_nonlocal_media_are_rejected(self):
        with self.assertRaises(ValueError):
            self.state(position=float("nan"))
        with self.assertRaises(ValueError):
            self.bridge.update({"path": "https://example.com/video.mp4", "enabled": True})

    def test_http_auth_origin_and_form_boolean_contract(self):
        token = "local-test-token-01234567890123456789"
        server = server_for(self.bridge, token, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, data=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(method, path, body=data, headers=headers or {})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            status, health = request("GET", "/health")
            self.assertEqual(status, 200)
            self.assertNotIn("path", health)
            self.assertEqual(health["version"], "1.4.0")
            self.assertEqual(len(health["runtime_id"]), 64)
            self.assertIn("state_form", health["capabilities"])
            self.assertIn("auto_restart", health["capabilities"])
            self.assertEqual(request("GET", "/status")[0], 401)
            form = urlencode(
                {
                    "path": str(self.video),
                    "position": 0,
                    "enabled": "true",
                    "paused": "false",
                    "native_russian_selected": "false",
                    "waiting_for_translation": "true",
                    "seek_generation": "2",
                }
            )
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            self.assertEqual(request("POST", "/state", form, headers)[0], 401)
            headers["Authorization"] = "Bearer " + token
            headers["Origin"] = "https://untrusted.example"
            self.assertEqual(request("POST", "/state", form, headers)[0], 403)
            del headers["Origin"]
            invalid_form = urlencode(
                {"path": str(self.video), "position": "NaN", "enabled": "true"}
            )
            status, invalid = request("POST", "/state", invalid_form, headers)
            self.assertEqual(status, 400)
            self.assertIn("finite", invalid["error"])
            wrong_type = {**headers, "Content-Type": "text/plain"}
            self.assertEqual(request("POST", "/state", form, wrong_type)[0], 415)
            status, state = request("POST", "/state", form, headers)
            self.assertEqual(status, 200)
            self.assertEqual(state["status"], "processing")
            self.assertEqual(state["runtime_id"], health["runtime_id"])
            self.assertEqual(state["protocol"], health["protocol"])
            self.assertFalse(self.bridge.paused)
            status, diagnostic = request(
                "GET", "/status", headers={"Authorization": "Bearer " + token}
            )
            self.assertEqual(status, 200)
            self.assertEqual(diagnostic["position"], 0)
            self.assertEqual(diagnostic["source"], str(self.video.resolve()))
            self.assertEqual(diagnostic["runtime_id"], health["runtime_id"])
            self.assertFalse(diagnostic["paused"])
            self.assertEqual(
                diagnostic["client"],
                {
                    "native_russian_selected": False,
                    "waiting_for_translation": True,
                    "seek_generation": 2,
                },
            )
            self.assertEqual(self.bridge.memory_events, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
