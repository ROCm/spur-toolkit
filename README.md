# spur-toolkit

Tools to deploy and debug [Spur](https://github.com/ROCm/spur), an AI-native job scheduler.

Spur's source code, build instructions, and releases live in [ROCm/spur](https://github.com/ROCm/spur). This repo is downstream of it — it doesn't build Spur, it operates on binaries/installers produced by that repo.

This repo is also packaged as a [Claude Code](.claude-plugin/plugin.json) / [Cursor](.cursor-plugin/plugin.json) plugin, so its skills can be installed directly without cloning the whole repo.

## Contents

- **[`ansible/`](ansible/README.md)** — self-contained Ansible playbooks for bare-metal deployment. `playbooks/deploy.yml` covers single-node, multi-node, HA (multi-controller Raft), and HA-with-separate-compute topologies, with optional PostgreSQL/spurdbd accounting and WireGuard mesh; `playbooks/teardown.yml` tears a cluster back down. All playbooks share the same `roles/` and `inventory/`.
- **[`skills/deploy-spur/`](skills/deploy-spur/SKILL.md)** — standalone Claude Code skill that drives the same deployment with only SSH + bash, no Ansible required. Useful for ad-hoc or one-off deploys.

The Ansible and skill paths reach the same end state (systemd-managed daemons, Slurm-compatible CLI symlinks) and are independent of each other — using one doesn't require the other to be present.

## Relationship to ROCm/spur

Building the `spur`/`spurctld`/`spurd` binaries still happens in [ROCm/spur](https://github.com/ROCm/spur) (`cargo build --release -p spur-cli -p spurctld -p spurd`). Release tarballs also include `spur_mpi_pmix.so`; the Ansible playbooks install it on agents at `/usr/lib/spur/`. See `ansible/README.md` for the full clone-build-stage-deploy walkthrough. This repo only consumes those binaries (via `spur_binary_src`/`SPUR_BINARY_SRC`) or the upstream `install.sh`.
