"""Fixed inference settings for reproducible, persistently cached subtitles.

Identical prompts use greedy decoding and a fixed seed. This removes sampling
variation; it does not promise bit-identical recomputation across different GPU
kernels, hardware or engine versions. Reusing the committed per-video cache is
the guarantee that the viewer sees the same subtitle text on subsequent plays.

The caller must supply only earlier cues from the *same fixed audio chunk* as
``history``. History from whichever chunks happened to run before this one would
make a seek change the prompt, even with all random sampling disabled.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import random
import re
import urllib.error
import urllib.request
from functools import lru_cache
from importlib import metadata
from pathlib import Path

from live_translation.text_pipeline import live_translation_messages, strip_llm_noise
from live_translation.translators import OllamaTranslator

SEED = 42
PROFILE_VERSION = 1


@lru_cache(maxsize=16)
def _local_file_sha256(path, size, modified_ns, changed_ns):
    """Hash custom local weights once per file version, never once per chunk."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _local_weight_identity(path, *, cached_hf_blob=False):
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    if not resolved.is_file() or stat.st_size < 1:
        raise ValueError("Пустой или некорректный файл модели Whisper")
    # HF LFS blobs are content-addressed by the SHA-256 already verified by the
    # downloader. Resolving the symlink is cheap, and follows a model update to
    # its new digest without rereading 1.6 GB before every playback chunk.
    if cached_hf_blob and re.fullmatch(r"[a-f0-9]{64}", resolved.name):
        digest = resolved.name
    else:
        digest = _local_file_sha256(str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return {"filename": path.name, "sha256": digest, "size": stat.st_size}


def _ollama_manifest_path(model, root):
    if not isinstance(model, str) or not model or "://" in model:
        raise ValueError("Некорректное имя локальной модели Ollama")
    parts = model.split("/")
    registry = "registry.ollama.ai"
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0]):
        registry = parts.pop(0)
    if len(parts) == 1:
        parts.insert(0, "library")
    if len(parts) != 2:
        raise ValueError("Неподдерживаемое имя локальной модели Ollama")
    namespace, tagged_name = parts
    name, separator, tag = tagged_name.partition(":")
    tag = tag if separator else "latest"
    components = (registry, namespace, name, tag)
    if any(
        part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*", part)
        for part in components
    ):
        raise ValueError("Некорректное имя локальной модели Ollama")
    return root / "manifests" / registry / namespace / name / tag


def local_model_identities(whisper_model, translation_model, *, hf_cache=None, ollama_root=None):
    """Resolve exact installed weights without networking or loading a model.

    HF repositories use the locally pinned ``refs/main`` snapshot and its
    content-addressed weights. Custom model folders are hashed with a small
    per-file-version memo. Ollama's digest is the SHA-256 of the exact manifest
    bytes, covering its weights, prompt template and model parameters together.
    File paths/mtimes are not included in the returned identity, so moving an
    unchanged custom model does not change the fingerprint.
    """
    try:
        local = Path(whisper_model).expanduser()
        is_local = local.is_dir()
        if is_local:
            snapshot, revision = local, None
        else:
            if (
                not isinstance(whisper_model, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", whisper_model)
                or any(part in {".", ".."} for part in whisper_model.split("/"))
            ):
                raise ValueError("Неподдерживаемое имя модели Whisper")
            if hf_cache is None:
                hf_cache = os.environ.get("HF_HUB_CACHE")
                if not hf_cache:
                    hf_home = Path(
                        os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface"))
                    )
                    hf_cache = hf_home / "hub"
            repository = Path(hf_cache).expanduser() / (
                "models--" + whisper_model.replace("/", "--")
            )
            revision = (repository / "refs/main").read_text(encoding="utf-8").strip()
            if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
                raise ValueError("Некорректная закреплённая ревизия Whisper")
            snapshot = repository / "snapshots" / revision
        config = snapshot / "config.json"
        config_bytes = config.read_bytes()
        if not isinstance(json.loads(config_bytes), dict):
            raise ValueError("Некорректная конфигурация Whisper")
        weights = next(
            (
                snapshot / name
                for name in ("weights.safetensors", "model.safetensors", "weights.npz")
                if (snapshot / name).is_file()
            ),
            None,
        )
        if weights is None:
            raise FileNotFoundError("Не найдены веса Whisper")
        whisper_identity = {
            "source": "local" if is_local else whisper_model,
            "revision": revision,
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "weights": _local_weight_identity(weights, cached_hf_blob=not is_local),
        }
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError(
            "Не удалось определить установленную локальную модель Whisper. "
            "Проверьте её кеш, конфигурацию и веса."
        ) from error

    try:
        root = Path(
            ollama_root or os.environ.get("OLLAMA_MODELS", str(Path.home() / ".ollama/models"))
        ).expanduser()
        manifest = _ollama_manifest_path(translation_model, root).read_bytes()
        metadata_value = json.loads(manifest)
        if not isinstance(metadata_value, dict) or not metadata_value.get("layers"):
            raise ValueError("Пустой манифест Ollama")
        artifacts = metadata_value["layers"] + [metadata_value["config"]]
        for artifact in artifacts:
            digest = artifact["digest"]
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise ValueError("Некорректная контрольная сумма модели Ollama")
            blob = root / "blobs" / digest.replace(":", "-")
            if not blob.is_file() or blob.stat().st_size != int(artifact["size"]):
                raise ValueError("Не завершена загрузка файлов модели Ollama")
        translation_digest = hashlib.sha256(manifest).hexdigest()
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError(
            "Не удалось определить установленную модель перевода Ollama. "
            f"Проверьте локальную установку {translation_model}."
        ) from error
    return {"whisper_revision": whisper_identity, "translation_digest": translation_digest}


def canonical_json(value):
    """Stable serialization used by both requests and cache fingerprints."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def descriptor_hash(profile):
    return hashlib.sha256(canonical_json(profile).encode("utf-8")).hexdigest()


def whisper_decode_options():
    """Return a fresh dict: a single T=0 pass selects MLX's GreedyDecoder."""
    return {
        "language": "en",
        "task": "transcribe",
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "no_speech_threshold": 0.6,
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
        "temperature": 0.0,
        "beam_size": None,
        "best_of": None,
        "verbose": None,
    }


def seed_asr(seed=SEED):
    """Seed the RNGs used by the speech path, without eagerly importing MLX."""
    import mlx.core as mx
    import numpy as np

    random.seed(seed)
    np.random.seed(seed)
    mx.random.seed(seed)


def translation_options(num_predict, num_ctx=4096):
    """All sampling controls are explicit rather than model/server defaults."""
    num_predict, num_ctx = int(num_predict), int(num_ctx)
    if num_predict < 1 or num_ctx < 1:
        raise ValueError("Token budget and context window must be positive")
    return {
        "temperature": 0.0,
        "seed": SEED,
        "top_k": 1,
        "top_p": 1.0,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
        "repeat_last_n": 0,
        "draft_num_predict": 0,
        "num_predict": num_predict,
        "num_ctx": num_ctx,
    }


def runtime_versions():
    """Inspect package metadata only; never load a model for provenance."""
    result = {"python": platform.python_version(), "machine": platform.machine()}
    for distribution in ("mlx", "mlx-metal", "mlx-whisper", "numpy"):
        try:
            result[distribution] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            result[distribution] = "not installed"
    return result


def profile_descriptor(
    whisper_model,
    translation_model,
    *,
    whisper_revision=None,
    translation_digest=None,
    engine_versions=None,
):
    """Describe the inference contract and artifact identities for JSON caches.

    ``engine_versions`` can also include the running Ollama/FFmpeg versions,
    resolved by the installer/bridge. Artifact digests/revisions must come from
    the installed local weights, not from a remotely mutable model name.
    """
    # A sentinel transcript and history pair include both prompt-template paths
    # without putting actual private transcript content into the fingerprint.
    prompt = live_translation_messages(
        "en", "ru", "__NEW_TRANSCRIPT__", [("__EARLIER_SOURCE__", "__EARLIER_TRANSLATION__")]
    )
    return {
        "profile_version": PROFILE_VERSION,
        "seed": SEED,
        "source_language": "en",
        "target_language": "ru",
        "whisper": {
            "model": str(whisper_model),
            "revision": whisper_revision,
            "options": whisper_decode_options(),
        },
        "translation": {
            "model": str(translation_model),
            "digest": translation_digest,
            "options": translation_options(256),
            "token_budget": {"minimum": 256, "maximum": 1024, "scale": 1.2, "offset": 96},
            "think": False,
            "stream": False,
            "history_scope": "earlier cues in the same fixed audio chunk, in timestamp order",
            "prompt_template_sha256": descriptor_hash(prompt),
        },
        "engine_versions": dict(runtime_versions() if engine_versions is None else engine_versions),
    }


class DeterministicTranslator(OllamaTranslator):
    """Upstream-compatible translator with one fixed, non-streaming request.

    Constructor compatibility allows the existing bridge to switch backends
    without changing model-memory handoff. Supplied ``temperature``/``reasoning``
    fields are deliberately overridden by this backend's fixed contract.
    """

    def translate(self, text, max_tokens=None, on_delta=None, history=None):
        payload = {
            "model": self.model,
            "messages": live_translation_messages(self.source, self.target, text, history),
            "stream": False,
            "think": False,
            "keep_alive": "30m",
            "options": translation_options(
                self.max_tokens if max_tokens is None else max_tokens,
                self.num_ctx or 4096,
            ),
        }
        request = urllib.request.Request(
            self.chat_url,
            data=canonical_json(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Локальный переводчик Ollama не вернул корректный ответ. "
                f"Проверьте модель {self.model}."
            ) from error
        if not isinstance(body, dict) or body.get("error"):
            raise RuntimeError("Локальный переводчик Ollama сообщил об ошибке")
        if body.get("done_reason") == "length":
            raise RuntimeError("Перевод не поместился в заданный лимит токенов")
        content = body.get("message", {})
        content = content.get("content") if isinstance(content, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Локальный переводчик Ollama вернул пустой перевод")
        translated = strip_llm_noise(content)
        if not translated.strip():
            raise RuntimeError("Локальный переводчик Ollama вернул пустой перевод")
        if on_delta is not None:
            with contextlib.suppress(Exception):
                on_delta(translated)
        return translated
