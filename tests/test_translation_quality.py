"""Portable quality contracts; synthetic text/audio and no real model or service."""

import copy
import io
import json
import math
from unittest import mock

import pytest

from live_translation import translation_quality as quality


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Quality unit tests must not call a real model")

    monkeypatch.setattr(quality, "local_urlopen", forbidden)


@pytest.fixture
def cues():
    return [
        {"start": 1, "end": 3, "source": "The GPU processes these numbers."},
        {"start": 4, "end": 6, "source": "It uses a fixed timeline."},
        {"start": 7, "end": 9, "source": "The result is available offline."},
    ]


def response(rows=None, *, content=None, **fields):
    if content is None:
        content = json.dumps({"translations": rows}, ensure_ascii=False)
    return io.BytesIO(json.dumps({"message": {"content": content}, **fields}).encode())


def translated(cues, prefix="Перевод"):
    return [
        {"id": quality.stable_cue_id(cue), "translation": f"{prefix} {index}."}
        for index, cue in enumerate(cues)
    ]


def wire_rows(cues, rows=None):
    """Model-side IDs are scoped to the original fixed batch, including retries."""
    ordered = sorted(cues, key=lambda cue: (cue["start"], cue["end"], quality.stable_cue_id(cue)))
    identifiers = {quality.stable_cue_id(cue): f"t{index}" for index, cue in enumerate(ordered)}
    return [
        {**row, "id": identifiers[row["id"]]}
        for row in (translated(cues) if rows is None else rows)
    ]


def client(**kwargs):
    return quality.OllamaBatchTranslator("gemma4:12b", base_url="http://127.0.0.1:19134", **kwargs)


def test_glossary_canonicalization_is_order_independent_and_does_not_modify_input():
    entries = [
        {"source": "  GPU  ", "target": " графический   процессор "},
        {"source": "cafe\u0301", "target": "кафе"},
    ]
    original = copy.deepcopy(entries)
    expected = [
        {"source": "café", "target": "кафе"},
        {"source": "GPU", "target": "графический процессор"},
    ]
    assert quality.validate_glossary(entries) == expected
    assert quality.validate_glossary(list(reversed(entries))) == expected
    assert entries == original


@pytest.mark.parametrize(
    "entries",
    [
        None,
        "GPU",
        [{"source": "GPU"}],
        [{"source": "GPU", "target": "ГП", "unexpected": True}],
        [{"source": " ", "target": "ГП"}],
        [{"source": "GPU", "target": 42}],
        [{"source": "GPU", "target": "\x00"}],
        [{"source": "x" * 121, "target": "ГП"}],
        [{"source": "GPU", "target": "x" * 241}],
        [{"source": "GPU", "target": "ГП"}, {"source": "gpu", "target": "другое"}],
        [{"source": str(index), "target": "термин"} for index in range(129)],
        [{"source": str(index), "target": "т" * 240} for index in range(128)],
    ],
)
def test_bad_glossaries_are_rejected(entries):
    with pytest.raises(ValueError):
        quality.validate_glossary(entries)


def test_matching_glossary_uses_word_boundaries_and_canonical_order():
    entries = [
        {"source": "GPU", "target": "ГП"},
        {"source": "C++", "target": "C++"},
        {"source": "CPU", "target": "ЦП"},
        {"source": "Straße", "target": "улица"},
    ]
    matches = quality.matching_glossary(entries, ["GPUs differ from GPU. C++17; STRASSE."])
    assert [entry["source"] for entry in matches] == ["C++", "GPU", "Straße"]
    assert quality.matching_glossary(entries, ["GPUs and SuperCPU"]) == []


def test_cue_id_uses_normalized_source_and_fixed_timestamps(cues):
    cue = cues[0]
    assert quality.stable_cue_id(cue) == quality.stable_cue_id(
        {**cue, "start": 1.0, "source": "  The GPU   processes these numbers. "}
    )
    assert quality.stable_cue_id(cue) != quality.stable_cue_id({**cue, "start": 0})
    assert quality.stable_cue_id({**cue, "id": "subtitle:abc-123"}) == "subtitle:abc-123"


@pytest.mark.parametrize(
    "changes",
    [
        {"start": True},
        {"start": -1},
        {"end": 0},
        {"end": math.inf},
        {"start": math.nan},
        {"source": ""},
        {"id": "bad id"},
    ],
)
def test_invalid_cue_identity_is_rejected(cues, changes):
    with pytest.raises(ValueError):
        quality.stable_cue_id({**cues[0], **changes})


def test_context_is_source_only_and_independent_of_input_seek_or_translation_order():
    timeline = [
        {
            "start": index,
            "end": index + 0.8,
            "source": f"source {index}",
            "translation": f"history {index}",
        }
        for index in range(9)
    ]
    target = quality.stable_cue_id(timeline[4])
    first = quality.build_timeline_context(timeline, [target])
    second = quality.build_timeline_context(list(reversed(timeline)), [target])
    assert first == second
    assert [cue["source"] for cue in first] == ["source 2", "source 3", "source 5", "source 6"]
    assert all(set(cue) == {"id", "start", "end", "source"} for cue in first)
    assert all(cue["id"] != target for cue in first)


def test_context_selects_nearest_complete_cues_under_both_budgets():
    timeline = [{"start": i, "end": i + 0.5, "source": str(i) * 10} for i in range(7)]
    target = quality.stable_cue_id(timeline[3])
    assert [
        cue["start"] for cue in quality.build_timeline_context(timeline, [target], max_chars=20)
    ] == [2, 4]
    assert len(quality.build_timeline_context(timeline, [target], max_cues=1)) == 1
    assert quality.build_timeline_context(timeline, [target], before=0, after=0) == []
    assert quality.build_timeline_context(timeline, [target], max_chars=1) == []


def test_context_rejects_unknown_targets_and_duplicate_cue_ids(cues):
    with pytest.raises(ValueError, match="absent"):
        quality.build_timeline_context(cues, ["unknown"])
    with pytest.raises(ValueError, match="Duplicate"):
        quality.build_timeline_context([cues[0], cues[0]], [])


def test_descriptor_captures_prompt_and_glossary_changes():
    initial = quality.quality_descriptor()
    assert initial["quality_version"] == quality.QUALITY_VERSION
    assert len(initial["prompt_sha256"]) == 64
    assert initial != quality.quality_descriptor([{"source": "GPU", "target": "ГП"}])
    assert client().descriptor()["options"]["seed"] == 42
    assert client().descriptor()["options"]["temperature"] == 0


def test_schema_request_uses_local_route_stable_ids_and_fixed_greedy_decoding(cues):
    progress = mock.Mock()
    rows = translated(cues)
    with mock.patch.object(
        quality, "local_urlopen", return_value=response(wire_rows(cues, list(reversed(rows))))
    ) as request:
        result = client().translate(cues, on_cue=progress)
    sent = request.call_args.args[0]
    payload = json.loads(sent.data)
    instruction = json.loads(payload["messages"][1]["content"])
    assert sent.full_url == "http://127.0.0.1:19134/api/chat"
    assert sent.method == "POST"
    assert payload["stream"] is False and payload["think"] is False
    assert payload["options"]["temperature"] == 0
    assert payload["options"]["top_k"] == 1
    assert payload["options"]["seed"] == 42
    assert payload["format"] == instruction["response_schema"]
    ids = [quality.stable_cue_id(cue) for cue in cues]
    assert [cue["id"] for cue in instruction["cues"]] == ["t0", "t1", "t2"]
    assert all(identifier.encode() not in sent.data for identifier in ids)
    assert list(result) == ids
    assert progress.call_args_list == [mock.call(row["id"], row["translation"]) for row in rows]


def test_same_timeline_generates_identical_request_bytes_for_reverse_input(cues):
    requests = []

    def answer(request, **kwargs):
        requests.append(request.data)
        return response(wire_rows(cues))

    with mock.patch.object(quality, "local_urlopen", side_effect=answer):
        client().translate(cues)
        client().translate(list(reversed(cues)))
    assert requests[0] == requests[1]


def test_wire_context_omits_ids_but_preserves_source_order_timing_and_input(cues):
    context = [
        {"id": "opaque-later", "start": 12.0, "end": 14.0, "source": "Later source."},
        {"id": "opaque-earlier", "start": 0.0, "end": 0.5, "source": "Earlier source."},
    ]
    original = copy.deepcopy(context)
    with mock.patch.object(
        quality, "local_urlopen", return_value=response(wire_rows(cues))
    ) as request:
        client().translate(cues, context=context)
    raw = request.call_args.args[0].data
    instruction = json.loads(json.loads(raw)["messages"][1]["content"])
    assert instruction["context"] == [
        {"start": 0.0, "end": 0.5, "source": "Earlier source."},
        {"start": 12.0, "end": 14.0, "source": "Later source."},
    ]
    assert b"opaque-earlier" not in raw and b"opaque-later" not in raw
    assert context == original


def test_out_of_order_wire_reply_restores_explicit_internal_ids_by_mapping():
    cues = [
        {"id": "stable-z", "start": 1, "end": 2, "source": "First source."},
        {"id": "stable-a", "start": 3, "end": 4, "source": "Second source."},
    ]
    rows = [
        {"id": "t1", "translation": "Вторая реплика."},
        {"id": "t0", "translation": "Первая реплика."},
    ]
    progress = mock.Mock()
    with mock.patch.object(quality, "local_urlopen", return_value=response(rows)):
        result = client().translate(list(reversed(cues)), on_cue=progress)
    assert result == {"stable-z": "Первая реплика.", "stable-a": "Вторая реплика."}
    assert progress.call_args_list == [
        mock.call("stable-z", "Первая реплика."),
        mock.call("stable-a", "Вторая реплика."),
    ]


@pytest.mark.parametrize("identifier", ["t9", "T0", "t00", "c000000000000000000000000"])
def test_only_exact_current_batch_wire_ids_are_accepted(cues, identifier):
    progress = mock.Mock()
    with (
        mock.patch.object(
            quality,
            "local_urlopen",
            side_effect=lambda *a, **k: response(
                [{"id": identifier, "translation": "Неверный ID."}]
            ),
        ),
        pytest.raises(quality.BatchTranslationError) as error,
    ):
        client().translate(cues, on_cue=progress)
    assert error.value.partial == {}
    assert error.value.missing_ids == tuple(quality.stable_cue_id(cue) for cue in cues)
    progress.assert_not_called()


def test_already_committed_wire_id_is_invalid_in_missing_only_retry(cues):
    progress = mock.Mock()
    first = {"id": "t0", "translation": "Первый результат."}
    with (
        mock.patch.object(
            quality, "local_urlopen", side_effect=[response([first]), response([first])]
        ) as request,
        pytest.raises(quality.BatchTranslationError) as error,
    ):
        client().translate(cues, on_cue=progress)
    retried = json.loads(
        json.loads(request.call_args_list[1].args[0].data)["messages"][1]["content"]
    )
    assert [cue["id"] for cue in retried["cues"]] == ["t1", "t2"]
    assert error.value.partial == {quality.stable_cue_id(cues[0]): "Первый результат."}
    progress.assert_called_once_with(quality.stable_cue_id(cues[0]), "Первый результат.")


def test_short_wire_ids_are_scoped_per_batch_and_do_not_overwrite_public_results(cues):
    seen = []

    def answer(request, **kwargs):
        instruction = json.loads(json.loads(request.data)["messages"][1]["content"])
        cue = instruction["cues"][0]
        seen.append(cue["id"])
        return response([{"id": cue["id"], "translation": "Перевод: " + cue["source"]}])

    with mock.patch.object(quality, "local_urlopen", side_effect=answer):
        result = client().translate(cues, batch_size=1)
    assert seen == ["t0", "t0", "t0"]
    assert result == {quality.stable_cue_id(cue): "Перевод: " + cue["source"] for cue in cues}
    assert quality.quality_descriptor()["quality_version"] == 2
    assert (
        "wire_ids" in quality.quality_descriptor()
        and "wire_context" in quality.quality_descriptor()
    )


def test_only_missing_cue_is_retried_with_unchanged_context_glossary_and_options(cues):
    rows = translated(cues)
    context = [{"start": 0, "end": 0.8, "source": "GPU is a graphics processor."}]
    progress = mock.Mock()
    with mock.patch.object(
        quality,
        "local_urlopen",
        side_effect=[
            response(wire_rows(cues, [rows[0], rows[2]])),
            response(wire_rows(cues, [rows[1]])),
        ],
    ) as request:
        result = client().translate(
            cues,
            context=context,
            glossary=[{"source": "GPU", "target": "графический процессор"}],
            on_cue=progress,
        )
    payloads = [json.loads(call.args[0].data) for call in request.call_args_list]
    first, retry = [json.loads(payload["messages"][1]["content"]) for payload in payloads]
    assert [cue["id"] for cue in retry["cues"]] == ["t1"]
    assert retry["response_schema"]["properties"]["translations"]["items"]["properties"]["id"][
        "enum"
    ] == ["t1"]
    assert first["context"] == retry["context"]
    assert first["glossary"] == retry["glossary"]
    assert payloads[0]["options"] == payloads[1]["options"]
    assert len(result) == 3 and progress.call_count == 3
    assert len({call.args[0] for call in progress.call_args_list}) == 3


def test_valid_sibling_is_preserved_when_another_translation_string_is_invalid(cues):
    rows = translated(cues[:2])
    first = [rows[0], {**rows[1], "translation": " "}]
    progress = mock.Mock()
    with mock.patch.object(
        quality,
        "local_urlopen",
        side_effect=[
            response(wire_rows(cues[:2], first)),
            response(wire_rows(cues[:2], [rows[1]])),
        ],
    ) as request:
        result = client().translate(cues[:2], on_cue=progress)
    retried = json.loads(
        json.loads(request.call_args_list[1].args[0].data)["messages"][1]["content"]
    )
    assert [cue["id"] for cue in retried["cues"]] == ["t1"]
    assert len(result) == 2 and progress.call_count == 2


def test_duplicate_json_fields_are_rejected_instead_of_using_last_value(cues):
    content = '{"translations": [], "translations": ' + json.dumps(wire_rows(cues)) + "}"
    progress = mock.Mock()
    with (
        mock.patch.object(
            quality, "local_urlopen", side_effect=lambda *a, **k: response(content=content)
        ),
        pytest.raises(quality.BatchTranslationError),
    ):
        client().translate(cues, on_cue=progress)
    progress.assert_not_called()


def test_context_limit_is_enforced_before_any_model_request(cues):
    context = [{"start": index + 20, "end": index + 21, "source": "a" * 4096} for index in range(3)]
    with pytest.raises(ValueError, match="context exceeds"):
        client().translate(cues, context=context)


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        '{"translations": "wrong"}',
        '{"translations": [{"id":"unknown","translation":"чужое"}]}',
        '{"translations": [], "extra": true}',
    ],
)
def test_invalid_structured_response_never_reaches_progress(cues, bad):
    progress = mock.Mock()
    with (
        mock.patch.object(
            quality, "local_urlopen", side_effect=lambda *a, **k: response(content=bad)
        ) as request,
        pytest.raises(quality.BatchTranslationError) as error,
    ):
        client().translate(cues, on_cue=progress)
    assert request.call_count == 2
    assert error.value.partial == {}
    assert set(error.value.missing_ids) == {quality.stable_cue_id(cue) for cue in cues}
    progress.assert_not_called()


@pytest.mark.parametrize("bad_text", [None, 1, " ", "\x00", "x" * 1000])
def test_invalid_translation_strings_cannot_be_committed(cues, bad_text):
    row = {"id": quality.stable_cue_id(cues[0]), "translation": bad_text}
    progress = mock.Mock()
    with (
        mock.patch.object(
            quality, "local_urlopen", side_effect=lambda *a, **k: response(wire_rows(cues, [row]))
        ),
        pytest.raises(quality.BatchTranslationError),
    ):
        client().translate(cues, on_cue=progress)
    progress.assert_not_called()


def test_duplicate_ids_are_rejected_as_ambiguous(cues):
    row = translated(cues)[0]
    with (
        mock.patch.object(
            quality,
            "local_urlopen",
            side_effect=lambda *a, **k: response(wire_rows(cues, [row, row])),
        ),
        pytest.raises(quality.BatchTranslationError),
    ):
        client().translate(cues)


def test_partial_success_survives_missing_retry_failure_and_is_not_sent_again(cues):
    row = translated(cues)[0]
    progress = mock.Mock()
    with (
        mock.patch.object(
            quality,
            "local_urlopen",
            side_effect=[response(wire_rows(cues, [row])), OSError("fixture timeout")],
        ) as request,
        pytest.raises(quality.BatchTranslationError) as error,
    ):
        client().translate(cues, on_cue=progress)
    assert error.value.partial == {row["id"]: row["translation"]}
    assert row["id"] not in error.value.missing_ids
    retried = json.loads(
        json.loads(request.call_args_list[1].args[0].data)["messages"][1]["content"]
    )
    assert [cue["id"] for cue in retried["cues"]] == ["t1", "t2"]
    progress.assert_called_once_with(row["id"], row["translation"])


def test_batch_size_and_text_budget_bound_each_request(cues):
    requests = []

    def answer(request, **kwargs):
        instruction = json.loads(json.loads(request.data)["messages"][1]["content"])
        requests.append(instruction)
        return response(
            [{"id": cue["id"], "translation": "Перевод."} for cue in instruction["cues"]]
        )

    with mock.patch.object(quality, "local_urlopen", side_effect=answer):
        assert len(client().translate(cues, batch_size=1, max_chars=128)) == 3
    assert len(requests) == 3
    assert all(len(request["cues"]) == 1 for request in requests)


@pytest.mark.parametrize(
    "kwargs", [{"batch_size": 0}, {"batch_size": 9}, {"max_chars": 127}, {"max_chars": 12001}]
)
def test_batch_bounds_fail_before_network(cues, kwargs):
    with pytest.raises(ValueError):
        client().translate(cues, **kwargs)


def test_long_single_cue_is_rejected_instead_of_silently_truncated():
    with pytest.raises(ValueError, match="batch text budget"):
        client().translate([{"start": 0, "end": 1, "source": "a" * 129}], max_chars=128)


def test_initial_cancellation_uses_no_model(cues):
    with pytest.raises(quality.TranslationCancelled):
        client().translate(cues, cancelled=lambda: True)


def test_cancellation_while_model_is_answering_commits_no_cues(cues):
    state = {"cancelled": False}
    progress = mock.Mock()

    def answer(*args, **kwargs):
        state["cancelled"] = True
        return response(wire_rows(cues))

    with (
        mock.patch.object(quality, "local_urlopen", side_effect=answer),
        pytest.raises(quality.TranslationCancelled),
    ):
        client().translate(cues, cancelled=lambda: state["cancelled"], on_cue=progress)
    progress.assert_not_called()


def test_no_progress_callback_occurs_after_first_callback_cancels(cues):
    state, accepted = {"cancelled": False}, []

    def on_cue(identifier, text):
        accepted.append(identifier)
        state["cancelled"] = True

    with (
        mock.patch.object(quality, "local_urlopen", return_value=response(wire_rows(cues))),
        pytest.raises(quality.TranslationCancelled),
    ):
        client().translate(cues, cancelled=lambda: state["cancelled"], on_cue=on_cue)
    assert accepted == [quality.stable_cue_id(cues[0])]


@pytest.mark.parametrize(
    "root",
    [
        "https://api.example.com",
        "http://example.com",
        "http://user@127.0.0.1",
        "http://127.0.0.1/path",
        "http://127.0.0.1/?x=y",
    ],
)
def test_remote_or_ambiguous_roots_are_rejected_before_http(root):
    with pytest.raises(ValueError, match="local Ollama"):
        quality.OllamaBatchTranslator("fixture", base_url=root)


def test_audio_gate_is_off_by_default_and_never_changes_the_input():
    samples = [0.0] * 1600
    original = samples.copy()
    gate = quality.conservative_audio_gate(samples)
    assert gate.should_transcribe and gate.reason == "disabled"
    assert samples == original
    assert gate.sample_count == 1600


def test_digital_gate_skips_only_exact_zero_audio():
    assert not quality.conservative_audio_gate([0.0] * 1600, mode="digital").should_transcribe
    assert quality.conservative_audio_gate([0.0] * 1599 + [1e-12], mode="digital").should_transcribe
    assert not quality.conservative_audio_gate([], mode="digital").should_transcribe


def test_experimental_energy_gate_handles_pauses_without_trimming_audio():
    samples = [0.0] * 1600 + [0.1] * 160 + [0.0] * 1600
    original = samples.copy()
    gate = quality.conservative_audio_gate(samples, mode="energy")
    assert gate.should_transcribe
    assert gate.sample_count == len(samples) and samples == original
    assert not quality.conservative_audio_gate([1e-7] * 1600, mode="energy").should_transcribe


def test_music_tones_and_noise_are_not_claimed_to_be_silence():
    tone = [0.01 * math.sin(index * 2 * math.pi * 440 / 16000) for index in range(1600)]
    noise = [0.003 if index % 3 else -0.002 for index in range(1600)]
    for samples in (tone, noise, [0.0] * 1599 + [0.05]):
        gate = quality.conservative_audio_gate(samples, mode="energy")
        assert gate.should_transcribe and gate.reason == "energy_present"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_invalid_audio_is_kept_rather_than_silently_dropped(value):
    assert quality.conservative_audio_gate([0.0, value], mode="energy").should_transcribe


@pytest.mark.parametrize(
    "kwargs",
    [{"mode": "music"}, {"silence_rms": 0.1}, {"silence_rms": math.nan}, {"sample_rate": 1}],
)
def test_audio_gate_rejects_unsafe_configuration(kwargs):
    with pytest.raises(ValueError):
        quality.conservative_audio_gate([0.0], **kwargs)
