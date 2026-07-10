# Spur — Ansible Deployment

Lifecycle playbooks: `deploy.yml` stands up a Spur cluster in every supported shape from inventory; `rolling_upgrade.yml` upgrades an already-running cluster's binaries with no full-cluster outage; `teardown.yml` stops it. Plus day-2 [admin operations](#admin-operations) — `manage_accounts.yml`, `add_nodes.yml`, `remove_nodes.yml`, `healthcheck.yml`. Daemons run as **systemd services**, Slurm-compatible CLI names are symlinked, and optional **PostgreSQL accounting** (embedded in `spurctld`) is on by default.

## Quick start

Run from this repo's `ansible/` directory. `spur` (the upstream source) is a separate repo — clone and build it wherever's convenient, then point `spur_binary_src` straight at the build output (no need to copy it anywhere; the role only ever reads the three named files `spur`/`spurctld`/`spurd` out of that directory).

```bash
# 1. Build spur (see Build prerequisites for why — TL;DR: picks up mainline
#    changes not yet in a tagged release; skip this and see Build prerequisites
#    if a published release already covers what you need)
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

# PostgreSQL on a DEDICATED accounting node (not a controller/agent): put that
# host in its own [spur_accounting_node] group in the inventory, and name it with
# -e (a play's hosts: field doesn't reliably read inventory [all:vars]). It runs
# only Postgres — no spur daemons — and every controller connects to it remotely.
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src="$SPUR_BUILD" -e spur_accounting_host=acct-0
```

By default (no `spur_accounting_host` set) PostgreSQL runs on the **first controller** — fine for most clusters. See [Accounting](#accounting-postgresql-embedded-in-spurctld) for the dedicated-node inventory layout and details.

The run ends by submitting a test job — check the `spur nodes` output and job stdout near the end. Real inventories are git-ignored (see `.gitignore`); only `inventory/*.example.ini` templates are tracked.

| Shape | Inventory pattern | Transport |
|---|---|---|
| Single-node | one host in **both** `spur_controllers` and `spur_agents` | local loopback |
| Multi-node — direct LAN | one host in `spur_controllers`, all compute in `spur_agents` | LAN IP, unencrypted |
| Multi-node — WireGuard mesh | as above + `spur_transport=wireguard` (single controller only) | encrypted mesh on `spur0` |
| HA — multi-controller Raft | **≥ 3 hosts** in `spur_controllers` (any number in `spur_agents`); auto-enabled | direct or wireguard |
| HA — separate compute | `spur_controllers` and `spur_agents` are **disjoint** host sets | direct or wireguard |

`deploy.yml` is idempotent — re-running it on a healthy cluster re-applies config and restarts daemons.

**Details below:** [Build prerequisites](#build-prerequisites) · [Ansible control node](#ansible-control-node) · [Example commands](#example-commands-per-scenario) · [Inventory examples](#inventory-examples) · [Login nodes](#login-submission-nodes) · [Accounting](#accounting-postgresql-embedded-in-spurctld) · [Variables](#variables-defaults-in-inventorygroup_varsallyml) · [Upgrading](#upgrading) · [Managing the cluster](#managing-the-cluster-after-deploy) · [Admin operations](#admin-operations) · [Tear down](#tear-down) · [Troubleshooting](#troubleshooting)

---

## Build prerequisites

ROCm/spur now publishes releases — check **https://github.com/ROCm/spur/releases** for the latest tag. If one already covers what you need, you can skip building entirely: omit `spur_binary_src` and the playbook falls back to upstream `install.sh`, which downloads it for you (`spur_version: latest` by default; set `nightly` for a build off the mainline branch, or a specific `vX.Y.Z`).

Building locally (the Quick start path above) is still worth it when you need mainline changes not yet in a tagged release, an air-gapped install, or a custom patch — build on a machine matching your targets' architecture/libc (the lab targets are Ubuntu 22.04 x86-64). rustup auto-selects the toolchain version pinned in `rust-toolchain.toml` the first time you run `cargo` in the repo, so there's no manual version matching.

> Already have Rust from somewhere other than rustup (distro package, asdf, …)? Compare `rustc --version` against `rust-toolchain.toml`'s `channel` — a mismatch can fail the build in ways that don't look version-related.

This produces, under `target/release/`:

| Binary | Role |
|---|---|
| `spur` | multi-call CLI, symlinked to 18 Slurm-compatible names (`sbatch`, `squeue`, `sacct`, `sacctmgr`, `scontrol`, `salloc`, `srun`, `sinfo`, `scancel`, `sattach`, `scrontab`, `sdiag`, `smd`, `sprio`, `sreport`, `sshare`, `sstat`, `strigger`) |
| `spurctld` | controller / scheduler / Raft — also serves accounting in-process on the same gRPC port when `[accounting].database_url` is set |
| `spurd` | node agent |

> A pre-merge build (before upstream folded `spurdbd` into `spurctld`) also produces a `spurdbd` binary. Harmless if it's sitting in the same directory as the other three — the role only ever reads `spur`/`spurctld`/`spurd` by exact name.

Omit `spur_binary_src` and the playbook falls back to downloading a published release via upstream `install.sh` instead (see [Build prerequisites](#build-prerequisites)).

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

## Login (submission) nodes

**Optional, off by default.** A login node is a host users SSH into to submit and query jobs — it runs the `spur` CLI but **no daemon** (no `spurctld`, no `spurd`). Enable it purely by adding a `[spur_login]` group to your inventory; omit the group and nothing happens.

```ini
[spur_controllers]
ctl ansible_host=10.0.0.10 ansible_user=root

[spur_agents]
gpu-1 ansible_host=10.0.0.11 ansible_user=root
gpu-2 ansible_host=10.0.0.12 ansible_user=root

[spur_login]
login1 ansible_host=10.0.0.20 ansible_user=root   ; users SSH here to run sbatch/squeue/sacct/srun
```

On each login node the playbook installs the `spur` CLI + all 18 Slurm symlinks and writes `SPUR_CONTROLLER_ADDR` (every controller, comma-joined) to `/etc/environment` — nothing else. Users can then SSH in and run `sbatch`/`squeue`/`sacct`/`srun`/etc. with no per-command flags. A host already in `[spur_controllers]` or `[spur_agents]` doesn't need to be listed here (it's already a client); `[spur_login]` is for **dedicated** submission hosts.

**Networking:** a login node needs outbound access to the controllers on `6817` (all CLI + accounting) and, for interactive `srun` live output, to the agents on `6818` (`srun` streams job output directly from the agent). Batch `sbatch` needs only the controller. Under `spur_transport=wireguard`, login nodes are automatically joined to the mesh so this works over the tunnel.

---

## Accounting (PostgreSQL, embedded in spurctld)

Enabled by default (`spur_accounting_enabled: true`). There's no separate accounting daemon — upstream folded the standalone `spurdbd` into `spurctld`, which serves the `SlurmAccounting` gRPC service in-process on its own controller port (6817) whenever `[accounting].database_url` is set. Only Postgres itself is a distinct service, running on **`spur_accounting_host`** (default: the first controller). Before the controllers start, the `spur_accounting` role:

1. Installs `postgresql` + `postgresql-contrib`.
2. Creates the `spur` role and `spur` database idempotently.
3. Configures Postgres to accept remote TCP connections (`listen_addresses`, `pg_hba.conf`) from every controller's IP — each controller's embedded accounting service connects to Postgres directly over the network, not just the one that happens to be co-located with it.
4. Writes every controller's `spur.conf` with an `[accounting]` block pointing `database_url` at that host.

**Dedicated accounting node:** set `spur_accounting_host` to any managed host — controller, agent, or a standalone host in its own group. Put that host in an `[spur_accounting_node]` group (it needs no spur binaries — only Postgres runs there):

```ini
[spur_controllers]
ctl ansible_host=10.0.0.10 ansible_user=root

[spur_agents]
gpu-1 ansible_host=10.0.0.11 ansible_user=root

[spur_accounting_node]
acct-0 ansible_host=10.0.0.20 ansible_user=root   ; Postgres only, no spur daemons
```

Then name it via `-e` (a play's `hosts:` field doesn't reliably read inventory `[all:vars]`):

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_accounting_host=acct-0
```

Every controller connects to it over the network (the role opens `listen_addresses`/`pg_hba.conf` for each controller's IP). If the host's PostgreSQL isn't on the default port, pass `-e spur_accounting_db_port=<port>`.

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

The day-2 admin playbooks take their own vars (`spur_accounts` / `spur_qos` / `spur_users` for `manage_accounts.yml`, `new_nodes` for `add_nodes.yml`, `nodes_to_remove` for `remove_nodes.yml`) — documented under [Admin operations](#admin-operations) rather than in the table above, since they're per-playbook inputs, not global cluster settings.

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

## Admin operations

Day-2 playbooks for routine cluster administration. All are safe to run against a live cluster.

### Manage accounts / users / QoS — `manage_accounts.yml`

Declaratively apply accounting entities. Runtime-only (talks to the controller's embedded accounting via `sacctmgr`) — **no restart, no disruption**, and idempotent (`sacctmgr add` upserts). Define the desired state in a group_vars file or pass with `-e`:

```yaml
# e.g. inventory/group_vars/all.yml (or a dedicated accounts.yml)
spur_qos:
  - { name: high, priority: 100, maxwall: 1440 }
spur_accounts:
  - { name: research, description: "Research group", fairshare: 100 }
  - { name: ml, parent: research, fairshare: 50 }
spur_users:
  - { name: alice, account: ml, defaultaccount: ml }
```

```bash
ansible-playbook playbooks/manage_accounts.yml -i inventory/hosts.ini
```

Removals go in `spur_qos_absent` / `spur_accounts_absent` / `spur_users_absent` (name-only; users also take `account`). The playbook applies in FK-safe order automatically — qos/accounts before users on add, users before accounts on remove (an account can't be deleted while a user association still references it). Requires accounting enabled.

### Add compute nodes — `add_nodes.yml`

Add agent(s) to a **running** cluster with **no disruption to existing daemons or jobs**. A full `deploy.yml` run also adds nodes, but it restarts *every* spurctld and spurd (see [Upgrading](#upgrading)); `add_nodes.yml` only starts `spurd` on the new hosts. It works because a new `spurd` registers itself at runtime — the node appears in `spur nodes` immediately, no controller restart needed.

First add the host(s) to `[spur_agents]` in your inventory (the source of truth), then:

```bash
ansible-playbook playbooks/add_nodes.yml -i inventory/hosts.ini -e new_nodes=gpu-3,gpu-4
```

What it does: gathers facts across the cluster (so the regenerated `spur.conf` has cpu/memory for every node), installs + starts `spurd` on the new hosts only, refreshes `spur.conf` on the controllers **without restarting them**, and verifies each new node registered. Idempotent — a node already registered is skipped (re-runs never bounce a healthy agent). Adding a **controller** is out of scope (that's a Raft membership change) — use `deploy.yml` for that; `add_nodes.yml` refuses controller names.

### Decommission compute nodes — `remove_nodes.yml`

Cleanly remove agents from a running cluster: drain → wait until `DRAINED` (running jobs finish first) → stop `spurd` on the node → `spur node remove`. No state wipe (agents aren't Raft members).

```bash
ansible-playbook playbooks/remove_nodes.yml -i inventory/hosts.ini -e nodes_to_remove=gpu-3,gpu-4
```

Names are as they appear in `spur nodes`. After it runs, **delete those hosts from `[spur_agents]` in your inventory** or a later `deploy.yml` will re-add them (the playbook prints this reminder).

### Health check — `healthcheck.yml`

Read-only cluster diagnostics — daemons active, controller reachable + leader elected, accounting/Postgres up, agent ports listening, disk/Raft-state size. Never changes anything; exits non-zero (with a per-host problem list) if anything's wrong, so it's usable as a cron/monitoring probe.

```bash
ansible-playbook playbooks/healthcheck.yml -i inventory/hosts.ini
```

> **Partitions** are not yet manageable this way — Spur has no runtime partition CLI (unlike Slurm's `scontrol create/update/delete partition`), so partition changes still mean editing the `[[partitions]]` blocks in the controller role's `spur.conf` template and re-running `deploy.yml` (a brief controller restart). Tracked upstream as a Spur feature request.

---

## Tear down

```bash
ansible-playbook playbooks/teardown.yml -i inventory/hosts.ini              # stop + disable daemons, remove units
ansible-playbook playbooks/teardown.yml -i inventory/hosts.ini -e wipe=true  # also rm -rf ~/spur
```

Stops/disables the systemd services and reaps stray daemons. Leaves PostgreSQL installed and the accounting database intact — drop it manually on the accounting host if needed: `sudo -u postgres dropdb spur; sudo -u postgres dropuser spur`.

---

## Troubleshooting

Things you might see while running or operating a cluster, and what they mean.

| Symptom | What's going on |
|---|---|
| `spurctld` logs an `ERROR` on restart — `Can not initialize last_log_id=... vote=...:committed` | Harmless — normal on any restart with existing Raft state. Judge health from `spur nodes` / jobs / `sacct`, not this line. |
| `spurd` logs `failed to load spur.conf ... /etc/spur/spur.conf` on startup | Harmless — `spurd` is configured entirely via CLI flags and never reads a config file. Always present. |
| `invalid transition from Completed to Completed` after a multi-node job | Harmless — a follower's redundant terminal-state report, rejected by the leader. The job succeeded. |
| `spur nodes` shows fewer rows than I have hosts | It collapses by partition — the NODES column is a count, not one row per host. Use `spur show node <name>` to check a specific host. |
| Playbook fails during the accounting apt install with a dpkg-lock error | Another `apt`/unattended-upgrade is running. Wait for it to finish (don't force-clear the lock unless `apt-get` is genuinely hung). |
| Can't find a job's output file | It lands in the job's working directory as `spur-<JOBID>.out`, on **whichever agent ran it** (no shared-FS assumption). Default working dir is `{{ spur_home }}`. |
| `spur --version` errors | Not a supported flag — use `spur nodes` / `spur show ...` to confirm the CLI works. |
| Job IDs restarted at 1 and old `sacct` history looks overwritten | You deployed with `spur_wipe_state=true`, which resets the Raft job-id counter. Use `spur_wipe_state=false` (the default) to preserve history. |

**Ports** the cluster listens on (open these between hosts): `6817` controller API + accounting, `6818` agents, `6821` Raft. `6821` is fixed in Spur 0.3.0.

For **why the roles are written the way they are** (implementation rationale, hard-won Ansible/Spur quirks encoded into the tasks), see [`CONTRIBUTING.md`](CONTRIBUTING.md) — that's maintainer reference, not needed to deploy.
