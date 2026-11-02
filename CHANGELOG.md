# Changelog

All notable changes to wsl-npu-bridge. Versions follow
[semantic versioning](https://semver.org); receipts for every
performance claim live in `bench/results/`.

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
