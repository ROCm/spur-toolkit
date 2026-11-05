This fixture guards local artifact ownership and remote installation permissions
with an actual `ubuntu` Ansible launcher, SSH, sudo, and systemd. All writes occur
in disposable containers. It does not connect to an existing cluster.

Run from a checkout on a Linux Docker host with cgroup v2:

```sh
python3 tests/integration/nonroot_upgrade/run.py --logs /tmp/nonroot-upgrade-logs
```

The driver downloads the published v0.14.0 and v0.15.0 Linux amd64 archives,
checks their published SHA-256 sums, and builds a test image. Downloads and package
installation happen during setup; the scenarios use local binary sources.
The image defaults to `ubuntu`; the driver explicitly uses root to boot systemd
on the managed hosts and to provision the runner's files before testing.
No SSH port is published to the host. A generated key is limited to the fixture,
and known host keys are obtained from the newly created containers. Containers
and their network are removed in `finally`, including after test failures.

The integration phase extracts the production preparation and copy tasks for
both install and in-place upgrade. It enables sudo in inventory, exercises real
root-owned remote staging, and verifies that the local caches and archives remain
owned by `ubuntu` with modes `0700` and `0600`. Both the implicit localhost and the
temporary explicit `[ansible_control]` localhost workaround are covered. The
second pass must reuse the same archive bytes, inode, and modification time.

The end-to-end phase deploys v0.14.0 with `deploy.yml`, then invokes the complete
`rolling_upgrade.yml` in-place path to install v0.15.0. The runner and SSH logins
have UID 1000; `/root` stays mode `0700`, and sudo resets HOME to `/root`. Assertions
check installed and running binary hashes, actual daemon restarts, root-owned
executables and units, configuration symlink preservation, cache reuse, and
successful jobs executed as UID 1000 before and after the upgrade. An unchanged
rolling rerun includes the explicit localhost workaround.

Both phases include a negative control: remove only the local `ansible_become`
task-variable override in an isolated copy. Real sudo then creates an archive in
`/root/.cache`, and the unprivileged uploader must fail to read it. For the complete
rolling playbook, service PIDs, binary hashes, and configuration must be unchanged
after this failure. Positive runs must create no root-local bundle caches.

CI runs this on Ansible 2.14.18 / Ubuntu 22.04, 2.17.14 / Ubuntu 24.04, and
2.19.14 / Ubuntu 24.04. The 2.14 case retains its supported Python runtime.
Override locally with `--ansible-core` and `--ubuntu`.

This fixture covers local filesystem permissions, SSH, passwordless sudo, and
the local-build upgrade path. NFS export policies, password-based sudo, native
authentication, and HA/job-survival behavior retain their separate coverage or
require additional fixtures. Global `-e ansible_become=true` is intentionally
excluded because extra vars override the production local opt-outs.
