# wsl-npu-bridge

[![CI](https://github.com/si-ready/wsl-npu-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/si-ready/wsl-npu-bridge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/si-ready/wsl-npu-bridge)](https://github.com/si-ready/wsl-npu-bridge/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**Use your Ryzen AI NPU from inside WSL2.**

Your laptop has a neural processing unit. Your Linux environment can't
see it. This bridge fixes that — not with a driver (a driver can't, see
below), but with a small service on the Windows host that runs the NPU
for real, and a client inside WSL2 that makes it feel like a local
accelerator:

```python
from wsl_npu import NPU

npu = NPU()                                      # finds the host by itself
print(npu.chat("Explain tensor cores"))          # hybrid LLM (NPU + iGPU)
print(npu.chat("Explain tensor cores", engine="flm"))  # FastFlowLM on the NPU
for delta in npu.chat_stream("Tell me a story", engine="flm"):
    print(delta, end="")                         # streamed tokens
text = npu.transcribe("standup.wav")             # Whisper, off your CPU entirely
```

- **LLM chat** through AMD's *hybrid* execution, which partitions
  work across the NPU and the Radeon iGPU per phase — at a few watts,
  while your discrete GPU and CPU stay free.
- **Speech-to-text** with Whisper backends that probe for the NPU first
  (AMD's NPU offload for Whisper is the encoder, Ryzen AI docs §3.8).
- **Embeddings**, plus a bench tool that files dated JSON receipts
  (tokens/sec, real-time factor) so "runs on the NPU" is a measurement,
  not a claim.

Status: **v0.2.** Verified end-to-end on an ASUS ProArt PX13
(Ryzen AI 9 HX 370, Windows 11 build 26300, WSL2 NAT mode).
Receipts live in `bench/results/`; the measured results — including
the two upstream defects that shape this release — are in
[Measured results](#measured-results-reference-machine) below.
New in 0.2: streaming chat, FastFlowLM as a first-class client
engine, transport retries, a median-of-N bench, autostart +
watchdog for the host stack, and a hardware-free test suite in CI.

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
 ┌────────────────────┐                    ┌───────────────────────────┐
 │ your code          │  127.0.0.1:13305   │ Lemonade daemon (lemond)  │
 │   │                │◄── SSH reverse ───►│   ├─ Ryzen AI (OGA hybrid)│─► NPU + iGPU
 │ client/wsl_npu.py  │     relay (host    │   ├─ whisper.cpp (NPU)    │─► NPU
 │ (stdlib only)      │     dials OUT)     │   └─ llama.cpp (embed)    │─► CPU/GPU
 │                    │  127.0.0.1:53999   │ FastFlowLM (flm serve)    │─► NPU
 │ any OpenAI client  │◄── SSH reverse ───►│   companion service       │
 └────────────────────┘     relay          └───────────────────────────┘
```

- The host runs AMD's [Lemonade](https://github.com/lemonade-sdk/lemonade)
  server (Apache-2.0), which drives the XDNA NPU through its Ryzen AI
  and whisper.cpp backends, plus a FastFlowLM companion service.
  This repo's host scripts install everything pinned and
  hash-verified, user-locally — no admin, no MSI.
- **The transport is an SSH reverse relay.** Windows Firewall silently
  drops inbound WSL→host connections to user processes, and fixing
  that needs an admin rule — so the host dials *out* into WSL over the
  sshd that is already there, and forwards the loopback services back
  through the tunnel. Zero firewall rules, zero admin, and the
  unauthenticated APIs keep loopback-only binds (never LAN-exposed).
  A dedicated, tightly restricted SSH key (single `authorized_keys`
  line, forwarding-only) is generated for this; see `host/relay.ps1`.
- The WSL client is one Python file, standard library only. In relay
  mode the host is simply `127.0.0.1` inside WSL; direct endpoint
  discovery (`$WSL_NPU_URL`, mirrored localhost, NAT gateway,
  resolv.conf nameserver) remains as a fallback for setups that
  prefer it. Mirrored networking mode makes the relay unnecessary —
  see `docs/ARCHITECTURE.md`, which also records the transport study
  behind these choices and the virtio-vsock plan.

## Quick start (10 minutes)

**1. On Windows** (PowerShell, no admin needed):

```powershell
git clone https://github.com/si-ready/wsl-npu-bridge
cd wsl-npu-bridge
powershell -ExecutionPolicy Bypass -File host\install.ps1   # checks NPU+driver, installs backends + models
powershell -ExecutionPolicy Bypass -File host\serve.ps1     # starts services + relays; stops and says so if the one-time WSL step below is still owed
```

**2. In WSL2 (once):**

```bash
bash wsl/authorize-relay.sh             # authorizes the bridge's restricted relay key
```

Then re-run `host\serve.ps1` on Windows (or `host\relay.ps1` if the
services are already up). To make the bridge permanent — start at
every logon, health-checked every 5 minutes, self-healing:

```powershell
powershell -ExecutionPolicy Bypass -File host\install-autostart.ps1   # -Remove undoes it
```

And from WSL:

```bash
python3 client/wsl_npu.py doctor        # checks both endpoints + model readiness
python3 client/wsl_npu.py transcribe some-audio.wav
python3 bench/bench_npu.py              # numbers + a filed JSON receipt
```

LLM chat works two ways, both through the one client
(`--engine flm` or `--engine lemond`, streaming with `--stream`):

- **Via FastFlowLM** (the path verified end-to-end): FLM models on
  the NPU, via the companion endpoint — also a standard
  OpenAI-compatible API at `http://127.0.0.1:53999` for any other
  client, model `llama3.2:1b`.
- **Via Lemonade hybrid models**: works when the machine's
  Windows ML / ONNX Runtime stack cooperates; on the reference
  machine's Windows build it currently does not — see Measured
  results. This is an upstream defect, not a bridge one, and the
  bridge is structured so the fix lights the path up with no
  changes here.

Defaults: LLM `Llama-3.2-1B-Instruct-Hybrid` (NPU+iGPU), STT
`Whisper-Large-v3-Turbo`, embeddings `nomic-embed-text-v1-GGUF`.
Override with `--model` or `$WSL_NPU_LLM` / `$WSL_NPU_STT` /
`$WSL_NPU_EMBED`. Bigger hybrid models (3B/7B/8B) are a `lemonade pull`
away on the host — with two cautions from AMD's docs: hybrid models are
release-coupled (a model published for an earlier Ryzen AI release is
not compatible with a newer runtime; after a Lemonade/Ryzen AI update,
re-pull hybrid models before debugging anything), and long contexts
are hungry (a 4B hybrid model at 64K–128K context can consume
20–30 GB of RAM, Ryzen AI 1.9.0 docs §3.14 — a real cliff on a
32 GB machine).

## Requirements

- A Ryzen AI 300/400-series machine (XDNA 2 NPU) — Strix Point and up
- Windows 11 with WSL2 (either networking mode), Python 3.10+ in WSL
- AMD NPU driver 32.0.203.314 or newer recommended
  (`install.ps1` checks and warns). AMD's documented minimum for the
  current execution provider is 32.0.203.280 (Ryzen AI 1.9.0 docs,
  EP/driver table §3.7.1); 314 is this bridge's tested floor.

## Measured results (reference machine)

ASUS ProArt PX13, Ryzen AI 9 HX 370, 32 GB, NPU driver 32.0.203.314,
2026-11-01. Full receipts in `bench/results/`.

| Path | Result |
|---|---|
| Whisper Large v3 Turbo, from WSL | **Works.** v0.2 bench (median of 3, warmed): 13.2 s clip in **2.91 s — 4.5× real-time**, word-perfect. (v0.1 first run: 9.0 s clip in 5.75 s warm, 32 s cold incl. model load) |
| Embeddings (nomic-embed-text-v1, 768 d), from WSL | **Works.** Warm median **0.011 s**. First-ever call: 72.9 s (lemond auto-pulled the model and auto-installed its backend) |
| LLM chat via FastFlowLM (`llama3.2:1b`), from WSL | **Works.** v0.2 bench medians (engine-reported): prefill **143.8 tok/s**, TTFT **0.36 s**, decode **58.3 tok/s**; 46.2 tok/s by the wall clock. (v0.1 single run: prefill 103, decode 55.1 tok/s) |
| LLM chat via Lemonade hybrid (`Llama-3.2-1B-Instruct-Hybrid`) | **Broken by an upstream defect on this Windows build** — details below |

Two defects found during bring-up, both upstream of this repo, both
documented with evidence in `docs/TROUBLESHOOTING.md`:

1. **RyzenAI-Server cannot start on Windows build 26300**: its
   bundled ONNX Runtime 1.23.2 is shadowed by the Windows inbox
   ONNX Runtime 1.17.1, and the server dies with *"The requested
   API version [23] is not available… Current ORT Version is:
   1.17.1"*. Machines whose Windows ML runtime is newer may not hit
   this; the hybrid path needs no bridge changes when it heals.
   Filed upstream: [lemonade#4038](https://github.com/lemonade-sdk/lemonade/issues/4038).
   AMD's own documentation puts this build inside the supported
   envelope — Ryzen AI 1.9.0 lists Windows 11 build 26100 or newer as
   its prerequisite (§3.2.1) — so this is an upstream defect on a
   supported configuration, not an unsupported-machine quirk.
2. **Lemonade's FLM model discovery fails** in the 2026.44.1
   embeddable build (JSON parse error after a successful
   `flm list --json`), so FLM models can't be served *through*
   lemond yet — hence the companion `flm serve`. Filed upstream:
   [lemonade#4039](https://github.com/lemonade-sdk/lemonade/issues/4039).
   Both pieces are AMD-documented, not hacks: FastFlowLM is an
   official Ryzen AI execution mode ("NPU-first runtime built
   exclusively for Ryzen AI", Ryzen AI 1.9.0 docs, execution-mode
   table) and Lemonade is AMD's documented server interface
   (§§3.11–3.12). Only Lemonade's *discovery* of FLM is broken.

Also disclosed: the first bench receipt
(`bench-20261101-144554.json`) reports a nonsense Whisper RTF of
438× — that was a bug in this repo's own WAV-duration parser (fixed;
the corrected receipt is `bench-20261101-144635.json`). The receipt
is kept, not deleted: a wrong number with a correction trail beats a
quiet edit.

## Security note

The host APIs have **no authentication**, and the design keeps them
that way *safely*: both services bind Windows loopback only, and the
only way in from WSL is the relay, which rides an SSH key restricted
to exactly those two forwards (no shell, no other ports). Nothing is
exposed on the LAN. If you reconfigure a service to bind beyond
loopback (the NAT-direct fallback), set a Lemonade API key first —
see `docs/TROUBLESHOOTING.md`.

## Layout

| Path | What it is |
|---|---|
| `client/wsl_npu.py` | The WSL-side client library + CLI (stdlib only; chat, streaming, FLM engine, transcribe, embed, doctor) |
| `host/install.ps1` | One-time host setup: hardware gate, pinned install, backends, models, script install |
| `host/serve.ps1` | Start/stop the whole stack (services first, verified, then relays) |
| `host/relay.ps1` | The SSH reverse relays + the dedicated restricted key |
| `host/watchdog.ps1` | Single-pass health check + conservative repair (run by the scheduler) |
| `host/install-autostart.ps1` | Logon start + 5-minute watchdog as user-level Scheduled Tasks |
| `wsl/authorize-relay.sh` | One-time WSL-side authorization of that key (convergent, idempotent) |
| `bench/bench_npu.py` | End-to-end benchmarks (warmup + medians) that file JSON receipts |
| `tests/` | Hardware-free unittest suite (fake host server) |
| `bridgekeeper/` | The resident WSL daemon (Rust, zero dependencies): one honest loopback API — aggregated `/health`, SSE chat, embed, transcribe — that fails fast and truthfully when an upstream dies. Its own README + an agent runbook (`AGENTS.md`) live there |
| `docs/ARCHITECTURE.md` | Layers, transports, and the language-mix design |
| `docs/WHY-RUST.md` | Why the daemon is Rust — the three-language bake-off record, and when Elixir or C++ would be the right call instead |
| `docs/TROUBLESHOOTING.md` | The failure modes we've actually hit, and fixes |

## Roadmap

- **v0.1** — HTTP bridge, hybrid LLM + Whisper + embeddings, receipts. *(shipped)*
- **v0.2** — production polish: streaming, FLM engine in the client,
  retries, autostart + watchdog, tests + CI. *(shipped)*
- **v0.3** — the bridgekeeper: a resident Rust daemon in WSL fronting
  both services behind one loopback API with truthful health and
  fail-fast semantics. It earned the place by winning a
  three-language bake-off (Elixir, Rust, C++) under a black-box
  chaos battery on the real hardware — see `docs/WHY-RUST.md`. *(now)*
- **v0.4** — virtio-vsock transport: no IP networking, no firewall,
  two-way gRPC streaming (guest side is already proven — `/dev/vsock`
  is present and openable in current WSL2 kernels; the study's verdict
  is that transports here are chosen for reliability and clean
  cancellation, since inference time dwarfs wire time by orders of
  magnitude). The bridgekeeper is the daemon that would front it.

## License

Apache-2.0. Lemonade and the AMD runtimes it orchestrates are separate
projects with their own licenses; this repo contains only the bridge:
host scripts, the WSL client, the bench tool, and the docs.
