# Security

## Threat model, plainly

The bridge runs two inference APIs (Lemonade, FastFlowLM) that have
**no authentication**. The design keeps them safe by *placement*,
not by pretending they have auth:

- Both services bind **Windows loopback only**.
- The only path in from WSL is an SSH tunnel authenticated by a
  **dedicated keypair** created for this purpose
  (`%LOCALAPPDATA%\wsl-npu-bridge\bridge_key`), authorized on a
  single `authorized_keys` line with `restrict,port-forwarding` and
  `permitlisten` for exactly the two bridge ports. That key cannot
  open a shell, cannot forward anything else, and is useless for any
  other host.
- Nothing is exposed on the LAN in the default (relay) setup.

Residual risks, honestly:

- Anything running **inside WSL as your user** can call the APIs —
  that is the point of the bridge. Don't run untrusted code in the
  same WSL user account if that worries you.
- The NAT-direct fallback (no relay) requires binding beyond
  loopback; if you use it, set a Lemonade API key first (the daemon
  reads `$LEMONADE_API_KEY`; its own startup log warns about exactly
  this when it binds beyond loopback unauthenticated).
- Model files are executed by the host runtimes. Pull models from
  sources you trust (the installer's defaults come from AMD's
  catalogs).

## Reporting a vulnerability

Open a GitHub issue marked `security` if it's safe to discuss
publicly; for anything sensitive, contact the maintainers via the
si-ready organization on GitHub first and we'll arrange a private
channel.
