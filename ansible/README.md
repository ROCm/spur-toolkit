# Spur — Ansible Deployment

Playbooks for the whole cluster lifecycle:

- **`deploy.yml`** — stands up a Spur cluster in any supported shape from your inventory.
- **`rolling_upgrade.yml`** — upgrades a running cluster's binaries with no full-cluster outage.
- **`teardown.yml`** — stops the cluster.
- Day-2 [admin operations](#admin-operations) — `manage_accounts.yml`, `add_nodes.yml`, `remove_nodes.yml`, `healthcheck.yml`.

Daemons run as **systemd services**, Slurm-compatible CLI names are symlinked, and optional **PostgreSQL accounting** (embedded in `spurctld`) is on by default.

## Quick start

Run everything from this repo's `ansible/` directory.

`spur` (the upstream source) is a separate repo. Clone and build it wherever's convenient, then point `spur_binary_src` straight at the build output — no need to copy it anywhere.

> The role only ever reads the three named files `spur`/`spurctld`/`spurd` out of that directory. The MPI PMIx plugin (`spur_mpi_pmix.so`) is installed separately on agents by the `spur_agent` role (see [Build prerequisites](#build-prerequisites)).

```bash
# 1. Build spur (picks up mainline changes not yet in a tagged release; see Build prerequisites)
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

> **Dedicated accounting node:** to run PostgreSQL on its own host (not a controller or agent), just add that host to a `[spur_accounting_node]` group in your inventory — the playbook auto-detects it, so the deploy command is unchanged (no `-e` needed). That host runs only Postgres (no spur daemons), and every controller connects to it remotely. See [Accounting](#accounting-postgresql-embedded-in-spurctld) for the inventory layout.

A few things to know:

- **Where Postgres runs:** by default (no `spur_accounting_host` set) it runs on the **first controller**, which is fine for most clusters. See [Accounting](#accounting-postgresql-embedded-in-spurctld) for the dedicated-node inventory layout and details.
- **Verify with a real test job is opt-in** (`-e spur_verify_enabled=true`) — off by default so routine deploys don't add job noise; check `spur nodes` yourself, or enable it to have the playbook submit and confirm one.
- **Your real inventory is git-ignored** (see `.gitignore`) — only `inventory/*.example.ini` templates are tracked.

| Shape | Inventory pattern | Transport |
|---|---|---|
| Single-node | one host in **both** `spur_controllers` and `spur_agents` | local loopback |
| Multi-node — direct LAN | one host in `spur_controllers`, all compute in `spur_agents` | LAN IP, unencrypted |
| Multi-node — WireGuard mesh | as above + `spur_transport=wireguard` | encrypted mesh on `spur0` |
| HA — multi-controller Raft | **≥ 3 hosts** in `spur_controllers` (any number in `spur_agents`); auto-enabled | direct or wireguard |
| HA — separate compute | `spur_controllers` and `spur_agents` are **disjoint** host sets | direct or wireguard |

`deploy.yml` is idempotent — re-running it on a healthy cluster re-applies controller config and restarts daemons. Agent `spur.conf` is preserved instead of re-applied (see [below](#variables-defaults-in-inventorygroup_varsallyml)), unless you pass `-e spur_overwrite_conf=true`.

**Details below:** [Build prerequisites](#build-prerequisites) · [Ansible control node](#ansible-control-node) · [Example commands](#example-commands-per-scenario) · [Inventory examples](#inventory-examples) · [Login nodes](#login-submission-nodes) · [Accounting](#accounting-postgresql-embedded-in-spurctld) · [Variables](#variables-defaults-in-inventorygroup_varsallyml) · [Upgrading](#upgrading) · [Managing the cluster](#managing-the-cluster-after-deploy) · [Admin operations](#admin-operations) · [Tear down](#tear-down) · [Troubleshooting](#troubleshooting)

---

## Build prerequisites

You have two options: use a published release, or build the binaries yourself.

**Option A — use a published release (no build needed).** ROCm/spur now publishes releases. Check **https://github.com/ROCm/spur/releases** for the latest tag. If a release covers what you need, just omit `spur_binary_src` and the playbook runs upstream `install.sh` to download the binaries for you. By default it installs `spur_version: latest`; set `nightly` for a build off the mainline branch, or pin a specific `vX.Y.Z`.

**Option B — build locally** (the Quick start path above). Worth it when you need mainline changes not yet in a tagged release, an air-gapped install, or a custom patch. Build on a machine matching your targets' architecture/libc — the lab targets are Ubuntu 22.04 x86-64. You don't need to match the Rust version by hand: the first time you run `cargo` in the repo, rustup auto-selects the toolchain pinned in `rust-toolchain.toml`.

> Already have Rust from somewhere other than rustup (distro package, asdf, …)? Compare `rustc --version` against `rust-toolchain.toml`'s `channel`. A mismatch can fail the build in ways that don't look version-related.

A local build produces three binaries under `target/release/`:

| Binary | Role |
|---|---|
| `spur` | multi-call CLI, symlinked to 18 Slurm-compatible names (`sbatch`, `squeue`, `sacct`, `sacctmgr`, `scontrol`, `salloc`, `srun`, `sinfo`, `scancel`, `sattach`, `scrontab`, `sdiag`, `smd`, `sprio`, `sreport`, `sshare`, `sstat`, `strigger`) |
| `spurctld` | controller / scheduler / Raft — also serves accounting in-process on the same gRPC port when `[accounting].database_url` is set |
| `spurd` | node agent |

Release and nightly tarballs from [ROCm/spur](https://github.com/ROCm/spur/releases) also ship `lib/spur/spur_mpi_pmix.so`. The playbooks install it on **agents only** at `/usr/lib/spur/` (matches `spurd`'s default plugin path). For a local build, also run `cargo build --release -p spur-mpi-pmix` and stage `target/release/libspur_mpi_pmix.so` (or rename to `spur_mpi_pmix.so`) in `spur_binary_src`. PMIx jobs (`--mpi=pmix`) additionally require `libpmix` and Open MPI on agent hosts — the playbooks do not install those packages.

> A pre-merge build (before upstream folded `spurdbd` into `spurctld`) also produces a `spurdbd` binary. It's harmless sitting alongside the other three — the role only ever reads `spur`/`spurctld`/`spurd` by exact name.

**Each host only gets the binaries it needs.** Agent-only hosts (in `[spur_agents]` but not `[spur_controllers]`/`[spur_login]`) get `spurd` only — no `spur` CLI, no `spurctld`, no Slurm symlinks. Controllers get `spur` + `spurctld` + symlinks (and `spurd` too if also an agent); login nodes get `spur` + symlinks. This keeps compute nodes free of an interactive CLI and the controller daemon they never run.

Omit `spur_binary_src` and the playbook falls back to downloading a published release via upstream `install.sh` instead (see [Build prerequisites](#build-prerequisites)).

## Ansible control node

The control node is wherever you run `ansible-playbook` — your workstation is fine, and it doesn't need to join the cluster.

WireGuard transport needs two extra dependencies on the control node — the `ansible.utils` collection and `netaddr`:

```bash
ansible-galaxy collection install -r requirements.yml
python3 -m pip install --user netaddr
```

Target hosts need: SSH reachable, sudo or root, `systemd`, and — only for the `install.sh` fallback — `curl` + `tar`.

- Every play runs with `become: true`, so a non-root `ansible_user` with passwordless sudo (or `ansible_become_password` supplied) works the same as `ansible_user=root`. This is required: `spur_home`/`spur_install_dir` default under `/root`, which a non-root user can't write to without becoming root.
- Password-based SSH works too — set `ansible_user`/`ansible_password` (via `sshpass`) in `[all:vars]` or per-host. `ansible.cfg` deliberately omits `-o BatchMode=yes`. That flag suppresses SSH's password prompt outright and silently breaks password auth even when `sshpass` is supplying the answer.

---

## Example commands per scenario

Every deploy uses the same base command; pick the inventory that matches your setup and add any extra flags from the table below.

```
ansible-playbook playbooks/deploy.yml -i <inventory> -e spur_binary_src=<path> [extra flags]
```

| Scenario | Inventory | Extra flags |
|---|---|---|
| Single-node | `inventory/single.ini` | *(none)* |
| Multi-node, direct LAN | `inventory/multi.ini` | *(none)* |
| HA, hyperconverged | `inventory/ha.ini` | *(none)* |
| HA, separate compute | `inventory/ha-separate.ini` | *(none)* |
| No accounting (jobs still run, `sacct` unavailable) | `inventory/multi.ini` | `-e spur_accounting_enabled=false` |
| WireGuard transport (single- or multi-controller) | `inventory/multi.ini` or `inventory/ha.ini` | `-e spur_transport=wireguard` |
| Preserve Raft state on re-deploy (production default) | `inventory/ha.ini` | `-e spur_wipe_state=false` |
| One host only (e.g. re-push config to a single agent) | `inventory/multi.ini` | `--limit gpu-1` |
| Dry-run without applying | `inventory/multi.ini` | `--check --diff` |
| Large fleet, tolerate a few agents being down for maintenance | `inventory/multi.ini` | `-e spur_ignore_unreachable_agents=true` |

---

## Inventory examples

Pick the example that matches your cluster shape and copy it into your inventory file.

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

> WireGuard supports both single- and multi-controller (HA) clusters. The bootstrap controller (first in `spur_controllers`) runs `spur net init` and auto-assigns `.1`; every other node — secondary controllers, agents, and login nodes — joins and auto-assigns `.2`, `.3`, … by position. For HA (>1 controller) a full-mesh pass (`spur net mesh`) then peers every node with every other, including controller↔controller, so Raft elections work over the mesh. Override any host with `spur_wg_address=…`. This needs the `ansible.utils` collection on the control node. See [HA — WireGuard mesh](#ha--wireguard-mesh-multi-controller) below for the multi-controller layout.

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
gpu-1 ansible_host=10.0.0.21 ansible_user=root   ; dedicated compute — no spurctld, no spur CLI/symlinks
gpu-2 ansible_host=10.0.0.22 ansible_user=root
```

### HA — WireGuard mesh (multi-controller)

Combine multi-controller Raft with `spur_transport=wireguard`: the control plane (and Raft) rides an encrypted mesh instead of the LAN. The bootstrap controller runs `net init` (`.1`); the other controllers and all agents join, then `net mesh` peers everyone directly so Raft elections work even if the bootstrap controller is down. See the tracked templates `inventory/hosts.d2ha.example.ini` + `inventory/d2ha.vars.yml`.

```ini
[spur_controllers]
ctl-0 ansible_host=10.0.0.10 ansible_user=root spur_wg_address=10.44.0.1
ctl-1 ansible_host=10.0.0.11 ansible_user=root spur_wg_address=10.44.0.3
ctl-2 ansible_host=10.0.0.12 ansible_user=root spur_wg_address=10.44.0.4

[spur_agents]
ctl-0    ansible_host=10.0.0.10 ansible_user=root spur_wg_address=10.44.0.1   ; controllers also run agents
ctl-1    ansible_host=10.0.0.11 ansible_user=root spur_wg_address=10.44.0.3
ctl-2    ansible_host=10.0.0.12 ansible_user=root spur_wg_address=10.44.0.4
worker-1 ansible_host=10.0.0.21 ansible_user=root spur_wg_address=10.44.0.2
```

> Pass the WireGuard run-vars via `-e @inventory/d2ha.vars.yml` (extra-vars), not inventory `[all:vars]`: `group_vars/all.yml` sets `spur_transport=direct` and outranks inventory group vars, so `spur_transport=wireguard` in `[all:vars]` is silently reverted and the mesh never forms. Pinning `spur_wg_address` per host (as above) is recommended for a fixed, readable layout; leave it unset to auto-assign positionally.

**In HA mode, the playbook:**
- Writes the same `peers = [...]` list to every controller's `spur.conf`. Order matters — don't reorder controllers between deploys without wiping state.
- Assigns `node_id` = the controller's 1-based position in `groups['spur_controllers']`.
- Waits for a leader to be elected before proceeding to agent registration.
- Lets non-leader controllers forward client RPCs to the leader internally, so clients can talk to any controller.

**Always use an odd `N` ≥ 3 in production.** An even `N` gives the same fault tolerance as `N-1` and is strictly worse, and `N=2` has zero fault tolerance (code-path testing only).

**Client-side failover is automatic.** Both `spurd --controller` and the CLI's `SPUR_CONTROLLER_ADDR` accept a comma-separated endpoint list and rotate past a dead one. The playbook therefore points every agent — and each controller's own `/etc/environment` — at *every* controller, not just the first. A single surviving controller is enough, since server-side leader forwarding handles writes from any endpoint in the list. No VIP/DNS round-robin is needed for basic failover.

A full HA inventory template lives at `inventory/hosts.ha.example.ini`.

---

## Login (submission) nodes

**Optional, off by default.** A login node is a host your users SSH into to submit and query jobs. It runs the `spur` CLI but **no daemon** — no `spurctld`, no `spurd`. To set one up, just add a `[spur_login]` group to your inventory. Leave the group out and nothing happens.

```ini
[spur_controllers]
ctl ansible_host=10.0.0.10 ansible_user=root

[spur_agents]
gpu-1 ansible_host=10.0.0.11 ansible_user=root
gpu-2 ansible_host=10.0.0.12 ansible_user=root

[spur_login]
login1 ansible_host=10.0.0.20 ansible_user=root   ; users SSH here to run sbatch/squeue/sacct/srun
```

On each login node the playbook does three things: installs the `spur` CLI, adds all 18 Slurm symlinks, and writes `SPUR_CONTROLLER_ADDR` (every controller, comma-joined) to `/etc/environment`. Nothing else. Users can then SSH in and run `sbatch`/`squeue`/`sacct`/`srun`/etc. with no per-command flags.

You only need `[spur_login]` for **dedicated** submission hosts. A host already in `[spur_controllers]` or `[spur_agents]` is already a client, so there's no need to list it here.

**Networking.** A login node needs outbound access to:

- the controllers on `6817` — used for all CLI commands and accounting.
- the agents on `6818` — used for interactive `srun` live output, which streams job output directly from the agent.

Batch `sbatch` needs only the controller. Under `spur_transport=wireguard`, login nodes are automatically joined to the mesh, so all of this works over the tunnel.

---

## Accounting (PostgreSQL, embedded in spurctld)

Accounting is on by default (`spur_accounting_enabled: true`), and there's no separate accounting daemon to manage. Upstream folded the old standalone `spurdbd` into `spurctld`, which now serves the `SlurmAccounting` gRPC service in-process on its own controller port (6817) whenever `[accounting].database_url` is set. The only distinct service is Postgres itself, which runs on **`spur_accounting_host`** (default: the first controller).

Before the controllers start, the `spur_accounting` role:

1. Installs `postgresql` + `postgresql-contrib`.
2. Creates the `spur` role and `spur` database idempotently.
3. Configures Postgres to accept remote TCP connections from every controller's IP (via `listen_addresses` and `pg_hba.conf`). Each controller's embedded accounting service connects to Postgres over the network — not just the one that happens to be co-located with it.
4. Writes an `[accounting]` block into every controller's `spur.conf`, pointing `database_url` at that host.

**Dedicated accounting node.** To run Postgres on its own host — not a controller or agent — add that host to a `[spur_accounting_node]` group in your inventory. That's all it takes: the playbook auto-detects the group and runs Postgres there. The host needs no spur binaries, since only Postgres runs on it.

```ini
[spur_controllers]
ctl ansible_host=10.0.0.10 ansible_user=root

[spur_agents]
gpu-1 ansible_host=10.0.0.11 ansible_user=root

[spur_accounting_node]
acct-0 ansible_host=10.0.0.20 ansible_user=root   ; Postgres only, no spur daemons
```

Deploy exactly as usual — no accounting-specific flag needed:

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini
```

The accounting host is resolved in this order: an explicit `spur_accounting_host` → the first host in `[spur_accounting_node]` → the first controller (the co-located default). So the group alone is enough to pin a dedicated node.

**Pointing accounting at a specific host instead.** To use an existing host without adding a group — say a particular controller or agent — set `spur_accounting_host` (it takes highest precedence). Pass it with `-e`:

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_accounting_host=acct-0
```

Every controller connects to the accounting host over the network; the role opens `listen_addresses`/`pg_hba.conf` for each controller's IP. If the host's PostgreSQL isn't on the default port, pass `-e spur_accounting_db_port=<port>`.

On **controller nodes**, the playbook writes `SPUR_CONTROLLER_ADDR` (listing every controller) to `/etc/environment`. This means the whole CLI works there with no per-command flags — `squeue`/`sinfo`/`scontrol` and `sacct`/`sacctmgr`/`sreport`/`sshare` alike. Accounting rides the same address; there's no separate env var for it anymore. On non-controller nodes, or to override, set it yourself:

```bash
export SPUR_CONTROLLER_ADDR=http://<a-controller>:6817[,http://<another>:6817,...]
```

Job submission still works without accounting — pass `-e spur_accounting_enabled=false` (shown in [Quick start](#quick-start)). You'll only lose `sacct` and fairshare.

> Default DB credentials are `spur`/`spur`/`spur` — fine for a lab, but **change `spur_accounting_db_password` for anything real.**

**Upgrading a pre-merge deployment.** If a cluster is still running the old, separate `spurdbd` (check with `systemctl is-active spurdbd`, or look for an `[accounting] host = ...` line in `spur.conf`), just re-run `deploy.yml` pointed at a merged-architecture build. `spur_accounting` stops, disables, and removes any leftover `spurdbd` unit and binary, reconfigures Postgres for remote access, and the regenerated `spur.conf` drops the old `host` key — no manual cleanup needed. Note that this is a full-cluster convergence, not a rolling upgrade (see [Upgrading](#upgrading)), so expect a brief restart of every daemon.

---

## Variables (defaults in `inventory/group_vars/all.yml`)

| Variable | Default | What it does |
|---|---|---|
| `spur_cluster_name` | `spur-cluster` | Sets `cluster_name` in `spur.conf`. |
| `spur_binary_src` | *(unset)* | Local dir of pre-built binaries to push. Leave unset to fetch via upstream `install.sh`. |
| `spur_version` | `latest` | Which `install.sh` channel to pull: `latest` / `nightly` / `vX.Y.Z`. |
| `spur_install_dir` | `/root/.local/bin` | Where binaries and Slurm symlinks land (added to `/etc/environment`). |
| `spur_home` | `/root/spur` | Per-host root for state, logs, and config. |
| `spur_mpi_plugin_dir` | `/usr/lib/spur` | Where `spur_mpi_pmix.so` is installed on agents (matches `spurd` default). |
| `spur_mpi_plugin_enabled` | `true` | Install the MPI plugin on agents when a source file is available. |
| `spur_transport` | `direct` | Network transport: `direct` or `wireguard`. |
| `spur_accounting_enabled` | `true` | Deploy PostgreSQL accounting (embedded in spurctld). Set `false` to skip. |
| `spur_accounting_host` | first controller | Host that runs Postgres (any managed host). Pass via `-e`. |
| `spur_accounting_db_name` / `_user` / `_password` | `spur` / `spur` / `spur` | Accounting DB credentials. |
| `spur_accounting_db_port` | `5432` | Postgres listen port. |
| `spur_wg_cidr` / `spur_wg_port` / `spur_wg_interface` | `10.44.0.0/16` / `51820` / `spur0` | WireGuard mesh settings. |
| `spur_controller_port` / `spur_agent_port` / `spur_raft_port` | `6817` / `6818` / `6821` | Daemon listen ports. |
| `spur_log_level` | `info` | Daemon log verbosity. |
| `spur_wipe_state` | `false` | Wipe `~/spur/state` (Raft job queue/registrations) on (re)deploy. Safe default for re-runs and upgrades; set `true` for a fresh install or intentional Raft reinit. |
| `spur_force_reinstall` | `false` | Re-pull/re-copy binaries via `spur_install` even if already present. Used by `rolling_upgrade.yml`. |
| `spur_rolling_batch_size` | `1` | Agents upgraded per batch in `rolling_upgrade.yml` (`serial`). Controllers always upgrade one at a time regardless. |
| `spur_controller_upgrade_order` | inventory order | Comma-separated controller names — upgrade them in this order instead of `[spur_controllers]`'s inventory order. Must name every controller exactly once; `rolling_upgrade.yml` fails fast otherwise. |
| `spur_ignore_unreachable_agents` | `false` | Skip agents unreachable over SSH instead of aborting — `deploy.yml`, `rolling_upgrade.yml`, `healthcheck.yml`, `teardown.yml`. Never loosens controller/accounting-host reachability. |
| `spur_verify_enabled` | `false` | Submit and wait for a real test job at the end of `deploy.yml` / `rolling_upgrade.yml` (`spur_verify` role). Off by default to avoid job noise on routine runs; CI enables it as a smoke test. |
| `spur_verify_submit_user` | *(unset — submits as the play's own user)* | Submit the verify role's test job as this user instead. Needed if the cluster's `spurd` rejects uid-0 job submission (`[auth] allow_root_jobs=false`), which newer builds default to. |
| `spur_gather_subset` | `['!all', network, hardware, distribution]` | Facts subset gathered by every `gather_facts: yes` play. Restricts Ansible's default full gather (every mount/PCI device/interface) to just what the roles use. Set `-e spur_gather_subset=all` to fall back to full gathering. |
| `spur_overwrite_conf` | `false` | Overwrite an agent's existing `spur.conf` instead of leaving it alone. Controllers always re-render theirs regardless. |
| `spur_drain_wait_secs` | `120` | How long to wait for a node to reach `DRAINED`/`DOWN` before treating it as busy — used by `deploy.yml`, `teardown.yml`, `rolling_upgrade.yml`, and `remove_nodes.yml` via the shared drain guard. |
| `spur_skip_busy_agents` | `false` | Leave a still-busy node untouched and continue with the rest of the fleet, instead of the playbook's default (refuse, or force for `deploy.yml`). Shared by `deploy.yml`, `teardown.yml`, `rolling_upgrade.yml`, `remove_nodes.yml`. |
| `spur_force_teardown_busy_agents` | `false` | `teardown.yml`-specific: kill a busy node's running job and tear it down anyway. |
| `spur_force_upgrade_busy_agents` | `false` | `rolling_upgrade.yml`-specific: kill a busy node's running job and upgrade it anyway. |
| `spur_force_remove_busy_nodes` | `false` | `remove_nodes.yml`-specific: kill a busy node's running job and remove it anyway. |

Override any of these per run with `-e key=value` (repeatable):

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src=/path/to/target/release -e spur_wipe_state=true
```

The day-2 admin playbooks take their own inputs — `spur_accounts` / `spur_qos` / `spur_users` for `manage_accounts.yml`, `new_nodes` for `add_nodes.yml`, `nodes_to_remove` for `remove_nodes.yml`. Those are per-playbook inputs rather than global cluster settings, so they live under [Admin operations](#admin-operations) instead of the table above.

---

## Upgrading

Two ways to roll out newer binaries. Pick based on whether the cluster can take a full-daemon bounce or needs to keep running jobs throughout.

| Path | Use when | Downtime |
|---|---|---|
| Full convergence (`deploy.yml`) | You're changing the topology, or a short cluster-wide blip is fine | Every daemon restarts at once |
| Rolling upgrade (`rolling_upgrade.yml`) | The cluster is live with running jobs and must stay up | One host at a time |

### Full convergence (`deploy.yml`) — simplest, brief outage

This is the simplest path. It's non-destructive by default: `spur_wipe_state=false` preserves job state and registrations. The catch is that it restarts **every** daemon on **every** host in the same play, with no batching, and in HA all controllers bounce together and briefly lose the Raft leader.

Reach for it when you're making a topology change — migrating off `spurdbd`, adding or removing hosts — or when a short full-cluster blip is acceptable.

An already-registered agent with a job still running is drained first; if it's still busy afterward, its job is killed and the restart proceeds anyway — full convergence is the point of this playbook. Opt out with `-e spur_skip_busy_agents=true` to leave busy agents untouched instead and deploy the rest of the fleet:

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_skip_busy_agents=true   # leave busy agents untouched, deploy the rest
```

Agents get `spur.conf` too (the same file controllers get — `spurd` reads it best-effort for `[cluster]`/k0s and `[devices]`/GRES settings). A missing one is always written; an existing one is left alone unless you pass `-e spur_overwrite_conf=true`.

```bash
cargo build --release -p spur-cli -p spurctld -p spurd   # rebuild all three together, see caveat below
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src=/path/to/target/release
```

A few things worth knowing:

- **Binaries roll out by content, not version string.** Ansible compares checksums, so an unchanged re-run is a near no-op.
- **Rebuild all three together.** They share a Raft WAL schema. Mixing binaries from different builds — say, rebuilding only `spurctld` — can leave a controller unable to parse a log a differently-versioned peer wrote.
- **HA topology changes are guarded.** Adding, removing, or reordering a **controller** needs a reinit. Spur 0.3.0 has no online Raft membership change, so the role fails early with an actionable message if you change the controller set without `-e spur_wipe_state=true`. **Compute agents aren't Raft members** — add or remove them freely, no wipe needed.
- **Demoting a controller to agent-only leaves a stale `spurctld`.** Moving a host out of `[spur_controllers]`? Run `systemctl disable --now spurctld` on it first. Otherwise the leftover daemon keeps the old membership and can block quorum.

### Rolling upgrade (`rolling_upgrade.yml`) — for a live cluster with running jobs

This path upgrades one host at a time, so the cluster keeps scheduling and running jobs throughout.

```bash
cargo build --release -p spur-cli -p spurctld -p spurd
ansible-playbook playbooks/rolling_upgrade.yml -i inventory/hosts.ini -e spur_binary_src=/path/to/target/release
```

It reuses the same `spur_install`/`spur_controller`/`spur_agent`/`spur_verify` roles that `deploy.yml` uses, so there's no duplicated install, config, or health-check logic. Here's what it does, in order:

1. **Guard rail.** Refuses to start if `spur_wipe_state=true`, if `spur_transport=wireguard` (this playbook doesn't support it yet), or if the cluster isn't already healthy (leader elected, `spur nodes` reachable).
2. **Controllers, one at a time** (`serial: 1`; the whole run aborts on the first failure). For each: force-reinstall, restart `spurctld`, then wait on the existing HA-aware "wait for Raft leader" task as the health gate before moving to the next controller. Client-side failover keeps the rest serving while one is down, since every agent and CLI is pointed at *every* controller.
3. **Agents, in configurable batches** (`spur_rolling_batch_size`, default `1`). For each node: `spur node drain <node>`, poll until `DRAINED` (once no running jobs are left), force-reinstall, restart `spurd`, wait for re-registration, then `scontrol update NodeName=<node> State=RESUME`.
4. **Verify** *(opt-in — `-e spur_verify_enabled=true`)*. Submits a real test job at the end to confirm the upgraded cluster actually schedules work. Off by default so routine upgrades don't add job noise; CI enables it.

The same rebuild-all-three caveat from full convergence applies here too.

`spur_rolling_batch_size` trades speed for blast radius. The default `1` disrupts at most one agent's capacity at a time; a higher value upgrades faster but drains more capacity concurrently.

Controllers upgrade in inventory order by default. To upgrade them in a specific order instead — say, a known-stable Raft leader first — pass `-e spur_controller_upgrade_order=ctl-2,ctl-0,ctl-1`. It must name every controller in `[spur_controllers]` exactly once; the guard-rail play fails fast (before touching any host) if a name is missing, misspelled, or duplicated.

A still-busy node that doesn't drain within `spur_drain_wait_secs` (default `120s`) aborts the run by default. Opt out per node or force through it:

```bash
ansible-playbook playbooks/rolling_upgrade.yml -i inventory/hosts.ini -e spur_binary_src=/path -e spur_skip_busy_agents=true         # leave busy agents on the old build, upgrade the rest
ansible-playbook playbooks/rolling_upgrade.yml -i inventory/hosts.ini -e spur_binary_src=/path -e spur_force_upgrade_busy_agents=true  # kill the running job and upgrade it anyway
```

> A single-controller cluster has no other controller to fail over to during its own restart, so rolling it is still a short outage. Real zero-downtime needs HA — three or more controllers.

---

## Managing the cluster after deploy

The daemons are ordinary systemd services, so manage them with the usual tools on any host:

```bash
systemctl status spurctld          # on a controller (also serves accounting when enabled)
systemctl status spurd             # on an agent
systemctl status postgresql        # on the accounting host
journalctl -u spurctld -f          # follow logs
```

The binaries are on your `PATH` (via `/etc/environment`), so the everyday cluster commands just work:

```bash
spur nodes        # partition/node summary
spur queue        # job queue
spur submit job.sh
sacct             # accounting (when enabled) — Slurm-compatible symlink to spur
```

---

## Admin operations

These are the day-2 playbooks for routine cluster administration. All of them are safe to run against a live cluster.

### Manage accounts / users / QoS — `manage_accounts.yml`

Declaratively apply accounting entities — QoS, accounts, and users. This is a runtime-only operation: it talks to the controller's embedded accounting through `sacctmgr`, so there's **no restart and no disruption**. It's also idempotent, since `sacctmgr add` upserts. Define the desired state in a group_vars file, or pass it with `-e`:

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

To remove entities, list them under `spur_qos_absent` / `spur_accounts_absent` / `spur_users_absent` (name-only; users also take `account`). The playbook applies everything in FK-safe order automatically: qos/accounts before users when adding, and users before accounts when removing. That ordering matters because an account can't be deleted while a user association still references it. Requires accounting enabled.

### Add compute nodes — `add_nodes.yml`

Add agent(s) to a **running** cluster with **no disruption to existing daemons or jobs**. A full `deploy.yml` run also adds nodes, but it restarts every spurctld and spurd (see [Upgrading](#upgrading)); `add_nodes.yml` only starts `spurd` on the new hosts. This works because a new `spurd` registers itself at runtime — the node shows up in `spur nodes` immediately, with no controller restart.

First add the host(s) to `[spur_agents]` in your inventory (the source of truth), then run:

```bash
ansible-playbook playbooks/add_nodes.yml -i inventory/hosts.ini -e new_nodes=gpu-3,gpu-4
```

What it does: gathers facts across the cluster so the regenerated `spur.conf` has cpu/memory for every node, installs and starts `spurd` on the new hosts only, refreshes `spur.conf` on the controllers **without restarting them**, and verifies that each new node registered. It's idempotent — a node that's already registered is skipped, so re-runs never bounce a healthy agent. Adding a **controller** is out of scope, because that's a Raft membership change; use `deploy.yml` for that. `add_nodes.yml` refuses controller names.

### Decommission compute nodes — `remove_nodes.yml`

Cleanly remove agents from a running cluster. The playbook drains each node, waits until it reaches `DRAINED` so running jobs finish first, stops `spurd` on the node, then runs `spur node remove`. There's no state wipe, since agents aren't Raft members.

```bash
ansible-playbook playbooks/remove_nodes.yml -i inventory/hosts.ini -e nodes_to_remove=gpu-3,gpu-4
```

Use the names as they appear in `spur nodes`. Afterward, **delete those hosts from `[spur_agents]` in your inventory** — otherwise a later `deploy.yml` will re-add them. The playbook prints this reminder when it finishes.

A node with a job still running does not finish draining — same job-safety model as `deploy.yml`/`rolling_upgrade.yml`/`teardown.yml`. By default the run aborts before removing anything:

```bash
ansible-playbook playbooks/remove_nodes.yml -i inventory/hosts.ini -e nodes_to_remove=gpu-3,gpu-4 -e spur_skip_busy_agents=true         # remove only the nodes that drained cleanly, leave busy ones in the cluster
ansible-playbook playbooks/remove_nodes.yml -i inventory/hosts.ini -e nodes_to_remove=gpu-3,gpu-4 -e spur_force_remove_busy_nodes=true   # kill the running job and remove it anyway
```

### Health check — `healthcheck.yml`

Read-only cluster diagnostics. It checks that daemons are active, the controller is reachable with a leader elected, accounting/Postgres is up, agent ports are listening, and disk/Raft-state size is healthy. It never changes anything. If something's wrong it exits non-zero with a per-host problem list, which makes it usable as a cron or monitoring probe.

```bash
ansible-playbook playbooks/healthcheck.yml -i inventory/hosts.ini
ansible-playbook playbooks/healthcheck.yml -i inventory/hosts.ini -e spur_ignore_unreachable_agents=true  # skip unreachable agents instead of aborting
```

> **Partitions** aren't manageable this way yet. Spur has no runtime partition CLI (unlike Slurm's `scontrol create/update/delete partition`), so partition changes still mean editing the `[[partitions]]` blocks in the controller role's `spur.conf` template and re-running `deploy.yml`, which involves a brief controller restart. This is tracked upstream as a Spur feature request.

---

## Tear down

Two flavors — the plain one stops the cluster, the `wipe=true` one also erases per-host state:

```bash
ansible-playbook playbooks/teardown.yml -i inventory/hosts.ini              # stop + disable daemons, remove units
ansible-playbook playbooks/teardown.yml -i inventory/hosts.ini -e wipe=true  # also rm -rf ~/spur and /etc/wireguard/spur0.conf
ansible-playbook playbooks/teardown.yml -i inventory/hosts.ini -e spur_ignore_unreachable_agents=true  # skip unreachable agents instead of aborting
```

Either way it stops and disables the systemd services and reaps any stray daemons. It deliberately leaves PostgreSQL installed and your accounting database intact — so if you want those gone too, drop them by hand on the accounting host.

An agent with a job still running is drained first; if it doesn't finish draining, that host is left running rather than silently killing the job:

```bash
ansible-playbook playbooks/teardown.yml -i inventory/hosts.ini -e spur_skip_busy_agents=true            # leave busy agents running, tear down the rest
ansible-playbook playbooks/teardown.yml -i inventory/hosts.ini -e spur_force_teardown_busy_agents=true  # kill the running job and tear it down anyway
```

```bash
sudo -u postgres dropdb spur
sudo -u postgres dropuser spur
```

---

## Troubleshooting

A field guide to things you might see while running or operating a cluster — most of the scary-looking ones are harmless.

| Symptom | What's going on |
|---|---|
| `spurctld` logs an `ERROR` on restart — `Can not initialize last_log_id=... vote=...:committed` | Harmless — normal on any restart with existing Raft state. Judge health from `spur nodes` / jobs / `sacct`, not this line. |
| `spurd` logs `failed to load spur.conf` on startup | `spurd` loads `spur.conf` best-effort for the `[cluster]` (SPUR-managed k0s) and `[devices]` (GPU/GRES) sections; everything else it needs (controller, node name, ports) comes from CLI flags. This role writes `spur.conf` on agents too (same file controllers get), so this should only appear on a brand-new agent whose config hasn't rendered yet (e.g. mid-`add_nodes.yml`, before the next full `deploy.yml`/`rolling_upgrade.yml`) — harmless there, self-resolves on the next run. |
| `invalid transition from Completed to Completed` after a multi-node job | Harmless — a follower's redundant terminal-state report, rejected by the leader. The job succeeded. |
| `spur nodes` shows fewer rows than I have hosts | It collapses by partition — the NODES column is a count, not one row per host. Use `spur show node <name>` to check a specific host. |
| Playbook fails during the accounting apt install with a dpkg-lock error | Another `apt`/unattended-upgrade is running. Wait for it to finish (don't force-clear the lock unless `apt-get` is genuinely hung). |
| Can't find a job's output file | It lands in the job's working directory as `spur-<JOBID>.out`, on **whichever agent ran it** (no shared-FS assumption). Default working dir is `{{ spur_home }}`. |
| `spur --version` errors | Not a supported flag — use `spur nodes` / `spur show ...` to confirm the CLI works. |
| Job IDs restarted at 1 and old `sacct` history looks overwritten | You deployed with `spur_wipe_state=true`, which resets the Raft job-id counter. Use `spur_wipe_state=false` (the default) to preserve history. |

**Ports** the cluster listens on — open these between hosts: `6817` controller API + accounting, `6818` agents, `6821` Raft. `6821` is fixed in Spur 0.3.0.

Curious **why the roles are written the way they are**? The implementation rationale, plus the hard-won Ansible/Spur quirks baked into the tasks, lives in [`CONTRIBUTING.md`](CONTRIBUTING.md) — that's maintainer reference, not something you need to deploy.
