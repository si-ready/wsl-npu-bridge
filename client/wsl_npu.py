#!/usr/bin/env python3
"""
wsl_npu.py -- use a Ryzen AI NPU from inside WSL2.

Why this exists
---------------
WSL2 cannot see your NPU. The device lives on the Windows host
(PCI VEN_1022&DEV_17F0 on Strix Point), WSL2 has no PCI bus and no
/dev/accel for it, and the only paravirtual hardware channel Microsoft
ships -- /dev/dxg -- carries GPUs, not NPUs. No Linux driver can fix
that from inside the guest; the channel has to come from the host side.

So this client talks to a small inference service running on the
Windows host (Lemonade, which drives the XDNA NPU through its
FastFlowLM / Ryzen AI backends) and gives WSL programs a dead-simple
local API on top of it:

    from wsl_npu import NPU
    npu = NPU()                                   # auto-discovers the host
    print(npu.chat("What is a tensor core?"))     # runs on the NPU
    text = npu.transcribe("meeting.wav")          # Whisper, NPU-probed

Design rules
------------
* Standard library only. No pip install, no venv drama, works on any
  Python 3.10+ in any WSL distro.
* Discovery, not configuration. It tries, in order: $WSL_NPU_URL,
  localhost (WSL mirrored-networking mode), the default gateway, and
  the /etc/resolv.conf nameserver (classic WSL NAT host address).
  First endpoint that answers the health check wins and is cached.
* Honest failures. If nothing answers, the error says what was tried
  and what to start on the host -- it never silently falls back to CPU.

CLI:
    python3 wsl_npu.py doctor
    python3 wsl_npu.py chat "Explain quaternions like I am five"
    python3 wsl_npu.py transcribe recording.wav
    python3 wsl_npu.py embed "some text to embed"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# The host service (Lemonade) speaks the OpenAI API dialect on this port.
DEFAULT_PORT = 13305
API_PREFIX = "/api/v1"

# Defaults chosen for a first run: small, NPU-native, already in the
# Lemonade catalog. Override per call or with $WSL_NPU_LLM / $WSL_NPU_STT.
DEFAULT_LLM = os.environ.get("WSL_NPU_LLM", "Llama-3.2-1B-Instruct-Hybrid")
DEFAULT_STT = os.environ.get("WSL_NPU_STT", "Whisper-Large-v3-Turbo")
DEFAULT_EMBED = os.environ.get("WSL_NPU_EMBED", "nomic-embed-text-v1-GGUF")
# FastFlowLM runs as a companion service in v0.1 (lemond's own FLM
# discovery is broken in current builds -- see docs/TROUBLESHOOTING.md).
# serve.ps1 relays it into WSL on this port; it is a standard
# OpenAI-compatible endpoint, usable with any OpenAI client.
DEFAULT_FLM_URL = os.environ.get("WSL_NPU_FLM_URL", "http://127.0.0.1:53999")


class NPUUnavailable(RuntimeError):
    """No host NPU service answered. The message says what to do."""


def _candidate_base_urls() -> list[str]:
    """Host addresses worth trying, most likely first.

    WSL networking comes in two flavours and both are handled:
    mirrored mode puts the Windows host on 127.0.0.1; NAT mode puts it
    on the virtual switch -- the default gateway and the DNS nameserver
    in /etc/resolv.conf are both that same host address.
    """
    urls: list[str] = []
    env = os.environ.get("WSL_NPU_URL")
    if env:
        urls.append(env.rstrip("/"))
    urls.append(f"http://127.0.0.1:{DEFAULT_PORT}")
    try:
        route = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        gateway = route.split()[2]  # "default via 172.x.x.1 dev eth0 ..."
        urls.append(f"http://{gateway}:{DEFAULT_PORT}")
    except Exception:
        pass  # no ip command / no default route: the other candidates remain
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            if line.startswith("nameserver"):
                urls.append(f"http://{line.split()[1]}:{DEFAULT_PORT}")
    except OSError:
        pass
    # De-duplicate, preserving order.
    return list(dict.fromkeys(urls))


def _request_json(url: str, payload: dict | None = None, timeout: float = 600) -> dict:
    """POST (or GET when payload is None) and parse JSON. Raises on HTTP errors."""
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class NPU:
    """A connection to the Windows host's NPU service."""

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url.rstrip("/") if base_url else self._discover()

    # -- discovery ------------------------------------------------------
    @staticmethod
    def _healthy(base: str) -> bool:
        try:
            _request_json(f"{base}{API_PREFIX}/models", timeout=5)
            return True
        except Exception:
            return False

    @classmethod
    def _discover(cls) -> str:
        tried = _candidate_base_urls()
        for base in tried:
            if cls._healthy(base):
                return base
        raise NPUUnavailable(
            "No NPU service answered. Tried: " + ", ".join(tried) + ".\n"
            "On the Windows host, run the bridge service (host\\serve.ps1 "
            "from the wsl-npu-bridge repo), then retry. If the host uses "
            "WSL NAT networking, also make sure the service binds beyond "
            "localhost (see docs/TROUBLESHOOTING.md)."
        )

    # -- API ------------------------------------------------------------
    def models(self) -> list[str]:
        """Model ids the host service currently knows about."""
        data = _request_json(f"{self.base_url}{API_PREFIX}/models")
        return [m["id"] for m in data.get("data", [])]

    def chat(self, prompt: str, model: str = DEFAULT_LLM,
             system: str | None = None, max_tokens: int = 512) -> str:
        """One-shot chat completion on the NPU. Returns the reply text."""
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        out = _request_json(
            f"{self.base_url}{API_PREFIX}/chat/completions",
            {"model": model, "messages": messages, "max_tokens": max_tokens},
        )
        return out["choices"][0]["message"]["content"]

    def chat_timed(self, prompt: str, model: str = DEFAULT_LLM,
                   max_tokens: int = 256) -> dict:
        """Chat completion with timing/usage -- the bench instrument."""
        start = time.perf_counter()
        out = _request_json(
            f"{self.base_url}{API_PREFIX}/chat/completions",
            {"model": model,
             "messages": [{"role": "user", "content": prompt}],
             "max_tokens": max_tokens},
        )
        wall = time.perf_counter() - start
        usage = out.get("usage", {})
        completion = usage.get("completion_tokens", 0)
        return {
            "model": model,
            "text": out["choices"][0]["message"]["content"],
            "wall_s": round(wall, 3),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": completion,
            "tokens_per_s": round(completion / wall, 2) if completion else None,
        }

    def transcribe(self, audio_path: str | Path, model: str = DEFAULT_STT) -> str:
        """Speech-to-text via the host service's Whisper backend.

        The multipart body is built by hand so this module stays
        standard-library-only.
        """
        path = Path(audio_path)
        boundary = f"----wsl-npu-{uuid.uuid4().hex}"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="model"\r\n\r\n',
            model.encode(), b"\r\n",
            f"--{boundary}\r\n".encode(),
            (f'Content-Disposition: form-data; name="file"; '
             f'filename="{path.name}"\r\n').encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            path.read_bytes(), b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        req = urllib.request.Request(
            f"{self.base_url}{API_PREFIX}/audio/transcriptions",
            data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=1800) as resp:
            return json.loads(resp.read().decode())["text"]

    def embed(self, text: str, model: str = DEFAULT_EMBED) -> list[float]:
        """Embeddings via the host service (llama.cpp-backed models)."""
        out = _request_json(
            f"{self.base_url}{API_PREFIX}/embeddings",
            {"model": model, "input": text},
        )
        return out["data"][0]["embedding"]


def _doctor() -> int:
    print("WSL -> host NPU bridge doctor")
    for base in _candidate_base_urls():
        ok = NPU._healthy(base)
        print(f"  [{'OK ' if ok else '  x'}] {base}")
        if ok:
            npu = NPU(base)
            npu_models = npu.models()
            print(f"      service up; {len(npu_models)} models known")
            for wanted in (DEFAULT_LLM, DEFAULT_STT, DEFAULT_EMBED):
                mark = "ready" if wanted in npu_models else "not pulled yet"
                print(f"      {wanted}: {mark}")
            # FLM companion service: a note, never a failure.
            try:
                req = urllib.request.Request(f"{DEFAULT_FLM_URL}/v1/models")
                with urllib.request.urlopen(req, timeout=4) as resp:
                    count = len(json.loads(resp.read()).get("data", []))
                print(f"  [OK ] FLM companion at {DEFAULT_FLM_URL} "
                      f"({count} models listed)")
            except Exception:
                print(f"  [  -] FLM companion at {DEFAULT_FLM_URL} "
                      f"(not running)")
            return 0
    print("  No endpoint answered -- see the host instructions in README.md")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Use a Ryzen AI NPU from WSL2")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="find the host service and report readiness")
    p_chat = sub.add_parser("chat", help="one-shot NPU chat completion")
    p_chat.add_argument("prompt")
    p_chat.add_argument("--model", default=DEFAULT_LLM)
    p_stt = sub.add_parser("transcribe", help="transcribe an audio file")
    p_stt.add_argument("audio")
    p_stt.add_argument("--model", default=DEFAULT_STT)
    p_emb = sub.add_parser("embed", help="embed a line of text")
    p_emb.add_argument("text")
    args = parser.parse_args()

    if args.cmd == "doctor":
        return _doctor()
    npu = NPU()
    if args.cmd == "chat":
        print(npu.chat(args.prompt, model=args.model))
    elif args.cmd == "transcribe":
        print(npu.transcribe(args.audio, model=args.model))
    elif args.cmd == "embed":
        vec = npu.embed(args.text)
        print(f"{len(vec)} dims; first 5: {[round(x, 4) for x in vec[:5]]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NPUUnavailable as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except urllib.error.URLError as exc:
        print(f"host service error: {exc}", file=sys.stderr)
        raise SystemExit(3)
