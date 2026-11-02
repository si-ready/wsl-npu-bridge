# Architecture

## The constraint that shapes everything

WSL2 is a utility VM. Hardware reaches it through exactly one
paravirtual channel Microsoft maintains — `/dev/dxg`, for GPUs. There
is no PCI bus in the guest, no `/dev/accel`, and no third-party way to
add a device channel: that layer is the host's, and it is closed.

So the bridge does not try to make the NPU *appear* in Linux. It makes
the NPU *answer* in Linux:

1. **Execution stays on the host**, where the silicon, the AMD driver,
   and the supported runtimes (FastFlowLM, Ryzen AI / ONNX Runtime
   GenAI) already work.
2. **The guest gets an API**, not a device: a small client library
   whose calls cross the VM boundary and come back with results.

Every design decision below is downstream of that split.

## Execution: hybrid is the interesting part

AMD's "hybrid" models split one inference across two engines. The
popular shorthand — *NPU does prefill, iGPU does decode* — is one
AMD itself uses for hybrid model *generation* (Ryzen AI docs
§3.15.2); the runtime mechanism underneath it is that the runtime
**partitions layers and operations across the NPU and iGPU per
phase**, with the NPU carrying the compute-dense work it is built for
at a few watts and the iGPU carrying the memory-bound work where its
access to the shared LPDDR5X pool wins. The engineering consequences
for the bridge are the same either way:

For WSL workloads this split is the whole point: a long prompt (a file,
a repo, a transcript) gets ingested by the NPU, the token stream flows
back into the guest, and neither the discrete GPU nor the CPU is
touched. Background agents in WSL can hold a permanent, low-power
inference channel while the machine's big silicon does other work.

## Transports, in order of preference

**As built (v0.1):** the reverse relay (§4 below) is what ships,
because on the reference machine the NAT route (§3) was eaten by the
Windows Firewall and mirrored mode (§1) asks for a WSL configuration
change the bridge shouldn't demand. The relay needs nothing from the
user or the OS, keeps both services loopback-only, and measured
indistinguishable from direct for this workload — which the study's
headline finding below predicts. The ranking stands as the design
order for v0.2; the relay is the pragmatic v0.1 baseline, and the
client still discovers direct endpoints when they exist.

A transport/language study (November 2026, primary sources) settled
the ordering below. Its headline finding disciplines all of it:
**the wire is never the bottleneck.** Hybrid inference on this class
of hardware spends ~1–5 s in time-to-first-token and ~50 ms per token;
every candidate transport moves a token in under 1 ms, and the fattest
payload (streaming audio, ~5 MB/s) is trivial for all of them.
Transports are therefore chosen for **reliability and clean
cancellation**, not speed.

### 1. Mirrored-mode localhost TCP (recommended baseline)

With `networkingMode=mirrored` in `.wslconfig`, Microsoft documents
`127.0.0.1` working in both directions between host and guest. The
host service binds loopback (its vendor default — the most secure
posture), the guest client just dials localhost, and NAT's per-boot
gateway addresses, wide binds, and LAN exposure all disappear.

### 2. virtio-vsock / Hyper-V sockets (v0.2 upgrade)

WSL2's kernel already carries virtio-vsock: `/dev/vsock` is present
and an `AF_VSOCK` socket can be opened from Python today (verified on
the reference machine, kernel 6.18.54.1-microsoft-standard-WSL2,
`CONFIG_VSOCKETS=y`). Microsoft officially supports third-party
host↔guest socket apps: the guest dials CID 2; the Windows side
listens on `AF_HYPERV` with {VmId, ServiceId} GUIDs. A vsock channel
gives no IP networking at all (no firewall in the path), pipe-like
latency, and a natural fit for bidirectional streaming.

Known friction, reported honestly because the field evidence
conflicts: some setups must register the service GUID in the registry
as admin; at least one production WSL2 app (X410) reportedly works
with no registration; another (VcXsrv) found a wildcard VmId bind
never matches the WSL2 utility VM — it needs the specific VmId, which
changes every WSL restart but is discoverable **without elevation**
via `wslinfo --vm-id`. The Windows-side listener is hand-wired Winsock
through the `windows` crate (there is no tokio-vsock for AF_HYPERV);
sizing that work is an explicit open item, to be answered by a spike,
not by optimism.

**Spike result (2026-11-01, reference machine, measured):** a
no-admin, no-registry test settles the conflicts. A Windows-side
`AF_HYPERV` listener (raw Winsock via a few lines of C#; protocol
must be `HV_PROTOCOL_RAW`) bound to the **specific** WSL VmId from
`wslinfo --vm-id` and a service GUID of the standard vsock form
`{port}-facb-11e6-bd58-64006a7986d3` accepted a guest `AF_VSOCK`
connection to CID 2 and round-tripped a payload — no elevation, no
registry registration. The **wildcard** VmId bind was also tested:
the guest times out and nothing is ever accepted, confirming the
VcXsrv report. Design consequence for any vsock transport: the
VmId changes every WSL restart, so the channel needs a discovery
step (the guest knows its VmId; the SSH relay can bootstrap it
once) — budget for that, not for admin or registration.

### 3. NAT-mode gateway TCP (fallback)

The v0.1 bring-up path: service bound beyond loopback, client
discovers the gateway/resolv.conf host address. It works, but it is
the fragile option — per-boot addresses, a bind the daemon must
actually honour, firewall cooperation, and an unauthenticated API
exposed on the LAN unless keyed. Kept because some machines can't
switch networking modes; documented, not recommended.

### 4. Reverse relay (fallback when the firewall says no)

If inbound-to-Windows is blocked and no admin is available, reverse
the direction: the host dials *out* into the guest (outbound is
default-allowed) and forwards the loopback service through that
connection. The guest sees a local listener; the service never leaves
host loopback. Same API, slightly more moving parts.

**Rejected with evidence:** Windows named pipes from WSL2 (the pipe
namespace is hidden; only relay hacks exist), Windows `AF_UNIX`
sockets from WSL2 (maintainer-documented as not working with WSL2
VMs), QUIC (no loss to fix on a local link, userspace CPU cost, and
it cannot run over hv_sock), and **shared memory** (no general
third-party mechanism exists — the GPU gets one via dxg by
Microsoft's grace; any design that needs it waits on Microsoft).

## Protocol

- **Internal channel (v0.2): gRPC bidirectional streaming** (tonic).
  It wins on single-connection cancellation, interleaving, and HTTP/2
  flow control — not on bytes. The real latency risk in any of these
  stacks is flush/Nagle/batching discipline, not protocol overhead.
- **Compatibility surface (kept):** the OpenAI-style HTTP + SSE API,
  because the upstream host service speaks it and every tool already
  understands it. Lemonade streams tokens incrementally over SSE, and
  exposes a WebSocket `/realtime` endpoint for audio.
- WebSocket is reserved for browser/Phoenix-style consumers.

## Language mix (study verdict)

The bridge has three layers with different physics; they don't want
the same language. The study confirmed the mix below, with evidence:

| Layer | Job | Choice | Why |
|---|---|---|---|
| Host + guest daemons | vsock/TCP listeners, framing, streaming to/from the runtimes | **Rust** (Tokio, axum/tonic) | One workspace and one `.proto` for both sides of the VM boundary; IOCP-class IO on Windows; the `windows` crate covers service + AF_HYPERV APIs; PyO3 available if the Python client wants a native core later |
| Native interop adapter | Only if a runtime is driven in-process (AMD calls the ONNX Runtime GenAI C++/Python API "the lowest-level API" for hybrid mode) | **C++**, surgically, or Python for the prototype | The one place C++ buys something real. For the daemon itself it is cargo cult — the daemon's IO ceiling is already reached by Rust |
| Control plane | Sessions, routing, retries, many concurrent streams, observability | **Elixir/BEAM** — *optional, Linux-side only, only if multi-session orchestration actually emerges* | Supervision trees, Phoenix Channels, and GenStage/Broadway demand-driven backpressure are superb for many two-way streams. It is the wrong tool for the Windows host daemon (packaging/deployment friction) and for anything per-token. Measured Rustler data sets the rule if it is ever used: a NIF call costs ~62 µs on a dirty scheduler vs ~9 µs in-process at 1 KB, so a NIF must own a whole stream or batch, never a per-token call |
| Guest client (today) | Discovery + API for any WSL program | **Python, stdlib only** | Zero-install is a feature; it stays the public face regardless of what the data plane becomes. gRPC stubs first if it grows; not the daemon (GIL, Windows-service packaging) |

Go is the defensible full substitute for the daemons (faster
development velocity, small measured proxy overhead) if Rust velocity
ever becomes the actual constraint — that is a team call, not a
performance one.

## Serialization (study verdict)

- **Protocol Buffers everywhere internally.** Token streams and
  control frames as protobuf messages; differences vs FlatBuffers are
  microseconds — five orders of magnitude below the token interval.
- **Embeddings and audio ride as `bytes` fields** holding raw
  little-endian float32 / opaque audio. That reaches the zero-copy
  end-state (numpy `frombuffer`, Rust `bytemuck`) without paying
  FlatBuffers complexity. JSON carries vectors at 2–4× the bytes and
  stays only on the compatibility surface.
- FlatBuffers second, Cap'n Proto third (weakest Rust/Windows
  fit), Apache Arrow rejected for single-vector wire use (columnar
  IPC pays only for bulk multi-column batches).
- Zero-copy itself matters **nowhere** in this design: every payload
  is produced once, sent once, consumed once, and the single cross-VM
  copy is unavoidable and costs microseconds at ≤5 MB/s.

## What the study could not verify (measure locally, don't assume)

- No published mirrored-vs-NAT localhost latency/throughput exists.
- No WSL2-specific vsock latency/throughput; the only figures are
  second-hand generic Hyper-V socket numbers.
- No independent HX 370 hybrid-mode benchmarks — only AMD's
  pre-release numbers (8B: TTFT 0.94 s @128 tokens → 5.01 s @2048,
  ~18–21 tok/s; 1.5B: up to ~67 tok/s). The widely quoted "NPU 2.3×"
  figures are Strix Halo measurements and are contradicted by another
  Halo test — never transfer them to this chip.
- Per-token SSE granularity and concurrent model co-residency in the
  host service; audio rates over `/realtime` from WSL; BEAM
  token-forwarding jitter; mirrored-mode stability alongside the
  machine's VPN/AV stack.

These are the bench tool's roadmap: `bench/` exists to replace every
item on this list with a dated local receipt.

## Failure philosophy

- Discovery failure names what was tried and what to start on the
  host. It never silently falls back to CPU inference — a bridge that
  quietly stops using the NPU is worse than one that stops.
- Every performance claim in this repo traces to a dated receipt
  produced by `bench/bench_npu.py` on named hardware.
