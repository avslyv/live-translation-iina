"""Experimental, local Silero speech gating without cutting the audio timeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

MODEL_SHA256 = "e1122837f4154c511485fe0b9c64455f7b929c96fbb8d79fbdb336383ebd3720"
MODEL_PATH = Path(__file__).with_name("assets") / "silero_vad.jit"
THRESHOLD = 0.25
WINDOW_SAMPLES = 512


def vad_descriptor():
    try:
        torch_version = metadata.version("torch")
    except metadata.PackageNotFoundError:
        torch_version = "not installed"
    return {
        "engine": "Silero VAD",
        "version": "6.2.1",
        "model_sha256": MODEL_SHA256,
        "torch": torch_version,
        "threshold": THRESHOLD,
        "sample_rate": 16000,
        "window_samples": WINDOW_SAMPLES,
        "policy": "skip only a whole source window with no speech candidate; never crop or concatenate; reset each window",
    }


@dataclass(frozen=True)
class SpeechGateResult:
    should_transcribe: bool
    max_probability: float
    sample_count: int


class SileroSpeechGate:
    def __init__(self, *, model=None):
        self.model = model

    def _load(self):
        if self.model is None:
            import torch

            if hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest() != MODEL_SHA256:
                raise RuntimeError("Модель определения речи повреждена. Повторите установку.")
            # The pinned upstream model is shipped with the installer. No Hub/network call.
            self.model = torch.jit.load(str(MODEL_PATH), map_location="cpu").eval()
        return self.model

    def analyze(self, samples):
        import numpy as np
        import torch

        audio = np.asarray(samples, dtype=np.float32)
        if audio.ndim != 1 or not np.isfinite(audio).all():
            raise ValueError("VAD expects finite mono float32 samples")
        if not len(audio):
            return SpeechGateResult(False, 0.0, 0)
        model = self._load()
        maximum = 0.0
        model.reset_states()
        try:
            with torch.inference_mode():
                for start in range(0, len(audio), WINDOW_SAMPLES):
                    # Padding is only for the detector. Whisper receives the unchanged original array.
                    window = np.array(audio[start : start + WINDOW_SAMPLES], copy=True)
                    if len(window) < WINDOW_SAMPLES:
                        window = np.pad(window, (0, WINDOW_SAMPLES - len(window)))
                    maximum = max(maximum, float(model(torch.from_numpy(window), 16000).item()))
                    if maximum >= THRESHOLD:
                        return SpeechGateResult(True, maximum, len(audio))
            return SpeechGateResult(False, maximum, len(audio))
        finally:
            model.reset_states()
