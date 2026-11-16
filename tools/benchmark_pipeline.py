#!/usr/bin/env python3
"""Compare serial and phased mini-batch inference without changing video caches."""

import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iina_bridge import SUPPORT, Bridge  # noqa: E402


def pressure():
    raw = subprocess.check_output(["/usr/bin/memory_pressure", "-Q"], text=True, timeout=3)
    free_match = re.search(r"free percentage:\s*([\d.]+)%", raw)
    if free_match is None:
        raise RuntimeError("Could not parse memory_pressure output")
    free = float(free_match.group(1))
    swap = subprocess.check_output(["/usr/sbin/sysctl", "-n", "vm.swapusage"], text=True, timeout=3)
    swap_match = re.search(r"used\s*=\s*([\d.]+)([KMGTP])", swap)
    if swap_match is None:
        raise RuntimeError("Could not parse vm.swapusage output")
    value, unit = swap_match.groups()
    used = float(value) * 1024 ** ("KMGTP".index(unit) + 1)
    return free, used


class MeasuredBridge(Bridge):
    def __init__(self, *args, **kwargs):
        self.timings = {}
        self.lowest_free, self.start_swap = pressure()
        super().__init__(*args, **kwargs)

    def measure(self, name, function, *args, **kwargs):
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            self.timings[name] = self.timings.get(name, 0) + time.perf_counter() - started

    def check_budget(self):
        free, used = pressure()
        self.lowest_free = min(self.lowest_free, free)
        if free < 20 or used - self.start_swap > 256 * 1024**2:
            super()._release_whisper()
            super()._release_translation(force=True)
            raise RuntimeError("Benchmark stopped at conservative memory budget")

    def _decode_audio(self, path, start, seconds):
        return self.measure("decode", super()._decode_audio, path, start, seconds)

    def _transcribe(self, audio):
        result = self.measure("asr", super()._transcribe, audio)
        self.check_budget()
        return result

    def _translate(self, source, history):
        result = self.measure("translate", super()._translate, source, history)
        self.timings["translation_requests"] = self.timings.get("translation_requests", 0) + 1
        self.check_budget()
        return result

    def _release_whisper(self):
        return self.measure("unload_asr", super()._release_whisper)

    def _release_translation(self, force=False):
        return self.measure("unload_translation", super()._release_translation, force=force)


def main():
    video = Path(sys.argv[1]).resolve()
    positions = (0, 30, 330)
    report = {"video": str(video), "positions": positions, "variants": {}}
    # Exclude Python/MLX import cost from both model-lifecycle comparisons.
    import mlx_whisper  # noqa: F401

    for label in ("serial", "phased"):
        with tempfile.TemporaryDirectory(prefix="live-translation-performance-") as cache:
            bridge = MeasuredBridge(cache_dir=cache, start_worker=False)
            measurements = []
            try:
                bridge.update({"path": str(video), "position": 0, "enabled": True})
                media = bridge.active
                assert media is not None
                bridge.last_heartbeat = math.inf
                bridge.timings = {}
                started = time.perf_counter()
                if label == "serial":
                    for position in positions:
                        bridge._process(media, int(position // 30), bridge.generation)
                else:
                    bridge._process_batch(
                        media, [int(p // 30) for p in positions], bridge.generation
                    )
                elapsed = time.perf_counter() - started
                for position in positions:
                    index = int(position // 30)
                    cues = media.chunks[str(index)]
                    digest = hashlib.sha256(
                        json.dumps(
                            cues, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest()
                    record = {"position": position, "cue_sha256": digest}
                    measurements.append(record)
                report["variants"][label] = {
                    "measurements": measurements,
                    "minimum_free_percent": bridge.lowest_free,
                    "total_seconds": elapsed,
                    "phases": dict(bridge.timings),
                }
                print(label, json.dumps(report["variants"][label]), flush=True)
            finally:
                bridge.close()
    serial, phased = (report["variants"][name] for name in ("serial", "phased"))
    report["identical_cues"] = all(
        a["cue_sha256"] == b["cue_sha256"]
        for a, b in zip(serial["measurements"], phased["measurements"], strict=True)
    )
    report["speedup"] = serial["total_seconds"] / phased["total_seconds"]
    target = SUPPORT / "performance-benchmark.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        "REPORT",
        target,
        "identical:",
        report["identical_cues"],
        "speedup:",
        round(report["speedup"], 3),
        flush=True,
    )


if __name__ == "__main__":
    main()
