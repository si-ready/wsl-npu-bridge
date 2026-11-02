# bridgekeeper

The resident daemon of **wsl-npu-bridge**. One small Rust binary —
zero dependencies — that lives in WSL and fronts the bridge's two
host services (Lemonade on `127.0.0.1:13305`, FastFlowLM on
`127.0.0.1:53999`) behind a single, honest API on
`127.0.0.1:14112`.

Why a daemon in front at all? Because the services behind it are
allowed to be slow, wedged, or dead, and callers deserve better
than a hung socket: one place that always answers `/health` in
milliseconds, turns a dying upstream into an error event instead
of a stuck stream, and fails an embed fast (502 in well under
2 s) when Lemonade is down. The inference itself takes ~50 ms per
token on the NPU; the daemon adds single-digit milliseconds. Its
job is not speed — it is to *not die and not lie* while waiting.

## API

| Route | What it does |
|---|---|
| `GET /health` | `{"ok":true,"uptime_s":N,"lemond":"up\|down","flm":"up\|down","requests":N,"errors":N,"panics":N}` — upstreams polled every 2 s; the first answer is measured before the port opens, never a default guess. |
| `POST /chat` `{"prompt":"...","max_tokens":512}` | Server-sent events proxied from FastFlowLM (`llama3.2:1b`): `data: {"delta":"..."}` per token, then `data: {"done":true}`. If the upstream dies mid-stream you get `data: {"error":"..."}` and a close — never a hang. If *you* disconnect, the upstream socket drops and generation stops. |
| `POST /embed` `{"text":"..."}` | `{"dims":768,"embedding":[...]}` via Lemonade's nomic embeddings. Upstream down → HTTP 502, fast. |
| `POST /transcribe` (raw WAV body) | `{"text":"..."}` via Lemonade's Whisper. |

## Build and run

```sh
./build.sh   # cargo test (the gate) + cargo build --release; works offline
./run.sh     # foreground on 127.0.0.1:14112
```

Requires a Rust toolchain (`rustup` is fine). No crates are
downloaded — there are no dependencies. The release binary is
~450 KB and idles at ~3 MB RSS.

Configuration is environment-only, no files, no flags:

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGEKEEPER_BIND` | `127.0.0.1:14112` | Listen address. Loopback by default on purpose: this API is unauthenticated, like the services behind it (see the repo's SECURITY.md). |
| `BRIDGEKEEPER_LEMOND` | `127.0.0.1:13305` | Lemonade endpoint. |
| `BRIDGEKEEPER_FLM` | `127.0.0.1:53999` | FastFlowLM endpoint. |

It is a foreground process with no state worth flushing (its
counters reset on restart). Run it under whatever supervisor you
already use — systemd user service, a scheduled task, tmux — and
stop it with SIGTERM.

## How it survives things

- **Handler isolation.** Every connection runs on its own thread
  inside `catch_unwind`. A panic kills at most that connection;
  the daemon counts it (`panics` in `/health`) and keeps serving.
- **Every read is bounded.** Connect/read/idle timeouts on every
  socket are what turn "the upstream wedged" into a prompt error
  instead of a stuck client. A chat stream has a 120 s per-read
  idle timeout; transcription gets 900 s because cold Whisper
  loads are genuinely slow.
- **Saturation refuses fast.** Past 128 concurrent connections,
  new ones get an immediate 503 rather than queuing threads
  behind a sick upstream.
- **Health never blocks.** `/health` reads two atomics set by the
  polling thread; it cannot be queued behind a slow upstream call.

## Testing

- `cargo test` — unit tests for the hand-rolled JSON surface
  (string-aware key search included: a prompt that *mentions*
  `"max_tokens":` is never misread as the field).
- The daemon was adopted only after winning a three-language
  bake-off against Elixir/BEAM and C++ implementations of this
  exact contract, run against the live bridge with a black-box
  battery (correctness, added latency, 8-way concurrency, killing
  each upstream mid-flight, `kill -9` on the daemon itself, and an
  8-minute soak). The comparative record is
  [docs/WHY-RUST.md](../docs/WHY-RUST.md); the full race — all
  three implementations and the battery — lives in the
  [SuperInstance bridgekeeper-bakeoff repo](https://github.com/SuperInstance/bridgekeeper-bakeoff).

## Deliberate simplifications

- One request per connection (`Connection: close`). Loopback
  reconnects cost under a millisecond; keep-alive was complexity
  the workload doesn't reward.
- No TLS, no HTTP/2. It binds loopback and speaks to loopback.
  If this API ever needs to cross a network, put it behind a
  proxy that terminates TLS — don't grow one into the daemon.
- The JSON handling is extraction, not a DOM: five fields out of
  three payloads, with the 768-float embedding array copied
  through as raw text (validated, never parsed into floats).
