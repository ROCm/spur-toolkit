# Contributing to the Spur Ansible roles

Implementation notes for anyone **editing** these roles/playbooks. If you're just deploying a cluster, you don't need this — see [`README.md`](README.md).

These are hard-won findings from validation: real bugs and non-obvious Spur/Ansible behaviors that shaped how the tasks are written. Each explains *why the code looks the way it does*, so you don't "simplify" it back into a bug. If you hit a new one, add it here and encode the fix in the role.

## Daemon / process management
- **Daemons run as systemd units**, not `nohup` — `/etc/systemd/system/spur{ctld,d}.service`, `enabled` (survive reboot), `Restart=on-failure`. The roles `daemon-reload` after templating a unit and use `state: restarted` so a redeploy always picks up new binaries/config. (`spurdbd.service` only exists as a leftover from a pre-merge deployment, cleaned up by `spur_accounting` on the accounting host.)
- **`-D` is `--foreground`, not "daemonize"** (`crates/spurctld/src/main.rs`). The systemd units correctly run the binary in the foreground under `Type=simple` — don't add `-D`.
- **`pkill -f spurd` also kills `spurctld`** (substring match). Teardown uses `pkill -x` (exact name) only.
- **Stop-before-wipe ordering.** The controller role stops spurctld *before* wiping `~/spur/state`, so the daemon can't rewrite the Raft log mid-delete.

## Install
- **`spur --version` is not a supported flag** — it errors. The roles check for the binary with `stat`, not by running `--version`.
- **All 18 Slurm-compat symlink names** the `spur` multi-call binary recognizes via argv[0] dispatch are created regardless of install source: `sbatch`, `squeue`, `sinfo`, `scancel`, `sacct`, `sacctmgr`, `scontrol`, `salloc`, `srun`, `sattach`, `scrontab`, `sdiag`, `smd`, `sprio`, `sreport`, `sshare`, `sstat`, `strigger`. Keep this list in sync with upstream's dispatch table if it changes.
- **`install.sh` and `spur_binary_src` are two interchangeable install sources** — the symlink + verify tasks run identically on both paths. `spur_binary_src` reads only the named files (`spur`/`spurctld`/`spurd`/`spurstepd`) out of the directory, never globs it, so an extra `spurdbd` sitting alongside them is ignored.
- **`spurstepd` is resolved beside `spurd`, never via `$PATH`.** `spurd` looks for it next to its own executable (`current_exe().parent()`); the fallback is a bare name that only `execv` can resolve, i.e. CWD-relative. So installing it anywhere but `spur_install_dir` fails every job launch on that node. It is copied in the same task as `spurd`, deliberately — a supervisor written by one build is not adopted by another, so the pair must never roll independently.
- **`spurstepd` can't be a hard requirement.** Every release before it existed ships three binaries; requiring four would break those deploys. `spur_install` probes the install source instead, installs it when present, and warns loudly when the source has `spurd` but no `spurstepd` (the usual cause of the latter on a local build is a `cargo build` missing `-p spur-stepd`). `spur_require_stepd=true` turns that into a failure.
- **`KillMode=process` in `spurd.service` is load-bearing, not tidying.** Supervisors detach into their own session and reparent to init, but they stay in the unit's cgroup, so systemd's default `control-group` kill mode signals them on every stop/restart and the "jobs survive an agent restart" property silently disappears.
- **Agent state dir goes to `spur_agent_state_dir`, via `SPUR_STEPD_STATE_DIR`, not `--state-dir`.** A build predating `spurstepd` has no such flag and refuses to start on an unknown one, but ignores an unknown environment variable — so the env var keeps one unit template valid across both. It must not be `{{ spur_home }}/state`: that is the controller's Raft directory, and a host running both daemons would have them share it.
- **`systemctl stop spurd` does not stop the supervisors** — that is the whole point of them. Anything that means "this host is done" has to reap them explicitly; `playbooks/tasks/reap_stepd_supervisors.yml` is the one implementation, used by `teardown.yml` and `remove_nodes.yml`. It runs *after* the workload sweep and *after* the daemons are stopped: a supervisor whose workload just died reports the exit, so killing it first strands the job at the controller instead of completing it. A supervisor also ignores a bare `SIGTERM` by design, so the `SIGKILL` is what actually lands.

## Accounting
- **Accounting is embedded in `spurctld`** — served on the controller's own gRPC port whenever `[accounting].database_url` is non-empty, so Postgres must be reachable and migrated *before* spurctld starts. `spur_accounting` runs before the controller role for this reason.
- **Every controller needs network access to Postgres, not just localhost** — each spurctld connects to the database directly (no single co-located daemon brokering it anymore), so `spur_accounting` configures `listen_addresses`/`pg_hba.conf` for every controller's IP.
- **`pg_hba.conf` needs a real IP, not a hostname** — the address field is `IP/CIDR`, so the role resolves each controller's `ansible_default_ipv4.address` rather than reusing `ansible_host` (which may be a DNS name). Passing a hostname produces an unparseable line that takes Postgres down on the next restart.
- **Migrations run automatically on spurctld startup** against the configured `database_url`; a failed migration disables accounting for that controller rather than crashing it (scheduling still works, `sacct` won't).
- **Upgrading a pre-merge cluster leaves a stale `spurdbd`** unit/binary/process behind if nothing removes it. `spur_accounting` stops/disables/deletes the unit *and* binary unconditionally (even with `spur_accounting_enabled=false`) on the accounting host; `spur_install` (which runs on *every* controller/agent) separately sweeps a stray `spurdbd` binary fleet-wide, since the old `spur_binary_src` push copied it everywhere even though it only ever ran as a service on the accounting host.
- **Extra-vars are strings — gate booleans with `| bool`.** `-e spur_accounting_enabled=false` sets the *string* `"false"`, which is truthy in a bare Jinja `{% if %}`. Every boolean check on such a var (templates and `when:` alike) must pass through `| bool`.

## Multi-node / HA
- **Agent `--hostname` must match the `[[nodes]]` name in `spur.conf`.** Both sides use `ansible_hostname` (short).
- **HA needs a leader-elected wait**, not just a port-listening one. `spurctld` binds `:6817` instantly but returns `Status::unavailable("no leader elected yet")` until quorum forms; the controller role loops on that message in `spur nodes` until it clears.
- **`peers` list order matters across controllers.** `node_id` is the 1-based position; openraft refuses to start if a node's `node_id` doesn't match its position. Both are derived from `groups['spur_controllers']` — don't reorder that group between deploys without wiping state.
- **`SPUR_CONTROLLER_ADDR` / `spurd --controller` take a comma-separated endpoint list** and rotate past dead ones, so the roles point every agent (and each controller's own env) at *all* controllers for automatic client-side failover. There's no separate accounting env var — it rides `SPUR_CONTROLLER_ADDR`.
- **`delegate_facts: true` evaluates `set_fact` on the controller, not the delegated target.** Use `hostvars[item].ansible_hostname` when setting a per-host fact via `delegate_to`.
- **`ansible.builtin.command` runs without a shell** — `command -v X` fails (bash builtin). Use `ansible.builtin.shell` with `executable: /bin/bash`.

## Rolling upgrade (`rolling_upgrade.yml`)
- **`spur node drain` doesn't kill running jobs** — it stops new scheduling and transitions the node `DRAINING → DRAINED` once its running jobs finish on their own. The playbook polls for `DRAINED` before touching the agent's binary or restarting `spurd`.
- **`spurd` deregisters gracefully on SIGTERM**, so `systemctl restart spurd` doesn't leave a stale node entry — but only after the node is drained, or its running jobs get evicted (`NODE_FAIL`).
- **A drained node stays drained after `spurd` restarts** — draining is server-side state, not tied to the agent process. The playbook explicitly `RESUME`s each node after its upgrade; skip that and upgraded capacity sits idle.
- **Controllers restart one at a time (`serial: 1`), never in parallel**, so Raft quorum is never lost — with 3 controllers, restarting 1 leaves 2 for quorum.
- **Refuses to run under `spur_transport=wireguard`** — it doesn't re-derive `spur_wg_address` the way `deploy.yml` does. Use `deploy.yml` for a wireguard cluster instead.
- **`spurstepd` installs unconditionally, like any other binary** — no refusal, but the agent play prints an informational note the first time it lands on a node, since the controllers ahead of it in the batch are already upgraded.
