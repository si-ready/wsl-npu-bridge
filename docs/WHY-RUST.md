# Why the bridgekeeper is Rust — and when we'd choose otherwise

The daemon in `bridgekeeper/` was not picked on taste. Before it
was adopted, the same contract was implemented three times —
**Elixir/BEAM**, **Rust (std-only)**, and **C++ (fork-per-request)**
— and all three were run against the live bridge on the target
hardware (ProArt PX13, WSL2, Lemonade + FastFlowLM on the Ryzen
AI NPU, reached through this repo's SSH reverse relay) under one
black-box battery: correctness, added latency vs calling the
upstreams directly, 8-way concurrent streams, each upstream
killed mid-flight, the daemon itself `kill -9`'d, and a
480-second soak. The production build then ran the same battery
as a fourth contestant. Full receipts (raw JSON), the battery,
and all three implementations live in the
[SuperInstance bridgekeeper-bakeoff repo](https://github.com/SuperInstance/bridgekeeper-bakeoff);
the dated report is `REPORT-2026-11-01.md` there.

## What the race actually measured

Speed was never going to decide it, and didn't: the NPU spends
~50 ms per generated token, and every daemon added single-digit
milliseconds (several measurements came out *negative* — pure
sampling noise around zero). The deciding axis was failure
behavior, which is the daemon's whole job.

| Line that mattered | Elixir | Rust | C++ | Rust (production) |
|---|---|---|---|---|
| Idle memory | ~150–160 MB | **2.7 MB** | ~18 MB | **2.7 MB** |
| Dependencies | 0 (OTP stdlib) | **0** | 2 | **0** |
| Correctness (4 routes) | 3/4 (embed dims missed once) | 3/4 (transcribe missed once) | 4/4 | **4/4** |
| Embed with Lemonade killed: fails < 2 s | **✓ 502 @ 1.5 s** | ✗ >10 s | ✗ >10 s | **✓ 502 @ 1.83 s** |
| Chat with FLM killed mid-stream | error event ✓ | error event ✓ | error event ✓ | error event ✓ |
| Chat survives the *other* upstream dying | ✓ | ✓ | ✗ | ✓ |
| Recovery after FLM restored | 0.5 s | 0.7 s | ✗ (not in 120 s window) | 2.6 s |
| Soak 480 s (errors / hangs) | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

## Why Rust won this job

1. **Fewest moving parts that can fail at 3 a.m.** Zero
   dependencies, one source file, a 3-second offline build, no
   runtime to babysit. On a personal machine that reboots,
   updates, and sleeps, the daemon most likely to still be
   working in six months is the one with the least in it.
2. **Memory rent.** 2.7 MB resident versus ~160 MB for the BEAM
   build — on a 32 GB laptop where WSL sees 16 GB, that's the
   difference between "always on, forgotten" and "a line item."
3. **Failure behavior is mechanical, not emergent.** Bounded
   socket timeouts + one thread per connection + `catch_unwind`
   per handler. Every failure the battery manufactured arrived
   as the contract's error shape, and a perfect soak followed.
4. **Its one real defect was fixable in-architecture.** The
   contestant failed the dead-Lemonade embed line — through the
   relay, a dead upstream's socket can sit open and silent, so
   dial-and-wait can't meet a 2 s bound. The production build
   answers from the health poller's knowledge and puts a short
   fuse on response headers (down → instant 502; alive-but-cold
   → patient wait). Measured after the fix: 502 in 1.83 s, and
   a fully clean card. Nothing about the fix required a
   different language — it required having *measured* the
   failure, which is what the bake-off was for.

## When to choose Elixir instead — honestly

Choose the BEAM design when the front door stops being a
single-user pipe and becomes a **control plane**: thousands of
concurrent connections, many tenants, hot code upgrades,
supervision trees as the organizing principle. Its chaos record
in this race was flawless (the only clean pass on the embed line
before the production fix — its tight internal budgets are a
real design virtue), `/health` reads there can *never* block
behind a sick poller, and "let it crash" is a stronger story
than `catch_unwind` when the process count is the point. The
cost is a runtime and ~150 MB idle on small hardware. If this
bridge ever grows a multi-client serving layer, the Elixir
contestant in the bake-off repo is the starting point — it is
complete, tested, and documented there.

## When to choose C++ instead — honestly

Choose C++ when the daemon's handlers must **parse hostile
input** or **drive the NPU stack directly** (ONNX Runtime GenAI,
DirectML, the vendor C++ SDKs). Its fork-per-request design
gives a containment guarantee threads cannot honestly make: a
crashed or compromised handler is a dead child process, counted
in `/health` as `worker_crashes`, never a corrupted daemon. It
also produced the smallest binary (216 KB) and the fastest
concurrency wall time. Its measured weaknesses here — a chat
that broke during the *other* upstream's outage, recovery
probes that never landed inside the window, the same
dead-socket embed flaw — are implementation notes, not language
verdicts; a C++ bridgekeeper built to the production contract
would start from the bake-off contestant and the failure table
in `bridgekeeper/AGENTS.md`. The cost is hand-rolled HTTP and a
listener that still needs an external supervisor.

## The rule this leaves behind

For resident glue on personal hardware — software whose job is
to *not die and not lie* while something slower does the real
work — default to the smallest dependency-free implementation
that can express the failure semantics directly, and make any
candidate *prove* its failure behavior against the real stack
before adoption. Language debates settled by benchmarketing
pick the wrong winner; this one was settled by killing things
and timing the corpses.
