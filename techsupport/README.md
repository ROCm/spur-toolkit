# spur-techsupport

Collects a diagnostic bundle from a Spur cluster into one `.tar.gz`. Plain SSH, no Ansible. Native-host (systemd) only — not for Kubernetes clusters.

## Quick start

```bash
./spur-techsupport.sh --controllers root@ctl-0,root@ctl-1,root@ctl-2 --agents root@gpu-1,root@gpu-2
```

Single controller works the same way — just pass one target: `--controllers root@ctl-0`.

For a large fleet, `--controllers`/`--agents`/`--nodes` accept Slurm-style `prefix[N-M]` ranges instead of spelling out every host: `--agents root@gpu-[1-100]`. Zero-padding, multiple ranges/singles, and a mix of both are all supported: `root@gpu-[001,010-012]`, `root@ctl-[1-3],root@gpu-[1-50]`.

Writes `spur-techsupport-<UTC-timestamp>.tar.gz` to the current directory. Each collection step prints a `==>` progress line to stderr as it starts, so a slow or unreachable host shows exactly where things are stuck.

SSH targets are `user@host`. For password auth, set `SSHPASS` instead of putting the password on the command line: `SSHPASS=... ./spur-techsupport.sh ...`.

## Requirements

- SSH access to every host.
- **Passwordless sudo** for the SSH user on every host. Collection runs `sudo journalctl`/`tar`/`sed`/`cat` and `sudo -u postgres psql`/`pg_dump` non-interactively — there's no TTY for `sudo` to prompt on, so a sudo password requirement fails every step with `sudo: a password is required` (being able to `sudo` interactively over an SSH session doesn't help; that has a TTY). Grant it narrowly in `/etc/sudoers.d/`:
  ```
  <user> ALL=(root) NOPASSWD: /usr/bin/journalctl, /usr/bin/tar, /usr/bin/sed, /usr/bin/cat, /usr/bin/chmod, /usr/bin/rm
  <user> ALL=(postgres) NOPASSWD: /usr/bin/psql, /usr/bin/pg_dump
  ```
  (confirm the binary paths with `which`) or `NOPASSWD: ALL` if that's acceptable in your environment.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--controllers <list>` | *(required, or `$CONTROLLERS`)* | comma-separated SSH targets running `spurctld` |
| `--agents <list>` | *(empty, or `$AGENTS`)* | comma-separated SSH targets running `spurd` |
| `--acct-host <target>` | auto-detected among `--controllers` (or `$ACCT_HOST`) | SSH target running PostgreSQL |
| `--nodes <names>` | all agents | limit **agent** log/config collection to these short hostnames (controller/cluster-state/DB collection is unaffected) |
| `--since <duration>` | `24h` | `journalctl` window; accepts either form (`24h` or `-24h`), or a date `journalctl` understands (e.g. `2026-01-01`) |
| `--skip-db` | off | omit PostgreSQL version/connection info and the accounting DB dump |
| `--skip-journal` | off | omit `journalctl` output for `spurctld`/`spurd` |
| `--skip-raft` | off | omit the Raft log/snapshot archive |
| `--raft-all-controllers` | off | collect Raft state from every controller instead of just the first |
| `--partial` | off | emit the archive even if some collection steps failed (see below) |
| `--output <dir>` | cwd | directory the `.tar.gz` is written to |

`$SPUR_HOME` (default `/root/spur`), `$SPUR_INSTALL_DIR` (default `/root/.local/bin`), `$ACCT_DB_NAME`/`$ACCT_DB_PORT` (default `spur`/`5432`) follow the same defaults as `ansible/inventory/group_vars/all.yml` — override them if your cluster used non-default values at deploy time.

Deployments that don't put config and state under a common `$SPUR_HOME` (e.g. an FHS-style install with `/etc/spur/spur.conf` and state in `/var/lib/...`) can override those two paths directly instead: `SPUR_CONF` (default `$SPUR_HOME/etc/spur.conf`) and `SPUR_STATE_DIR` (default `$SPUR_HOME/state`).

## What's collected

| Item | Host(s) | Skippable via |
|---|---|---|
| `spurctld` journal | every controller | `--skip-journal` |
| Raft log/snapshots (`$SPUR_STATE_DIR`) | first controller only (or every controller with `--raft-all-controllers`) | `--skip-raft` |
| `spur.conf` (sanitized, `$SPUR_CONF`) | every controller | — |
| Build fingerprint (path/size/mtime/sha256 of `spur`/`spurctld`/`spurd`) | every host | — |
| `sinfo` / `squeue -t all` / `sdiag` | first controller | — |
| PostgreSQL version + connection info | auto-detected controller (or `--acct-host`) | `--skip-db` |
| Full `pg_dump` of the accounting database | auto-detected controller (or `--acct-host`) | `--skip-db` |
| `spurd` journal | every agent (filtered by `--nodes`) | `--skip-journal` |
| `spurd.service` unit (agents have no separate config file) | every agent (filtered by `--nodes`) | — |

A `manifest.txt` at the archive root records `[OK]`/`[SKIPPED]`/`[FAILED]` for every item with a reason. `spur --version` is not a supported flag (it errors), which is why build identity is a binary fingerprint instead of a version string.

Raft state is replicated across controllers, so collecting it from just one is normally enough — the rest are marked `[SKIPPED]` with a reason rather than silently omitted. Pass `--raft-all-controllers` to collect from every controller instead, e.g. to compare/debug divergence in an HA cluster. There's no automatic leader detection — "first controller" just means the first entry in `--controllers`.

## What's deliberately excluded

- WireGuard private keys (`/etc/wireguard/*`) — never touched.
- `spur.conf`'s `database_url` password segment — redacted in place before the file is copied back; host/port/dbname/user are kept for diagnostic value. `pg_dump` output contains no credentials (the connection itself authenticates via local `postgres`-user access, not the password in the DSN).

## Partial-failure behavior

By default, if any collection step fails, the manifest is printed, nothing is archived, and the script exits non-zero — a partial bundle without an escape hatch would silently hide missing data. Pass `--partial` to package the archive anyway; failures still show up in the manifest.
