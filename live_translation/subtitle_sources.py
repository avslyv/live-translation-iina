"""Resolve local text subtitles or the selected audio without importing models.

Text cue timestamps are the unshifted source timeline. ``subtitle_delay`` and
``audio_delay`` are validated metadata, never applied here: the player applies
its global subtitle delay to generated native tracks as well. The bridge owns
that final timeline conversion and must not apply the same delay twice.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .media_cache import SourceChangedError, fingerprint_file, source_signature

MAX_SUBTITLE_BYTES = 16 * 1024 * 1024
MAX_CUES = 100_000
MAX_CUE_CHARS = 16_384
EXTRACTION_TIMEOUT = 30
PARSER_VERSION = 1
_FORMATS = {"srt": "srt", "subrip": "srt", "ass": "ass", "ssa": "ass", "mov_text": "srt"}
_SUFFIXES = {".srt": "srt", ".ass": "ass", ".ssa": "ass"}
_CLOCK = r"(\d{1,3}):([0-5]\d):([0-5]\d)[,.](\d{1,3})"
_SRT_TIMING = re.compile(r"^\s*" + _CLOCK + r"\s*-->\s*" + _CLOCK + r"(?:\s+.*)?$")
_ASS_TIME = re.compile(r"^\s*" + _CLOCK + r"\s*$")
_ENGLISH_WORDS = frozenset(
    [
        "the",
        "this",
        "that",
        "these",
        "those",
        "is",
        "are",
        "was",
        "were",
        "you",
        "your",
        "we",
        "they",
        "their",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "with",
        "from",
        "what",
        "when",
        "where",
        "how",
        "would",
        "should",
        "could",
        "not",
        "it",
        "its",
        "to",
        "of",
        "for",
        "and",
        "in",
        "on",
        "but",
        "at",
        "be",
        "will",
        "our",
        "there",
        "here",
        "because",
        "about",
        "as",
        "an",
        "if",
        "then",
        "than",
        "can",
        "cannot",
        "into",
        "which",
        "who",
        "all",
        "just",
        "so",
        "my",
        "i",
        "he",
        "she",
        "his",
        "her",
        "we're",
        "you're",
        "it's",
        "don't",
    ]
)


class SubtitleSourceError(ValueError):
    """The requested source cannot be used without choosing different content."""


@dataclass(frozen=True)
class SourceSignature:
    path: Path
    signature: tuple

    def check(self) -> None:
        try:
            matches = source_signature(self.path) == self.signature
        except (OSError, ValueError) as error:
            raise SourceChangedError("The selected media or subtitle source disappeared") from error
        if not matches:
            raise SourceChangedError("The selected media or subtitle source changed")


@dataclass(frozen=True)
class ResolvedSource:
    kind: str
    label: str
    identity: dict
    cues: list[dict]
    audio_map: str
    audio_delay: float
    subtitle_delay: float
    signatures: tuple[SourceSignature, ...]
    diagnostics: tuple[str, ...] = ()

    def check_inputs(self) -> None:
        for signature in self.signatures:
            signature.check()


def _clock(groups) -> float:
    hours, minutes, seconds, fraction = groups
    return (
        int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction) / 10 ** len(fraction)
    )


def decode_subtitle(data: bytes) -> str:
    """Decode common subtitle encodings strictly; do not replace lost words."""
    if len(data) > MAX_SUBTITLE_BYTES:
        raise SubtitleSourceError("The subtitle file exceeds the 16 MiB limit")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    elif data.startswith(b"\xef\xbb\xbf"):
        encodings = ("utf-8-sig",)
    elif b"\x00" in data:
        # UTF-16 without BOM: a timestamp is ASCII, so its zero-byte lane is
        # deterministic. Reject other NUL-containing encodings rather than
        # silently interpreting binary input as a legacy single-byte subtitle.
        even = data[::2].count(0)
        odd = data[1::2].count(0)
        encodings = ("utf-16-le" if odd >= even else "utf-16-be",)
    else:
        encodings = ("utf-8", "cp1251")
    for encoding in encodings:
        try:
            return data.decode(encoding).lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeError:
            continue
    raise SubtitleSourceError("The subtitle text has an unsupported or damaged encoding")


def clean_subtitle_text(text: str) -> str:
    """Keep spoken text, discarding formatting, ASS drawings and HTML payloads."""
    drawing = False
    kept = []
    for part in re.split(r"(\{[^}\n]*\})", text):
        if part.startswith("{\\") and part.endswith("}"):
            modes = re.findall(r"\\p(\d+)(?=\b|\\|})", part)
            if modes:
                drawing = int(modes[-1]) != 0
            continue
        if not drawing:
            kept.append(part)
    text = "".join(kept).replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    text = html.unescape(text)
    text = re.sub(
        r"<(script|style|iframe|object)\b[^>]*>.*?(?:</\1\s*>|$)", " ", text, flags=re.I | re.S
    )
    text = re.sub(r"<br\s*/?>|</(?:p|div)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>\n]{1,2048}>", "", text)
    text = "".join(
        char for char in text if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    return re.sub(r"\s+", " ", text).strip()


def _validated_cues(rows) -> list[dict]:
    cues = []
    seen = set()
    for start, end, text in rows:
        text = clean_subtitle_text(text)
        if not text or not 0 <= start < end <= 7 * 86400:
            continue
        if len(text) > MAX_CUE_CHARS:
            raise SubtitleSourceError("A subtitle cue exceeds the supported text limit")
        key = (round(start, 3), round(end, 3), text)
        if key not in seen:
            seen.add(key)
            cues.append({"start": key[0], "end": key[1], "source": text})
        if len(cues) > MAX_CUES:
            raise SubtitleSourceError("The subtitle source contains too many cues")
    return sorted(cues, key=lambda cue: (cue["start"], cue["end"], cue["source"]))


def parse_srt(data: bytes) -> list[dict]:
    rows = []
    lines = decode_subtitle(data).splitlines()
    index = 0
    while index < len(lines):
        timing = _SRT_TIMING.fullmatch(lines[index])
        index += 1
        if not timing:
            continue
        text = []
        while index < len(lines):
            line = lines[index]
            if not line.strip() or "-->" in line:
                break
            if line.strip().isdigit() and index + 1 < len(lines) and "-->" in lines[index + 1]:
                break
            text.append(line)
            index += 1
        groups = timing.groups()
        rows.append((_clock(groups[:4]), _clock(groups[4:]), "\n".join(text)))
    return _validated_cues(rows)


def parse_ass(data: bytes) -> list[dict]:
    fields = [
        "layer",
        "start",
        "end",
        "style",
        "name",
        "marginl",
        "marginr",
        "marginv",
        "effect",
        "text",
    ]
    events = False
    rows = []
    for line in decode_subtitle(data).splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            events = stripped.lower() == "[events]"
            continue
        if not events or ":" not in line:
            continue
        kind, value = line.split(":", 1)
        if kind.strip().lower() == "format":
            fields = [field.strip().lower() for field in value.split(",")]
            continue
        if kind.strip().lower() != "dialogue" or not fields or fields[-1] != "text":
            continue
        values = value.split(",", len(fields) - 1)
        if len(values) != len(fields) or not {"start", "end", "text"}.issubset(fields):
            continue
        event = dict(zip(fields, values, strict=True))
        start, end = _ASS_TIME.fullmatch(event["start"]), _ASS_TIME.fullmatch(event["end"])
        if start and end:
            rows.append((_clock(start.groups()), _clock(end.groups()), event["text"]))
    return _validated_cues(rows)


def _language(value) -> str:
    value = str(value or "").strip().lower().replace("_", "-")
    base = value.split("-", 1)[0]
    return {
        "eng": "en",
        "english": "en",
        "rus": "ru",
        "russian": "ru",
        "fra": "fr",
        "fre": "fr",
        "deu": "de",
        "ger": "de",
        "spa": "es",
        "und": "",
        "unknown": "",
    }.get(base, base)


def _english(cues) -> bool:
    text = " ".join(cue["source"] for cue in cues)[:30_000]
    letters = [char for char in text if char.isalpha()]
    words = re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower())
    common = [word for word in words if word in _ENGLISH_WORDS]
    return bool(
        len(words) >= 6
        and letters
        and sum(char.isascii() for char in letters) / len(letters) >= 0.9
        and len(common) >= max(2, len(words) * 0.12)
        and len(set(common)) >= 2
    )


def _delay(value, name) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or abs(value) > 3600
    ):
        raise SubtitleSourceError(f"{name} must be a finite number between -3600 and 3600 seconds")
    return float(value) or 0.0


def _track_id(value) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int))
        or not re.fullmatch(r"[0-9]{1,9}", str(value))
    ):
        raise SubtitleSourceError("The selected track id must be a nonnegative integer")
    return str(int(value))


def _metadata(track) -> dict:
    # mpv's player-local id can change after reloading tracks. It is a selection
    # handle, not a content/stream identity, and must not invalidate saved text.
    return {
        key: track[key]
        for key in ("ff-index", "codec", "lang", "title", "external", "forced")
        if key in track and isinstance(track[key], (str, int, bool))
    }


def _external_path(track) -> Path | None:
    value = track.get("external-filename")
    if not value:
        return None
    if not isinstance(value, str):
        raise SubtitleSourceError("The external subtitle path is invalid")
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
        ):
            raise SubtitleSourceError("Only local subtitle files are supported")
        value = unquote(parsed.path)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SubtitleSourceError("Only absolute local subtitle paths are supported")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise SubtitleSourceError("Select a regular local subtitle file")
    return path


def _mapping(track, ordinal, media_type) -> str:
    index = track.get("ff-index")
    if index is None or index == -1:
        return f"0:{media_type}:{ordinal}"
    if type(index) is not int or not 0 <= index <= 100_000:
        raise SubtitleSourceError("The selected track has an invalid FFmpeg stream index")
    return f"0:{index}"


def _audio(selection, tracks):
    audio = [track for track in tracks if track.get("type") == "audio"]
    requested = selection.get("audio_track_id")
    if requested is not None and requested != "auto":
        requested = _track_id(requested)
        chosen = next((track for track in audio if _track_id(track.get("id")) == requested), None)
        if chosen is None:
            raise SubtitleSourceError("The selected audio track is no longer available")
    else:
        chosen = next((track for track in audio if track.get("selected") is True), None) or next(
            iter(audio), None
        )
    if chosen is None:
        return "0:a:0", {}
    if chosen.get("external") or chosen.get("external-filename"):
        raise SubtitleSourceError("External audio tracks are not supported by local file decoding")
    ordinal = sum(
        not track.get("external") and not track.get("external-filename")
        for track in audio[: audio.index(chosen)]
    )
    return _mapping(chosen, ordinal, "a"), _metadata(chosen)


def _generated(track) -> bool:
    title = str(track.get("title", "")).lower()
    filename = unquote(str(track.get("external-filename", ""))).lower()
    return (
        "live translation" in title
        or "live translation" in filename
        or "livetranslate/subtitles/" in filename
    )


def _forced(track) -> bool:
    title = str(track.get("title", "")).lower()
    title = re.sub(r"\b(?:not[ -]+forced|non[ -]+forced|unforced)\b", "", title)
    return track.get("forced") is True or bool(
        re.search(r"\b(?:forced|signs|foreign parts)\b|только надписи", title)
    )


def _language_rank(track, path, source_language) -> int | None:
    language = _language(track.get("lang"))
    if language:
        return 0 if language == source_language else None
    hint = str(track.get("title", "")) + " " + (path.name if path else "")
    aliases = ("en", "eng", "english") if source_language == "en" else (source_language,)
    if any(
        re.search(r"(?:^|[^a-z])" + re.escape(alias) + r"(?:$|[^a-z])", hint, flags=re.I)
        for alias in aliases
    ):
        return 1
    return 2


def _extract(media, mapping, ffmpeg, format_name) -> bytes:
    with tempfile.TemporaryFile() as output:
        try:
            subprocess.run(
                [
                    str(ffmpeg),
                    "-nostdin",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(media),
                    "-map",
                    mapping,
                    "-c:s",
                    format_name,
                    "-f",
                    format_name,
                    "-fs",
                    str(MAX_SUBTITLE_BYTES + 1),
                    "pipe:1",
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=EXTRACTION_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SubtitleSourceError(
                "Could not extract the selected embedded text subtitles"
            ) from error
        size = output.tell()
        if size > MAX_SUBTITLE_BYTES:
            raise SubtitleSourceError("The extracted subtitle source exceeds the 16 MiB limit")
        output.seek(0)
        return output.read(MAX_SUBTITLE_BYTES + 1)


def resolve_source(
    media_path, selection, *, ffmpeg, duration, source_language="en", media_sha256=None
) -> ResolvedSource:
    """Select text first in auto mode, or fail closed for an explicit selection.

    Empty legacy selection keeps first-audio behavior. A supplied
    ``media_sha256`` must be the caller's verified full media-content SHA-256;
    it avoids hashing that same file twice. Cues always retain raw timestamps.
    """
    media = Path(media_path).expanduser()
    if not media.is_absolute():
        raise SubtitleSourceError("Only absolute local media paths are supported")
    media = media.resolve(strict=True)
    media_stamp = SourceSignature(media, source_signature(media))
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise SubtitleSourceError("Media duration must be positive and finite")
    if selection is None:
        selection = {}
    if not isinstance(selection, dict):
        raise SubtitleSourceError("The media source selection must be an object")
    mode = selection.get("mode", "auto" if selection else "audio")
    if not isinstance(mode, str) or mode not in {"auto", "audio", "subtitles"}:
        raise SubtitleSourceError("The source mode must be auto, audio, or subtitles")
    tracks = selection.get("tracks", [])
    if (
        not isinstance(tracks, list)
        or len(tracks) > 512
        or any(not isinstance(track, dict) for track in tracks)
    ):
        raise SubtitleSourceError("The player track list is invalid")
    language = _language(source_language)
    if not re.fullmatch(r"[a-z]{2,3}", language):
        raise SubtitleSourceError("A source language code is required")
    audio_delay = _delay(selection.get("audio_delay", 0), "audio_delay")
    subtitle_delay = _delay(selection.get("subtitle_delay", 0), "subtitle_delay")
    audio_map, audio_track = _audio(selection, tracks)
    base = {
        "schema": PARSER_VERSION,
        "source_language": language,
        "audio_map": audio_map,
        "audio_track": audio_track,
        "audio_delay": audio_delay,
        "subtitle_delay": subtitle_delay,
    }
    diagnostics = []
    candidates = []
    requested = selection.get("subtitle_track_id")
    if requested is not None:
        requested = _track_id(requested)
    subtitles = [track for track in tracks if track.get("type") == "sub"]
    if (
        mode == "subtitles"
        and requested is not None
        and not any(_track_id(track.get("id")) == requested for track in subtitles)
    ):
        raise SubtitleSourceError("The selected subtitle track is no longer available")
    if mode != "audio":
        for ordinal, track in enumerate(subtitles):
            if (
                mode == "subtitles"
                and requested is not None
                and _track_id(track.get("id")) != requested
            ):
                continue
            if _generated(track) or (mode == "auto" and _forced(track)):
                continue
            try:
                path = _external_path(track)
                format_name = (
                    _SUFFIXES.get(path.suffix.lower())
                    if path
                    else _FORMATS.get(str(track.get("codec", "")).lower())
                )
                if not format_name:
                    raise SubtitleSourceError("The selected subtitle codec is not supported text")
                rank = _language_rank(track, path, language) if mode == "auto" else 0
                if rank is None:
                    continue
                preference = (
                    0 if requested is not None and _track_id(track.get("id")) == requested else 1
                )
                candidates.append(
                    (
                        (preference, rank, 0 if track.get("selected") is True else 1, ordinal),
                        track,
                        path,
                        format_name,
                        ordinal,
                    )
                )
            except (OSError, SubtitleSourceError) as error:
                diagnostics.append(str(error))
        for ordering, track, path, format_name, ordinal in sorted(
            candidates, key=lambda item: item[0]
        ):
            try:
                stamps = [media_stamp]
                if path:
                    stamp = SourceSignature(path, source_signature(path))
                    if stamp.signature[2] > MAX_SUBTITLE_BYTES:
                        raise SubtitleSourceError("The subtitle file exceeds the 16 MiB limit")
                    with path.open("rb") as stream:
                        data = stream.read(MAX_SUBTITLE_BYTES + 1)
                    if len(data) > MAX_SUBTITLE_BYTES:
                        raise SubtitleSourceError("The subtitle file exceeds the 16 MiB limit")
                    stamp.check()
                    stamps.append(stamp)
                else:
                    embedded_ordinal = sum(
                        not item.get("external-filename") and not item.get("external")
                        for item in subtitles[:ordinal]
                    )
                    data = _extract(
                        media, _mapping(track, embedded_ordinal, "s"), ffmpeg, format_name
                    )
                cues = parse_ass(data) if format_name == "ass" else parse_srt(data)
                if not cues:
                    raise SubtitleSourceError(
                        "The selected subtitle source contains no usable timed text"
                    )
                if mode == "auto" and ordering[1] == 2 and (language != "en" or not _english(cues)):
                    continue
                if (
                    mode == "auto"
                    and duration >= 120
                    and len(cues) < 3
                    and sum(cue["end"] - cue["start"] for cue in cues) < duration * 0.01
                ):
                    continue
                media_stamp.check()
                identity = {
                    **base,
                    "kind": "text",
                    "format": format_name,
                    "track": _metadata(track),
                    "source_sha256": hashlib.sha256(data).hexdigest(),
                }
                label = "Субтитры: " + str(
                    track.get("title")
                    or track.get("lang")
                    or (path.name if path else track.get("id", ""))
                )
                return ResolvedSource(
                    "text",
                    label,
                    identity,
                    cues,
                    audio_map,
                    audio_delay,
                    subtitle_delay,
                    tuple(stamps),
                    tuple(diagnostics),
                )
            except (OSError, SubtitleSourceError) as error:
                diagnostics.append(str(error))
    if mode == "subtitles":
        raise SubtitleSourceError(
            diagnostics[-1] if diagnostics else "No supported subtitle source was selected"
        )
    if media_sha256 is None:
        media_sha256, _ = fingerprint_file(media, {})
    elif not isinstance(media_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", media_sha256):
        raise SubtitleSourceError("The verified media SHA-256 is invalid")
    media_stamp.check()
    identity = {**base, "kind": "audio", "source_sha256": media_sha256}
    label = "Аудио: " + str(audio_track.get("title") or audio_track.get("lang") or audio_map)
    return ResolvedSource(
        "audio",
        label,
        identity,
        [],
        audio_map,
        audio_delay,
        subtitle_delay,
        (media_stamp,),
        tuple(diagnostics),
    )
