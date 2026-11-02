# Changelog

All notable changes to wsl-npu-bridge. Versions follow
[semantic versioning](https://semver.org); receipts for every
performance claim live in `bench/results/`.

## [0.3.1] — 2026-11-01

### Added
- **CI/CD deployment, all in GitHub.** CI now builds the
  bridgekeeper (`cargo test` as the gate + release build) on every
  push. A new release workflow turns any `v*` tag into a published
  GitHub Release automatically: gates run, the bridgekeeper binary
  is built and attached with its sha256 — no manual steps, no local
  builds. A user without a Rust toolchain can now fetch the daemon
  prebuilt from the release page and verify it.

### Unchanged
- Everything else. No functional changes to the client, the host
  scripts, or the bridgekeeper — this release is delivery
  machinery around a finished tool, deliberately.

## [0.3.0] — 2026-11-01

### Added
- **bridgekeeper** (`bridgekeeper/`): a resident WSL daemon in Rust
  with zero dependencies that fronts Lemonade + FastFlowLM behind
  one loopback API on `127.0.0.1:14112` — aggregated `/health`
  (poll-based, pre-bind first probe), SSE `/chat`, `/embed`,
  `/transcribe`. Handler isolation via `catch_unwind`, bounded
  timeouts on every socket, a 128-connection saturation cap with
  fast 503s, env-only configuration, and unit tests that gate the
  build (`cargo test` in `build.sh`).
- **Fail-fast semantics measured, not asserted:** an upstream
  known to be down is refused without dialing; a request that
  dials inside the health poller's ~2 s blind window gets a 1.8 s
  header fuse, after which the poller arbitrates — down → 502,
  alive-but-cold → patient retry. Dead-Lemonade embed: **502 in
  1.83 s** under the bake-off battery (the contract bound is 2 s).
- The daemon was adopted by race, not by preference: Elixir/BEAM,
  Rust, and C++ implementations of the same contract ran a
  black-box battery (correctness, latency vs direct, 8-way
  concurrency, per-upstream kills, `kill -9`, 480 s soak) on the
  reference machine; the production build then ran it as a fourth
  contestant and posted the only fully clean card (correctness
  4/4, all chaos lines passed, soak 0 errors / 0 hangs, 2.7 MB
  idle RSS). Comparative record: `docs/WHY-RUST.md`; raw receipts
  and all three contestants: the SuperInstance
  `bridgekeeper-bakeoff` repository.
- `bridgekeeper/AGENTS.md`: an agent-facing runbook — mission
  invariants, an executable verify sequence, a failure-signature
  → remedy table from the failures actually observed, and the
  rules for modifying the daemon safely.

## [0.2.0] — 2026-11-01

### Added
- **Client: streaming chat** (`chat_stream`, CLI `chat --stream`) over
  SSE for both engines.
- **Client: FastFlowLM as a first-class engine** — `chat(...,
  engine="flm")`, `chat_timed` carrying FLM's own prefill/decode/TTFT
  figures, `flm_models()`, structured `doctor()` with `--json` CLI
  output.
- **Client: transport retry with backoff.** Connection-level failures
  (relay mid-restart, service booting) are retried; server verdicts
  (HTTP errors) are raised immediately as `NPUServiceError` with the
  response body. Timeouts configurable via `$WSL_NPU_TIMEOUT`.
- **Bench v0.2:** warmups + median-of-N per leg, a dedicated FLM chat
  leg reporting engine speeds separately from wall-clock rates,
  client version stamped into receipts.
- **Production host stack:** `host/watchdog.ps1` (single-pass health
  check + conservative repair ladder, run by the scheduler) and
  `host/install-autostart.ps1` (user-level Scheduled Tasks: start at
  logon, watchdog every 5 minutes; `-Remove` unwinds). No admin.
- `host/install.ps1` now installs the host scripts under
  `%LOCALAPPDATA%\wsl-npu-bridge\host\`, so autostart survives the
  git clone being moved or deleted.
- **Tests + CI:** stdlib `unittest` suite against a fake host server
  (client routing/streaming/errors, bench WAV parsing) and a GitHub
  Actions workflow (unittest on Python 3.10/3.12, PSScriptAnalyzer,
  an ASCII gate for the PowerShell scripts, shellcheck).

### Fixed
- Bench/chat error handling: one failing leg no longer aborts the
  whole bench (it is recorded as a receipt and the bench continues).

## [0.1.0] — 2026-11-01

First release. Verified end-to-end on an ASUS ProArt PX13
(Ryzen AI 9 HX 370).

- SSH reverse-relay transport (host dials out into WSL; no firewall
  rule, no admin; services keep loopback-only binds) with a dedicated
  SSH key restricted to the two forwards on a single
  `authorized_keys` line.
- Lemonade host service (hybrid LLM, Whisper, embeddings) installed
  pinned + hash-verified, user-local; all three NPU backends.
- FastFlowLM companion service (Lemonade's FLM discovery is broken
  upstream — see TROUBLESHOOTING).
- WSL client (stdlib-only Python): discovery, chat, transcribe, embed,
  doctor.
- Bench tool filing dated JSON receipts.
- Documented upstream defect: ryzenai-server cannot start on
  Windows build 26300 (bundled ONNX Runtime 1.23.2 shadowed by the
  inbox 1.17.1), which takes Lemonade hybrid chat down on that build.
