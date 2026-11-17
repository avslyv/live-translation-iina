"""Benchmark statistics and isolation guards, with no model calls."""

import importlib.util
import io
import json
from pathlib import Path
from unittest import mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "benchmark_native", Path(__file__).resolve().parents[1] / "tools/benchmark_native.py"
)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


@pytest.mark.parametrize(
    "reference,hypothesis,edits,rate",
    [
        ("Hello, world!", "hello world", 0, 0),
        ("one two three", "one three", 1, 1 / 3),
        ("one two", "one new two", 1, 0.5),
        ("one two", "one three", 1, 0.5),
        ("слово", "слово", 0, 0),
        ("one", "one two three", 2, 2),
        ("", "hallucinated", 1, None),
    ],
)
def test_word_error_rate_uses_word_edits_not_translation_scoring(
    reference, hypothesis, edits, rate
):
    result = benchmark.word_error_rate(reference, hypothesis)
    assert result["edits"] == edits
    assert result["wer"] == rate


def test_quality_diagnostics_preserve_boundary_term_and_count_silence_only_cues():
    reference = [{"start": 28.5, "end": 35, "source": "F sharp minor"}]
    cues = [
        {"start": 28.5, "end": 29.9, "source": "F-sharp"},
        {"start": 30, "end": 35, "source": "minor"},
        {"start": 40, "end": 41, "source": "Thanks for watching"},
    ]
    result = benchmark.summarize_source(reference, cues)
    assert result["boundary_term_present"] is True
    assert result["silence_only_cues"] == 1
    assert result["edits"] == 3


def test_rss_normalizes_platform_specific_resource_units():
    assert benchmark.peak_rss_bytes(123, "Darwin") == 123
    assert benchmark.peak_rss_bytes(123, "Linux") == 123 * 1024


def test_audio2_quality_uses_the_french_reference_instead_of_english(tmp_path):
    english = [{"start": 1, "end": 2, "source": "English speech"}]
    french = [{"start": 1, "end": 2, "source": "Une autre piste"}]
    manifest = {"reference_cues": english, "alternate_reference_cues": french}
    assert benchmark.reference_for_variant(manifest, tmp_path, "audio2") == french
    assert benchmark.reference_for_variant(manifest, tmp_path, "audio") == english


def test_srt_clock_has_exact_millisecond_carry():
    assert benchmark.srt_clock(59.9996) == "00:01:00,000"
    assert benchmark.srt_clock(3661.25) == "01:01:01,250"


def test_chat_measurement_preserves_bytes_and_captures_only_token_metadata():
    data = json.dumps(
        {
            "message": {"content": "private content"},
            "eval_count": 19,
            "prompt_eval_count": 80,
            "total_duration": 1234,
        }
    ).encode()
    records = []
    with benchmark.MeasuredChatResponse(io.BytesIO(data), 0, records) as response:
        assert response.read() == data
    assert records[0]["eval_count"] == 19
    assert records[0]["prompt_eval_count"] == 80
    assert records[0]["total_duration"] == 1234
    assert "message" not in records[0]
    assert "private content" not in str(records)


def test_chat_measurement_does_not_suppress_response_errors():
    records = []
    with (
        pytest.raises(RuntimeError, match="original failure"),
        benchmark.MeasuredChatResponse(io.BytesIO(b"incomplete"), 0, records),
    ):
        raise RuntimeError("original failure")
    assert records[0]["error"] == "RuntimeError"


def test_fixture_never_overwrites_existing_files_or_invokes_tts(tmp_path):
    (tmp_path / "private.mkv").write_bytes(b"existing")
    with (
        mock.patch.object(benchmark.subprocess, "run") as process,
        pytest.raises(ValueError, match="empty"),
    ):
        benchmark.fixture(tmp_path, 75, "ffmpeg", "ffprobe")
    process.assert_not_called()
    assert (tmp_path / "private.mkv").read_bytes() == b"existing"


def test_measure_requires_an_empty_new_cache_before_importing_any_worker(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    with (
        mock.patch.object(benchmark.importlib, "import_module") as load,
        pytest.raises(ValueError, match="must not exist"),
    ):
        benchmark.measure(tmp_path, tmp_path, cache, "baseline", tmp_path / "report.json")
    load.assert_not_called()
