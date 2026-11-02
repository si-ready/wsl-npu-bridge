# Contributing

Thanks for looking. This project has a few strong opinions; they are
the reason it works.

## The rules that matter

1. **The client is standard-library-only Python.** No exceptions, no
   "just one small dependency." A WSL user should be able to clone
   and run it on any distro with the Python that's already there.
   The test suite is stdlib `unittest` for the same reason.
2. **PowerShell scripts are pure ASCII.** PowerShell 5.1 reads
   BOM-less files as ANSI; one stray em-dash has already corrupted a
   script in this project's history. CI enforces it.
3. **Receipts or it didn't happen.** Performance claims come with a
   dated JSON receipt in `bench/results/`, produced by
   `bench/bench_npu.py` on real hardware. A wrong receipt is kept
   and corrected in the open (see the 0.1.0 RTF note in the README),
   never quietly deleted.
4. **Fail honestly.** A leg that fails is recorded with its error.
   Nothing falls back to CPU quietly; nothing reports a success it
   didn't measure.
5. **Documentation includes the dead ends.** If you hit a failure
   mode someone else can hit, it goes in `docs/TROUBLESHOOTING.md`
   with the evidence, not just the fix.

## Developing

```bash
python3 -m unittest discover -s tests -v   # no hardware needed
```

The tests stand up a fake host server (both the Lemonade and
FastFlowLM dialects) and exercise the client's parsing, routing,
streaming, retry and error behaviour. Please add a fake-server case
for any client behaviour you add.

Hardware work (backends, models, the watchdog) can only be verified
on a real Ryzen AI machine; if you change `host/*.ps1`, say in your
PR what you ran it on and what the resulting `doctor` output was.

## Pull requests

- One topic per PR; keep the diff reviewable.
- CI must be green (unittest, PSScriptAnalyzer, ASCII gate,
  shellcheck).
- Update `CHANGELOG.md` (Unreleased section) and the docs your
  change touches.
