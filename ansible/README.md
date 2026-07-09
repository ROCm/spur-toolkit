# Spur — Ansible Deployment

Three playbooks: `deploy.yml` stands up a Spur cluster in every supported shape from inventory; `rolling_upgrade.yml` upgrades an already-running cluster's binaries with no full-cluster outage; `teardown.yml` stops it. Daemons run as **systemd services**, Slurm-compatible CLI names are symlinked, and optional **PostgreSQL accounting** (embedded in `spurctld`) is on by default.

## Quick start

Run from this repo's `ansible/` directory. `spur` (the upstream source) is a separate repo — clone and build it wherever's convenient, then point `spur_binary_src` straight at the build output (no need to copy it anywhere; the role only ever reads the three named files `spur`/`spurctld`/`spurd` out of that directory).

```bash
# 1. Build spur (only needed until ROCm/spur publishes releases — see Build prerequisites)
git clone https://github.com/ROCm/spur.git && cd spur
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && source "$HOME/.cargo/env"
sudo apt install -y protobuf-compiler build-essential
cargo build --release -p spur-cli -p spurctld -p spurd
SPUR_BUILD="$(pwd)/target/release"
cd -

# 2. Ansible + inventory
python3 -m pip install --user 'ansible-core>=2.14'
cp inventory/hosts.example.ini inventory/hosts.ini
$EDITOR inventory/hosts.ini

# 3. Deploy
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src="$SPUR_BUILD"

# Without PostgreSQL accounting (jobs still run, sacct/fairshare unavailable):
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src="$SPUR_BUILD" -e spur_accounting_enabled=false
```

The run ends by submitting a test job — check the `spur nodes` output and job stdout near the end. Real inventories are git-ignored (see `.gitignore`); only `inventory/*.example.ini` templates are tracked.

| Shape | Inventory pattern | Transport |
|---|---|---|
| Single-node | one host in **both** `spur_controllers` and `spur_agents` | local loopback |
| Multi-node — direct LAN | one host in `spur_controllers`, all compute in `spur_agents` | LAN IP, unencrypted |
| Multi-node — WireGuard mesh | as above + `spur_transport=wireguard` (single controller only) | encrypted mesh on `spur0` |
| HA — multi-controller Raft | **≥ 3 hosts** in `spur_controllers` (any number in `spur_agents`); auto-enabled | direct or wireguard |
| HA — separate compute | `spur_controllers` and `spur_agents` are **disjoint** host sets | direct or wireguard |

`deploy.yml` is idempotent — re-running it on a healthy cluster re-applies config and restarts daemons.

**Details below:** [Build prerequisites](#build-prerequisites) · [Ansible control node](#ansible-control-node) · [Example commands](#example-commands-per-scenario) · [Inventory examples](#inventory-examples) · [Accounting](#accounting-postgresql-embedded-in-spurctld) · [Variables](#variables-defaults-in-inventorygroup_varsallyml) · [Upgrading](#upgrading) · [Managing the cluster](#managing-the-cluster-after-deploy) · [Tear down](#tear-down) · [Gotchas](#hard-won-gotchas-baked-into-these-roles)

---

## Build prerequisites

No published GitHub release yet, so upstream `install.sh` returns 403 — build locally, on a machine matching your targets' architecture/libc (the lab targets are Ubuntu 22.04 x86-64). rustup auto-selects the toolchain version pinned in `rust-toolchain.toml` the first time you run `cargo` in the repo, so there's no manual version matching.

> Already have Rust from somewhere other than rustup (distro package, asdf, …)? Compare `rustc --version` against `rust-toolchain.toml`'s `channel` — a mismatch can fail the build in ways that don't look version-related.

This produces, under `target/release/`:

| Binary | Role |
|---|---|
| `spur` | multi-call CLI, symlinked to 18 Slurm-compatible names (`sbatch`, `squeue`, `sacct`, … — full list in [Gotchas](#hard-won-gotchas-baked-into-these-roles)) |
| `spurctld` | controller / scheduler / Raft — also serves accounting in-process on the same gRPC port when `[accounting].database_url` is set |
| `spurd` | node agent |

> A pre-merge build (before upstream folded `spurdbd` into `spurctld`) also produces a `spurdbd` binary. Harmless if it's sitting in the same directory as the other three — the role only ever reads `spur`/`spurctld`/`spurd` by exact name.

Omit `spur_binary_src` and the playbook falls back to upstream `install.sh` — only works once ROCm/spur publishes releases.

## Ansible control node

The control node is wherever you run `ansible-playbook` — your workstation is fine, it doesn't need to join the cluster. WireGuard transport additionally needs the `ansible.utils` collection and `netaddr`:

```bash
ansible-galaxy collection install -r requirements.yml
python3 -m pip install --user netaddr
```

Target hosts need: SSH reachable, sudo or root, `systemd`, and (only for the `install.sh` fallback) `curl` + `tar`.

- Every play runs with `become: true`, so a non-root `ansible_user` with passwordless sudo (or `ansible_become_password` supplied) works the same as `ansible_user=root`. This is required — `spur_home`/`spur_install_dir` default under `/root`, unwritable by a non-root user without becoming root.
- Password-based SSH works too: set `ansible_user`/`ansible_password` (via `sshpass`) in `[all:vars]` or per-host. `ansible.cfg` deliberately omits `-o BatchMode=yes` — that flag suppresses SSH's password prompt outright and silently breaks password auth even with `sshpass` supplying the answer.

---

## Example commands per scenario

Base pattern: `ansible-playbook playbooks/deploy.yml -i <inventory> -e spur_binary_src=<path> [extra flags]`.

| Scenario | Inventory | Extra flags |
|---|---|---|
| Single-node | `inventory/single.ini` | *(none)* |
| Multi-node, direct LAN | `inventory/multi.ini` | *(none)* |
| HA, hyperconverged | `inventory/ha.ini` | *(none)* |
| HA, separate compute | `inventory/ha-separate.ini` | *(none)* |
| No accounting (jobs still run, `sacct` unavailable) | `inventory/multi.ini` | `-e spur_accounting_enabled=false` |
| WireGuard transport | `inventory/multi.ini` | `-e spur_transport=wireguard` |
| Preserve Raft state on re-deploy (production default) | `inventory/ha.ini` | `-e spur_wipe_state=false` |
| One host only (e.g. re-push config to a single agent) | `inventory/multi.ini` | `--limit gpu-1` |
| Dry-run without applying | `inventory/multi.ini` | `--check --diff` |

---

## Inventory examples

### Single-node

```ini
[spur_controllers]
node1 ansible_host=10.0.0.10 ansible_user=root

[spur_agents]
node1 ansible_host=10.0.0.10 ansible_user=root
```

### Multi-node, direct LAN

```ini
[spur_controllers]
ctl ansible_host=10.0.0.10 ansible_user=root

[spur_agents]
ctl   ansible_host=10.0.0.10 ansible_user=root   ; controller also runs an agent (hyperconverged)
gpu-1 ansible_host=10.0.0.11 ansible_user=root
gpu-2 ansible_host=10.0.0.12 ansible_user=root

[all:vars]
spur_transport=direct
```

### Multi-node, WireGuard mesh

```ini
[spur_controllers]
ctl ansible_host=ctl.example.com ansible_user=root

[spur_agents]
gpu-1 ansible_host=gpu1.example.com ansible_user=root
gpu-2 ansible_host=gpu2.example.com ansible_user=root

[all:vars]
spur_transport=wireguard
spur_wg_cidr=10.44.0.0/16
spur_wg_port=51820
```

> WireGuard is **single-controller only** — `spur net init` auto-assigns the controller `.1` and there's no multi-controller mesh command, so HA needs `direct` instead. Agents auto-assign `.2`, `.3`, … by inventory position; override any host with `spur_wg_address=…`. Requires the `ansible.utils` collection on the control node.

### HA — multi-controller Raft (hyperconverged)

```ini
[spur_controllers]
ctl-0 ansible_host=10.0.0.10 ansible_user=root
ctl-1 ansible_host=10.0.0.11 ansible_user=root
ctl-2 ansible_host=10.0.0.12 ansible_user=root   ; 3 → tolerates 1 failure

[spur_agents]
ctl-0 ansible_host=10.0.0.10 ansible_user=root   ; controllers also run agents
ctl-1 ansible_host=10.0.0.11 ansible_user=root
ctl-2 ansible_host=10.0.0.12 ansible_user=root
```

### HA — separate compute (control plane ≠ compute plane)

```ini
[spur_controllers]
ctl-0 ansible_host=10.0.0.10 ansible_user=root
ctl-1 ansible_host=10.0.0.11 ansible_user=root
ctl-2 ansible_host=10.0.0.12 ansible_user=root

[spur_agents]
gpu-1 ansible_host=10.0.0.21 ansible_user=root   ; dedicated compute — no spurctld here
gpu-2 ansible_host=10.0.0.22 ansible_user=root
```

**In HA mode, the playbook:**
- Writes the same `peers = [...]` list to every controller's `spur.conf` (order matters — don't reorder controllers between deploys without wiping state).
- Assigns `node_id` = the controller's 1-based position in `groups['spur_controllers']`.
- Waits for a leader to be elected before proceeding to agent registration.
- Lets non-leader controllers forward client RPCs to the leader internally, so clients can talk to any controller.

**Always use an odd `N` ≥ 3 in production** — even `N` gives the same fault tolerance as `N-1` and is strictly worse; `N=2` has zero fault tolerance (code-path testing only).

**Client-side failover is automatic.** `spurd --controller` and the CLI's `SPUR_CONTROLLER_ADDR` both accept a comma-separated endpoint list and rotate past a dead one, so the playbook points every agent — and each controller's own `/etc/environment` — at *every* controller, not just the first. A single surviving controller is enough; server-side leader forwarding handles writes from any endpoint in the list. No VIP/DNS round-robin needed for basic failover.

A full HA inventory template lives at `inventory/hosts.ha.example.ini`.

---

## Accounting (PostgreSQL, embedded in spurctld)

Enabled by default (`spur_accounting_enabled: true`). There's no separate accounting daemon — upstream folded the standalone `spurdbd` into `spurctld`, which serves the `SlurmAccounting` gRPC service in-process on its own controller port (6817) whenever `[accounting].database_url` is set. Only Postgres itself is a distinct service, running on **`spur_accounting_host`** (default: the first controller). Before the controllers start, the `spur_accounting` role:

1. Installs `postgresql` + `postgresql-contrib`.
2. Creates the `spur` role and `spur` database idempotently.
3. Configures Postgres to accept remote TCP connections (`listen_addresses`, `pg_hba.conf`) from every controller's IP — each controller's embedded accounting service connects to Postgres directly over the network, not just the one that happens to be co-located with it.
4. Writes every controller's `spur.conf` with an `[accounting]` block pointing `database_url` at that host.

**Dedicated accounting node:** set `spur_accounting_host` to any managed host — controller, agent, or a standalone host in its own group (e.g. `[spur_accounting_node]`). Pass it via `-e` (a play's `hosts:` field doesn't reliably read inventory `[all:vars]`):

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_accounting_host=acct-0
```

The playbook writes `SPUR_CONTROLLER_ADDR` (listing every controller) to `/etc/environment` on **controller nodes**, so the whole CLI — `squeue`/`sinfo`/`scontrol` and `sacct`/`sacctmgr`/`sreport`/`sshare` alike — works there with no per-command flags; accounting rides the same address, there's no separate env for it anymore. On non-controller nodes, or to override: `export SPUR_CONTROLLER_ADDR=http://<a-controller>:6817[,http://<another>:6817,...]`.

Job submission still works without accounting (`-e spur_accounting_enabled=false`, shown in [Quick start](#quick-start)) — only `sacct`/fairshare are unavailable.

> Default DB credentials are `spur`/`spur`/`spur` — fine for a lab, **change `spur_accounting_db_password` for anything real.**

**Upgrading a pre-merge deployment:** if a cluster is still running the old, separate `spurdbd` (`systemctl is-active spurdbd`, or an `[accounting] host = ...` line in `spur.conf`), just re-run `deploy.yml` pointed at a merged-architecture build. `spur_accounting` stops/disables/removes any leftover `spurdbd` unit and binary, reconfigures Postgres for remote access, and the regenerated `spur.conf` drops the old `host` key — no manual cleanup needed. This is a full-cluster convergence, not a rolling upgrade (see [Upgrading](#upgrading)) — expect a brief restart of every daemon.

---

## Variables (defaults in `inventory/group_vars/all.yml`)

| Variable | Default | What it does |
|---|---|---|
| `spur_cluster_name` | `spur-cluster` | `cluster_name` in `spur.conf` |
| `spur_binary_src` | *(unset)* | Local dir of pre-built binaries to push. Unset → use upstream `install.sh`. |
| `spur_version` | `latest` | Install channel for `install.sh`: `latest` / `nightly` / `vX.Y.Z` |
| `spur_install_dir` | `/root/.local/bin` | Where binaries + Slurm symlinks land (added to `/etc/environment`) |
| `spur_home` | `/root/spur` | Per-host state/log/etc root |
| `spur_transport` | `direct` | `direct` or `wireguard` |
| `spur_accounting_enabled` | `true` | Deploy PostgreSQL accounting (embedded in spurctld). `false` to skip. |
| `spur_accounting_host` | first controller | Host that runs Postgres (any managed host). Pass via `-e`. |
| `spur_accounting_db_name` / `_user` / `_password` | `spur` / `spur` / `spur` | Accounting DB credentials |
| `spur_accounting_db_port` | `5432` | Postgres listen port |
| `spur_wg_cidr` / `spur_wg_port` / `spur_wg_interface` | `10.44.0.0/16` / `51820` / `spur0` | WireGuard mesh settings |
| `spur_controller_port` / `spur_agent_port` / `spur_raft_port` | `6817` / `6818` / `6821` | Listen ports |
| `spur_log_level` | `info` | Daemon log verbosity |
| `spur_wipe_state` | `false` | Wipe `~/spur/state` (Raft job queue/registrations) on (re)deploy. Non-destructive default for re-runs/upgrades; `true` for a fresh install or intentional Raft reinit. |
| `spur_force_reinstall` | `false` | Force `spur_install` to re-pull/re-copy binaries even if already present. Used by `rolling_upgrade.yml`. |
| `spur_rolling_batch_size` | `1` | Agents upgraded per batch in `rolling_upgrade.yml` (`serial`). Controllers always upgrade one at a time regardless. |

Override per-run with `-e key=value` (repeatable):

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src=/path/to/target/release -e spur_wipe_state=true
```

---

## Upgrading

Two ways to roll out newer binaries, depending on whether the cluster can tolerate a full-daemon bounce.

### Full convergence (`deploy.yml`) — simplest, brief outage

Non-destructive by default (`spur_wipe_state=false` preserves job state/registrations), but it restarts **every** daemon on **every** host in the same play — no batching. In-flight jobs are disrupted, and in HA all controllers bounce together (briefly losing a Raft leader). Use this for a topology change (e.g. migrating off `spurdbd`, adding/removing hosts) or when a short full-cluster blip is fine:

```bash
cargo build --release -p spur-cli -p spurctld -p spurd   # rebuild all three together, see caveat below
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src=/path/to/target/release
```

- **Binaries roll out by content, not version string** — Ansible compares checksums, so an unchanged re-run is a near no-op.
- **Rebuild all three together.** They share a Raft WAL schema; mixing binaries from different builds (e.g. only rebuilding `spurctld`) can leave a controller unable to parse a log a differently-versioned peer wrote.
- **HA topology changes are guarded.** Spur 0.3.0 has no online Raft membership change, so adding/removing/reordering a **controller** needs a reinit — the role fails early with an actionable message if you change the controller set without `-e spur_wipe_state=true`. **Compute agents aren't Raft members** — add/remove freely, no wipe needed.
- **Demoting a controller to agent-only leaves a stale `spurctld`.** Moving a host out of `[spur_controllers]`? `systemctl disable --now spurctld` on it first, or the leftover daemon keeps the old membership and can block quorum.

### Rolling upgrade (`rolling_upgrade.yml`) — for a live cluster with running jobs

Upgrades one host at a time so the cluster keeps scheduling and running jobs throughout:

```bash
cargo build --release -p spur-cli -p spurctld -p spurd
ansible-playbook playbooks/rolling_upgrade.yml -i inventory/hosts.ini -e spur_binary_src=/path/to/target/release
```

Reuses the same `spur_install`/`spur_controller`/`spur_agent`/`spur_verify` roles `deploy.yml` uses — no duplicated install/config/health-check logic:

1. **Guard rail** — refuses to start if `spur_wipe_state=true`, if `spur_transport=wireguard` (not yet supported by this playbook), or if the cluster isn't already healthy (leader elected, `spur nodes` reachable).
2. **Controllers, one at a time** (`serial: 1`, aborts the whole run on first failure) — force-reinstall, restart `spurctld`, reuse the existing HA-aware "wait for Raft leader" task as the health gate before the next controller. Client-side failover (every agent/CLI is pointed at *every* controller) keeps the rest serving while one is down.
3. **Agents, in configurable batches** (`spur_rolling_batch_size`, default `1`) — `spur node drain <node>`, poll until `DRAINED` (no running jobs left), force-reinstall, restart `spurd`, wait for re-registration, `scontrol update NodeName=<node> State=RESUME`.
4. **Verify** — submits a real test job at the end to confirm the upgraded cluster actually schedules work.

Same rebuild-all-three caveat applies. `spur_rolling_batch_size` trades speed for blast radius: `1` (default) disrupts at most one agent's capacity at a time; higher upgrades faster but drains more capacity concurrently. A single-controller cluster has no other controller to fail over to during its own restart, so a rolling upgrade of it is still a short outage — real zero-downtime needs HA (≥ 3 controllers).

---

## Managing the cluster after deploy

Daemons are systemd services — use the normal tools on any host:

```bash
systemctl status spurctld          # on a controller (also serves accounting when enabled)
systemctl status spurd             # on an agent
systemctl status postgresql        # on the accounting host
journalctl -u spurctld -f          # follow logs
```

Basic cluster commands (binaries are on `PATH` via `/etc/environment`):

```bash
spur nodes        # partition/node summary
spur queue        # job queue
spur submit job.sh
sacct             # accounting (when enabled) — Slurm-compatible symlink to spur
```

---

## Tear down

```bash
ansible-playbook playbooks/teardown.yml -i inventory/hosts.ini              # stop + disable daemons, remove units
ansible-playbook playbooks/teardown.yml -i inventory/hosts.ini -e wipe=true  # also rm -rf ~/spur
```

Stops/disables the systemd services and reaps stray daemons. Leaves PostgreSQL installed and the accounting database intact — drop it manually on the accounting host if needed: `sudo -u postgres dropdb spur; sudo -u postgres dropuser spur`.

---

## Hard-won gotchas baked into these roles

Real bugs hit during validation — listed so anyone reading the playbook understands why the roles look the way they do. If you hit a new one, add it here and (where applicable) encode the fix in the roles.

### Daemon / process management
- **Daemons run as systemd units**, not `nohup` — `/etc/systemd/system/spur{ctld,d}.service` (`spurdbd.service` only exists as a leftover from a pre-merge deployment, cleaned up by `spur_accounting` on the accounting host), `enabled` (survive reboot), `Restart=on-failure`. The roles `daemon-reload` after templating a unit and use `state: restarted` so a redeploy always picks up new binaries/config.
- **`-D` is `--foreground`, not "daemonize"** (`crates/spurctld/src/main.rs`). The systemd units correctly run the binary in the foreground under `Type=simple` — don't add `-D`.
- **`pkill -f spurd` also kills `spurctld`** (substring match). Teardown uses `pkill -x` (exact name) only.
- **Stop-before-wipe ordering.** The controller role stops spurctld *before* wiping `~/spur/state`, so the daemon can't rewrite the Raft log mid-delete.

### Install
- **Upstream `install.sh` returns 403** because ROCm/spur has no published release yet. Use `spur_binary_src` to push locally-built binaries — the symlink + verify tasks run on both paths.
- **`spur --version` is not a supported flag** — it errors. The roles check for the binary with `stat`, not by running `--version`.
- **All 18 Slurm-compat symlink names** the `spur` multi-call binary recognizes via argv[0] dispatch are created regardless of install source: `sbatch`, `squeue`, `sinfo`, `scancel`, `sacct`, `sacctmgr`, `scontrol`, `salloc`, `srun`, `sattach`, `scrontab`, `sdiag`, `smd`, `sprio`, `sreport`, `sshare`, `sstat`, `strigger`.
- **A stale dpkg lock** during the accounting apt install means another apt/unattended-upgrade is running — wait for it (or clear a genuinely hung `apt-get`); don't `--force`.

### Accounting
- **Accounting is embedded in `spurctld`** — served on the controller's own gRPC port whenever `[accounting].database_url` is non-empty, so Postgres must be reachable and migrated *before* spurctld starts. `spur_accounting` runs before the controller role for this reason.
- **Every controller needs network access to Postgres, not just localhost** — each spurctld connects to the database directly (no single co-located daemon brokering it anymore), so `spur_accounting` configures `listen_addresses`/`pg_hba.conf` for every controller's IP.
- **Migrations run automatically on spurctld startup** against the configured `database_url`; a failed migration disables accounting for that controller rather than crashing it (scheduling still works, `sacct` won't).
- **With `spur_wipe_state=true`, the Raft job-id counter resets each deploy**, so re-deploys reuse job ids 1, 2, … which upsert onto the same accounting rows. Set `spur_wipe_state=false` to preserve job history.
- **Upgrading a pre-merge cluster** leaves a stale `spurdbd` unit/binary/process behind if nothing removes it. `spur_accounting` stops/disables/deletes both unconditionally (even with `spur_accounting_enabled=false`) on the accounting host; `spur_install` (every controller/agent) separately sweeps a stray `spurdbd` *binary* fleet-wide, since the old `spur_binary_src` push copied it everywhere even though it only ever ran as a service on the accounting host.

### Spur quirks
- **Job stdout lands in spurd's working directory at startup** — the `spurd` unit sets `WorkingDirectory={{ spur_home }}`, so output is predictably at `{{ spur_home }}/spur-<JOBID>.out`, even though `spur show job` reports the submitter's `WorkDir=`.
- **The single-node job's host is unpredictable** — the backfill scheduler picks any idle node, so `spur_verify` searches every agent rather than assuming the controller.
- **No shared-FS assumption in multi-node verify** — the play fetches `spur-<JOBID>.out` from each agent, not just the controller.
- **`spur nodes` collapses by partition** — the "NODES" column is a count, not one row per host. Loop `spur show node <name>` to confirm each expected host registered.
- **`spur show job` uses `JobState=COMPLETED` (uppercase)**, not `State: Completed`. The wait-loop greps `JobState=[A-Z]+`.
- **Harmless log spam `invalid transition from Completed to Completed`** on followers after a multi-node job (`crates/spurctld/src/cluster.rs`) — the leader already reported terminal state, the follower's redundant report is rejected. The job succeeded.
- **Harmless `ERROR`-level openraft log line on every spurctld restart** — `Can not initialize last_log_id=Some(...) vote=...:committed`. Normal on a restart with existing Raft state; judge success from `spur nodes`/job/accounting behavior, not this line.
- **Harmless spurd startup warning `failed to load spur.conf ... path=/etc/spur/spur.conf`** — `spurd` takes all its config via CLI flags and never reads a config file; always present.
- **Raft port 6821 is hardcoded** in spurctld, not a flag in 0.3.0. Preflight checks it alongside 6817/6818.

### Multi-node / HA specifics
- **Agent `--hostname` must match the `[[nodes]]` name in `spur.conf`.** Both sides use `ansible_hostname` (short).
- **HA needs a leader-elected wait**, not just a port-listening one. `spurctld` binds `:6817` instantly but returns `Status::unavailable("no leader elected yet")` until quorum forms; the controller role loops on that message in `spur nodes` until it clears.
- **`peers` list order matters across controllers.** `node_id` is the 1-based position; openraft refuses to start if a node's `node_id` doesn't match its position. Both are derived from `groups['spur_controllers']` — don't reorder that group between deploys without wiping state.
- **`delegate_facts: true` evaluates `set_fact` on the controller, not the delegated target.** Use `hostvars[item].ansible_hostname` when setting a per-host fact via `delegate_to`.
- **`ansible.builtin.command` runs without a shell** — `command -v X` fails (bash builtin). Use `ansible.builtin.shell` with `executable: /bin/bash`.

### Rolling upgrade specifics
- **`spur node drain` doesn't kill running jobs** — it stops new scheduling and transitions the node `DRAINING → DRAINED` once its running jobs finish on their own. `rolling_upgrade.yml` polls for `DRAINED` before touching the agent's binary or restarting `spurd`.
- **`spurd` deregisters gracefully on SIGTERM**, so `systemctl restart spurd` doesn't leave a stale node entry — but only do this after the node is actually drained, or its running jobs get evicted (`NODE_FAIL`).
- **A drained node stays drained after `spurd` restarts** — draining is server-side state, not tied to the agent process. `rolling_upgrade.yml` explicitly `RESUME`s each node after its upgrade; skip that and upgraded capacity sits idle.
- **Controllers restart one at a time (`serial: 1`), never in parallel**, so Raft quorum is never lost — with 3 controllers, restarting 1 still leaves 2 for quorum.
- **`rolling_upgrade.yml` refuses to run under `spur_transport=wireguard`** — it doesn't re-derive `spur_wg_address` the way `deploy.yml` does. Use `deploy.yml` for a wireguard cluster instead.
