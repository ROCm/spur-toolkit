# Fleet task and remote-call review

This review covers all nine playbooks and their shared roles. Counts compare
against the initial optimized in-place implementation (`23bc075`). They describe
work removed, not a measured wall-clock speedup. No new operator mode or tuning
variable is required.

In-place upgrades and deployments both reconcile their applicable provisioning
roles, including files, services and authentication. The shared rule is to
prepare identical inputs once, omit disabled workflows before expanding their
tasks, and combine independent probes without removing safety barriers.

| Playbook | Change | Behavior retained |
|---|---|---|
| `deploy.yml` | Shared installation preparation, MPI discovery, endpoint construction and batched registration verification; merged auth preflight and controller identity facts. | Full convergence, config preservation/overwrite, default force behavior, skip-busy option, controller auth-switch barrier and unreachable-agent policy. |
| `add_nodes.yml` | Skip the entire mesh play on direct transport; dynamically include only required roles; one registration query for the candidates and one fleet query per readiness attempt. | Existing agents are not restarted; resolved node names, including inventory aliases, are checked; controller config refresh and full mesh updates remain. |
| `rolling_upgrade.yml` | Local in-place builds are prepared and staged before rolling; shared provisioning roles reuse those bundles and reconcile the full installation. Read-only final checks verify every target artifact and running daemon. Unhealthy existing services fail before provisioning; the tolerance flag continues later batches and reports failures at the end. | Controller serialization/leader ordering, batching, native-auth carry-forward, binary identity verification and full provisioning, unit reload/enable/restart, auth and safety checks. |
| `remove_nodes.yml` | One shared drain polling window instead of a sequential timeout per node; identity-only facts; cleanup tasks expand only on selected agents; no mesh play under direct transport or repeated mesh fact gather. | Refuse/skip/force decisions, workload cleanup before daemon stop, supervisor cleanup after stop, authoritative removal and surviving mesh-member updates. |
| `teardown.yml` | Combine service stop and stray-daemon cleanup; omit native-auth cleanup tasks on non-wiping or non-auth runs. | k0s reset before stopping Spur, optional drain, workload/daemon/supervisor order, session preservation without wipe and shared-key protection. |
| `healthcheck.yml` | One remote probe per host role. Controller tasks: 10 → 2; agent tasks: 11 → 3; accounting tasks: 4 → 2, with the disabled accounting play omitted. | Service/port, controller/queue/accounting, auth socket/key, cgroup, optional supervisor binary and disk checks; failures remain nonzero, and only explicitly ignored unreachable agents are skipped. |
| `k8s_up.yml` | Print the status snapshot that satisfied readiness; remove the duplicate final query. | Rejected requests and degraded/failed status remain failures; provisioning timeout unchanged. |
| `k8s_add_nodes.yml` | One registration query for all requested workers; reuse the ready snapshot. | Exact node membership, rejection checks and readiness polling. Nonzero CLI exits cannot be mistaken for success even if stdout contains a ready phase. |
| `manage_accounts.yml` | Reviewed; unchanged. Its work runs on one controller, not across the fleet. | Entity-specific argv validation and QoS → account → user creation order, reversed for deletion. There is no bulk CLI operation to substitute without introducing a separate executor. |

## Shared costs removed

- Extraction and binary housekeeping share one remote call. Optional binary discovery,
  initial file hashing and packaging run once per distinct source/component set
  in a batch. A delegated fact holds the result instead of copying the entire
  manifest into every host's facts. Bundle assembly still verifies source bytes.
- MPI discovery no longer launches four local `stat` modules for each agent:
  a 200-agent batch sharing one source avoids 800 repeated local probes.
- Agent address and controller endpoints are resolved together, without one
  `set_fact` result per controller per agent. Repeated role use does not append
  duplicate endpoints.
- Verification, including the in-place activation path, uses one `show node` query per batch, rather than one delegated
  query per agent. Failed snapshots clear previous state instead of reporting
  stale registrations. Retryable query failures share the configured deadline.
- Staged in-place upgrades avoid a second packaging/upload pass. Matching
  installed binaries avoid extraction, while provisioning and service restarts
  still run. Final manifest verification never writes or restarts anything.
- Authentication's clock, NSS and binary checks share one read-only remote call.
  Time-sync/NSS warnings and the missing-binary failure remain, including in
  check mode. Key distribution and coordinated service startup remain separate.
- HA mesh setup reuses the public keys already read during init/join, removing
  another remote key read and a fact-publication pass across every member.

## Agent provisioning consolidation

Agent configuration now uses one remote module invocation for the CLI environment,
configuration file, systemd unit, private supervisor-state directory, and MPI
plugin. Templates are still rendered by Ansible. Existing configuration remains
untouched unless overwrite is requested; managed files are replaced atomically
only when their contents differ, and required permissions are reconciled.
Environment updates preserve unrelated lines. File symlinks retain the original
lineinfile/template behavior, and check mode reports changes without writing.

Local-build installation includes the optional MPI plugin in the shared binary
archive. Agents copy it from that staged file to the configured plugin directory,
avoiding a separate upload per host. An absent plugin remains optional; a plugin
that was selected for staging but is missing remotely fails before configuration.
Upstream-installer layouts and disabled MPI remain supported.

An unchanged archive is not proof that installed files still match it: an
in-place upgrade may have replaced those files since the last deployment.
Installation checks their hashes against the prepared manifest in its existing
remote call and re-extracts a cached archive when files differ or are missing.
The mixed-source fixture covers this transition and damaged staged MPI files.
A separate fixture checks that ignored unreachable copy results, which have no
archive destination, do not become fatal template errors.

Cgroup validation still precedes configuration, and deployment still restarts
selected agents even if every file is already current. Drain/kill/skip decisions,
registration checks, binary verification, and test-job coverage are unchanged.
The new module has six filesystem regression tests; the existing mixed-source
installation fixture now exercises the consolidated configuration task. CI also
introduces file drift and asserts that deployment repairs it, preserves unrelated
environment settings, retains an unchanged unit's inode, and restarts the daemon.

## Boundaries deliberately retained

Fresh provisioning still needs package installation, file transfers, permissions,
service startup, key distribution and mesh application. The full controller
configuration needs facts for all agents, so add-nodes retains its initial fact
gather. Removal still resolves real hostnames to match registered names to the
inventory; it gathers only identity facts for that purpose.

Drain/resume and workload cleanup remain per-node in deployment and configuration
upgrades. They carry job-safety decisions, unlike a registration snapshot. The
verification role still submits and checks real single-node/multi-node jobs;
this review does not remove those checks to reduce runtime. The account manager's
per-entity operations and WireGuard's actual peer changes are also retained.

## Validation and CI coverage

`python3 -m unittest discover -s tests/ansible -v` includes the existing upgrade
suite plus fleet fixtures. The fixtures exercise production task files and the
real add-nodes, healthcheck and k8s playbooks, with temporary files, local
connections and fixture commands replacing live services. Coverage includes:

- shared artifacts across 200 specifications, mixed sources, controller/agent
  unions, login hosts, optional older binaries, MPI and unchanged reruns;
- exact registration names, shared drain deadlines, transient query failures,
  removal's refuse/skip/force decisions and stale-snapshot clearing;
- already-registered add-node skips, alias handling, and disabled mesh/auth roles;
- health success/failure/check mode, ignored versus strict unreachable agents,
  native-auth preflight warnings and missing binaries;
- k8s registration/query counts and nonzero CLI failures;
- the existing binary activation, authentication restart order, idempotency,
  stale executable, unsafe unit, corrupt archive and provisioning fallback tests;
- controller health using its preserved daemon config despite stale CLI auth
  configuration, informative readiness errors, and early refusal of unhealthy
  existing services without re-provisioning or changing their binaries;
- ordinary agent failures with serial batches of one and two: strict aborts,
  opt-in continuation through later batches, and a nonzero final result;
- unchanged activation without temporary binary writes, and rejection of corrupt
  archive bytes even when every installed binary already matches the manifest;
- check-mode comparison of installed and running binaries without a staged remote
  archive, executable writes, restarts, or post-upgrade verification.

All existing CI job commands/assertions remain. A healthcheck container smoke
test is added to `integration-conf`; a compatibility job runs the local suite on
Ansible 2.14, 2.17 and 2.19 alongside the existing default-version lint job. The lab
validation below is separate from the deterministic fixture suite.

The local suite and all nine playbooks are checked on Ansible 2.14, 2.17 and 2.19. Ansible 2.14 uses Ubuntu
22.04's Python 3.10 in CI to stay within its supported control-node Python range.

## Additional regression coverage

The existing CI jobs and their shell commands/assertions are retained. The
container configuration job now also stops one agent deliberately and checks
that an in-place run with the existing tolerance flag processes later serial
batches, leaves the failed agent stopped, and returns a nonzero final result.
Local fixtures cover this policy with batches of one and two.

Workload cleanup rejects PID 0, PID 1 and negative PIDs before signaling or
identifying jobs for cancellation. This matters with shared cgroup mounts:
processes outside a container's PID namespace appear as zero, which would
otherwise signal the cleanup process group. An isolated process-group test
exercises the production cleanup script. Another real-task fixture checks that
idle cleanup does not expand controller reconciliation tasks.

## Live validation

The scale fixture has three HA controllers and 200 workers. Historical timings
for the earlier binary-only implementation do not describe the restored full
convergence path. Its interleaved unchanged in-place run took 2m59.78s with batch
200 and 40 forks. The earlier grouped-inventory 5m31.87s run exited nonzero on an
unhealthy worker and is not a successful benchmark.

Prior measured full deployment improved from 11m15.39s to 8m41.97s (23% faster),
with 100 busy nodes skipped and 100 idle nodes restarted. A full force upgrade
with batch 200 took 11m31.43s and killed 100 jobs; teardown took 3m13.04s. These
measurements precede the restoration of provisioning on staged in-place runs.

Removal and re-addition of one idle worker through the full inventory passed
(1m03s and 1m41s). The full deployment test with busy-node skipping passed:
100 busy workers kept their daemon/supervisor PIDs and jobs; 100 idle workers
restarted. That deployment measured 13m32s before the last extraction/fact/task
merges. Final health and binary verification passed on all 203 hosts. Binary
verification accepts a re-extracted identical executable despite its new inode,
while still rejecting different running bytes.

A separate container cluster exercised fresh deployment, native authentication,
PMIx deployment, PostgreSQL account/QoS/user creation and deletion, Kubernetes
startup and worker addition, cross-worker pod traffic, force-upgrade workload
termination, and Kubernetes reset before teardown. Full cleanup used the real
teardown playbook. Default JWT Kubernetes operations passed; native-auth worker
addition exposed an upstream CLI authentication defect and remains blocked on
that binary fix. Shared host cgroup mounts also exposed an upstream daemon
cleanup bug (signaling PID zero). Isolating test-container cgroups avoids that
fixture collision; the playbook cleanup now rejects zero/negative PIDs itself.

## In-place convergence regression coverage

The real-playbook fixture repairs unit and PMIx drift, reloads/enables/restarts
services, restores symlinks and supervisor-state directories, preserves existing
configuration, verifies target artifacts, and rejects drain/kill/resume commands.
The helper suite separately checks corrupt archives and read-only verification
of disk/running drift. Custom Ansible configuration discovery is covered.

Container CI introduces unit/environment/symlink/plugin drift before the HA
in-place upgrade while retaining job survival, supervisor identity, leader order
and no-drain assertions. Native-auth CI changes all executable checksums and
checks auth/daemon restarts, key permissions, CLI configuration and PMIx delivery.
Its small shared-library fixture tests delivery, not MPI runtime behavior.
