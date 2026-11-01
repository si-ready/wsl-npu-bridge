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

AMD's "hybrid" models split one inference across two engines:

- **Prefill on the NPU.** Prompt processing is dense matrix work —
  exactly what XDNA's spatial array is built for, at a few watts.
- **Decode on the iGPU.** Token generation is memory-bound; the Radeon
  890M's access to the shared LPDDR5X pool wins there.

For WSL workloads this split is the whole point: a long prompt (a file,
a repo, a transcript) gets ingested by the NPU, the token stream flows
back into the guest, and neither the discrete GPU nor the CPU is
touched. Background agents in WSL can hold a permanent, low-power
inference channel while the machine's big silicon does other work.

## Transports, in order of preference

### 1. HTTP over the WSL virtual network (v0.1, baseline)

The host service (Lemonade, OpenAI-compatible API, port 13305) bound
beyond loopback; the guest client discovers the host address
(mirrored-mode localhost, NAT gateway, or the resolv.conf nameserver).

- *Strengths:* zero new code on the host, every tool already speaks it,
  works today.
- *Weaknesses:* NAT mode needs the bind + firewall cooperation;
  per-request HTTP overhead is real but irrelevant next to inference
  time for chat — it matters more for high-rate small calls
  (embeddings, realtime audio chunks).

### 2. Reverse relay (fallback when the firewall says no)

If inbound-to-Windows is blocked and no admin is available, reverse
the direction: the host dials *out* into the guest (outbound is
default-allowed) and forwards the loopback service through that
connection. The guest sees a local listener; the service never leaves
host loopback. Same API, slightly more moving parts.

### 3. virtio-vsock (v0.2, in study)

WSL2's kernel already carries virtio-vsock: `/dev/vsock` is present
and an `AF_VSOCK` socket can be opened from Python today (verified on
the reference machine, kernel 6.18.54.1-microsoft-standard-WSL2,
`CONFIG_VSOCKETS=y`). A vsock channel would give:

- no IP networking at all — no NAT gateway, no bind addresses,
  **no firewall in the path**,
- stream semantics with latency closer to a pipe than a network,
- a natural fit for bidirectional streaming: prompts and audio chunks
  up, token deltas down, control frames both ways.

The open half is the host pairing: the guest speaks virtio-vsock while
Windows listeners use Hyper-V sockets (`AF_HYPERV`), and WSL2 bridges
the two — the exact service-GUID/VM-ID mechanics for third-party apps
are what the transport study (running alongside this build) is
verifying from primary sources before code depends on them.

**Shared memory** across the boundary was considered and rejected:
no general mechanism is available to third parties (the GPU gets one
via dxg by Microsoft's grace; nothing equivalent exists for arbitrary
processes). Any design that needs it is a design that waits on
Microsoft.

## Language mix (design position, study in progress)

The bridge has three layers with different physics; they don't want
the same language.

| Layer | Job | Choice | Why |
|---|---|---|---|
| Host data plane | vsock listener, framing, streaming to/from the runtimes | **Rust** (Tokio) | C++-class performance for IO-bound streaming, memory safety, one codebase cross-compiles the Windows daemon and the Linux client, `windows-rs` for service/device APIs, PyO3 if the Python client wants a native core |
| Native interop shim | Only if a runtime must be driven in-process (custom ORT execution provider, zero-copy ring buffers) | **C++**, surgically | The one place its cost is buying something real; kept behind a narrow C ABI so it can be replaced |
| Control plane | Sessions, routing, retries, many concurrent streams, observability | **Elixir/BEAM** (candidate) | Supervision trees and per-session processes are the best tool in the industry for "thousands of two-way streams that must not die"; a Rust NIF (Rustler) keeps the hot path native. Wrong tool for per-tensor work; right tool for everything around it |
| Guest client (today) | Discovery + API for any WSL program | **Python, stdlib only** | Zero-install is a feature; it stays the public face regardless of what the data plane becomes |

Go is the credible single-language alternative for the data plane
(simpler, slightly softer latency tail); Python alone was rejected for
the daemon role (GIL, deployment weight) but kept for the client,
where its job is discovery and JSON, not throughput.

A formal transport/language study with sourced measurements is being
run against this position; this document will be revised by its
findings, and the deltas recorded here, before v0.2 code is written.

## Serialization

- Token streams and control: JSON today (the upstream API is JSON);
  Protocol Buffers when the vsock protocol lands — small frames,
  streaming-friendly, every candidate language has first-class support.
- Audio chunks (16–100 KB, tens/sec): raw bytes with a length prefix;
  transcoding to a columnar format (Arrow) buys nothing at this size.
- Embedding vectors (768–4096 × float32): raw little-endian bytes in
  the vsock protocol; JSON arrays of floats are ~4× the bytes and
  measurably slower to parse — the one place the baseline HTTP path
  pays real overhead, and a receipt in `bench/` shows it.

## Failure philosophy

- Discovery failure names what was tried and what to start on the
  host. It never silently falls back to CPU inference — a bridge that
  quietly stops using the NPU is worse than one that stops.
- Every performance claim in this repo traces to a dated receipt
  produced by `bench/bench_npu.py` on named hardware.
