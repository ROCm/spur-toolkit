# Spur — Ansible Deployment

One playbook (`deploy.yml`) stands up a Spur cluster in every supported shape, driven entirely by inventory. A second playbook (`rolling_upgrade.yml`) upgrades an already-running cluster's binaries with no full-cluster outage. Daemons run as **systemd services** (survive reboot), the Slurm-compatible CLI names are symlinked, and optional **PostgreSQL accounting** (embedded in `spurctld`) is deployed by default.

| Shape | Inventory pattern | Transport |
|---|---|---|
| Single-node | one host in **both** `spur_controllers` and `spur_agents` | local loopback |
| Multi-node — direct LAN | one host in `spur_controllers`, all compute in `spur_agents` | LAN IP, unencrypted |
| Multi-node — WireGuard mesh | as above + `spur_transport=wireguard` (single controller only) | encrypted mesh on `spur0` |
| HA — multi-controller Raft | **≥ 3 hosts** in `spur_controllers` (any number in `spur_agents`); auto-enabled | direct or wireguard |
| HA — separate compute | `spur_controllers` and `spur_agents` are **disjoint** host sets | direct or wireguard |

The playbook is idempotent — re-running it on a healthy cluster re-applies config and restarts daemons.

---

## End-to-end walkthrough

The full journey from a clean machine to a running cluster is: **clone → build binaries → stage them → write inventory → run the playbook.**

### 1. Clone the repo

```bash
git clone https://github.com/ROCm/spur.git
cd spur
```

### 2. Build all three binaries

Spur has no published GitHub release yet, so the upstream `install.sh` returns 403. Build the binaries yourself on a machine matching the targets' architecture/libc (the lab targets are Ubuntu 22.04 x86-64), then push them with the playbook (Step 3).

```bash
# Install Rust via rustup if you don't already have it. rustup automatically
# picks up the exact toolchain version pinned in rust-toolchain.toml the first
# time you run cargo inside the repo — no separate version-matching needed.
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

# Other prerequisites:
sudo apt install -y protobuf-compiler build-essential

# Build the three daemons/CLI in release mode:
cargo build --release -p spur-cli -p spurctld -p spurd
```

> Already have Rust installed via something other than rustup (a distro package, asdf, etc.)? Run `rustc --version` and compare against the `channel` in `rust-toolchain.toml` — a mismatch can fail the build in ways that look unrelated to versioning. rustup handles this automatically; other installers won't switch versions for you.

This produces, under `target/release/`:

| Binary | Role |
|---|---|
| `spur` | multi-call CLI (also symlinked to `sbatch`, `squeue`, `sinfo`, … by the playbook) |
| `spurctld` | controller / scheduler / Raft — also serves accounting (needs PostgreSQL) on the same gRPC port when `[accounting].database_url` is set |
| `spurd` | node agent |

> Pre-merge Spur builds (before upstream folded the standalone `spurdbd` accounting daemon into `spurctld`) additionally produce a `spurdbd` binary. `spur_binary_src` only needs `spur`/`spurctld`/`spurd` for current builds — an extra `spurdbd` alongside them is harmless and ignored.

Stage them into one directory the control node can read:

```bash
mkdir -p /tmp/spur-bin
cp target/release/spur target/release/spurctld target/release/spurd /tmp/spur-bin/
```

You point the playbook at this directory with `spur_binary_src` (see Step 4). If you omit `spur_binary_src`, the playbook falls back to the upstream `install.sh` — which only works once ROCm/spur publishes releases.

### 3. Install Ansible on the control node

The control node is wherever you run `ansible-playbook` — your workstation is fine; it does not need to be part of the cluster.

```bash
python3 -m pip install --user 'ansible-core>=2.14'
# WireGuard transport additionally needs the ansible.utils collection and the
# netaddr Python library on the control node:
ansible-galaxy collection install -r requirements.yml
python3 -m pip install --user netaddr
```

Target hosts need: SSH reachable, sudo or root, `systemd`, and (for the `install.sh` fallback path only) `curl` + `tar`. Every play runs with `become: true`, so a non-root `ansible_user` with passwordless (or `ansible_become_password`-supplied) sudo works the same as `ansible_user=root` — this is required since `spur_home`/`spur_install_dir` default under `/root`, which a non-root user can't write to without becoming root.

Password-based SSH (`ansible_password`, via `sshpass`) works too — set `ansible_user`/`ansible_password` in `[all:vars]` or per-host. `ansible.cfg`'s `ssh_args` intentionally omits `BatchMode=yes`: that option suppresses SSH's password prompt entirely and silently breaks password auth even though sshpass is supplying the answer.

### 4. Write your inventory

Copy an example and edit it. Real inventories are git-ignored (see `.gitignore`) so host addresses and credentials are never committed — only the `*.example.ini` placeholder templates are tracked.

```bash
cd ansible
cp inventory/hosts.example.ini inventory/hosts.ini
$EDITOR inventory/hosts.ini
```

Point `spur_binary_src` at the directory from Step 2 (in `[all:vars]` or on the command line with `-e`).

### 5. Run the playbook

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src=/tmp/spur-bin
```

The play ends by running a single-node test job (and, when ≥ 2 agents, a multi-node `-N` job) and — when accounting is enabled — recording them in PostgreSQL. Look for the `spur nodes` output and job stdout near the end of the run.

---

## Example commands per scenario

Each command assumes binaries are staged at `/tmp/spur-bin` (Step 2). Drop `-e spur_binary_src=…` once upstream publishes releases.

```bash
# Single-node (controller + agent on one host)
ansible-playbook playbooks/deploy.yml -i inventory/single.ini      -e spur_binary_src=/tmp/spur-bin

# Multi-node, direct LAN (1 controller + N agents)
ansible-playbook playbooks/deploy.yml -i inventory/multi.ini        -e spur_binary_src=/tmp/spur-bin

# HA, hyperconverged (3 controllers that also run agents)
ansible-playbook playbooks/deploy.yml -i inventory/ha.ini           -e spur_binary_src=/tmp/spur-bin

# HA, separate compute (3 dedicated controllers + distinct agent hosts)
ansible-playbook playbooks/deploy.yml -i inventory/ha-separate.ini  -e spur_binary_src=/tmp/spur-bin

# Any scenario WITHOUT accounting (no PostgreSQL; sacct unavailable, jobs still run)
ansible-playbook playbooks/deploy.yml -i inventory/multi.ini        -e spur_binary_src=/tmp/spur-bin -e spur_accounting_enabled=false

# WireGuard transport (encrypted mesh instead of LAN)
ansible-playbook playbooks/deploy.yml -i inventory/multi.ini        -e spur_binary_src=/tmp/spur-bin -e spur_transport=wireguard

# Preserve Raft state on re-deploy (production; don't wipe job history)
ansible-playbook playbooks/deploy.yml -i inventory/ha.ini           -e spur_binary_src=/tmp/spur-bin -e spur_wipe_state=false

# Limit to one host (e.g. re-push config to a single agent)
ansible-playbook playbooks/deploy.yml -i inventory/multi.ini        -e spur_binary_src=/tmp/spur-bin --limit gpu-1

# Dry-run the change set without applying
ansible-playbook playbooks/deploy.yml -i inventory/multi.ini        -e spur_binary_src=/tmp/spur-bin --check --diff
```

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

> WireGuard is **single-controller only** — `spur net init` auto-assigns the controller `.1` and there is no multi-controller mesh command, so HA over WireGuard is not supported (use `direct` for HA). Agents are auto-assigned `.2`, `.3`, … by inventory position; override any host with `spur_wg_address=…`. Requires the `ansible.utils` collection on the control node (`ansible-galaxy collection install -r requirements.yml`).

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

What the playbook does in HA mode:

- Writes the same `peers = [...]` list to every controller's `spur.conf` (order matters — don't reorder controllers between deploys without wiping state).
- Assigns `node_id` = the controller's 1-based position in `groups['spur_controllers']`.
- Waits for a leader to be elected before proceeding to agent registration.
- Non-leader controllers forward client RPCs to the leader internally — clients can talk to any controller.

**Always use an odd `N` ≥ 3 in production.** Even N gives the same fault tolerance as `N-1` and is strictly worse. `N=2` has zero fault tolerance — only useful for exercising the HA code path.

**Client-side failover is automatic.** `spurd --controller` and the CLI's `SPUR_CONTROLLER_ADDR` accept a comma-separated endpoint list and rotate past a dead one, so the playbook points every agent (and each controller's own `/etc/environment`) at *every* controller, not just the first. A single surviving controller is enough — server-side leader forwarding handles writes from any endpoint in the list. No VIP/DNS round-robin is required for basic failover.

A full HA inventory template lives at `inventory/hosts.ha.example.ini`.

---

## Accounting (PostgreSQL, embedded in spurctld)

Enabled by default (`spur_accounting_enabled: true`). There is no separate accounting daemon — upstream folded the standalone `spurdbd` into `spurctld`, which now serves the `SlurmAccounting` gRPC service in-process on its own controller port (6817) whenever `[accounting].database_url` is set. Only Postgres itself is a distinct service, and it runs on **`spur_accounting_host`** (default: the first controller). Before the controllers start, the `spur_accounting` role:

1. Installs `postgresql` + `postgresql-contrib`.
2. Creates the `spur` role and `spur` database idempotently.
3. Configures Postgres to accept remote TCP connections (`listen_addresses`, `pg_hba.conf`) from every controller's IP — required because each controller's embedded accounting service connects to this Postgres directly over the network, not just the one that happens to be co-located with it.
4. Every controller's `spur.conf` gets an `[accounting]` block with `database_url` pointing at that host.

**Placing accounting on a dedicated node:** set `spur_accounting_host` to any managed host — a controller, an agent, or a standalone node listed in its own group (e.g. `[spur_accounting_node]`). Pass it via `-e` (a play's `hosts:` field does not reliably read inventory `[all:vars]`):

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_accounting_host=acct-0
```

Then `sacct`/fairshare work. The playbook writes `SPUR_CONTROLLER_ADDR` (listing every controller) to `/etc/environment` on **controller nodes**, so the CLI (`squeue`/`sinfo`/`scontrol` and `sacct`/`sacctmgr`/`sreport`/`sshare`) works there with no per-command flags — accounting rides the same address, there's no separate flag/env for it anymore. On non-controller nodes (or to override), set it yourself: `export SPUR_CONTROLLER_ADDR=http://<a-controller>:6817[,http://<another-controller>:6817,...]`. To skip the entire stack:

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_accounting_enabled=false
```

Job submission still works without accounting — only `sacct`/fairshare are unavailable.

> The default DB credentials are `spur` / `spur` / `spur` — fine for a lab, **change `spur_accounting_db_password` for anything real.**

**Upgrading a pre-merge deployment:** if a cluster is currently running the old, separate `spurdbd` (identifiable by `spurdbd.service` being active and `spur.conf` having an `[accounting] host = ...` line), just re-run `deploy.yml` pointed at a merged-architecture build. The `spur_accounting` role stops/disables/removes any leftover `spurdbd` unit and binary, reconfigures Postgres for remote access, and the regenerated `spur.conf` drops the old `host` key — no manual cleanup needed. This is a full-cluster convergence, not a rolling upgrade (see below); expect a brief restart of every daemon.

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
| `spur_wipe_state` | `false` | Wipe `~/spur/state` (Raft job queue/registrations) on (re)deploy. Default `false` so re-runs and upgrades are non-destructive; set `true` for a fresh install or an intentional Raft reinit. |
| `spur_force_reinstall` | `false` | Force `spur_install` to re-pull/re-copy binaries even if already present. Used by `rolling_upgrade.yml`; `deploy.yml` normally leaves this `false`. |
| `spur_rolling_batch_size` | `1` | Agents upgraded per batch in `rolling_upgrade.yml` (`serial`). Controllers are always upgraded one at a time regardless of this setting. |

Override per-run with `-e key=value` (repeatable):

```bash
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src=/tmp/spur-bin -e spur_wipe_state=true
```

---

## Upgrading

There are two ways to roll out newer binaries, depending on whether the cluster can tolerate a full-daemon bounce.

### Full convergence (`deploy.yml`) — simplest, brief outage

Re-running `deploy.yml` is non-destructive by default (`spur_wipe_state=false`) — job state and node registrations survive — but it restarts **every** daemon on **every** host in the same play (no batching), so in-flight jobs are disrupted and, in HA, all controllers bounce together (briefly losing a Raft leader). Use this for a topology change (e.g. migrating a pre-merge cluster off the standalone `spurdbd`, adding/removing hosts) or whenever a short full-cluster blip is acceptable:

```bash
# rebuild the three binaries together (see caveat below)
cargo build --release -p spur-cli -p spurctld -p spurd
cp target/release/{spur,spurctld,spurd} /tmp/spur-bin/

# re-run — the binary copy is checksum-based, so only changed binaries are
# pushed; daemons restart to pick them up; Raft state is preserved
ansible-playbook playbooks/deploy.yml -i inventory/hosts.ini -e spur_binary_src=/tmp/spur-bin
```

- **Binaries are rolled out by content, not version string** — Ansible compares checksums, so a newer local build is pushed and a re-run with unchanged binaries is a near no-op.
- **Rebuild all three together.** The daemons share a Raft WAL schema. Mixing binaries from different builds (e.g. rebuilding only `spurctld`) can leave a controller unable to parse a log written by a differently-versioned peer.
- **HA topology changes are guarded.** Because Spur 0.3.0 has no online Raft membership change, adding/removing/reordering a **controller** requires a Raft reinit. If you change the controller set with `spur_wipe_state=false`, the controller role fails early with an actionable message telling you to re-run with `-e spur_wipe_state=true`. **Compute agents are not Raft members** — add or remove them freely, no wipe needed.
- **Demoting a controller to agent-only leaves a stale `spurctld`.** If you move a host from `[spur_controllers]` to agent-only, stop and disable its controller first: `systemctl disable --now spurctld` on that host — otherwise the leftover daemon keeps the old membership and can block quorum.

### Rolling upgrade (`rolling_upgrade.yml`) — for a live cluster with running jobs

Upgrades one host at a time so the cluster keeps scheduling and running jobs throughout:

```bash
cargo build --release -p spur-cli -p spurctld -p spurd
cp target/release/{spur,spurctld,spurd} /tmp/spur-bin/

ansible-playbook playbooks/rolling_upgrade.yml -i inventory/hosts.ini -e spur_binary_src=/tmp/spur-bin
```

What it does, reusing the same `spur_install`/`spur_controller`/`spur_agent`/`spur_verify` roles `deploy.yml` uses (so install/config/health-check logic isn't duplicated):

1. **Guard rail**: refuses to start if `spur_wipe_state=true` is set, or if the cluster isn't already healthy (leader elected, `spur nodes` reachable).
2. **Controllers, one at a time** (`serial: 1`, aborts the whole run on the first failure): force-reinstall, restart `spurctld`, and reuse the existing HA-aware "wait for Raft leader" task as the health gate before moving to the next controller. Client-side failover (every agent/CLI is pointed at *every* controller — see HA above) means the other controllers keep serving while one is down.
3. **Agents, in configurable batches** (`spur_rolling_batch_size`, default `1`): `spur node drain <node>` first, poll until it reaches `DRAINED` (no running jobs left on it), then force-reinstall and restart `spurd`, wait for it to re-register, and `scontrol update NodeName=<node> State=RESUME` to bring it back into service.
4. **Verify**: submits a real test job at the end to confirm the upgraded cluster actually schedules work.

Same binary-compatibility caveat as above applies — rebuild all three together. `spur_rolling_batch_size` trades speed for blast radius: `1` (default) disrupts at most one agent's capacity at a time; a larger value upgrades faster but drains more capacity concurrently.

---

## Managing the cluster after deploy

Daemons are systemd services, so use the normal tools on any host:

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

Teardown stops and disables the systemd services and reaps any stray daemons. It leaves PostgreSQL installed and the accounting database intact — to drop the DB, do it manually on the first controller (`sudo -u postgres dropdb spur; sudo -u postgres dropuser spur`).

---

## Hard-won gotchas baked into these roles

These are real bugs we hit during validation — listed so anyone reading the playbook understands why the roles look the way they do.

### Daemon / process management
- **Daemons run as systemd units**, not `nohup` — `/etc/systemd/system/spur{ctld,d}.service` (`spurdbd.service` only exists as a leftover from a pre-merge deployment, which `spur_accounting` cleans up on the accounting host), `enabled` (survive reboot), `Restart=on-failure`. The roles `daemon-reload` after templating a unit and use `state: restarted` so a redeploy always picks up new binaries/config.
- **`-D` is `--foreground`, not "daemonize"** (`crates/spurctld/src/main.rs`). The systemd units run the binary in the foreground under `Type=simple`, which is correct — do not add `-D`.
- **`pkill -f spurd` also kills `spurctld`** (substring match). Teardown uses `pkill -x` (exact name) only.
- **Stop-before-wipe ordering.** The controller role stops spurctld *before* wiping `~/spur/state`, so the daemon can't rewrite the Raft log mid-delete.

### Install
- **Upstream `install.sh` returns 403** because ROCm/spur has no published release yet. Use `spur_binary_src` to push locally-built binaries. The symlink + verify tasks run on both paths.
- **`spur --version` is not a supported flag** — it errors. The roles check for the binary with `stat`, not by running `--version`.
- **Slurm-compat symlinks** — all 18 names the `spur` multi-call binary recognizes via argv[0] dispatch (`sbatch`, `squeue`, `sinfo`, `scancel`, `sacct`, `sacctmgr`, `scontrol`, `salloc`, `srun`, `sattach`, `scrontab`, `sdiag`, `smd`, `sprio`, `sreport`, `sshare`, `sstat`, `strigger`) → `spur` — are created regardless of install source.
- **A stale dpkg lock** during the accounting apt install means another apt/unattended-upgrade is running — wait for it (or clear a genuinely hung `apt-get`); don't `--force`.

### Accounting
- **Accounting is embedded in `spurctld`** (upstream folded the standalone `spurdbd` daemon in) — it's served on the controller's own gRPC port whenever `[accounting].database_url` is non-empty, so Postgres must be reachable and migrated *before* spurctld starts, not the other way around. The `spur_accounting` role runs before the controller role for this reason.
- **Every controller needs network access to Postgres, not just localhost** — each spurctld connects to the database directly (there's no longer a single co-located daemon brokering it), so `spur_accounting` configures `listen_addresses`/`pg_hba.conf` for every controller's IP.
- **Migrations run automatically on spurctld startup** against the configured `database_url`; a failed migration disables accounting for that controller rather than crashing it (job scheduling still works, `sacct` won't).
- **With `spur_wipe_state=true`, the Raft job-id counter resets each deploy**, so re-deploys reuse job ids 1, 2, … which upsert onto the same accounting rows. Set `spur_wipe_state=false` to preserve job history.
- **Upgrading a pre-merge cluster** leaves a stale `spurdbd` systemd unit/binary/process behind if nothing removes it — `spur_accounting`'s tasks stop, disable, and delete both unconditionally (even if `spur_accounting_enabled=false`) on the accounting host. The old `spur_binary_src` push copied `spurdbd` to *every* host, not just the accounting host, even though it only ran as a service there — `spur_install` (runs on every controller/agent) separately sweeps the stray binary fleet-wide.

### Spur quirks
- **Job stdout file lands in spurd's working directory at startup.** The `spurd` systemd unit sets `WorkingDirectory={{ spur_home }}` so output is predictably at `{{ spur_home }}/spur-<JOBID>.out`, even though `spur show job` reports the submitter's `WorkDir=`.
- **The single-node job's host is unpredictable.** The backfill scheduler picks any idle node, so `spur_verify` searches every agent rather than assuming the controller.
- **No shared-FS assumption in multi-node verify.** The play fetches `spur-<JOBID>.out` from each agent, not just the controller.
- **`spur nodes` collapses by partition** — the "NODES" column is a count, not one row per host. To confirm each expected host registered, loop `spur show node <name>`.
- **`spur show job` uses `JobState=COMPLETED` (uppercase)**, not `State: Completed`. The wait-loop greps `JobState=[A-Z]+`.
- **Harmless log spam `invalid transition from Completed to Completed`** on followers after a multi-node job (`crates/spurctld/src/cluster.rs`). The leader already reported terminal state; the follower's redundant report is rejected. The job succeeded.
- **Harmless `ERROR`-level openraft log line on every spurctld restart** — `Can not initialize last_log_id=Some(...) vote=...:committed`. Normal on a restart with existing Raft state; judge success from `spur nodes`/job/accounting behavior, not this line.
- **Harmless spurd startup warning `failed to load spur.conf ... path=/etc/spur/spur.conf`** — `spurd` takes all its config via CLI flags and never reads a config file; this warning is always present.
- **Raft port 6821 is hardcoded** in spurctld and isn't a flag in 0.3.0. Preflight checks it alongside 6817/6818.

### Multi-node / HA specifics
- **Agent `--hostname` must match the `[[nodes]]` name in `spur.conf`.** The templates use `ansible_hostname` (short) on both sides.
- **HA needs a leader-elected wait**, not just a port-listening wait. `spurctld` binds `:6817` instantly but returns `Status::unavailable("no leader elected yet")` until quorum forms. The controller role loops on that message in `spur nodes` until it clears.
- **`peers` list order matters across controllers.** `node_id` is the 1-based position; openraft refuses to start if a node's `node_id` doesn't match its position. The role derives both from `groups['spur_controllers']` — don't reorder that group between deploys without wiping state.
- **`delegate_facts: true` evaluates `set_fact` on the controller, not the delegated target.** Use `hostvars[item].ansible_hostname` when setting a per-host fact via `delegate_to`.
- **`ansible.builtin.command` runs without a shell** — `command -v X` fails (bash builtin). Use `ansible.builtin.shell` with `executable: /bin/bash`.

### Rolling upgrade specifics
- **`spur node drain` doesn't kill running jobs** — it stops new scheduling onto the node and transitions it `DRAINING → DRAINED` once its running jobs finish on their own. `rolling_upgrade.yml` polls for `DRAINED` before touching the agent's binary or restarting `spurd`.
- **`spurd` deregisters gracefully on SIGTERM**, so `systemctl restart spurd` (which sends SIGTERM first) doesn't leave a stale node entry — but only do this after the node is actually drained, or its currently-running jobs get evicted (`NODE_FAIL`).
- **A drained node stays drained after `spurd` restarts** — draining is server-side state, not tied to the agent process. `rolling_upgrade.yml` explicitly `scontrol update NodeName=<node> State=RESUME`s each node after its upgrade; forgetting this step leaves upgraded capacity sitting idle.
- **Controllers restart one at a time (`serial: 1`), never in parallel**, so Raft quorum is never lost — with 3 controllers, restarting 1 still leaves 2 for quorum. This is why HA production clusters need `spur_ha_enabled` (≥ 3 controllers): a single-controller cluster has no other controller to fail over to during its own restart, so a rolling upgrade of it is a (short) outage no matter what.

If you hit a new gotcha, please add it here and (where applicable) encode the fix in the roles.

---

## Verified

`spur 0.3.0`, Ubuntu 22.04, validated across all four topologies on a 4-node cluster (fresh install — PostgreSQL purged beforehand), Ansible run from an operator workstation. Every scenario ran basic commands, drove a sample job to `COMPLETED`, confirmed all daemons `active` under systemd, and recorded jobs in PostgreSQL via `sacct`:

- **Single-node** — controller + agent on one host; job COMPLETED, accounted.
- **Multi-node** — 1 controller + 2 agents; `-N 2` job fanned across both nodes, accounted.
- **HA hyperconverged** — 3 controllers (Raft) + 3 agents; leader elected, `-N 3` job across all three, accounted.
- **HA + separate compute** — 3 dedicated controllers (no agent) + 1 dedicated agent; leader elected, controllers ran no `spurd`, job ran on the separate compute node, accounted.
- **Accounting disabled** (`-e spur_accounting_enabled=false`) — PostgreSQL skipped, job still COMPLETED.

WireGuard transport is supported via `roles/spur_wireguard`. Enable it with `-e spur_transport=wireguard`. It is **single-controller only** (`spur net init` auto-assigns the controller `.1`; there is no multi-controller mesh command), so HA runs over `direct`. Agents auto-assign `.2`, `.3`, … Requires the `ansible.utils` collection (`ansible-galaxy collection install -r requirements.yml`).

**Merged-accounting migration and `rolling_upgrade.yml` are verified on a narrower footprint than the above** — a live 2-node lab (single controller + 2 agents, `direct` transport, accounting co-located with the controller, accounting enabled, default `spur_rolling_batch_size`), not the full 4-topology matrix:

- Migrating an already-running pre-merge cluster (separate `spurdbd`) to the merged-accounting build via `deploy.yml` — `spurdbd` fully removed, `pg_hba.conf`/`spur.conf` regenerated correctly, job history preserved, jobs COMPLETED post-migration.
- A genuinely fresh install directly onto the merged-accounting build via `deploy.yml` (not a migration) — same checks, from a clean slate.
- `rolling_upgrade.yml` upgrading a merged-accounting cluster to a further build, from both of the states above, with **real in-flight jobs**: confirmed the agent drain actually blocks on running jobs (observed `DRAINING` holding for the jobs' full duration, only reaching `DRAINED` once they completed) rather than being a formality, and that nodes correctly `RESUME` afterward with no stuck `DRAIN` state.
- **Not yet lab-verified**: HA (≥ 3 controllers) or wireguard transport with either the migration path or `rolling_upgrade.yml` (the latter explicitly refuses to run under `spur_transport=wireguard` rather than risk an unverified path — see the Rolling upgrade section above), a dedicated (non-controller/non-agent) accounting host, `spur_accounting_enabled=false`, and `spur_rolling_batch_size > 1`.
