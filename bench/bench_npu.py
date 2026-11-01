#!/usr/bin/env python3
"""
bench_npu.py -- receipts for the WSL2 NPU bridge.

"Runs on the NPU" is a claim; this tool turns it into numbers. Run it
from inside WSL2. It measures, end to end across the bridge:

  * chat:      wall time, completion tokens, tokens/second for the
               configured hybrid LLM (NPU prefill + iGPU decode),
  * whisper:   real-time factor (audio seconds / wall seconds) for the
               bench WAV that host/install.ps1 synthesised,
  * embedding: round-trip latency for one embedding call (optional).

Output is printed as a table AND written as JSON next to this script's
results/ folder, so runs can be compared across machines and dates --
and quoted verbatim in the repo's results section.

    python3 bench_npu.py [--wav /mnt/c/Users/<you>/AppData/Local/wsl-npu-bridge/bench-sample.wav]
                         [--audio-seconds 12.0] [--skip-whisper] [--skip-embed]

The WAV's duration is needed for RTF: pass --audio-seconds, or let the
tool read it from the WAV header (PCM WAVs only -- the install fixture
is one, so the default path just works).
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
from wsl_npu import NPU, NPUUnavailable  # noqa: E402

DEFAULT_WAV_WINDOWS = Path(
    "/mnt/c/Users")  # resolved per-user below when --wav is omitted


def wav_duration_seconds(path: Path) -> float | None:
    """Duration of a WAV via the stdlib wave module (it walks the chunk
    list properly -- a fixed-offset header read once reported 4,351 s
    for a 12 s clip because the writer had inserted extra chunks)."""
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else None
    except Exception:
        return None


def default_wav() -> Path | None:
    """Find install.ps1's fixture under the Windows user's LocalAppData."""
    users = DEFAULT_WAV_WINDOWS
    if not users.exists():
        return None
    for user_dir in users.iterdir():
        candidate = user_dir / "AppData/Local/wsl-npu-bridge/bench-sample.wav"
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the WSL2 NPU bridge")
    parser.add_argument("--wav", help="path to a WAV file for the Whisper run")
    parser.add_argument("--audio-seconds", type=float,
                        help="duration of the WAV, if its header can't be read")
    parser.add_argument("--skip-whisper", action="store_true")
    parser.add_argument("--skip-embed", action="store_true")
    parser.add_argument("--prompt", default=(
        "In two sentences, explain why a neural processing unit is good "
        "at matrix multiplication."))
    args = parser.parse_args()

    npu = NPU()
    receipt: dict = {
        "when_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bridge_base_url": npu.base_url,
        "runs": {},
    }
    print(f"bridge endpoint: {npu.base_url}")

    # -- chat: the headline number --------------------------------------
    # A failed leg is a receipt too: record it and keep going, so one
    # broken backend never hides the legs that work.
    try:
        chat = npu.chat_timed(args.prompt)
        receipt["runs"]["chat"] = chat
        print(f"\nchat   model={chat['model']}")
        print(f"       {chat['completion_tokens']} tokens in {chat['wall_s']} s "
              f"= {chat['tokens_per_s']} tok/s (incl. prefill)")
        print(f"       reply starts: {chat['text'][:80]!r}")
    except Exception as exc:
        receipt["runs"]["chat"] = {"error": str(exc)}
        print(f"\nchat   FAILED: {exc}")

    # -- whisper ---------------------------------------------------------
    if not args.skip_whisper:
        wav = Path(args.wav) if args.wav else default_wav()
        if wav and wav.exists():
            seconds = args.audio_seconds or wav_duration_seconds(wav)
            start = time.perf_counter()
            try:
                text = npu.transcribe(wav)
            except Exception as exc:
                receipt["runs"]["whisper"] = {"error": str(exc)}
                print(f"\nwhisper  FAILED: {exc}")
                text = None
            wall = time.perf_counter() - start
            if text is not None:
                run = {"wav": str(wav), "wall_s": round(wall, 3),
                       "audio_s": seconds,
                       "rtf": round(seconds / wall, 2) if seconds else None,
                       "text": text}
                receipt["runs"]["whisper"] = run
                print(f"\nwhisper  {seconds:.1f} s of audio in {wall:.2f} s "
                      f"= RTF {run['rtf']}x" if seconds else
                      f"\nwhisper  transcribed in {wall:.2f} s (duration unknown)")
                print(f"       text: {text[:100]!r}")
        else:
            print("\nwhisper  skipped: no bench WAV found "
                  "(run host/install.ps1, or pass --wav)")

    # -- embeddings -------------------------------------------------------
    if not args.skip_embed:
        try:
            start = time.perf_counter()
            vec = npu.embed("a tugboat crossing a cold harbour at dawn")
            wall = time.perf_counter() - start
            receipt["runs"]["embed"] = {
                "dims": len(vec), "wall_s": round(wall, 3)}
            print(f"\nembed    {len(vec)} dims in {wall:.3f} s")
        except Exception as exc:  # embeddings are optional in v1; say so
            print(f"\nembed    unavailable on this host service: {exc}")

    # -- file the receipt --------------------------------------------------
    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_file = out_dir / f"bench-{stamp}.json"
    out_file.write_text(json.dumps(receipt, indent=2))
    print(f"\nreceipt written: {out_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NPUUnavailable as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
