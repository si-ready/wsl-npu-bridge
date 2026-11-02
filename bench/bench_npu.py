#!/usr/bin/env python3
"""
bench_npu.py -- measure the WSL2 NPU bridge, file a receipt.

Runs each capability the bridge offers, from inside WSL, and writes
a dated JSON receipt under bench/results/. Design rules:

* Warm up first, then measure -- first calls include model loads and
  NPU compiles and are not measurements.
* Medians, not singles: every leg runs N times and reports the
  median (default 3).
* A failed leg is a receipt too. It is recorded with its error and
  the bench continues, so one broken backend never hides the rest.
* Engine-reported speeds (FastFlowLM's prefill/decode figures) are
  filed separately from wall-clock rates: the wall includes two
  relay hops, the engine numbers are the NPU truth.

    python3 bench_npu.py [--runs 3] [--wav file.wav]
                         [--skip-lemond] [--skip-flm]
                         [--skip-whisper] [--skip-embed]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
import wsl_npu  # noqa: E402
from wsl_npu import NPU, NPUUnavailable  # noqa: E402

DEFAULT_WAV_WINDOWS = Path("/mnt/c/Users")  # per-user resolution below


def wav_duration_seconds(path: Path) -> float | None:
    """Duration of a WAV via the stdlib wave module (it walks the chunk
    list properly -- a fixed-offset header read once reported 4,351 s
    for a 9 s clip because the writer had inserted extra chunks)."""
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else None
    except Exception:
        return None


def default_wav() -> Path | None:
    """Find install.ps1's fixture under the Windows user's LocalAppData."""
    if not DEFAULT_WAV_WINDOWS.exists():
        return None
    for user_dir in DEFAULT_WAV_WINDOWS.iterdir():
        candidate = user_dir / "AppData/Local/wsl-npu-bridge/bench-sample.wav"
        if candidate.exists():
            return candidate
    return None


def _median_run(fn, runs: int) -> list[dict]:
    """Call fn() `runs` times; return the raw per-run dicts."""
    return [fn() for _ in range(runs)]


def _median_of(runs: list[dict], key: str) -> float | None:
    vals = [r[key] for r in runs if r.get(key) is not None]
    return round(statistics.median(vals), 3) if vals else None


def bench_chat(npu: NPU, engine: str, prompt: str, runs: int) -> dict:
    """One chat leg: 1 warmup (unrecorded) + `runs` measured runs."""
    npu.chat_timed(prompt, engine=engine)  # warmup: load + compile
    measured = _median_run(
        lambda: npu.chat_timed(prompt, engine=engine), runs)
    summary = {
        "engine": engine,
        "model": measured[0]["model"],
        "wall_s_median": _median_of(measured, "wall_s"),
        "tokens_per_s_wall_median": _median_of(measured, "tokens_per_s"),
    }
    if engine == "flm":
        summary["decode_tps_engine_median"] = _median_of(measured, "decode_tps")
        summary["prefill_tps_engine_median"] = _median_of(measured, "prefill_tps")
        summary["ttft_s_engine_median"] = _median_of(measured, "ttft_s")
    return {"summary": summary, "runs": measured}


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the WSL2 NPU bridge")
    parser.add_argument("--runs", type=int, default=3,
                        help="measured runs per leg (after 1 warmup)")
    parser.add_argument("--wav", help="path to a WAV file for the Whisper run")
    parser.add_argument("--audio-seconds", type=float,
                        help="duration of the WAV, if its header can't be read")
    parser.add_argument("--skip-lemond", action="store_true")
    parser.add_argument("--skip-flm", action="store_true")
    parser.add_argument("--skip-whisper", action="store_true")
    parser.add_argument("--skip-embed", action="store_true")
    parser.add_argument("--prompt", default=(
        "In two sentences, explain why a neural processing unit is good "
        "at matrix multiplication."))
    args = parser.parse_args()

    npu = NPU()
    receipt: dict = {
        "when_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "client_version": wsl_npu.__version__,
        "bridge_base_url": npu.base_url,
        "flm_base_url": npu.flm_url,
        "runs_requested": args.runs,
        "legs": {},
    }
    print(f"bridge endpoint: {npu.base_url}  (flm: {npu.flm_url})")

    # -- chat legs --------------------------------------------------------
    for engine, skip in (("lemond", args.skip_lemond),
                         ("flm", args.skip_flm)):
        if skip:
            continue
        label = f"chat-{engine}"
        try:
            leg = bench_chat(npu, engine, args.prompt, args.runs)
            receipt["legs"][label] = leg
            s = leg["summary"]
            line = (f"\n{label:14s} {s['model']}: wall tok/s "
                    f"{s['tokens_per_s_wall_median']}")
            if engine == "flm":
                line += (f" | engine decode {s['decode_tps_engine_median']}"
                         f" tok/s, prefill {s['prefill_tps_engine_median']}"
                         f" tok/s, TTFT {s['ttft_s_engine_median']} s")
            print(line)
            print(f"               reply starts: {leg['runs'][0]['text'][:80]!r}")
        except Exception as exc:
            receipt["legs"][label] = {"error": str(exc)}
            print(f"\n{label:14s} FAILED: {exc}")

    # -- whisper -----------------------------------------------------------
    if not args.skip_whisper:
        wav = Path(args.wav) if args.wav else default_wav()
        if wav and wav.exists():
            seconds = args.audio_seconds or wav_duration_seconds(wav)
            try:
                npu.transcribe(wav)  # warmup: model load
                walls = []
                text = ""
                for _ in range(args.runs):
                    start = time.perf_counter()
                    text = npu.transcribe(wav)
                    walls.append(round(time.perf_counter() - start, 3))
                wall = statistics.median(walls)
                run = {"wav": str(wav), "wall_s_median": wall,
                       "wall_s_runs": walls, "audio_s": seconds,
                       "rtf": round(seconds / wall, 2) if seconds else None,
                       "text": text}
                receipt["legs"]["whisper"] = run
                print(f"\n{'whisper':14s} {seconds:.1f} s of audio, median "
                      f"{wall:.2f} s = RTF {run['rtf']}x (speed multiple)")
                print(f"               text: {text[:100]!r}")
            except Exception as exc:
                receipt["legs"]["whisper"] = {"error": str(exc)}
                print(f"\n{'whisper':14s} FAILED: {exc}")
        else:
            print("\nwhisper        skipped: no bench WAV found "
                  "(run host/install.ps1, or pass --wav)")

    # -- embeddings ----------------------------------------------------------
    if not args.skip_embed:
        try:
            npu.embed("warmup")  # warmup: model load (+ possible pull)
            walls = []
            dims = 0
            for _ in range(args.runs):
                start = time.perf_counter()
                vec = npu.embed("a tugboat crossing a cold harbour at dawn")
                walls.append(round(time.perf_counter() - start, 3))
                dims = len(vec)
            receipt["legs"]["embed"] = {
                "dims": dims, "wall_s_median": statistics.median(walls),
                "wall_s_runs": walls}
            print(f"\n{'embed':14s} {dims} dims, median "
                  f"{statistics.median(walls):.3f} s")
        except Exception as exc:
            receipt["legs"]["embed"] = {"error": str(exc)}
            print(f"\n{'embed':14s} FAILED: {exc}")

    # -- file the receipt -----------------------------------------------------
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
