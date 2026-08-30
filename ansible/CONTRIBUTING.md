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
- **`install.sh` and `spur_binary_src` are two interchangeable install sources** — the symlink + verify tasks run identically on both paths. `spur_binary_src` reads only the three named files (`spur`/`spurctld`/`spurd`) out of the directory, never globs it, so an extra `spurdbd` sitting alongside them is ignored.

## Accounting
- **Accounting is embedded in `spurctld`** — served on the controller's own gRPC port whenever `[accounting].database_url` is non-empty, so Postgres must be reachable and migrated *before* spurctld starts. `spur_accounting` runs before the controller role for this reason.
- **Every controller needs network access to Postgres, not just localhost** — each spurctld connects to the database directly (no single co-located daemon brokering it anymore), so `spur_accounting` configures `listen_addresses`/`pg_hba.conf` for every controller's IP.
- **`pg_hba.conf` needs a real IP, not a hostname** — the address field is `IP/CIDR`, so the role resolves each controller's `ansible_default_ipv4.address` rather than reusing `ansible_host` (which may be a DNS name). Passing a hostname produces an unparseable line that takes Postgres down on the next restart.
- **Migrations run automatically on spurctld startup** against the configured `database_url`; a failed migration disables accounting for that controller rather than crashing it (scheduling still works, `sacct` won't).
- **Upgrading a pre-merge cluster leaves a stale `spurdbd`** unit/binary/process behind if nothing removes it. `spur_accounting` stops/disables/deletes the unit *and* binary unconditionally (even with `spur_accounting_enabled=false`) on the accounting host; `spur_install` (which runs on *every* controller/agent) separately sweeps a stray `spurdbd` binary fleet-wide, since the old `spur_binary_src` push copied it everywhere even though it only ever ran as a service on the accounting host.
- **Extra-vars are strings — gate booleans with `| bool`.** `-e spur_accounting_enabled=false` sets the *string* `"false"`, which is truthy in a bare Jinja `{% if %}`. Every boolean check on such a var (templates and `when:` alike) must pass through `| bool`.

## Multi-node / HA
- **Agent `--hostname` must match the `[[nodes]]` name in `spur.conf`.** `spur_node_name` comes straight from that `[[nodes]]` entry's `names` field (set by the `spur_conf` inventory plugin as a hostvar) — not derived from `ansible_hostname` anymore, so it's stable regardless of the box's actual OS hostname.
- **HA needs a leader-elected wait**, not just a port-listening one. `spurctld` binds `:6817` instantly but returns `Status::unavailable("no leader elected yet")` until quorum forms; the controller role loops on that message in `spur nodes` until it clears.
- **`peers` list order matters across controllers.** `node_id` is the 1-based position; openraft refuses to start if a node's `node_id` doesn't match its position. Both are auto-filled (only when the master `spur.conf` leaves them absent) from `[controller].hosts`'s order — don't reorder that list between deploys without wiping state.
- **`SPUR_CONTROLLER_ADDR` / `spurd --controller` take a comma-separated endpoint list** and rotate past dead ones, so the roles point every agent (and each controller's own env) at *all* controllers for automatic client-side failover. There's no separate accounting env var — it rides `SPUR_CONTROLLER_ADDR`.
- **`ansible.builtin.command` runs without a shell** — `command -v X` fails (bash builtin). Use `ansible.builtin.shell` with `executable: /bin/bash`.

## spur.conf as inventory
- **The master `spur.conf` (the `-i` argument) is the single source of truth** — `spur_controller`/`spur_agent` push its content (read via the `inventory_file` magic var) to each node, they never render it from Ansible facts/vars. Controller addresses, wg addresses, and node names are all plain hostvars the `spur_conf` inventory plugin sets upfront — but a pure agent's push content is identical across the whole fleet (same master file, same stripped `[accounting]`), so `spur_agent` computes it ONCE per run and shares it via `delegate_to` + `delegate_facts` to a controller host, instead of re-parsing the master file per agent (O(N²) on a large fleet otherwise).
- **A controller can also be an agent.** The plugin gives every controller a synthesized name `ctl-<index>` (its position in `[controller].hosts`); a `[[nodes]]` entry whose `names` matches that exact string is treated as the SAME host in both groups (its connection info wins), not a second identity. This is how single-node and hyperconverged-HA layouts work with no special-casing in the roles.
- **`[ansible].exclude_sections` is enforced by splicing, not by skipping the write.** The push always computes the full desired file, then a section named there is overwritten back with whatever's CURRENTLY on that node before the file is written — so a first-ever push (nothing on disk yet) still gets every section, and only a genuinely pre-existing excluded section survives.
- **A pure agent's `[accounting]` drop is separate from `exclude_sections` and always wins.** `spur_conf_for_push`'s `drop_sections` param removes `[accounting]` unconditionally before a non-controller agent's content is computed — it's not something an operator can opt back into via `exclude_sections`, since that would just let a stale on-disk copy (e.g. from before this existed) splice back in.
- **Any new field on `[[nodes]]`/`[[ansible_controllers]]`/top-level tables meant for ansible's own use must be prefixed `ansible_`** (or added as a whole `ansible_*`-prefixed table) — `spur_conf_for_push` strips exactly that pattern before anything reaches a node. An unprefixed addition would leak into the real file and could break spurctld/spurd's TOML parsing.

## Rolling upgrade (`rolling_upgrade.yml`)
- **`spur node drain` doesn't kill running jobs** — it stops new scheduling and transitions the node `DRAINING → DRAINED` once its running jobs finish on their own. The playbook polls for `DRAINED` before touching the agent's binary or restarting `spurd`.
- **`spurd` deregisters gracefully on SIGTERM**, so `systemctl restart spurd` doesn't leave a stale node entry — but only after the node is drained, or its running jobs get evicted (`NODE_FAIL`).
- **A drained node stays drained after `spurd` restarts** — draining is server-side state, not tied to the agent process. The playbook explicitly `RESUME`s each node after its upgrade; skip that and upgraded capacity sits idle.
- **Controllers restart one at a time (`serial: 1`), never in parallel**, so Raft quorum is never lost — with 3 controllers, restarting 1 leaves 2 for quorum.
- **Refuses to run under `spur_transport=wireguard`** — not because `spur_wg_address` is unavailable (it's a plain hostvar now, always known), but because this playbook never runs the `spur_wireguard` role, so a rotated key or changed peer set is never reconciled mid-upgrade. Use `deploy.yml` for a wireguard cluster instead.
