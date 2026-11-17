"""Independent source selection/parsing contracts; no inference or real services."""

import hashlib
import shutil
import subprocess
from unittest import mock

import pytest

from live_translation import subtitle_sources as sources
from live_translation.media_cache import SourceChangedError

ENGLISH = """1
00:00:00,250 --> 00:00:01,500
<i>This is the first sentence.</i>

2
00:00:02,000 --> 00:00:04,000
We are learning how the system works.
"""
ASS = r"""[Script Info]
ScriptType: v4.00+
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.25,0:00:01.50,Default,,0,0,0,,{\i1}This is the first sentence.{\i0}
Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Not dialogue.
Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,We are learning\Nhow the system works.
Dialogue: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,{\p1}m 0 0 l 10 10{\p0}
"""


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"local media fixture")
    return path


def external(tmp_path, *, name="video.en.srt", text=ENGLISH, **metadata):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return {"type": "sub", "id": 1, "external": True, "external-filename": str(path), **metadata}


def resolve(media, selection=None, **kwargs):
    return sources.resolve_source(media, selection, ffmpeg="/fixture/ffmpeg", duration=75, **kwargs)


@pytest.mark.parametrize(
    "encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1251"]
)
def test_srt_supported_encodings_keep_words_and_raw_times(encoding):
    text = ENGLISH.replace("first sentence", "first sentence — пример")
    cues = sources.parse_srt(text.encode(encoding))
    assert cues[0] == {"start": 0.25, "end": 1.5, "source": "This is the first sentence — пример."}
    assert cues[1]["source"] == "We are learning how the system works."


def test_srt_discards_invalid_times_missing_times_controls_and_harmful_markup():
    text = """1
00:00:02,000 --> 00:00:01,000
reversed

2
00:99:00,000 --> 01:00:00,000
invalid minutes

3
00:00:03,000 --> 00:00:04,000
<script>do bad things</script><b>Real</b> &amp; <font color="red">safe</font>\u202etext\x01

4
no timestamp
must not appear
"""
    assert sources.parse_srt(text.encode()) == [{"start": 3, "end": 4, "source": "Real & safetext"}]


def test_srt_optional_indexes_cue_settings_duplicates_and_overlap():
    data = b"00:00:01.20 --> 00:00:02.500 X1:0 X2:20\nHello there.\n\n1\n00:00:01.200 --> 00:00:02.500\nHello there.\n\n2\n00:00:01.300 --> 00:00:03.000\nAnother speaker."
    cues = sources.parse_srt(data)
    assert len(cues) == 2
    assert cues[0]["start"] == 1.2
    assert cues[1]["source"] == "Another speaker."


def test_ass_ssa_respects_event_fields_karaoke_drawings_and_commas():
    cues = sources.parse_ass(ASS.encode())
    assert len(cues) == 2
    assert cues == sources.parse_srt(ENGLISH.encode())
    ssa = """[Events]
Format: Marked, End, Start, Text
Dialogue: Marked=0,0:00:03.00,0:00:01.00,{\\k20}Hello, my friend.\\hWelcome.
"""
    assert sources.parse_ass(ssa.encode()) == [
        {"start": 1, "end": 3, "source": "Hello, my friend. Welcome."}
    ]


def test_srt_missing_blank_separator_never_translates_the_next_timestamp_as_text():
    data = b"1\n00:00:01,000 --> 00:00:02,000\nFirst<br>line.\n2\n00:00:03,000 --> 00:00:04,000\nNext line.\n"
    assert sources.parse_srt(data) == [
        {"start": 1, "end": 2, "source": "First line."},
        {"start": 3, "end": 4, "source": "Next line."},
    ]


def test_cleaning_never_emits_ass_drawing_commands_or_unclosed_html_payloads():
    assert (
        sources.clean_subtitle_text(r"{\p1}m 0 0 l 20 30{\p0}Spoken {\pos(1,2)}words.")
        == "Spoken words."
    )
    assert sources.clean_subtitle_text("Hello<script>payload never closes") == "Hello"


def test_auto_uses_full_english_text_without_ffmpeg_or_model_imports(media, tmp_path):
    track = external(tmp_path, lang="eng")
    with mock.patch.object(
        sources.subprocess, "run", side_effect=AssertionError("external text needs no decoder")
    ):
        result = resolve(media, {"mode": "auto", "tracks": [track]})
    assert result.kind == "text"
    assert result.cues == sources.parse_srt(ENGLISH.encode())
    assert result.identity["source_sha256"] == hashlib.sha256(ENGLISH.encode()).hexdigest()
    result.check_inputs()


@pytest.mark.parametrize(
    "metadata",
    [
        {"forced": True},
        {"title": "English signs & songs"},
        {"title": "Forced English"},
        {"title": "Русский · Live Translation (локально)"},
        {"lang": "rus"},
        {"codec": "hdmv_pgs_subtitle", "external": False},
    ],
)
def test_auto_excludes_forced_generated_wrong_language_or_bitmap_sources(media, tmp_path, metadata):
    if metadata.get("codec"):
        track = {"type": "sub", "id": 1, **metadata}
    else:
        track = (
            external(tmp_path, lang="en", **metadata)
            if "lang" not in metadata
            else external(tmp_path, **metadata)
        )
    result = resolve(media, {"mode": "auto", "tracks": [track]})
    assert result.kind == "audio"
    assert result.audio_map == "0:a:0"


def test_auto_prefers_full_track_over_selected_forced_track(media, tmp_path):
    forced = external(tmp_path, name="forced.en.srt", id=1, lang="en", forced=True, selected=True)
    full = external(tmp_path, name="full.en.srt", id=2, lang="en", title="Full dialogue")
    result = resolve(media, {"mode": "auto", "subtitle_track_id": 1, "tracks": [forced, full]})
    assert result.kind == "text"
    assert result.identity["track"]["title"] == "Full dialogue"


def test_untagged_text_uses_conservative_english_heuristic(media, tmp_path):
    english = external(tmp_path, name="video.srt")
    assert resolve(media, {"mode": "auto", "tracks": [english]}).kind == "text"
    french = external(
        tmp_path,
        name="video.srt",
        text=ENGLISH.replace(
            "This is the first sentence.", "Bonjour le monde et tous les amis."
        ).replace(
            "We are learning how the system works.",
            "Nous apprenons ensemble dans cette belle maison.",
        ),
    )
    assert resolve(media, {"mode": "auto", "tracks": [french]}).kind == "audio"


@pytest.mark.parametrize("language", ["es", "fr"])
def test_untagged_english_is_not_inferred_as_another_source_language(media, tmp_path, language):
    track = external(tmp_path, name="video.srt")
    assert (
        resolve(media, {"mode": "auto", "tracks": [track]}, source_language=language).kind
        == "audio"
    )
    track["lang"] = language
    assert (
        resolve(media, {"mode": "auto", "tracks": [track]}, source_language=language).kind == "text"
    )


def test_sparse_unlabelled_signs_only_source_falls_back_to_audio(media, tmp_path):
    track = external(
        tmp_path, lang="en", text="1\n00:00:01,000 --> 00:00:02,000\nPlease keep the door closed.\n"
    )
    result = sources.resolve_source(
        media, {"mode": "auto", "tracks": [track]}, ffmpeg="unused", duration=3600
    )
    assert result.kind == "audio"


def test_explicit_non_english_srt_is_supported_and_delays_stay_raw(media, tmp_path):
    track = external(
        tmp_path,
        name="video.ru.srt",
        lang="ru",
        text="1\n00:00:01,000 --> 00:00:02,000\nПривет, мир.\n",
    )
    result = resolve(
        media,
        {
            "mode": "subtitles",
            "subtitle_track_id": 1,
            "tracks": [track],
            "audio_delay": -0.5,
            "subtitle_delay": 1.25,
        },
        source_language="ru",
    )
    assert result.cues == [{"start": 1, "end": 2, "source": "Привет, мир."}]
    assert result.audio_delay == -0.5
    assert result.subtitle_delay == 1.25
    assert result.identity["subtitle_delay"] == 1.25


def test_explicit_missing_or_unsupported_subtitle_does_not_silently_use_audio(media):
    with pytest.raises(sources.SubtitleSourceError, match="no longer available"):
        resolve(media, {"mode": "subtitles", "subtitle_track_id": 9, "tracks": []})
    with pytest.raises(sources.SubtitleSourceError, match="not supported text"):
        resolve(
            media,
            {
                "mode": "subtitles",
                "subtitle_track_id": 1,
                "tracks": [{"type": "sub", "id": 1, "codec": "dvd_subtitle"}],
            },
        )


def test_selected_audio_uses_global_index_and_falls_back_to_audio_ordinal(media):
    tracks = [
        {"type": "video", "id": 1, "ff-index": 0},
        {"type": "audio", "id": 1, "ff-index": 2},
        {"type": "audio", "id": 2, "ff-index": 4},
    ]
    result = resolve(media, {"mode": "audio", "audio_track_id": "2", "tracks": tracks})
    assert result.audio_map == "0:4"
    tracks[2].pop("ff-index")
    result = resolve(media, {"mode": "audio", "audio_track_id": 2, "tracks": tracks})
    assert result.audio_map == "0:a:1"
    tracks[2]["ff-index"] = -1
    assert (
        resolve(media, {"mode": "audio", "audio_track_id": 2, "tracks": tracks}).audio_map
        == "0:a:1"
    )


def test_selected_audio_missing_or_external_fails_closed(media):
    with pytest.raises(sources.SubtitleSourceError, match="no longer available"):
        resolve(
            media, {"mode": "audio", "audio_track_id": 2, "tracks": [{"type": "audio", "id": 1}]}
        )


def test_audio_ordinal_fallback_does_not_count_external_audio_inputs(media):
    tracks = [
        {"type": "audio", "id": 1, "external": True},
        {"type": "audio", "id": 2},
    ]
    assert (
        resolve(media, {"mode": "audio", "audio_track_id": 2, "tracks": tracks}).audio_map
        == "0:a:0"
    )
    with pytest.raises(sources.SubtitleSourceError, match="External audio"):
        resolve(
            media,
            {
                "mode": "audio",
                "audio_track_id": 1,
                "tracks": [{"type": "audio", "id": 1, "external": True}],
            },
        )


def test_current_native_audio_selection_and_legacy_empty_selection(media):
    tracks = [{"type": "audio", "id": 1}, {"type": "audio", "id": 2, "selected": True}]
    assert resolve(media, {"mode": "auto", "tracks": tracks}).audio_map == "0:a:1"
    legacy = resolve(media)
    assert legacy.kind == "audio"
    assert legacy.audio_map == "0:a:0"
    assert legacy.identity["source_sha256"] == hashlib.sha256(media.read_bytes()).hexdigest()


def test_source_identity_changes_with_mapping_delays_and_external_bytes(media, tmp_path):
    track = external(tmp_path, lang="en")
    selection = {"mode": "auto", "tracks": [track]}
    first = resolve(media, selection)
    assert first.identity != resolve(media, {**selection, "subtitle_delay": 1}).identity
    assert first.identity != resolve(media, {**selection, "audio_delay": 1}).identity
    assert (
        first.identity
        != resolve(
            media, {**selection, "tracks": [track, {"type": "audio", "id": 1, "ff-index": 2}]}
        ).identity
    )
    path = tmp_path / "video.en.srt"
    path.write_text(ENGLISH.replace("first sentence", "second sentence"))
    assert first.identity != resolve(media, selection).identity
    with pytest.raises(SourceChangedError):
        first.check_inputs()


def test_player_track_id_renumbering_does_not_change_source_cache_identity(media, tmp_path):
    subtitle = external(tmp_path, id=1, lang="en", title="English full dialogue")
    audio = {"type": "audio", "id": 1, "ff-index": 2, "lang": "eng", "title": "English"}
    first = resolve(
        media,
        {"mode": "auto", "audio_track_id": 1, "subtitle_track_id": 1, "tracks": [audio, subtitle]},
    )
    renamed = resolve(
        media,
        {
            "mode": "auto",
            "audio_track_id": 40,
            "subtitle_track_id": 70,
            "tracks": [{**audio, "id": 40}, {**subtitle, "id": 70}],
        },
    )
    assert first.identity == renamed.identity
    assert "id" not in first.identity["track"]
    assert "id" not in first.identity["audio_track"]


def test_check_inputs_detects_changed_or_removed_media(media):
    result = resolve(media)
    media.write_bytes(b"different content")
    with pytest.raises(SourceChangedError):
        result.check_inputs()
    media.unlink()
    with pytest.raises(SourceChangedError):
        result.check_inputs()


def test_verified_media_hash_avoids_second_full_file_read(media):
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    with mock.patch.object(
        sources, "fingerprint_file", side_effect=AssertionError("must reuse verified hash")
    ):
        result = resolve(media, media_sha256=digest)
    assert result.identity["source_sha256"] == digest


def test_source_replaced_by_non_regular_file_is_reported_as_changed(media):
    result = resolve(media)
    media.unlink()
    media.mkdir()
    with pytest.raises(SourceChangedError):
        result.check_inputs()


@pytest.mark.parametrize(
    "field,value",
    [
        ("audio_delay", float("nan")),
        ("subtitle_delay", float("inf")),
        ("subtitle_delay", 3601),
        ("audio_delay", True),
        ("audio_delay", "0.3"),
        ("mode", []),
    ],
)
def test_invalid_selection_is_rejected(media, field, value):
    with pytest.raises(sources.SubtitleSourceError):
        resolve(media, {field: value})


def test_external_network_paths_and_oversized_files_are_not_read(media, tmp_path, monkeypatch):
    track = {"type": "sub", "id": 1, "external-filename": "file://remote/video.srt"}
    with pytest.raises(sources.SubtitleSourceError, match="Only local"):
        resolve(media, {"mode": "subtitles", "tracks": [track]})
    track = external(tmp_path, lang="en")
    monkeypatch.setattr(sources, "MAX_SUBTITLE_BYTES", 8)
    with pytest.raises(sources.SubtitleSourceError, match="16 MiB"):
        resolve(media, {"mode": "subtitles", "tracks": [track]})


def test_embedded_ordinal_excludes_external_tracks_and_output_is_bounded(media, tmp_path):
    sidecar = external(tmp_path, lang="ru")
    embedded = {"type": "sub", "id": 2, "codec": "subrip", "lang": "en"}

    def extract(command, **kwargs):
        assert command[command.index("-map") + 1] == "0:s:0"
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert kwargs["timeout"] == sources.EXTRACTION_TIMEOUT
        assert "-fs" in command
        kwargs["stdout"].write(ENGLISH.encode())

    with mock.patch.object(sources.subprocess, "run", side_effect=extract):
        result = resolve(media, {"mode": "auto", "tracks": [sidecar, embedded]})
    assert result.kind == "text"
    assert result.cues == sources.parse_srt(ENGLISH.encode())


def test_failed_embedded_extraction_falls_back_only_in_auto_mode(media):
    track = {"type": "sub", "id": 1, "codec": "subrip", "lang": "en", "ff-index": 3}
    with mock.patch.object(
        sources.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)
    ):
        assert resolve(media, {"mode": "auto", "tracks": [track]}).kind == "audio"
        with pytest.raises(sources.SubtitleSourceError, match="Could not extract"):
            resolve(media, {"mode": "subtitles", "tracks": [track]})


def test_oversized_extraction_is_rejected_before_parsing(media, monkeypatch):
    monkeypatch.setattr(sources, "MAX_SUBTITLE_BYTES", 8)
    track = {"type": "sub", "id": 1, "codec": "subrip", "lang": "en"}

    def extract(_command, **kwargs):
        kwargs["stdout"].write(ENGLISH.encode())

    with (
        mock.patch.object(sources.subprocess, "run", side_effect=extract),
        pytest.raises(sources.SubtitleSourceError, match="16 MiB"),
    ):
        resolve(media, {"mode": "subtitles", "tracks": [track]})


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed")
@pytest.mark.parametrize("format_name", ["srt", "ass", "mov_text"])
def test_real_ffmpeg_mux_and_embedded_extraction_preserve_text_and_times(tmp_path, format_name):
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subtitle = tmp_path / ("source.ass" if format_name == "ass" else "source.srt")
    subtitle.write_text(ASS if format_name == "ass" else ENGLISH)
    media = tmp_path / ("real-source.mp4" if format_name == "mov_text" else "real-source.mkv")
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=1:d=5",
            "-i",
            str(subtitle),
            "-map",
            "0:v:0",
            "-map",
            "1:s:0",
            "-c:v",
            "mpeg4" if format_name == "mov_text" else "ffv1",
            "-c:s",
            format_name,
            "-t",
            "5",
            str(media),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    result = sources.resolve_source(
        media,
        {
            "mode": "auto",
            "tracks": [{"type": "sub", "id": 1, "ff-index": 1, "codec": format_name, "lang": "en"}],
        },
        ffmpeg=ffmpeg,
        duration=5,
    )
    assert result.kind == "text"
    assert result.cues == sources.parse_srt(ENGLISH.encode())
    result.check_inputs()
