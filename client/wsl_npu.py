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

So this client talks to inference services running on the Windows
host and gives WSL programs a dead-simple local API on top of them:

    from wsl_npu import NPU
    npu = NPU()                                   # auto-discovers the host
    print(npu.chat("What is a tensor core?"))     # hybrid LLM (lemond)
    print(npu.chat("...", engine="flm"))          # FastFlowLM on the NPU
    text = npu.transcribe("meeting.wav")          # Whisper on the NPU
    vec = npu.embed("text to embed")              # embeddings

Two host services, one client:

* **Lemonade** (port 13305) -- hybrid Ryzen AI models, Whisper,
  embeddings. Reached at /api/v1.
* **FastFlowLM** (port 53999) -- FLM models, served standalone
  because Lemonade's FLM discovery is broken in current builds
  (docs/TROUBLESHOOTING.md). A standard OpenAI endpoint at /v1.

With the bridge's SSH reverse relay running (host/serve.ps1), both
are simply 127.0.0.1 inside WSL, in either networking mode. Without
it, the client discovers the host: $WSL_NPU_URL, localhost (mirrored
mode), the default gateway, the /etc/resolv.conf nameserver.

Design rules
------------
* Standard library only. No pip install, no venv drama, works on any
  Python 3.10+ in any WSL distro.
* Discovery, not configuration.
* Honest failures. Transport errors are retried (a relay restarting
  mid-boot shouldn't fail a call); server errors are NOT retried and
  are reported as-is. Nothing ever silently falls back to CPU.

CLI:
    python3 wsl_npu.py doctor [--json]
    python3 wsl_npu.py chat "Explain quaternions like I am five"
    python3 wsl_npu.py chat --engine flm --stream "Tell me a story"
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
from collections.abc import Iterator
from pathlib import Path

__version__ = "0.2.0"

# The host service (Lemonade) speaks the OpenAI API dialect on this port.
DEFAULT_PORT = 13305
API_PREFIX = "/api/v1"

# Defaults chosen for a first run: small, NPU-native. Override per call
# or with $WSL_NPU_LLM / $WSL_NPU_STT / $WSL_NPU_EMBED / $WSL_NPU_FLM_*.
DEFAULT_LLM = os.environ.get("WSL_NPU_LLM", "Llama-3.2-1B-Instruct-Hybrid")
DEFAULT_STT = os.environ.get("WSL_NPU_STT", "Whisper-Large-v3-Turbo")
DEFAULT_EMBED = os.environ.get("WSL_NPU_EMBED", "nomic-embed-text-v1-GGUF")
DEFAULT_FLM_URL = os.environ.get("WSL_NPU_FLM_URL", "http://127.0.0.1:53999")
DEFAULT_FLM_MODEL = os.environ.get("WSL_NPU_FLM_MODEL", "llama3.2:1b")

# Long by design: a first chat call can include model load + NPU
# compile on the host. Transport retries ride on top of this.
DEFAULT_TIMEOUT = float(os.environ.get("WSL_NPU_TIMEOUT", "600"))


class NPUUnavailable(RuntimeError):
    """No host NPU service answered. The message says what to do."""


class NPUServiceError(RuntimeError):
    """The host service answered with an error. Not retried."""


def _candidate_base_urls() -> list[str]:
    """Host addresses worth trying, most likely first.

    With the bridge relay running, 127.0.0.1 is the host in both WSL
    networking modes. Mirrored mode also puts the host on localhost.
    NAT mode without the relay puts it on the virtual switch -- the
    default gateway and the /etc/resolv.conf nameserver are that same
    host address.
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


def _open(req: urllib.request.Request, timeout: float):
    """urlopen with retry on *transport* failures only.

    A server that answers -- even with an error -- has spoken; its
    answer is final (retrying a deterministic HTTP 500 just makes it
    slower). A connection that never formed (relay mid-restart,
    service still booting) is worth two more tries with backoff.
    """
    delays = (0.0, 0.5, 1.5)
    last: Exception | None = None
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise  # the server answered; do not retry its verdict
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
    raise NPUUnavailable(f"no answer from {req.full_url}: {last}")


def _request_json(url: str, payload: dict | None = None,
                  timeout: float = DEFAULT_TIMEOUT) -> dict:
    """POST (or GET when payload is None) and parse JSON."""
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with _open(req, timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise NPUServiceError(
            f"host service error: HTTP {exc.code} from {url}: {detail}"
        ) from exc


def _sse_deltas(resp) -> Iterator[str]:
    """Yield content deltas from an OpenAI-style SSE stream."""
    for raw in resp:
        line = raw.decode(errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
            delta = chunk["choices"][0].get("delta", {})
            if delta.get("content"):
                yield delta["content"]
        except (json.JSONDecodeError, KeyError, IndexError):
            continue  # keep-alive / role-only chunks carry no content


class NPU:
    """A connection to the Windows host's NPU services."""

    def __init__(self, base_url: str | None = None,
                 flm_url: str | None = None):
        self.base_url = base_url.rstrip("/") if base_url else self._discover()
        self.flm_url = (flm_url or DEFAULT_FLM_URL).rstrip("/")

    # -- discovery ------------------------------------------------------
    @staticmethod
    def _healthy(base: str) -> bool:
        try:
            _request_json(f"{base}{API_PREFIX}/models", timeout=5)
            return True
        except Exception:
            return False

    def flm_healthy(self) -> bool:
        try:
            _request_json(f"{self.flm_url}/v1/models", timeout=5)
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
            "On the Windows host, run the bridge (host\\serve.ps1 from "
            "the wsl-npu-bridge repo), then retry. With the relay "
            "running, the host is 127.0.0.1 in both WSL networking "
            "modes; see docs/TROUBLESHOOTING.md."
        )

    # -- Lemonade API -----------------------------------------------------
    def models(self) -> list[str]:
        """Model ids the Lemonade service currently knows about."""
        data = _request_json(f"{self.base_url}{API_PREFIX}/models")
        return [m["id"] for m in data.get("data", [])]

    def chat(self, prompt: str, model: str = DEFAULT_LLM,
             system: str | None = None, max_tokens: int = 512,
             engine: str = "lemond") -> str:
        """One-shot chat completion. Returns the reply text.

        engine="lemond" (default) uses the hybrid Ryzen AI models;
        engine="flm" routes to the FastFlowLM companion service and
        `model` is then an FLM tag (default llama3.2:1b).
        """
        if engine == "flm":
            return self._flm_chat(prompt, model=model if model != DEFAULT_LLM
                                  else DEFAULT_FLM_MODEL,
                                  system=system, max_tokens=max_tokens)
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        out = _request_json(
            f"{self.base_url}{API_PREFIX}/chat/completions",
            {"model": model, "messages": messages, "max_tokens": max_tokens},
        )
        return out["choices"][0]["message"]["content"]

    def chat_stream(self, prompt: str, model: str = DEFAULT_LLM,
                    system: str | None = None, max_tokens: int = 512,
                    engine: str = "lemond") -> Iterator[str]:
        """Streaming chat: yields reply text deltas as they arrive."""
        if engine == "flm":
            url = f"{self.flm_url}/v1/chat/completions"
            if model == DEFAULT_LLM:
                model = DEFAULT_FLM_MODEL
        else:
            url = f"{self.base_url}{API_PREFIX}/chat/completions"
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        req = urllib.request.Request(
            url,
            data=json.dumps({"model": model, "messages": messages,
                             "max_tokens": max_tokens,
                             "stream": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with _open(req, DEFAULT_TIMEOUT) as resp:
                yield from _sse_deltas(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise NPUServiceError(
                f"host service error: HTTP {exc.code} from {url}: {detail}"
            ) from exc

    def chat_timed(self, prompt: str, model: str = DEFAULT_LLM,
                   max_tokens: int = 256, engine: str = "lemond") -> dict:
        """Chat completion with timing/usage -- the bench instrument.

        For engine="flm" the result carries FLM's own reported
        prefill/decode speeds and TTFT, which are the honest NPU
        numbers (the wall clock includes two relay hops).
        """
        if engine == "flm":
            return self._flm_chat_timed(
                prompt, model if model != DEFAULT_LLM else DEFAULT_FLM_MODEL,
                max_tokens)
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
            "engine": "lemond",
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
        try:
            with _open(req, 1800) as resp:
                return json.loads(resp.read().decode())["text"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise NPUServiceError(
                f"host service error: HTTP {exc.code}: {detail}") from exc

    def embed(self, text: str, model: str = DEFAULT_EMBED) -> list[float]:
        """Embeddings via the host service (llama.cpp-backed models)."""
        out = _request_json(
            f"{self.base_url}{API_PREFIX}/embeddings",
            {"model": model, "input": text},
        )
        return out["data"][0]["embedding"]

    # -- FastFlowLM API ---------------------------------------------------
    def flm_models(self) -> list[str]:
        """Model tags the FLM companion service lists."""
        data = _request_json(f"{self.flm_url}/v1/models")
        return [m["id"] for m in data.get("data", [])]

    def _flm_payload(self, prompt: str, model: str,
                     system: str | None, max_tokens: int) -> dict:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        return {"model": model, "messages": messages,
                "max_tokens": max_tokens}

    def _flm_chat(self, prompt: str, model: str = DEFAULT_FLM_MODEL,
                  system: str | None = None, max_tokens: int = 512) -> str:
        out = _request_json(
            f"{self.flm_url}/v1/chat/completions",
            self._flm_payload(prompt, model, system, max_tokens),
        )
        return out["choices"][0]["message"]["content"]

    def _flm_chat_timed(self, prompt: str, model: str,
                        max_tokens: int) -> dict:
        start = time.perf_counter()
        out = _request_json(
            f"{self.flm_url}/v1/chat/completions",
            self._flm_payload(prompt, model, None, max_tokens),
        )
        wall = time.perf_counter() - start
        usage = out.get("usage", {})
        return {
            "model": model,
            "engine": "flm",
            "text": out["choices"][0]["message"]["content"],
            "wall_s": round(wall, 3),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            # FLM reports its own engine-side speeds; keep them
            # distinct from the wall-clock rate.
            "prefill_tps": usage.get("prefill_speed_tps"),
            "decode_tps": usage.get("decoding_speed_tps"),
            "ttft_s": usage.get("prefill_duration_ttft"),
            "tokens_per_s": (
                round(usage["completion_tokens"] / wall, 2)
                if usage.get("completion_tokens") else None),
        }


def doctor(base_url: str | None = None, flm_url: str | None = None) -> dict:
    """Structured health report (the CLI prints it; tests assert it)."""
    report: dict = {"client_version": __version__, "lemond": None,
                    "flm": None}
    for base in ([base_url] if base_url else _candidate_base_urls()):
        if NPU._healthy(base):
            npu = NPU(base, flm_url=flm_url)
            known = npu.models()
            report["lemond"] = {
                "base_url": base,
                "models_known": len(known),
                "defaults": {
                    name: ("ready" if wanted in known else "not pulled yet")
                    for name, wanted in (("llm", DEFAULT_LLM),
                                         ("stt", DEFAULT_STT),
                                         ("embed", DEFAULT_EMBED))
                },
            }
            break
    probe = NPU.__new__(NPU)  # FLM probe without Lemonade discovery
    probe.flm_url = (flm_url or DEFAULT_FLM_URL).rstrip("/")
    if probe.flm_healthy():
        report["flm"] = {"base_url": probe.flm_url,
                         "models_listed": len(probe.flm_models())}
    return report


def _doctor_cli(as_json: bool) -> int:
    report = doctor()
    if as_json:
        print(json.dumps(report, indent=2))
        return 0 if report["lemond"] else 1
    print("WSL -> host NPU bridge doctor")
    if report["lemond"]:
        lem = report["lemond"]
        print(f"  [OK ] {lem['base_url']}")
        print(f"      service up; {lem['models_known']} models known")
        for name, wanted in (("llm", DEFAULT_LLM), ("stt", DEFAULT_STT),
                             ("embed", DEFAULT_EMBED)):
            print(f"      {wanted}: {lem['defaults'][name]}")
    else:
        print("  [  x] Lemonade service: no endpoint answered")
    if report["flm"]:
        print(f"  [OK ] FLM companion at {report['flm']['base_url']} "
              f"({report['flm']['models_listed']} models listed)")
    else:
        print(f"  [  -] FLM companion at {DEFAULT_FLM_URL} (not running)")
    if not report["lemond"]:
        print("  Start the bridge on the host: host\\serve.ps1 "
              "(see README.md)")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Use a Ryzen AI NPU from WSL2")
    parser.add_argument("--version", action="version",
                        version=f"wsl_npu {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_doc = sub.add_parser("doctor", help="find the host services and report readiness")
    p_doc.add_argument("--json", action="store_true", help="machine-readable output")
    p_chat = sub.add_parser("chat", help="one-shot NPU chat completion")
    p_chat.add_argument("prompt")
    p_chat.add_argument("--model", default=DEFAULT_LLM)
    p_chat.add_argument("--engine", choices=("lemond", "flm"),
                        default="lemond")
    p_chat.add_argument("--stream", action="store_true",
                        help="stream reply deltas as they arrive")
    p_chat.add_argument("--system", default=None)
    p_stt = sub.add_parser("transcribe", help="transcribe an audio file")
    p_stt.add_argument("audio")
    p_stt.add_argument("--model", default=DEFAULT_STT)
    p_emb = sub.add_parser("embed", help="embed a line of text")
    p_emb.add_argument("text")
    args = parser.parse_args()

    if args.cmd == "doctor":
        return _doctor_cli(args.json)
    npu = NPU()
    if args.cmd == "chat":
        if args.stream:
            for delta in npu.chat_stream(args.prompt, model=args.model,
                                         system=args.system,
                                         engine=args.engine):
                print(delta, end="", flush=True)
            print()
        else:
            print(npu.chat(args.prompt, model=args.model,
                           system=args.system, engine=args.engine))
    elif args.cmd == "transcribe":
        print(npu.transcribe(args.audio, model=args.model))
    elif args.cmd == "embed":
        vec = npu.embed(args.text)
        print(f"{len(vec)} dims; first 5: {[round(x, 4) for x in vec[:5]]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (NPUUnavailable, NPUServiceError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
