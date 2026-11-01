# Troubleshooting

Run `python3 client/wsl_npu.py doctor` first. It tries every route to
the host and tells you which layer is unhappy. Then find your symptom:

## Doctor: no endpoint answered

**The host service isn't running.** On Windows:
`powershell -ExecutionPolicy Bypass -File host\serve.ps1`
It starts the daemon and *waits until the API answers* before
returning — if it throws, read the log path it names.

**The service runs but WSL is in NAT mode and the bind is loopback.**
Lemonade defaults to `host = localhost`, which a NAT-mode guest cannot
reach at all (the guest's 127.0.0.1 is itself). `serve.ps1` sets
`host=0.0.0.0` for this reason; if you started the daemon some other
way, run `lemonade config set host=0.0.0.0` and restart it.

**Windows Firewall is eating it.** The first inbound connection to a
new listener needs an allow rule. Check with `host\diag.ps1` (shows
listen addresses, firewall profiles, and self-tests). Options, in
order of preference:

1. Approve the firewall prompt for the Lemonade daemon, or add an
   inbound rule for `lemond.exe` (admin PowerShell):
   `New-NetFirewallRule -DisplayName "WSL NPU Bridge" -Direction Inbound -Program "<path>\lemond.exe" -Action Allow`
2. No admin available? Use the reverse-relay arrangement described in
   `docs/ARCHITECTURE.md` — the host dials out into WSL, so no inbound
   rule is needed.
3. Or switch WSL to mirrored networking
   (`networkingMode=mirrored` in `%UserProfile%\.wslconfig`, then
   `wsl --shutdown`): localhost then works in both directions.

Which mode am I in? From WSL: `wslinfo --networking-mode`.

## install.ps1: "No XDNA NPU found"

This bridge needs a Ryzen AI 300/400-series processor (XDNA 2). The
installer looks for PCI `VEN_1022&DEV_17F0`. Ryzen 7040/8040 (XDNA 1)
and all Intel/Qualcomm NPUs are different stacks and are not supported
by the backends used here.

## install.ps1: driver warning

FastFlowLM / Ryzen AI current builds are happiest on NPU driver
**32.0.203.314+**. Update through Windows Update (optional updates) or
AMD's Ryzen AI software page, then re-run — the install is idempotent.

## Chat works, but it's slow / clearly not on the NPU

- Use a model whose name ends in **-Hybrid** (Ryzen AI recipe) or an
  FLM model. A `-GGUF` model runs on llama.cpp (CPU/iGPU) by design.
- First call after a pull includes model load and NPU compile; the
  *second* call is the honest measurement. `bench/bench_npu.py` warms
  up implicitly by reporting the chat run it makes — run it twice and
  compare the receipts.
- Check what the daemon thinks it's doing: `lemonade status` and
  `lemonade logs` on the host.

## Transcription returns nonsense or silence

Whisper expects ordinary WAV/MP3 audio. The installer's own fixture
(`%LOCALAPPDATA%\wsl-npu-bridge\bench-sample.wav`, synthesized speech)
is the control: if the fixture transcribes correctly and your file
doesn't, the difference is the file (codec, sample rate, or it's
actually silent — check with any player).

## "Address already in use" / two daemons

`serve.ps1 -Stop` kills the daemon it knows about. Strays from other
installs (MSI, pip) can hold port 13305: on Windows,
`Get-NetTCPConnection -LocalPort 13305 -State Listen` shows the owning
PID's address; `Get-Process -Id <pid>` names it.

## Still stuck

File an issue with: your `doctor` output, `host\diag.ps1` output, CPU
model, NPU driver version (`install.ps1` prints it), and WSL networking
mode. Those five things answer most questions before they're asked.
