# Spur — Ansible Deployment

Playbooks for the whole cluster lifecycle:

- **`deploy.yml`** — stands up a Spur cluster in any supported shape from your `spur.conf`.
- **`rolling_upgrade.yml`** — upgrades a running cluster's binaries with no full-cluster outage.
- **`teardown.yml`** — stops the cluster.
- Day-2 [admin operations](#admin-operations) — `manage_accounts.yml`, `add_nodes.yml`, `remove_nodes.yml`, `healthcheck.yml`.

Daemons run as **systemd services**, Slurm-compatible CLI names are symlinked, and optional **PostgreSQL accounting** (embedded in `spurctld`) is on by default.

## spur.conf IS the inventory

There's no separate `hosts.ini`. Every playbook reads its inventory — controllers, agents, login nodes, everything — directly from a `spur.conf` file, via the `spur_conf` Ansible inventory plugin (`inventory_plugins/spur_conf.py`). You pass that same file as `-i`:

```bash
ansible-playbook playbooks/deploy.yml -i spur.conf -e spur_binary_src=<path>
```

Controllers come from `[controller].hosts`, agents from `[[nodes]]` — the same sections `spurctld`/`spurd` themselves read. A small `[ansible]`-prefixed layer carries connection metadata the runtime daemons don't need (SSH user/host, WireGuard address, per-section opt-outs) — it's stripped before the file is ever pushed to a node, so what lands on disk on each host is exactly the real schema, nothing ansible-only leaks into it.

**The master file is always kept in sync.** Every playbook run pushes the current content of `spur.conf` to every node it manages — there's no "did I remember to re-render this" step, no drift between what you wrote and what's live. The only "leave this alone" mechanism is declarative: list a section under `[ansible].exclude_sections` and no playbook will ever overwrite whatever's already on that node for that section, until you remove it from that list — a brand-new node still gets the master's version on its first-ever push, since there's nothing on disk yet to preserve. This replaced an older `-e spur_overwrite_conf=true` flag entirely — there's no flag to pass anymore, just the file's own declared intent.

**No spur.conf yet?** Run the one-time bootstrap generator first:

```bash
cp inventory/hosts.example.ini inventory/hosts.ini && $EDITOR inventory/hosts.ini
ansible-playbook playbooks/bootstrap_conf.yml -i inventory/hosts.ini -e spur_conf_out=./spur.conf
```

This is the **one deliberate exception** that still needs a classic hosts.ini — on a genuinely fresh cluster there's nothing else to derive topology from. It writes a starter `spur.conf`; review it, fill in anything it can't know (accounting credentials, k8s CIDRs), then use that file as `-i` for everything else from here on.

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

# 2. Ansible + spur.conf — hand-filling the example is simplest; if you'd
# rather seed it from a classic hosts list instead, use the bootstrap
# generator above (skip this cp and $EDITOR step).
python3 -m pip install --user 'ansible-core>=2.14' tomlkit
cp inventory/examples/spur.conf.master.example spur.conf
$EDITOR spur.conf

# 3. Deploy
ansible-playbook playbooks/deploy.yml -i spur.conf -e spur_binary_src="$SPUR_BUILD"

# Without PostgreSQL accounting (jobs still run, sacct/fairshare unavailable):
ansible-playbook playbooks/deploy.yml -i spur.conf -e spur_binary_src="$SPUR_BUILD" -e spur_accounting_enabled=false
```

> **tomlkit is required on the control node** (not on any remote host) — the inventory plugin and push filters parse/render TOML with it, preserving comments and formatting on anything they don't touch.

> **Dedicated accounting node:** to run PostgreSQL on its own host (not a controller or agent), add an `[[ansible_accounting_nodes]]` entry to `spur.conf` — the playbook auto-detects it, so the deploy command is unchanged (no `-e` needed). That host runs only Postgres (no spur daemons), and every controller connects to it remotely. See [Accounting](#accounting-postgresql-embedded-in-spurctld) for the layout.

A few things to know:

- **Where Postgres runs:** by default (no `spur_accounting_host` set) it runs on the **first controller**, which is fine for most clusters. See [Accounting](#accounting-postgresql-embedded-in-spurctld) for the dedicated-node layout and details.
- **Verify with a real test job is opt-in** (`-e spur_verify_enabled=true`) — off by default so routine deploys don't add job noise; check `spur nodes` yourself, or enable it to have the playbook submit and confirm one.
- **Your real spur.conf is git-ignored** (see `.gitignore`) — only `inventory/examples/*.example` templates are tracked.

| Shape | spur.conf pattern | Transport |
|---|---|---|
| Single-node | one `[[nodes]]` entry whose `names` matches the controller's `ctl-0` inventory name (co-location) | local loopback |
| Multi-node — direct LAN | `[controller].hosts` has one entry, `[[nodes]]` lists the rest | LAN IP, unencrypted |
| Multi-node — WireGuard mesh | as above + `-e spur_transport=wireguard` and `ansible_wg_address` per node | encrypted mesh on `spur0` |
| HA — multi-controller Raft | `[controller].hosts` has **≥ 3 entries**; auto-enabled | direct or wireguard |
| HA — separate compute | controllers have no matching `[[nodes]]` co-location entry | direct or wireguard |

**Details below:** [Build prerequisites](#build-prerequisites) · [Ansible control node](#ansible-control-node) · [Example commands](#example-commands-per-scenario) · [spur.conf examples](#spurconf-examples) · [Login nodes](#login-submission-nodes) · [Accounting](#accounting-postgresql-embedded-in-spurctld) · [Variables](#variables-defaults-in-playbooksgroup_varsallyml) · [Upgrading](#upgrading) · [Managing the cluster](#managing-the-cluster-after-deploy) · [Admin operations](#admin-operations) · [Tear down](#tear-down) · [Troubleshooting](#troubleshooting)

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

**Each host only gets the binaries it needs.** Agent-only hosts (in `spur_agents` but not `spur_controllers`/`spur_login`) get `spurd` only — no `spur` CLI, no `spurctld`, no Slurm symlinks. Controllers get `spur` + `spurctld` + symlinks (and `spurd` too if also an agent via co-location); login nodes get `spur` + symlinks. This keeps compute nodes free of an interactive CLI and the controller daemon they never run.

Omit `spur_binary_src` and the playbook falls back to downloading a published release via upstream `install.sh` instead (see [Build prerequisites](#build-prerequisites)).

## Ansible control node

The control node is wherever you run `ansible-playbook` — your workstation is fine, and it doesn't need to join the cluster.

Required on the control node (not on any remote host):

```bash
python3 -m pip install --user tomlkit                # spur_conf inventory plugin + push filters
ansible-galaxy collection install -r requirements.yml # ansible.utils, for WireGuard's ipaddr/ipmath filters
python3 -m pip install --user netaddr                 # also needed by ansible.utils, WireGuard only
```

Target hosts need: SSH reachable, sudo or root, `systemd`, and — only for the `install.sh` fallback — `curl` + `tar`.

- Every play runs with `become: true`, so a non-root `ansible_user` with passwordless sudo (or `ansible_become_password` supplied) works the same as `ansible_user=root`. This is required: `spur_home`/`spur_install_dir` default under `/root`, which a non-root user can't write to without becoming root.
- Password-based SSH works too — set `ansible_user`/an `ansible_password` (via `sshpass`) on the `[[nodes]]`/`[[ansible_controllers]]` entry, or `default_user` under `[ansible]` for the whole fleet. `ansible.cfg` deliberately omits `-o BatchMode=yes`. That flag suppresses SSH's password prompt outright and silently breaks password auth even when `sshpass` is supplying the answer.

---

## Example commands per scenario

Every deploy uses the same base command; pick the `spur.conf` example that matches your setup and add any extra flags from the table below.

```
ansible-playbook playbooks/deploy.yml -i <spur.conf> -e spur_binary_src=<path> [extra flags]
```

| Scenario | spur.conf example | Extra flags |
|---|---|---|
| Single-node / multi-node, direct LAN | `inventory/examples/spur.conf.master.example` | *(none)* |
| HA, hyperconverged (controllers double as agents) | `inventory/examples/spur.conf.ha.example` | *(none)* |
| HA, WireGuard mesh | `inventory/examples/spur.conf.ha-wireguard.example` | `-e spur_transport=wireguard` |
| HA, WireGuard + SPUR-managed k0s | `inventory/examples/spur.conf.k8s-ha.example` | `-e spur_transport=wireguard -e spur_k8s_enabled=true` (see [k0s](#ha--wireguard-mesh--spur-managed-k0s-k8s)) |
| No accounting (jobs still run, `sacct` unavailable) | any | `-e spur_accounting_enabled=false` |
| Preserve Raft state on re-deploy (production default) | `inventory/examples/spur.conf.ha.example` | `-e spur_wipe_state=false` |
| One host only (e.g. re-push config to a single agent) | any | `--limit gpu-1` |
| Dry-run without applying | any | `--check --diff` |
| Large fleet, tolerate a few agents being down for maintenance | any | `-e spur_ignore_unreachable_agents=true` |

---

## spur.conf examples

Pick the example that matches your cluster shape and copy it as your `spur.conf`. All of them live under `inventory/examples/`.

### Single-node / small multi-node, direct LAN

`inventory/examples/spur.conf.master.example` — one controller, one agent, direct transport, accounting on. A single-node deploy is the same file with the controller and agent co-located: give the controller's `[[nodes]]` entry `names = "ctl-0"` (matching its synthesized controller name) instead of a separate node name — see the co-location note below.

```toml
cluster_name = "example-cluster"

[ansible]
default_user = "vm"
exclude_sections = ["accounting"]   # preserved once it exists on a node; a brand-new node still gets this from the master file

[controller]
listen_addr = "[::]:6817"
hosts = ["10.0.0.10"]
state_dir = "/opt/spur/state"
raft_listen_addr = "[::]:6821"

[scheduler]
plugin = "backfill"
interval_secs = 1

[accounting]
database_url = "postgresql://spur:spur@10.0.0.10:5432/spur"
fairshare_refresh_secs = 30

[network]
wg_enabled = false
agent_port = 6818

# cpus/memory_mb are optional — ansible fills them in from the node's live
# gathered facts the first time it's pushed, ONLY if left absent here. An
# explicit value always wins.
[[nodes]]
names = "gpu-1"
ansible_host = "10.0.0.20"
ansible_user = "vm"

[[partitions]]
name = "default"
default = true
nodes = "gpu-1"
max_time = "INFINITE"
```

`[ansible]`, `[[ansible_controllers]]`, `[[ansible_login_nodes]]`, `[[ansible_accounting_nodes]]`, and any `ansible_*` key on a `[[nodes]]`/`[[ansible_controllers]]` entry are ansible-only — stripped before this file is ever pushed to a node. Everything else is the real schema `spurctld`/`spurd` read.

**Controller/agent co-location.** A controller's inventory name is always `ctl-<index>` (its position in `[controller].hosts`). To make it *also* an agent — single-node deploys, or an HA controller that's also a compute node — add a `[[nodes]]` entry whose `names` matches that synthesized name (`ctl-0`, `ctl-1`, …). The plugin recognizes the collision and treats it as one host in both groups, using the `[[nodes]]` entry's connection info.

**Controller with SSH target diverging from its listen IP** (behind NAT, etc.) — the rare case a bare `[controller].hosts` entry can't express — add a matching `[[ansible_controllers]]` override:

```toml
[[ansible_controllers]]
host = "10.0.0.10"        # must match a [controller].hosts entry
ansible_host = "ctl-0.internal"
ansible_user = "root"
```

**Dedicated accounting node** and **login-only nodes** get their own ansible-only arrays, since neither has a natural home in the runtime schema:

```toml
[[ansible_accounting_nodes]]
name = "db1"
ansible_host = "10.0.0.30"
ansible_user = "vm"

[[ansible_login_nodes]]
name = "login1"
ansible_host = "10.0.0.20"
ansible_user = "vm"
```

### HA — multi-controller Raft (hyperconverged)

`inventory/examples/spur.conf.ha.example` — 3 controllers (co-located as agents, so each also runs `spurd`) + 2 dedicated workers, direct transport. `peers`/`node_id` are left absent in the example on purpose — ansible auto-fills them (mechanical, derived from `[controller].hosts`'s order + the raft port) the first time it's pushed; an explicit value in the file always wins if you'd rather set it by hand.

**In HA mode, the playbook:**
- Pushes the same `peers = [...]` list to every controller's `spur.conf` (auto-filled if absent). Order matters — don't reorder `[controller].hosts` between deploys without wiping state.
- Auto-fills `node_id` = the controller's 1-based position in `[controller].hosts`, if absent.
- Waits for a leader to be elected before proceeding to agent registration.
- Lets non-leader controllers forward client RPCs to the leader internally, so clients can talk to any controller.

**Always use an odd `N` ≥ 3 in production.** An even `N` gives the same fault tolerance as `N-1` and is strictly worse, and `N=2` has zero fault tolerance (code-path testing only).

**Client-side failover is automatic.** Both `spurd --controller` and the CLI's `SPUR_CONTROLLER_ADDR` accept a comma-separated endpoint list and rotate past a dead one. The playbook therefore points every agent — and each controller's own `/etc/environment` — at *every* controller, not just the first. A single surviving controller is enough, since server-side leader forwarding handles writes from any endpoint in the list. No VIP/DNS round-robin is needed for basic failover.

### HA — WireGuard mesh (multi-controller)

`inventory/examples/spur.conf.ha-wireguard.example` — combine multi-controller Raft with `-e spur_transport=wireguard`: the control plane (and Raft) rides an encrypted mesh instead of the LAN. `[controller].hosts` holds the **mesh** addresses here (not the LAN IPs — those go on each `[[nodes]]` entry's `ansible_host`, since ansible still needs to reach the host over SSH/LAN before the mesh exists). The bootstrap controller runs `net init` (`.1`); the other controllers and all agents join, then `net mesh` peers everyone directly so Raft elections work even if the bootstrap controller is down.

`ansible_wg_address` pins the mesh IP per node explicitly (recommended for a fixed, readable layout); omit it to auto-assign positionally instead.

`spur_transport` is still an ansible-side variable (gates which roles/tasks run) — pass it via `-e`, separate from the file's own `[network].wg_enabled` key which reflects the same fact for the daemon's own use.

A full HA + WireGuard example lives at `inventory/examples/spur.conf.ha-wireguard.example`.

### HA — WireGuard mesh + SPUR-managed k0s (k8s)

`inventory/examples/spur.conf.k8s-ha.example` — layers a SPUR-managed k0s cluster on top of the HA mesh: 3 SPUR controllers (co-located as agents), an HA k0s control plane (3 nodes), 5 k0s workers, 9 plain compute agents, and 1 login node (21 hosts total). The key thing to understand: **there is no separate k8s inventory concept.** Every k8s node is first a registered SPUR agent (`[[nodes]]` entry); the k0s control-plane-vs-worker split is a **runtime CSV** passed to `spur k8s up` via `spur_k8s_control_plane_nodes` / `spur_k8s_nodes` (ansible `-e` vars, not file content). The k0s control plane is a separate quorum from the SPUR (`spurctld`) controllers — they can be different hosts, as in the example.

`ansible_wg_address` is pinned per host in the example (per-role IP blocks) — the positional auto-assign is not stable once you add/remove nodes, so pinning is recommended for any cluster you'll grow over time.

Deploy, then bring up k0s:

```bash
ansible-playbook playbooks/deploy.yml -i spur.conf \
  -e spur_transport=wireguard -e spur_k8s_enabled=true -e spur_k8s_cni=calico \
  -e spur_k8s_control_plane_nodes=k8s-cp-0,k8s-cp-1,k8s-cp-2 \
  -e spur_k8s_nodes=k8s-cp-0,k8s-cp-1,k8s-cp-2,k8s-w-0,k8s-w-1,k8s-w-2,k8s-w-3,k8s-w-4

ansible-playbook playbooks/k8s_up.yml -i spur.conf \
  -e spur_k8s_control_plane_nodes=k8s-cp-0,k8s-cp-1,k8s-cp-2 \
  -e spur_k8s_nodes=k8s-cp-0,k8s-cp-1,k8s-cp-2,k8s-w-0,k8s-w-1,k8s-w-2,k8s-w-3,k8s-w-4
```

> `spur_k8s_enabled` must be `true` at `deploy.yml` time so `spur.conf`'s `[cluster]` section is populated — `k8s_up.yml` asserts this and fails fast otherwise. The 9 `gpu-*` compute agents and `login-0` are in the mesh and the SPUR scheduler but deliberately excluded from `spur_k8s_nodes`, so they never run k0s.

---

## Login (submission) nodes

**Optional, off by default.** A login node is a host your users SSH into to submit and query jobs. It runs the `spur` CLI but **no daemon** — no `spurctld`, no `spurd`. To set one up, add an `[[ansible_login_nodes]]` entry to `spur.conf`. Leave it out and nothing happens.

```toml
[[ansible_login_nodes]]
name = "login1"
ansible_host = "10.0.0.20"
ansible_user = "root"
```

On each login node the playbook does three things: installs the `spur` CLI, adds all 18 Slurm symlinks, and writes `SPUR_CONTROLLER_ADDR` (every controller, comma-joined) to `/etc/environment`. Nothing else. Users can then SSH in and run `sbatch`/`squeue`/`sacct`/`srun`/etc. with no per-command flags.

You only need an `[[ansible_login_nodes]]` entry for **dedicated** submission hosts. A host already a controller or agent is already a client, so there's no need to list it here too.

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
4. The `[accounting].database_url` your `spur.conf` carries is pushed to every controller verbatim — you write it, ansible doesn't derive or rewrite it.

**Dedicated accounting node.** To run Postgres on its own host — not a controller or agent — add an `[[ansible_accounting_nodes]]` entry to `spur.conf`. That's all it takes: the playbook auto-detects it and runs Postgres there. The host needs no spur binaries, since only Postgres runs on it.

```toml
[[ansible_accounting_nodes]]
name = "acct-0"
ansible_host = "10.0.0.20"
ansible_user = "root"
```

Deploy exactly as usual — no accounting-specific flag needed:

```bash
ansible-playbook playbooks/deploy.yml -i spur.conf
```

The accounting host is resolved in this order: an explicit `spur_accounting_host` → the first entry in `[[ansible_accounting_nodes]]` → the first controller (the co-located default). So the entry alone is enough to pin a dedicated node.

**Pointing accounting at a specific host instead.** To use an existing host without adding an entry — say a particular controller or agent — set `spur_accounting_host` (it takes highest precedence). Pass it with `-e`:

```bash
ansible-playbook playbooks/deploy.yml -i spur.conf -e spur_accounting_host=acct-0
```

Every controller connects to the accounting host over the network; the role opens `listen_addresses`/`pg_hba.conf` for each controller's IP. If the host's PostgreSQL isn't on the default port, pass `-e spur_accounting_db_port=<port>` — and make sure `[accounting].database_url` in `spur.conf` matches.

On **controller nodes**, the playbook writes `SPUR_CONTROLLER_ADDR` (listing every controller) to `/etc/environment`. This means the whole CLI works there with no per-command flags — `squeue`/`sinfo`/`scontrol` and `sacct`/`sacctmgr`/`sreport`/`sshare` alike. Accounting rides the same address; there's no separate env var for it anymore. On non-controller nodes, or to override, set it yourself:

```bash
export SPUR_CONTROLLER_ADDR=http://<a-controller>:6817[,http://<another>:6817,...]
```

Job submission still works without accounting — pass `-e spur_accounting_enabled=false` (shown in [Quick start](#quick-start)). You'll only lose `sacct` and fairshare.

> Set real DB credentials directly in `[accounting].database_url` — there's no separate `spur_accounting_db_password` var driving it anymore; the URL you write is the URL that ships. Also set `-e spur_accounting_db_password=<same password>` so the `spur_accounting` role provisions the matching Postgres role — the two must agree.

**Upgrading a pre-merge deployment.** If a cluster is still running the old, separate `spurdbd` (check with `systemctl is-active spurdbd`, or look for an `[accounting] host = ...` line in `spur.conf`), just re-run `deploy.yml` pointed at a merged-architecture build. `spur_accounting` stops, disables, and removes any leftover `spurdbd` unit and binary, and reconfigures Postgres for remote access — update `spur.conf`'s `[accounting].database_url` by hand to drop any old `host` key if present. Note that this is a full-cluster convergence, not a rolling upgrade (see [Upgrading](#upgrading)), so expect a brief restart of every daemon.

---

## Variables (defaults in `playbooks/group_vars/all.yml`)

Note: `group_vars` lives under `playbooks/` (not `inventory/`) — auto-discovery is tied to a fixed directory, and the inventory source (`spur.conf`) can now live anywhere, so it's anchored to the playbook directory instead.

| Variable | Default | What it does |
|---|---|---|
| `spur_binary_src` | *(unset)* | Local dir of pre-built binaries to push. Leave unset to fetch via upstream `install.sh`. |
| `spur_version` | `latest` | Which `install.sh` channel to pull: `latest` / `nightly` / `vX.Y.Z`. |
| `spur_install_dir` | `/root/.local/bin` | Where binaries and Slurm symlinks land (added to `/etc/environment`). |
| `spur_home` | `/root/spur` | Per-host root for state, logs, and config. |
| `spur_mpi_plugin_dir` | `/usr/lib/spur` | Where `spur_mpi_pmix.so` is installed on agents (matches `spurd` default). |
| `spur_mpi_plugin_enabled` | `true` | Install the MPI plugin on agents when a source file is available. |
| `spur_transport` | `direct` | Network transport: `direct` or `wireguard`. Ansible-side var — separate from `spur.conf`'s own `[network].wg_enabled`. |
| `spur_accounting_enabled` | `true` | Deploy PostgreSQL accounting (embedded in spurctld). Set `false` to skip. |
| `spur_accounting_host` | first controller | Host that runs Postgres (any managed host). Pass via `-e`. |
| `spur_accounting_db_password` | `spur` | Password the `spur_accounting` role provisions the Postgres role with — must match whatever's embedded in `spur.conf`'s `[accounting].database_url`. |
| `spur_accounting_db_port` | `5432` | Postgres listen port. |
| `spur_controller_port` / `spur_agent_port` / `spur_raft_port` | `6817` / `6818` / `6821` | Daemon listen ports. |
| `spur_log_level` | `info` | Daemon log verbosity. |
| `spur_wipe_state` | `false` | Wipe `~/spur/state` (Raft job queue/registrations) on (re)deploy. Safe default for re-runs and upgrades; set `true` for a fresh install or intentional Raft reinit. |
| `spur_force_reinstall` | `false` | Re-pull/re-copy binaries via `spur_install` even if already present. Used by `rolling_upgrade.yml`. |
| `spur_rolling_batch_size` | `1` | Agents upgraded per batch in `rolling_upgrade.yml` (`serial`). Controllers always upgrade one at a time regardless. |
| `spur_controller_upgrade_order` | inventory order | Comma-separated controller names — upgrade them in this order instead of `[controller].hosts`'s order. Must name every controller exactly once; `rolling_upgrade.yml` fails fast otherwise. |
| `spur_ignore_unreachable_agents` | `false` | Skip agents unreachable over SSH instead of aborting — `deploy.yml`, `rolling_upgrade.yml`, `healthcheck.yml`, `teardown.yml`, `add_nodes.yml`, `remove_nodes.yml`. Never loosens controller/accounting-host reachability. |
| `spur_verify_enabled` | `false` | Submit and wait for a real test job at the end of `deploy.yml` / `rolling_upgrade.yml` (`spur_verify` role). Off by default to avoid job noise on routine runs; CI enables it as a smoke test. |
| `spur_verify_submit_user` | *(unset — submits as the play's own user)* | Submit the verify role's test job as this user instead. Needed if the cluster's `spurd` rejects uid-0 job submission (`[auth] allow_root_jobs=false`), which newer builds default to. |
| `spur_gather_subset` | `['!all', network, hardware, distribution]` | Facts subset gathered by every `gather_facts: yes` play. Restricts Ansible's default full gather (every mount/PCI device/interface) to just what the roles use. Set `-e spur_gather_subset=all` to fall back to full gathering. |
| `spur_drain_wait_secs` | `120` | How long to wait for a node to reach `DRAINED`/`DOWN` before treating it as busy — used by `deploy.yml`, `rolling_upgrade.yml`, and (only if draining was requested) `teardown.yml` via the shared drain task, plus `remove_nodes.yml`'s own separate inline drain-wait logic. |
| `spur_skip_busy_agents` | `false` | Leave a still-busy node untouched and continue with the rest of the fleet, instead of the playbook's default (refuse, or force for `deploy.yml`). Shared task in `deploy.yml`/`rolling_upgrade.yml`; `remove_nodes.yml` reads the same variable in its own inline logic. On `teardown.yml` (disruptive/no-drain by default), setting this is also what opts back into draining first. |
| `spur_force_teardown_busy_agents` | `false` | `teardown.yml`-specific: opts into draining first (like `spur_skip_busy_agents` above), then kills a busy node's running job and tears it down anyway. |
| `spur_force_upgrade_busy_agents` | `false` | `rolling_upgrade.yml`-specific: kill a busy node's running job and upgrade it anyway. |
| `spur_force_remove_busy_nodes` | `false` | `remove_nodes.yml`-specific: kill a busy node's running job and remove it anyway. |

Override any of these per run with `-e key=value` (repeatable):

```bash
ansible-playbook playbooks/deploy.yml -i spur.conf -e spur_binary_src=/path/to/target/release -e spur_wipe_state=true
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
ansible-playbook playbooks/deploy.yml -i spur.conf -e spur_skip_busy_agents=true   # leave busy agents untouched, deploy the rest
```

Agents get `spur.conf` too (same content controllers get, minus `[accounting]` — `spurd` reads it best-effort for `[cluster]`/k0s and `[devices]`/GRES settings). Every run pushes whatever's currently in the master file — list a section under `[ansible].exclude_sections` to preserve a node's existing on-disk copy of it going forward (a brand-new node still gets the master's version the first time, since nothing exists yet to preserve).

```bash
cargo build --release -p spur-cli -p spurctld -p spurd   # rebuild all three together, see caveat below
ansible-playbook playbooks/deploy.yml -i spur.conf -e spur_binary_src=/path/to/target/release
```

A few things worth knowing:

- **Binaries roll out by content, not version string.** Ansible compares checksums, so an unchanged re-run is a near no-op.
- **Rebuild all three together.** They share a Raft WAL schema. Mixing binaries from different builds — say, rebuilding only `spurctld` — can leave a controller unable to parse a log a differently-versioned peer wrote.
- **HA topology changes are guarded.** Adding, removing, or reordering a **controller** needs a reinit. Spur 0.3.0 has no online Raft membership change, so the role fails early with an actionable message if you change `[controller].hosts` without `-e spur_wipe_state=true`. **Compute agents aren't Raft members** — add or remove them freely, no wipe needed.
- **Demoting a controller to agent-only leaves a stale `spurctld`.** Removing a host from `[controller].hosts`? Run `systemctl disable --now spurctld` on it first. Otherwise the leftover daemon keeps the old membership and can block quorum.

### Rolling upgrade (`rolling_upgrade.yml`) — for a live cluster with running jobs

This path upgrades one host at a time, so the cluster keeps scheduling and running jobs throughout.

```bash
cargo build --release -p spur-cli -p spurctld -p spurd
ansible-playbook playbooks/rolling_upgrade.yml -i spur.conf -e spur_binary_src=/path/to/target/release
```

It reuses the same `spur_install`/`spur_controller`/`spur_agent`/`spur_verify` roles that `deploy.yml` uses, so there's no duplicated install, config, or health-check logic. Here's what it does, in order:

1. **Guard rail.** Refuses to start if `spur_wipe_state=true`, if `spur_transport=wireguard` (this playbook doesn't support it yet), or if the cluster isn't already healthy (leader elected, `spur nodes` reachable).
2. **Controllers, one at a time** (`serial: 1`; the whole run aborts on the first failure). For each: force-reinstall, restart `spurctld`, then wait on the existing HA-aware "wait for Raft leader" task as the health gate before moving to the next controller. Client-side failover keeps the rest serving while one is down, since every agent and CLI is pointed at *every* controller.
3. **Agents, in configurable batches** (`spur_rolling_batch_size`, default `1`). For each node: `spur node drain <node>`, poll until `DRAINED` (once no running jobs are left), force-reinstall, restart `spurd`, wait for re-registration, then `scontrol update NodeName=<node> State=RESUME`.
4. **Verify** *(opt-in — `-e spur_verify_enabled=true`)*. Submits a real test job at the end to confirm the upgraded cluster actually schedules work. Off by default so routine upgrades don't add job noise; CI enables it.

The same rebuild-all-three caveat from full convergence applies here too.

`spur_rolling_batch_size` trades speed for blast radius. The default `1` disrupts at most one agent's capacity at a time; a higher value upgrades faster but drains more capacity concurrently.

Controllers upgrade in `[controller].hosts` order by default. To upgrade them in a specific order instead — say, a known-stable Raft leader first — pass `-e spur_controller_upgrade_order=ctl-2,ctl-0,ctl-1`. It must name every controller exactly once; the guard-rail play fails fast (before touching any host) if a name is missing, misspelled, or duplicated.

A still-busy node that doesn't drain within `spur_drain_wait_secs` (default `120s`) aborts the run by default. Opt out per node or force through it:

```bash
ansible-playbook playbooks/rolling_upgrade.yml -i spur.conf -e spur_binary_src=/path -e spur_skip_busy_agents=true         # leave busy agents on the old build, upgrade the rest
ansible-playbook playbooks/rolling_upgrade.yml -i spur.conf -e spur_binary_src=/path -e spur_force_upgrade_busy_agents=true  # kill the running job and upgrade it anyway
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
# e.g. playbooks/group_vars/all.yml (or a dedicated accounts.yml)
spur_qos:
  - { name: high, priority: 100, maxwall: 1440 }
spur_accounts:
  - { name: research, description: "Research group", fairshare: 100 }
  - { name: ml, parent: research, fairshare: 50 }
spur_users:
  - { name: alice, account: ml, defaultaccount: ml }
```

```bash
ansible-playbook playbooks/manage_accounts.yml -i spur.conf
```

To remove entities, list them under `spur_qos_absent` / `spur_accounts_absent` / `spur_users_absent` (name-only; users also take `account`). The playbook applies everything in FK-safe order automatically: qos/accounts before users when adding, and users before accounts when removing. That ordering matters because an account can't be deleted while a user association still references it. Requires accounting enabled.

### Add compute nodes — `add_nodes.yml`

Add agent(s) to a **running** cluster with **no disruption to existing daemons or jobs**. A full `deploy.yml` run also adds nodes, but it restarts every spurctld and spurd (see [Upgrading](#upgrading)); `add_nodes.yml` only starts `spurd` on the new hosts. This works because a new `spurd` registers itself at runtime — the node shows up in `spur nodes` immediately, with no controller restart.

**First add a `[[nodes]]` entry for the new host(s) to `spur.conf` by hand** (`names`/`ansible_host`/`ansible_user`) — spur.conf is the source of truth for cluster membership, and ansible can't target a host it doesn't know about yet. Then run:

```bash
ansible-playbook playbooks/add_nodes.yml -i spur.conf -e new_nodes=gpu-3,gpu-4
```

What it does: gathers facts across the cluster so the pushed `spur.conf` has cpu/memory for every node whose entry omits them, installs and starts `spurd` on the new hosts only, refreshes `spur.conf` on the controllers **without restarting them**, and verifies that each new node registered. It's idempotent — a node that's already registered is skipped, so re-runs never bounce a healthy agent. Adding a **controller** is out of scope, because that's a Raft membership change; use `deploy.yml` for that. `add_nodes.yml` refuses controller names.

On a **WireGuard** cluster it joins the new node to the mesh before starting `spurd`, and meshes it with every existing member (controllers, agents, and login nodes) so it's fully peered — not just to the controllers. An unrelated existing member being down doesn't abort the add when `-e spur_ignore_unreachable_agents=true` is set. The new node's `[[nodes]]` entry needs an `ansible_wg_address` (pinned, or auto-assigned by position).

### Decommission compute nodes — `remove_nodes.yml`

Cleanly remove agents from a running cluster. The playbook drains each node, waits until it reaches `DRAINED` so running jobs finish first, stops `spurd` on the node, then runs `spur node remove`. There's no state wipe, since agents aren't Raft members.

```bash
ansible-playbook playbooks/remove_nodes.yml -i spur.conf -e nodes_to_remove=gpu-3,gpu-4
```

Use the names as they appear in `spur nodes`. The node(s) must still be listed in `spur.conf` when you run this (ansible needs to reach them for drain/cleanup) — **once live removal succeeds, the playbook prunes their `[[nodes]]` entries out of the master `spur.conf` file itself, in place.** No manual file edit needed afterward, unlike `add_nodes.yml`'s precondition.

On a **WireGuard** cluster it also tears down the removed node's mesh interface and drops its WG peer from **every surviving mesh member** — controllers, other agents, and login nodes — so it doesn't linger as a ghost peer (in a full mesh every node peered it). An unrelated agent being down doesn't block the removal when `-e spur_ignore_unreachable_agents=true` is set.

A node with a job still running does not finish draining — same job-safety model as `deploy.yml`/`rolling_upgrade.yml`. By default the run aborts before removing anything:

```bash
ansible-playbook playbooks/remove_nodes.yml -i spur.conf -e nodes_to_remove=gpu-3,gpu-4 -e spur_skip_busy_agents=true         # remove only the nodes that drained cleanly, leave busy ones in the cluster
ansible-playbook playbooks/remove_nodes.yml -i spur.conf -e nodes_to_remove=gpu-3,gpu-4 -e spur_force_remove_busy_nodes=true   # kill the running job and remove it anyway
```

### Health check — `healthcheck.yml`

Read-only cluster diagnostics. It checks that daemons are active, the controller is reachable with a leader elected, accounting/Postgres is up, agent ports are listening, and disk/Raft-state size is healthy. It never changes anything. If something's wrong it exits non-zero with a per-host problem list, which makes it usable as a cron or monitoring probe.

```bash
ansible-playbook playbooks/healthcheck.yml -i spur.conf
ansible-playbook playbooks/healthcheck.yml -i spur.conf -e spur_ignore_unreachable_agents=true  # skip unreachable agents instead of aborting
```

> **Partitions** aren't manageable this way yet. Spur has no runtime partition CLI (unlike Slurm's `scontrol create/update/delete partition`), so partition changes still mean editing the `[[partitions]]` blocks in `spur.conf` by hand and re-running `deploy.yml`, which involves a brief controller restart. This is tracked upstream as a Spur feature request.

---

## Tear down

Two flavors — the plain one stops the cluster, the `wipe=true` one also erases per-host state:

```bash
ansible-playbook playbooks/teardown.yml -i spur.conf              # stop + disable daemons, remove units
ansible-playbook playbooks/teardown.yml -i spur.conf -e wipe=true  # also rm -rf ~/spur and /etc/wireguard/spur0.conf
ansible-playbook playbooks/teardown.yml -i spur.conf -e spur_ignore_unreachable_agents=true  # skip unreachable agents instead of aborting
```

Either way it stops and disables the systemd services and reaps any stray daemons. On a **WireGuard** cluster it also brings the mesh interface down and disables its `wg-quick@<iface>` boot unit, so a plain (non-wipe) teardown doesn't silently re-establish the mesh on the next reboot; `-e wipe=true` additionally removes the saved `/etc/wireguard/<iface>.conf`. It deliberately leaves PostgreSQL installed and your accounting database intact — so if you want those gone too, drop them by hand on the accounting host.

Disruptive by default, with no drain step: teardown means the whole cluster is coming down, so there's no surviving fleet left to protect by draining agents first. To take a node out of a cluster that keeps running (draining it first so its job finishes), use `remove_nodes.yml` instead.

An agent with a job still running is stopped immediately by default — including any Docker/Podman container the job launched, via the same cgroup sweep `rolling_upgrade.yml --force` uses; only the wait-for-drain step is skipped, not cleanup. Pass either busy-agent flag to opt back into draining it first — if it doesn't finish draining, that host is left running rather than silently killing the job:

```bash
ansible-playbook playbooks/teardown.yml -i spur.conf -e spur_skip_busy_agents=true            # drain first; leave busy agents running, tear down the rest
ansible-playbook playbooks/teardown.yml -i spur.conf -e spur_force_teardown_busy_agents=true   # drain first; kill the running job and tear it down anyway
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
| `spurd` logs `failed to load spur.conf` on startup | `spurd` loads `spur.conf` best-effort for the `[cluster]` (SPUR-managed k0s) and `[devices]` (GPU/GRES) sections; everything else it needs (controller, node name, ports) comes from CLI flags. This role pushes agents the same master `spur.conf` controllers get (minus `[accounting]`), so this should only appear if that write itself failed — check disk space/permissions on `{{ spur_home }}/etc`. |
| `invalid transition from Completed to Completed` after a multi-node job | Harmless — a follower's redundant terminal-state report, rejected by the leader. The job succeeded. |
| `spur nodes` shows fewer rows than I have hosts | It collapses by partition — the NODES column is a count, not one row per host. Use `spur show node <name>` to check a specific host. |
| Playbook fails during the accounting apt install with a dpkg-lock error | Another `apt`/unattended-upgrade is running. Wait for it to finish (don't force-clear the lock unless `apt-get` is genuinely hung). |
| Can't find a job's output file | It lands in the job's working directory as `spur-<JOBID>.out`, on **whichever agent ran it** (no shared-FS assumption). Default working dir is `{{ spur_home }}`. |
| `spur --version` errors | Not a supported flag — use `spur nodes` / `spur show ...` to confirm the CLI works. |
| Job IDs restarted at 1 and old `sacct` history looks overwritten | You deployed with `spur_wipe_state=true`, which resets the Raft job-id counter. Use `spur_wipe_state=false` (the default) to preserve history. |
| `ansible-inventory`/`ansible-playbook` says "No inventory was parsed" against your `spur.conf` | The `spur_conf` plugin content-sniffs for a `cluster_name` key and a `[controller]` section — make sure both are present, and that `tomlkit` is installed on the control node (`pip install tomlkit`). |

**Ports** the cluster listens on — open these between hosts: `6817` controller API + accounting, `6818` agents, `6821` Raft. `6821` is fixed in Spur 0.3.0.

Curious **why the roles are written the way they are**? The implementation rationale, plus the hard-won Ansible/Spur quirks baked into the tasks, lives in [`CONTRIBUTING.md`](CONTRIBUTING.md) — that's maintainer reference, not something you need to deploy.
