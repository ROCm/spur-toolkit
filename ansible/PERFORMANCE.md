# Fleet task and remote-call review

This review covers all nine playbooks and their shared roles. Counts compare
against the initial optimized in-place implementation (`23bc075`). They describe
work removed, not a measured wall-clock speedup. No new operator mode or tuning
variable is required.

An in-place binary replacement can skip provisioning. A deployment must still
configure files, services, authentication and networking. The shared rule is to
prepare identical inputs once, omit disabled workflows before expanding their
tasks, and combine independent probes without removing safety barriers.

| Playbook | Change | Behavior retained |
|---|---|---|
| `deploy.yml` | Shared installation preparation, MPI discovery, endpoint construction and batched registration verification; merged auth preflight and controller identity facts. | Full convergence, config preservation/overwrite, default force behavior, skip-busy option, controller auth-switch barrier and unreachable-agent policy. |
| `add_nodes.yml` | Skip the entire mesh play on direct transport; dynamically include only required roles; one registration query for the candidates and one fleet query per readiness attempt. | Existing agents are not restarted; resolved node names, including inventory aliases, are checked; controller config refresh and full mesh updates remain. |
| `rolling_upgrade.yml` | Existing binary fast path retained; configuration fallback benefits from shared role changes. Controller health uses the preserved daemon config, with underlying errors included in timeouts. An unhealthy existing service fails immediately instead of entering provisioning. | Controller serialization/leader ordering, batching, native-auth carry-forward, binary identity verification and the original provisioning fallback. |
| `remove_nodes.yml` | One shared drain polling window instead of a sequential timeout per node; identity-only facts; cleanup tasks expand only on selected agents; no mesh play under direct transport or repeated mesh fact gather. | Refuse/skip/force decisions, workload cleanup before daemon stop, supervisor cleanup after stop, authoritative removal and surviving mesh-member updates. |
| `teardown.yml` | Combine service stop and stray-daemon cleanup; omit native-auth cleanup tasks on non-wiping or non-auth runs. | k0s reset before stopping Spur, optional drain, workload/daemon/supervisor order, session preservation without wipe and shared-key protection. |
| `healthcheck.yml` | One remote probe per host role. Controller tasks: 10 → 2; agent tasks: 11 → 3; accounting tasks: 4 → 2, with the disabled accounting play omitted. | Service/port, controller/queue/accounting, auth socket/key, cgroup, optional supervisor binary and disk checks; failures remain nonzero, and only explicitly ignored unreachable agents are skipped. |
| `k8s_up.yml` | Print the status snapshot that satisfied readiness; remove the duplicate final query. | Rejected requests and degraded/failed status remain failures; provisioning timeout unchanged. |
| `k8s_add_nodes.yml` | One registration query for all requested workers; reuse the ready snapshot. | Exact node membership, rejection checks and readiness polling. Nonzero CLI exits cannot be mistaken for success even if stdout contains a ready phase. |
| `manage_accounts.yml` | Reviewed; unchanged. Its work runs on one controller, not across the fleet. | Entity-specific argv validation and QoS → account → user creation order, reversed for deletion. There is no bulk CLI operation to substitute without introducing a separate executor. |

## Shared costs removed

- Installation has 11 task definitions instead of 14. Optional binary discovery,
  initial file hashing and packaging run once per distinct source/component set
  in a batch. A delegated fact holds the result instead of copying the entire
  manifest into every host's facts. Bundle assembly still verifies source bytes.
- MPI discovery no longer launches four local `stat` modules for each agent:
  a 200-agent batch sharing one source avoids 800 repeated local probes.
- Agent controller endpoints are constructed in one expression, without one
  `set_fact` result per controller per agent. Repeated role use does not append
  duplicate endpoints.
- Verification uses one `show node` query per batch, rather than one delegated
  query per agent. Failed snapshots clear previous state instead of reporting
  stale registrations. Retryable query failures share the configured deadline.
- Authentication's clock, NSS and binary checks share one read-only remote call.
  Time-sync/NSS warnings and the missing-binary failure remain, including in
  check mode. Key distribution and coordinated service startup remain separate.
- HA mesh setup reuses the public keys already read during init/join, removing
  another remote key read and a fact-publication pass across every member.

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
  existing services without re-provisioning or changing their binaries.

All existing CI job commands/assertions remain. A healthcheck container smoke
test is added to `integration-conf`; a compatibility job runs the local suite on
Ansible 2.17 and 2.19 alongside the existing default-version lint job. The live
container integration jobs are retained but require CI execution; local fixture
results do not establish fleet timing, HA behavior or workload survival.
