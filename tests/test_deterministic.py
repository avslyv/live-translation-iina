import copy
import hashlib
import json
import sys
import types
import urllib.error
from unittest import mock

import pytest

from live_translation.deterministic import (
    SEED,
    DeterministicTranslator,
    descriptor_hash,
    local_model_identities,
    profile_descriptor,
    seed_asr,
    translation_options,
    whisper_decode_options,
)


class Response:
    def __init__(self, body):
        self.raw = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.raw


def translator(**overrides):
    arguments = {
        "model": "gemma4:12b",
        "target": "ru",
        "source": "en",
        "url": "http://127.0.0.1:11434",
        "max_tokens": 256,
        "temperature": 0.0,
        "reasoning": False,
        "num_ctx": 4096,
    }
    return DeterministicTranslator(**(arguments | overrides))


def test_identical_input_has_identical_wire_request_and_fixed_sampling(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append((request.full_url, request.data, timeout))
        return Response({"message": {"content": "фа-диез минор"}, "done_reason": "stop"})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    engine = translator(temperature=0.95, reasoning=True)
    history = [("This riff is in E minor.", "Этот рифф в ми миноре.")]
    before = copy.deepcopy(history)
    first = engine.translate("F sharp minor", max_tokens=301, history=history)
    second = engine.translate("F sharp minor", max_tokens=301, history=history)
    assert first == second == "фа-диез минор"
    assert requests[0] == requests[1]
    assert requests[0][0] == "http://127.0.0.1:11434/api/chat"
    assert requests[0][2] == 120
    body = json.loads(requests[0][1])
    assert body["options"] == translation_options(301, 4096)
    assert body["options"]["seed"] == SEED
    assert body["options"]["temperature"] == 0
    assert body["options"]["top_k"] == 1
    assert body["options"]["draft_num_predict"] == 0
    assert body["think"] is False
    assert body["stream"] is False
    assert history == before


def test_no_hidden_conversation_state_leaks_between_calls(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append(request.data)
        return Response({"message": {"content": "перевод"}})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    engine = translator()
    engine.translate("Middle", history=[])
    engine.translate("Unrelated beginning", history=[("old", "старое")])
    engine.translate("Middle", history=[])
    assert requests[0] == requests[2]
    assert b"old" not in requests[2]
    assert len(json.loads(requests[2])["messages"]) == 2


def test_callback_gets_only_the_complete_committable_translation(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: Response({"message": {"content": "<think>hidden</think>Привет"}}),
    )
    values = []
    assert translator().translate("hello", on_delta=values.append) == "Привет"
    assert values == ["Привет"]


@pytest.mark.parametrize(
    "body",
    [
        {"message": {"content": ""}},
        {"message": {"content": 123}},
        {"message": "invalid"},
        {"error": "missing model"},
        {"message": {"content": "incomplete"}, "done_reason": "length"},
        [],
    ],
)
def test_unusable_or_truncated_responses_are_not_cached_as_translation(monkeypatch, body):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(body))
    with pytest.raises(RuntimeError):
        translator().translate("hello")


def test_http_error_becomes_runtime_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(RuntimeError, match="Ollama"):
        translator().translate("hello")


def test_profile_hash_captures_model_artifacts_options_and_prompt_but_not_dict_order():
    profile = profile_descriptor(
        "whisper",
        "gemma",
        whisper_revision="commit-a",
        translation_digest="sha256:one",
        engine_versions={"mlx": "fixed"},
    )
    repeat = profile_descriptor(
        "whisper",
        "gemma",
        whisper_revision="commit-a",
        translation_digest="sha256:one",
        engine_versions={"mlx": "fixed"},
    )
    assert descriptor_hash(profile) == descriptor_hash(repeat)
    assert descriptor_hash(profile) == descriptor_hash(dict(reversed(list(profile.items()))))
    changed = copy.deepcopy(profile)
    changed["translation"]["digest"] = "sha256:two"
    assert descriptor_hash(changed) != descriptor_hash(profile)
    assert len(profile["translation"]["prompt_template_sha256"]) == 64
    assert profile["whisper"]["options"]["temperature"] == 0
    assert "same fixed audio chunk" in profile["translation"]["history_scope"]
    repeat["translation"]["options"]["seed"] = 99
    assert profile["translation"]["options"]["seed"] == 42


def test_options_are_fresh_and_disable_sampling_fallback():
    first = whisper_decode_options()
    assert first["temperature"] == 0.0
    assert isinstance(first["temperature"], float)
    assert first["beam_size"] is None
    assert first["best_of"] is None
    assert first["condition_on_previous_text"] is False
    first["temperature"] = 1
    assert whisper_decode_options()["temperature"] == 0
    with pytest.raises(ValueError):
        translation_options(0)


def test_seed_asr_sets_all_used_rngs_without_importing_torch():
    mlx_random = mock.Mock()
    numpy_random = mock.Mock()
    fake_mlx = types.ModuleType("mlx")
    fake_core = types.ModuleType("mlx.core")
    fake_core.random = mlx_random
    fake_mlx.core = fake_core
    fake_numpy = types.ModuleType("numpy")
    fake_numpy.random = numpy_random
    with (
        mock.patch.dict(sys.modules, {"mlx": fake_mlx, "mlx.core": fake_core, "numpy": fake_numpy}),
        mock.patch("random.seed") as python_seed,
    ):
        seed_asr()
    python_seed.assert_called_once_with(42)
    numpy_random.seed.assert_called_once_with(42)
    mlx_random.seed.assert_called_once_with(42)


def model_fixture(tmp_path):
    hf = tmp_path / "hf"
    repository = hf / "models--mlx-community--whisper-test"
    revision = "a" * 40
    snapshot = repository / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (repository / "refs").mkdir()
    (repository / "refs/main").write_text(revision)
    blobs = repository / "blobs"
    blobs.mkdir()
    weights = b"test weights"
    digest = hashlib.sha256(weights).hexdigest()
    (blobs / digest).write_bytes(weights)
    (snapshot / "weights.safetensors").symlink_to(blobs / digest)
    (snapshot / "config.json").write_text('{"model_type":"whisper"}')
    ollama = tmp_path / "ollama"
    (ollama / "blobs").mkdir(parents=True)
    layer, configuration = [], None
    for kind, raw in (("model", b"gguf"), ("template", b"prompt"), ("config", b"{}")):
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        (ollama / "blobs" / digest.replace(":", "-")).write_bytes(raw)
        artifact = {"digest": digest, "size": len(raw), "mediaType": kind}
        if kind == "config":
            configuration = artifact
        else:
            layer.append(artifact)
    manifest = ollama / "manifests/registry.ollama.ai/library/gemma4/12b"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"layers": layer, "config": configuration}))
    return hf, repository, snapshot, ollama, manifest


def test_local_model_identities_are_stable_offline_and_cover_manifest_and_hf_snapshot(
    tmp_path, monkeypatch
):
    hf, repository, snapshot, ollama, manifest = model_fixture(tmp_path)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("Artifact identity must not access the network"),
    )
    identity = local_model_identities(
        "mlx-community/whisper-test", "gemma4:12b", hf_cache=hf, ollama_root=ollama
    )
    assert identity["translation_digest"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert identity["whisper_revision"]["revision"] == "a" * 40
    assert (
        identity["whisper_revision"]["weights"]["sha256"]
        == (snapshot / "weights.safetensors").resolve().name
    )
    assert identity["whisper_revision"]["weights"]["size"] == len(b"test weights")
    assert "mtime" not in json.dumps(identity)
    again = local_model_identities(
        "mlx-community/whisper-test", "gemma4:12b", hf_cache=hf, ollama_root=ollama
    )
    assert again == identity
    # A model tag can change templates/parameters while keeping its weight blob.
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    changed = local_model_identities(
        "mlx-community/whisper-test", "gemma4:12b", hf_cache=hf, ollama_root=ollama
    )
    assert changed["translation_digest"] != identity["translation_digest"]


def test_hf_model_update_follows_pinned_ref_without_rereading_large_blob(tmp_path, monkeypatch):
    hf, repository, snapshot, ollama, _manifest = model_fixture(tmp_path)
    previous = local_model_identities(
        "mlx-community/whisper-test", "gemma4:12b", hf_cache=hf, ollama_root=ollama
    )
    next_snapshot = repository / "snapshots" / ("b" * 40)
    next_snapshot.mkdir()
    (next_snapshot / "config.json").write_bytes((snapshot / "config.json").read_bytes())
    (next_snapshot / "weights.safetensors").symlink_to((snapshot / "weights.safetensors").resolve())
    (repository / "refs/main").write_text("b" * 40)
    monkeypatch.setattr(
        "live_translation.deterministic._local_file_sha256",
        lambda *_args: pytest.fail("Content-addressed HF blobs do not need a full rehash"),
    )
    changed = local_model_identities(
        "mlx-community/whisper-test", "gemma4:12b", hf_cache=hf, ollama_root=ollama
    )
    assert previous["whisper_revision"]["revision"] != changed["whisper_revision"]["revision"]


def test_custom_local_model_identity_survives_move_but_detects_weight_changes(tmp_path):
    hf, _repository, snapshot, ollama, _manifest = model_fixture(tmp_path)
    local = tmp_path / "custom"
    local.mkdir()
    (local / "config.json").write_bytes((snapshot / "config.json").read_bytes())
    (local / "weights.safetensors").write_bytes(b"original custom weights")
    before = local_model_identities(str(local), "gemma4:12b", hf_cache=hf, ollama_root=ollama)
    moved = tmp_path / "moved"
    local.rename(moved)
    after_move = local_model_identities(str(moved), "gemma4:12b", hf_cache=hf, ollama_root=ollama)
    assert before == after_move
    (moved / "weights.safetensors").write_bytes(b"different custom weights")
    after_edit = local_model_identities(str(moved), "gemma4:12b", hf_cache=hf, ollama_root=ollama)
    assert after_edit["whisper_revision"] != before["whisper_revision"]


@pytest.mark.parametrize(
    "damage", ["missing ref", "invalid ref", "missing weights", "missing blob", "invalid model"]
)
def test_incomplete_or_unsupported_model_installation_fails_clearly(tmp_path, damage):
    hf, repository, snapshot, ollama, manifest = model_fixture(tmp_path)
    model = "gemma4:12b"
    if damage == "missing ref":
        (repository / "refs/main").unlink()
    elif damage == "invalid ref":
        (repository / "refs/main").write_text("../../outside")
    elif damage == "missing weights":
        (snapshot / "weights.safetensors").unlink()
    elif damage == "missing blob":
        artifact = json.loads(manifest.read_text())["layers"][0]
        (ollama / "blobs" / artifact["digest"].replace(":", "-")).unlink()
    else:
        model = "../gemma4:12b"
    with pytest.raises(RuntimeError, match="модел"):
        local_model_identities("mlx-community/whisper-test", model, hf_cache=hf, ollama_root=ollama)
