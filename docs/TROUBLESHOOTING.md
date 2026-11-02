# Troubleshooting

Run `python3 client/wsl_npu.py doctor` first — it checks both bridge
endpoints and tells you which layer is unhappy. Then find your symptom:

## Doctor: no endpoint answered

Work down the stack, in the order the pieces depend on each other:

1. **Is lemond alive on the host?** In PowerShell:
   `Invoke-WebRequest http://127.0.0.1:13305/api/v1/models`
   If that fails, the services aren't up:
   `powershell -ExecutionPolicy Bypass -File host\serve.ps1`
   serve.ps1 starts lemond *and waits until it answers host-local*
   before doing anything else; if it throws, the log it names
   (`%LOCALAPPDATA%\wsl-npu-bridge\logs\lemond.out.log`) says why.
2. **Are the relays up?** Doctor reaches lemond *through* the relay,
   so with lemond healthy host-local, a dead relay is the gap.
   Re-run `host\relay.ps1`. If it says the bridge key is not
   authorized, do the one-time WSL step: `bash wsl/authorize-relay.sh`.
3. **Did WSL reboot?** The relays are per-boot (the guest IP changes
   in NAT mode). Re-run `host\serve.ps1` after a WSL restart.

### Why a relay at all? (the firewall lesson)

Windows Defender Firewall **silently drops** inbound connections
from the WSL VM to a Windows user process unless a rule allows them
— the connection doesn't get refused, it hangs. Before the relay,
the bring-up tried the NAT route (bind `0.0.0.0`, dial the gateway):
the bind was verified correct and the traffic still vanished into
the firewall. The relay inverts the connection (outbound from
Windows is allowed by default), needs no admin, and as a bonus both
services keep loopback-only binds, so nothing is LAN-exposed.

Two traps from that investigation, because they will fool you too:

- **A host-side self-test cannot detect a loopback-only bind.**
  Windows reaches its own vEthernet (WSL gateway) address through
  the loopback path, so `http://<gateway-ip>:13305` answers from
  PowerShell even when the daemon bound 127.0.0.1 only. Only a test
  *from inside WSL* (or `Get-NetTCPConnection -LocalPort 13305`)
  tells the truth about a bind.
- `lemonade config set …` talks to the *running* daemon; with no
  daemon up, the CLI hangs instead of editing the file. The config
  file itself is `%USERPROFILE%\.config\lemonade\config.json`, and
  `lemond.exe --host <addr>` overrides it at launch — serve.ps1 uses
  the flag, deterministically.

### The wedged-port state

If lemond was ever killed ungracefully while relays were connected,
port 13305 can end up held by orphaned sockets: connections time out
instead of refusing, `Get-NetTCPConnection` shows listeners owned by
PID 0, and a fresh lemond exits instantly because its "is another
instance running?" probe sees the port as taken. Recovery is
`host\serve.ps1` itself: it kills the whole stack first, then starts
exactly one lemond and verifies it host-local before relays restart.
Never debug a half-dead stack — restart it cleanly.

## Chat: "RyzenAI-Server failed to start" (HTTP 500 from /load)

Run the backend binary by hand and read its stderr — lemond only
reports that it failed. On the reference machine
(Windows 11 build 26300) the binary dies with:

```
The requested API version [23] is not available, only API versions
[1, 17] are supported in this build. Current ORT Version is: 1.17.1
```

Root cause, verified by file versions: `ryzenai-server.exe` bundles
ONNX Runtime **1.23.2** in its own folder, but on this build the
process loads the Windows **inbox** ONNX Runtime **1.17.1**
(`C:\Windows\System32\onnxruntime.dll`, the Windows ML stack) instead.
PATH, working directory, and the app-directory rule all fail to change
it. This is an upstream/Windows defect, not a bridge defect: the
hybrid models are pulled and ready, and the path lights up unchanged
when the OS-side runtime is new enough. Until then, use the FLM
companion endpoint for LLM chat (it is unaffected — FLM has no ONNX
Runtime dependency).

Related smaller trap: **pulling a model does not install its
backend.** A fresh Lemonade with models but no backends fails every
load the same way. `host/install.ps1` installs all three NPU
backends; manually: `lemonade backends install ryzenai-llm:npu`
(and `flm:npu`, `whispercpp:npu`).

## FLM models don't appear in lemond's model list

Known defect in the Lemonade 2026.44.1 embeddable build: startup logs

```
(FLM) flm list --json exit code: 0, output length: 50569
(ModelManager) FLM model discovery failed: Failed to parse JSON
string: ... invalid literal; last read: ''
```

even with `flm.exe` on PATH and models present in
`%USERPROFILE%\.flm\models`. Because discovery fails, lemond never
registers FLM models. The bridge's workaround is the companion
service: `flm serve` runs standalone (serve.ps1 starts it) and is
relayed on its own port. When upstream fixes discovery, the companion
can retire.

FLM's own trap: its **default serve port (52625) failed to bind**
on the reference machine (`bind: … forbidden … [system:10013]`)
although the port is outside every range in
`netsh interface ipv4 show excludedportrange protocol=tcp`. The
bridge pins FLM to **53999**, which binds cleanly.

## SSH relay: "administratively prohibited" / a forward won't open

`sshd` honors only the **first** `authorized_keys` line matching a
key. If the bridge key ever got authorized on multiple lines ( e.g.
one per port), only the first line's `permitlisten` applies and the
other forwards are refused. `wsl/authorize-relay.sh` converges to
exactly one line carrying both permits — re-run it. Also note
`restrict` + `permitlisten` are per-line: the canonical line is
generated by `host/relay.ps1` into `authorized-line.txt`; don't
hand-edit copies of it.

## install.ps1 says "Installing host scripts" but they never arrive

`$PSScriptRoot` can be **empty** when a script is launched through
some hosts (observed with `powershell.exe -File` driven from WSL
interop; `$PSCommandPath` is empty there too). Code that does
`Join-Path $PSScriptRoot "*.ps1"` then resolves the wildcard
relative to the *working directory*, matches nothing, and
`Copy-Item` "succeeds" copying zero files — a completely silent
no-op that cost this project one debugging round. install.ps1 now
resolves its folder defensively, throws if it can't, and counts
what landed. If you write host scripts for this project: never let
a copy (or any file operation) succeed silently at zero items.

## PowerShell ate my script

Two 5.1 classics, both hit during bring-up:

- **Encoding:** PowerShell 5.1 reads BOM-less files as ANSI. All
  scripts in this repo are pure ASCII for exactly this reason; if
  you edit one, keep it ASCII (an em-dash once corrupted a string
  and changed what the script did).
- **Empty arguments:** `& ssh-keygen.exe -N "" …` silently drops the
  empty argument in 5.1 and the tool then misparses everything
  after it ("Too many arguments"). The working form is `-N '""'`.

## install.ps1: "No XDNA NPU found"

This bridge needs a Ryzen AI 300/400-series processor (XDNA 2). The
installer looks for PCI `VEN_1022&DEV_17F0`. Ryzen 7040/8040 (XDNA 1)
and all Intel/Qualcomm NPUs are different stacks and are not supported
by the backends used here.

## install.ps1: driver warning

FastFlowLM / Ryzen AI current builds are happiest on NPU driver
**32.0.203.314+**. Update through Windows Update (optional updates)
or AMD's Ryzen AI software page, then re-run — the install is
idempotent. Check the stack any time with `flm validate`.

## Chat works, but it's slow / clearly not on the NPU

- Hybrid (-Hybrid) models and FLM models run on the NPU. A `-GGUF`
  model runs on llama.cpp (CPU/iGPU) by design — that's the
  embeddings path, not a bug.
- First call after a pull includes model load and NPU compile; the
  *second* call is the honest measurement. `bench/bench_npu.py`
  files a receipt per run — run it twice and compare.
- Check what the daemon thinks it's doing: `lemonade status` and
  `lemonade logs` on the host.

## Transcription returns nonsense or silence

Whisper expects ordinary WAV/MP3 audio. The installer's own fixture
(`%LOCALAPPDATA%\wsl-npu-bridge\bench-sample.wav`, synthesized speech)
is the control: if the fixture transcribes correctly and your file
doesn't, the difference is the file (codec, sample rate, or it's
actually silent — check with any player).

## Mirrored networking instead of the relay

Perfectly supported: with `networkingMode=mirrored` in
`%UserProfile%\.wslconfig` (then `wsl --shutdown`), WSL localhost
*is* host localhost, the client finds the service at 127.0.0.1 with
no relay, and `serve.ps1`'s relays simply have nothing to fix. The
relay remains the default because it asks nothing of the user's WSL
configuration. Which mode am I in? From WSL:
`wslinfo --networking-mode`.

## Still stuck

File an issue with: your `doctor` output, CPU model, NPU driver
version (`host/install.ps1` prints it), Windows build
(`winver`), WSL networking mode, and the last 30 lines of
`%LOCALAPPDATA%\wsl-npu-bridge\logs\lemond.out.log`. Those answer
most questions before they're asked.
