# bridgekeeper — agent runbook

Audience: an agent (or a script) that must build, verify, operate,
or modify this daemon and be able to *prove* each step worked.
Humans: README.md is yours. Everything here is executable:
commands, expected observations, invariants, and the failure
signatures seen on the real hardware with their remedies.

## Mission invariant

The daemon fronts two loopback upstreams that are allowed to be
slow, wedged, or dead — Lemonade `127.0.0.1:13305`, FastFlowLM
`127.0.0.1:53999` — behind one API on `127.0.0.1:14112`.
Its contract is behavioral, not functional:

1. `/health` always answers within milliseconds, from the
   poller's atomics — never by dialing an upstream inline.
2. A dead upstream produces a fast, explicit failure: `/embed`
   and `/transcribe` → HTTP 502; `/chat` → an SSE
   `data: {"error": ...}` event followed by close. **Never a
   hang, never a silent success, never a dead daemon.**
3. A handler failure (including a panic) costs at most that one
   connection. The listener and the poller are untouchable.
4. Latency added over calling the upstreams directly must stay
   in the noise (measured: ≤ ~3 ms; the NPU spends ~50 ms/token,
   the wire is never the bottleneck — do not optimize the wire).

If a change breaks any of these, the change is wrong regardless
of what else it improves.

## Build (offline, ~3 s + test compile)

```sh
cd bridgekeeper
./build.sh        # = cargo test --release && cargo build --release
```

Expected: all unit tests pass (the gate — the JSON surface is
hand-rolled and carries its own evidence), then
`target/release/bridgekeeper` exists (~450 KB, stripped, LTO).
Zero dependencies: if `Cargo.toml` gains a dependency, that is a
design decision requiring justification in docs/WHY-RUST.md
terms, not a convenience.

## Verify (in order; stop at the first failure)

Environment: the bridge stack must be up (host services + relay),
and this daemon runs in WSL.

```sh
# 1. Stack check, from WSL:
python3 ../client/wsl_npu.py doctor
#    expect: [OK] on 13305 with 3 models ready; FLM [OK] 41 models.

# 2. Start the daemon (foreground; background it however you like):
./run.sh &

# 3. Health must be truthful immediately (main() probes before bind):
curl -s http://127.0.0.1:14112/health
#    expect: {"ok":true,...,"lemond":"up","flm":"up",...}
#    A "down" here in the first second is a bug: the pre-bind
#    synchronous poll exists precisely to prevent it.

# 4. The four routes, against the live stack:
curl -s -X POST http://127.0.0.1:14112/embed \
  -d '{"text":"a tugboat crossing a cold harbour"}'
#    expect: {"dims":768,"embedding":[...]} (768 floats, verbatim)
curl -s -N -X POST http://127.0.0.1:14112/chat \
  -d '{"prompt":"Count from one to five.","max_tokens":32}'
#    expect: data: {"delta":...} lines, then data: {"done":true}
curl -s -X POST http://127.0.0.1:14112/transcribe \
  --data-binary @../bench/bench-sample.wav
#    expect: {"text":"...quick brown fox..."} (the fixture's text)

# 5. Full battery (the bake-off harness; see "Reproduce the race"):
#    run against this build as contestant "prod".
```

## Failure signatures → remedy (all observed on the real stack)

| Signature | Meaning | Remedy |
|---|---|---|
| `/embed` hangs >2 s with Lemonade down | Two layers guard this; a hang means one regressed. (1) Known-down: handler checks `lemond_up` and 502s without dialing. (2) Blind window: a request that dials within the poller's ~2 s blind window after a death connects through the relay to a socket that sits open and silent (measured: no answer in 12 s) — the embed path therefore puts a 1.8 s fuse on response *headers*, then re-checks the poller: down → 502, up → re-issue with the generous timeout (cold loads are legitimately slow). | Verify both layers are in `handle_embed`. If a hang persists, check the poll thread is alive (upstream flags in `/health` still change when you kill an upstream). |
| `flm unreachable: no response` in the log after a kill | Relay accepted the connection, host side had nothing. Expected during an FLM outage; the daemon must surface it as an SSE error, which it does. | None (daemon behavior correct). Restore the stack (below). |
| FLM never answers after being killed and restarted alone | Measured platform behavior: a bare `Start-Process flm.exe` after a hard kill wedges in model load (still silent at 290 s). | **Never restart bridge services piecemeal.** Restore via `host/serve.ps1` (clean-slate kill, ordered starts, host-local verify, relay restart). The production watchdog does exactly this. |
| Lemonade chat returns HTTP 500, everything else fine | Known upstream defect (lemonade #4038): ryzenai-server is shadowed by the Windows inbox ONNX Runtime on build 26300. Not a daemon bug; chat flows through FLM instead. | None available locally; do not "fix" by rerouting silently — surface it. |
| `/health` shows `panics` > 0 | A handler panicked and was contained (the design working). | Read the stderr log line `PANIC contained at file:line` — fix the cause; the counter is the tripwire, not the problem. |
| New connections get 503 `saturated` | >128 concurrent connections: something is piling clients onto a wedged upstream. | Check upstream health first; the cap is backpressure, not a bug. Raise `MAX_ACTIVE` only with evidence. |
| CPU spin / accept errors in log | Accept loop retries after 50 ms by design. | If persistent, the listener socket is broken; restart the daemon (it holds no state worth saving). |

## Restart / recovery semantics

- The daemon holds **no durable state** (counters only). Kill it,
  start it, done. SIGTERM/SIGINT are fine; there is nothing to
  flush. Recovery time measured after `kill -9`: ~0.25 s to
  healthy.
- The *stack's* recovery is not the daemon's job: serve.ps1 +
  the 5-minute watchdog own host-service recovery. The daemon's
  job during an outage is truthful `/health` and fast failures,
  then instant recovery when the poller sees the upstream return
  (measured daemon-side recovery after upstream return: <1.5 s).

## Modification rules

1. `panic = "unwind"` in Cargo.toml is load-bearing:
   handler isolation is `catch_unwind`. An `abort` profile
   silently defeats it. Do not change it.
2. Every socket read/write keeps an explicit timeout. A call
   without one is a hang waiting for its upstream.
3. Key search over JSON must stay string-aware
   (`find_key_value_start` skips string literals). A prompt that
   mentions `"max_tokens":` is data, not a field. The unit tests
   pin this; extend them when touching the parser.
4. `/health`'s required keys never change shape; additive extra
   fields are allowed (clients tolerate them, the battery checks
   the required set).
5. Env-only config (`BRIDGEKEEPER_BIND`, `BRIDGEKEEPER_LEMOND`,
   `BRIDGEKEEPER_FLM`). No config files, no CLI flags.
6. After any change: `./build.sh`, the verify sequence above,
   and the battery as `prod`. Report numbers, not adjectives.
7. A deploy that depends on an edit is strictly serial — never
   issue the edit and the copy in parallel (a tar once shipped
   pre-fix bytes and the battery "failed" code that was never
   deployed). Read the deployed file back on the target before
   testing anything.

## Reproduce the race (how this daemon earned its place)

The daemon is the production descendant of the Rust contestant
in the SuperInstance `bridgekeeper-bakeoff` repo — three
implementations (Elixir/BEAM, Rust, C++) of one contract, one
black-box battery, run on the ProArt against the live bridge:

```sh
# On the ProArt, in ~/bakeoff (see that repo's README/SPEC):
python3 battery.py elixir   # ports: elixir 14111, rust 14112,
python3 battery.py rust     # cpp 14113, prod 14112 (never together)
python3 battery.py cpp
python3 battery.py prod     # this production build
```

Battery rules an agent must respect (each was learned by
breaking something):

- **Pause the bridge watchdog first** (Scheduled Task
  "WSL NPU Bridge (watchdog)") or it will resurrect services
  mid-chaos-phase and invalidate the measurements. Re-enable it
  after, and verify with `wsl_npu.py doctor`.
- The battery kills real upstreams. Restores go through
  `serve.ps1`, never bare process starts (see the table above).
- Run the battery **detached** (`setsid nohup ... &`) writing to
  a session log: an SSH-attached run dies with the connection
  and orphans state.
- Results are the JSON files in `results/`; a phase that raises
  is a harness bug — the harness records failures, it does not
  crash on them (guards in `daemon_embed`/`direct_embed` exist
  because the unguarded version did).
