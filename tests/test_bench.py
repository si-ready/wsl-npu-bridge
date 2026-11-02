#!/usr/bin/env python3
"""
Tests for bench/bench_npu.py helpers -- stdlib unittest only.

The WAV-duration test exists because the original hand-rolled header
parser once reported 4,351 s for a 9 s clip (the writer had inserted
extra chunks before the data chunk). These tests pin the fix.
Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

import bench_npu  # noqa: E402


def _write_wav(path: Path, seconds: float = 2.0, rate: int = 16000,
               extra_chunk: bool = False) -> None:
    frames = int(seconds * rate)
    pcm = b"\x00\x00" * frames
    if not extra_chunk:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        return
    # Hand-build a WAV with a LIST chunk between fmt and data -- the
    # shape that broke the old fixed-offset parser.
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    list_body = b"INFO" + b"JUNK" + struct.pack("<I", 4) + b"abcd"
    riff_size = 4 + (8 + len(fmt)) + (8 + len(list_body)) + (8 + len(pcm))
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE")
        f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        f.write(b"LIST" + struct.pack("<I", len(list_body)) + list_body)
        f.write(b"data" + struct.pack("<I", len(pcm)) + pcm)


class WavDurationTests(unittest.TestCase):
    def test_canonical_wav(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.wav"
            _write_wav(p, seconds=2.0)
            self.assertAlmostEqual(bench_npu.wav_duration_seconds(p), 2.0,
                                   places=2)

    def test_wav_with_extra_chunks(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.wav"
            _write_wav(p, seconds=2.0, extra_chunk=True)
            self.assertAlmostEqual(bench_npu.wav_duration_seconds(p), 2.0,
                                   places=2)

    def test_non_wav_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.wav"
            p.write_bytes(b"this is not a wav file at all")
            self.assertIsNone(bench_npu.wav_duration_seconds(p))


if __name__ == "__main__":
    unittest.main()
