"""Detector decisions never modify or shorten audio sent to recognition."""

import hashlib

import numpy as np
import pytest

from live_translation.speech_vad import MODEL_PATH, MODEL_SHA256, SileroSpeechGate


class Probability:
    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value


class FakeDetector:
    def __init__(self, values):
        self.values = iter(values)
        self.resets = 0
        self.seen = []

    def reset_states(self):
        self.resets += 1

    def __call__(self, frame, sr):
        self.seen.append((frame.numpy().copy(), sr))
        return Probability(next(self.values))


def test_shipped_model_matches_pinned_upstream_digest():
    assert hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest() == MODEL_SHA256


def test_quiet_speech_candidate_keeps_the_entire_original_window():
    pytest.importorskip("torch")
    model = FakeDetector([0.01, 0.30])
    audio = np.linspace(-0.001, 0.001, 1100, dtype=np.float32)
    before = audio.copy()
    result = SileroSpeechGate(model=model).analyze(audio)
    assert result.should_transcribe
    assert result.sample_count == len(audio)
    np.testing.assert_array_equal(audio, before)
    assert model.resets == 2


def test_non_speech_windows_are_skipped_and_tail_padding_is_detector_only():
    pytest.importorskip("torch")
    model = FakeDetector([0.001, 0.002])
    audio = np.zeros(513, dtype=np.float32)
    result = SileroSpeechGate(model=model).analyze(audio)
    assert not result.should_transcribe
    assert len(audio) == result.sample_count == 513
    assert [len(frame) for frame, _ in model.seen] == [512, 512]
    assert model.resets == 2


def test_real_shipped_model_rejects_digital_silence_and_synthetic_tone():
    pytest.importorskip("torch")
    gate = SileroSpeechGate()
    assert not gate.analyze(np.zeros(16000, dtype=np.float32)).should_transcribe
    tone = (0.1 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.float32)
    assert not gate.analyze(tone).should_transcribe
