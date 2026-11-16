"""Contracts for source identity, immutable cache selection and damage recovery."""

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from live_translation import media_cache
from live_translation.media_cache import (
    SourceChangedError,
    fingerprint_file,
    key_for,
    source_signature,
    validate_chunks,
)


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"video bytes, independent of the filename")
        self.memo = {}

    def tearDown(self):
        self.directory.cleanup()

    def test_unchanged_poll_does_not_read_video_twice(self):
        with mock.patch.object(media_cache, "_sha256_file", wraps=media_cache._sha256_file) as read:
            first = fingerprint_file(self.video, self.memo)
            second = fingerprint_file(self.video, self.memo)
        self.assertEqual(first, second)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(first[0], hashlib.sha256(self.video.read_bytes()).hexdigest())
        self.assertEqual(self.memo[first[1]], first[0])

    def test_moved_and_copied_bytes_select_the_same_cache(self):
        initial, _ = fingerprint_file(self.video, self.memo)
        renamed = self.root / "renamed video.mp4"
        self.video.rename(renamed)
        moved, _ = fingerprint_file(renamed, self.memo)
        copied = self.root / "copied.mp4"
        shutil.copy2(renamed, copied)
        duplicate, _ = fingerprint_file(copied, self.memo)
        self.assertEqual(initial, moved)
        self.assertEqual(initial, duplicate)
        self.assertEqual(key_for(initial, "fixed profile"), key_for(duplicate, "fixed profile"))

    def test_changed_source_invalidates_memo_even_with_same_length(self):
        first, signature = fingerprint_file(self.video, self.memo)
        self.video.write_bytes(b"X" * self.video.stat().st_size)
        second, changed = fingerprint_file(self.video, self.memo)
        self.assertNotEqual(signature, changed)
        self.assertNotEqual(first, second)

    def test_source_signature_covers_inode_and_both_change_clocks(self):
        info = self.video.stat()
        self.assertEqual(
            source_signature(self.video),
            (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ),
        )

    def test_source_change_during_hash_never_enters_memo(self):
        before = source_signature(self.video)
        after = (*before[:-1], before[-1] + 1)
        with (
            mock.patch.object(media_cache, "source_signature", side_effect=[before, after]),
            self.assertRaises(SourceChangedError),
        ):
            fingerprint_file(self.video, self.memo)
        self.assertEqual(self.memo, {})

    def test_generation_profile_is_part_of_cache_identity(self):
        digest = fingerprint_file(self.video, self.memo)[0]
        self.assertEqual(len(key_for(digest, "profile A")), 32)
        self.assertNotEqual(key_for(digest, "profile A"), key_for(digest, "profile B"))


class ChunkValidationTests(unittest.TestCase):
    @staticmethod
    def cue(start: object = 1, end: object = 2, **changes):
        return {"start": start, "end": end, "source": "Hello.", "translated": "Привет.", **changes}

    def test_empty_completed_silence_is_valid(self):
        self.assertEqual(validate_chunks({"0": [], "1": []}, 31, 30), {"0": [], "1": []})

    def test_source_only_recognition_cache_uses_the_same_timestamp_validation(self):
        raw = {
            "0": [{"start": 1, "end": 2, "source": "Hello."}],
            "1": [{"start": 31, "end": 32, "source": " "}],
            "2": [],
        }
        self.assertEqual(validate_chunks(raw, 90, 30), {"2": []})
        checked = validate_chunks(raw, 90, 30, required_fields=("source",))
        self.assertEqual(set(checked), {"0", "2"})
        self.assertNotIn("translated", checked["0"][0])
        self.assertEqual(checked["0"][0]["source"], "Hello.")

    def test_invalid_ids_are_not_counted_as_prepared(self):
        raw = {
            "0": [],
            "00": [],
            "+0": [],
            " 0": [],
            "-1": [],
            "1.0": [],
            "2": [],
            "０": [],
            "9" * 5000: [],
            1: [],
        }
        self.assertEqual(validate_chunks(raw, 31, 30), {"0": []})

    def test_one_bad_cue_invalidates_only_its_own_chunk(self):
        raw = {
            "0": [self.cue(), self.cue(10, 11, translated=" ")],
            "1": [self.cue(30, 31)],
            "2": [],
        }
        checked = validate_chunks(raw, 65, 30)
        self.assertEqual(set(checked), {"1", "2"})
        self.assertEqual(checked["1"][0]["translated"], "Привет.")

    def test_nonfinite_reversed_and_outside_timestamps_are_rejected(self):
        for start, end in [
            (float("nan"), 2),
            (1, float("inf")),
            (2, 1),
            (1, 1),
            (-0.1, 2),
            (29, 30.1),
            (True, 2),
            ("1", 2),
        ]:
            with self.subTest(start=start, end=end):
                self.assertEqual(validate_chunks({"0": [self.cue(start, end)]}, 60, 30), {})

    def test_tiny_rounding_error_is_clamped_to_real_chunk_boundaries(self):
        cue = self.cue(29.999, 31.002)
        self.assertEqual(validate_chunks({"1": [cue]}, 31, 30)["1"][0], self.cue(30.0, 31.0))

    def test_overlaps_and_additional_metadata_survive_timestamp_sorting(self):
        raw = {"0": [self.cue(3, 6, diagnostic="kept"), self.cue(1, 4)]}
        checked = validate_chunks(raw, 30, 30)["0"]
        self.assertEqual([item["start"] for item in checked], [1, 3])
        self.assertEqual(checked[1]["diagnostic"], "kept")
        self.assertEqual(checked[0]["end"], 4)

    def test_malformed_chunk_and_non_object_cue_are_dropped(self):
        self.assertEqual(validate_chunks({"0": {}, "1": ["text"], "2": []}, 90, 30), {"2": []})
        self.assertEqual(validate_chunks(None, 90, 30), {})


if __name__ == "__main__":
    unittest.main()
