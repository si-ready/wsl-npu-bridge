# wsl-npu-bridge

**Use your Ryzen AI NPU from inside WSL2.**

Your laptop has a neural processing unit. Your Linux environment can't
see it. This bridge fixes that — not with a driver (a driver can't, see
below), but with a small service on the Windows host that runs the NPU
for real, and a client inside WSL2 that makes it feel like a local
accelerator:

```python
from wsl_npu import NPU

npu = NPU()                                # finds the host by itself
print(npu.chat("Explain tensor cores"))    # LLM on the NPU (+ iGPU decode)
text = npu.transcribe("standup.wav")       # Whisper, off your CPU entirely
```

- **LLM chat** through AMD's *hybrid* execution, which partitions
  work across the NPU and the Radeon iGPU per phase — at a few watts,
  while your discrete GPU and CPU stay free.
- **Speech-to-text** with Whisper backends that probe for the NPU first.
- **Embeddings**, plus a bench tool that files dated JSON receipts
  (tokens/sec, real-time factor) so "runs on the NPU" is a measurement,
  not a claim.

Status: **v0.1 in active bring-up.** Verified end-to-end on an ASUS
ProArt PX13 (Ryzen AI 9 HX 370). Receipts live in `bench/results/`.

---

## Why not just a driver?

Because the device never reaches the guest. Verified on the reference
machine:

- Windows host: `NPU Compute Accelerator Device, PCI VEN_1022&DEV_17F0`
  — present, healthy, drivable.
- Inside WSL2: no `/dev/accel`, no PCI bus to bind anything to. The
  only paravirtual hardware channel WSL2 ships is `/dev/dxg`, and it
  carries GPUs, not NPUs.

The Linux driver (`amdxdna`) already exists and is mainlined; a perfect
build of it inside WSL2 would find no device. The missing piece is a
host-side channel that only Microsoft can ship — so this project builds
the channel at the layer that's actually open: a service on the host,
a client in the guest. This is also how you stay compatible with every
future WSL release instead of fighting them.

## How it works

```
  WSL2 (Linux)                              Windows host
 ┌────────────────────┐                    ┌──────────────────────────┐
 │ your code          │                    │ Lemonade daemon (lemond) │
 │   │                │   OpenAI-style     │   ├─ FastFlowLM backend  │──► XDNA NPU
 │ client/wsl_npu.py  │──── HTTP API ─────►│   ├─ Ryzen AI (OGA)      │──► NPU prefill
 │ (stdlib only)      │   tokens stream    │   └─ whisper.cpp         │──► iGPU decode
 └────────────────────┘      back           └──────────────────────────┘
```

- The host runs AMD's [Lemonade](https://github.com/lemonade-sdk/lemonade)
  server (Apache-2.0), which drives the XDNA NPU through its FastFlowLM
  and Ryzen AI backends. This repo's host scripts install it pinned and
  hash-verified, user-locally — no admin, no MSI.
- The WSL client is one Python file, standard library only. It
  *discovers* the host: `$WSL_NPU_URL`, then mirrored-mode localhost,
  then the NAT gateway, then the `/etc/resolv.conf` nameserver — so it
  works in both WSL networking modes with zero configuration.
- Recommended setup is WSL **mirrored** networking (localhost works
  both ways, the service keeps its loopback-only bind). NAT mode works
  via gateway discovery, with the caveats in
  `docs/TROUBLESHOOTING.md`. A network-free virtio-vsock transport is
  the v0.2 upgrade; the transport/language study behind these choices
  is summarised in `docs/ARCHITECTURE.md`.

## Quick start (10 minutes)

**1. On Windows** (PowerShell, no admin needed):

```powershell
git clone https://github.com/si-ready/wsl-npu-bridge
cd wsl-npu-bridge
powershell -ExecutionPolicy Bypass -File host\install.ps1   # checks NPU+driver, installs, pulls models
powershell -ExecutionPolicy Bypass -File host\serve.ps1     # starts the service, waits until it answers
```

**2. In WSL2:**

```bash
python3 client/wsl_npu.py doctor        # finds the host, checks model readiness
python3 client/wsl_npu.py chat "Write a haiku about a tugboat"
python3 client/wsl_npu.py transcribe some-audio.wav
python3 bench/bench_npu.py              # numbers + a filed JSON receipt
```

Defaults: LLM `Llama-3.2-1B-Instruct-Hybrid` (NPU+iGPU), STT
`Whisper-Large-v3-Turbo`, embeddings `nomic-embed-text-v1-GGUF`.
Override with `--model` or `$WSL_NPU_LLM` / `$WSL_NPU_STT` /
`$WSL_NPU_EMBED`. Bigger hybrid models (3B/7B/8B) are a `lemonade pull`
away on the host.

## Requirements

- A Ryzen AI 300/400-series machine (XDNA 2 NPU) — Strix Point and up
- Windows 11 with WSL2 (either networking mode), Python 3.10+ in WSL
- AMD NPU driver 32.0.203.314 or newer recommended
  (`install.ps1` checks and warns)

## Security note

The host API has **no authentication by default**, and `serve.ps1`
binds it beyond loopback so NAT-mode WSL can reach it. On a network you
don't trust, set a Lemonade API key (`lemonade config`) or restrict the
bind — details in `docs/TROUBLESHOOTING.md`. Treat it like any local
dev server.

## Layout

| Path | What it is |
|---|---|
| `client/wsl_npu.py` | The WSL-side client library + CLI (stdlib only) |
| `host/install.ps1` | One-time host setup: hardware gate, pinned install, models |
| `host/serve.ps1` | Start/stop the host service with a readiness gate |
| `host/diag.ps1` | Read-only diagnostics when the two sides can't talk |
| `bench/bench_npu.py` | End-to-end benchmarks that file JSON receipts |
| `docs/ARCHITECTURE.md` | Layers, transports, and the language-mix design |
| `docs/TROUBLESHOOTING.md` | The failure modes we've actually hit, and fixes |

## Roadmap

- **v0.1** — HTTP bridge, hybrid LLM + Whisper + embeddings, receipts. *(now)*
- **v0.2** — virtio-vsock transport: no IP networking, no firewall,
  two-way gRPC streaming (guest side is already proven — `/dev/vsock`
  is present and openable in current WSL2 kernels; the study's verdict
  is that transports here are chosen for reliability and clean
  cancellation, since inference time dwarfs wire time by orders of
  magnitude).
- **v0.3** — Rust data-plane daemon for the vsock channel, token
  streaming first-class; Elixir control plane evaluated for
  many-session orchestration (see the architecture study).

## License

Apache-2.0. Lemonade and the AMD runtimes it orchestrates are separate
projects with their own licenses; this repo contains only the bridge:
host scripts, the WSL client, the bench tool, and the docs.
