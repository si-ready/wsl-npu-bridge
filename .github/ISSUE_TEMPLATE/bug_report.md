---
name: Bug report
about: Something in the bridge doesn't work
labels: bug
---

**First: the doctor.** Paste the output of both:

```
python3 client/wsl_npu.py doctor
python3 client/wsl_npu.py doctor --json
```

**Your machine**
- CPU (must be Ryzen AI 300/400 for the NPU paths):
- NPU driver version (`host/install.ps1` prints it; or `flm validate`):
- Windows build (`winver`):
- WSL networking mode (`wslinfo --networking-mode` from WSL):
- Lemonade version:

**What happened**
What you ran, what you expected, what you got. If a model load is
involved, include the last 30 lines of
`%LOCALAPPDATA%\wsl-npu-bridge\logs\lemond.out.log` — lemond's own
summary ("failed to start") hides the real error, which is always
in the log or in the backend binary's stderr.

**What you already tried**
Checked `docs/TROUBLESHOOTING.md`? Which entries matched?
