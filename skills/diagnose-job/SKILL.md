---
name: diagnose-job
description: >
  Use when a user asks why a Spur (or Slurm-compatible) job failed, is stuck, won't
  start, crashed, timed out, got OOM-killed, or is otherwise misbehaving. Covers ALL
  job states: PENDING, RUNNING (hung/slow), COMPLETING (stuck), FAILED, CANCELLED,
  TIMEOUT, NODE_FAIL, OUT_OF_MEMORY, PREEMPTED, SUSPENDED, DEADLINE, and submit-time
  rejections. Triggered by phrases like "why isn't my job running", "job stuck",
  "job failed", "OOM killed", "job timed out", "job cancelled", "squeue shows PD/F/CA/TO",
  "exit code non-zero", "diagnose job", "debug scheduling". Accepts a job ID or inspects
  all problem jobs when none is given. Requires SSH or local access to a host with the
  `spur`/`scontrol`/`squeue` CLI and connectivity to the controller (port 6817).
---

# diagnose-job — Full job lifecycle diagnostician

You are a Spur cluster job diagnostician. Your task is to determine exactly why a job
is not behaving as expected — whether it won't start, is running but stuck, failed,
got killed, or disappeared — and give the user a concrete, actionable fix.

Spur is a Slurm-compatible job scheduler. Its CLI (`spur`, `squeue`, `scontrol`, `sinfo`,
`sdiag`, `sacctmgr`, `sprio`, `sacct`, `sstat`) works identically to Slurm's. All
commands below work on both Spur and Slurm clusters.

## Job state reference

| State | Code | Meaning |
|-------|------|---------|
| PENDING | PD | Waiting to be scheduled |
| RUNNING | R | Executing on node(s) |
| COMPLETING | CG | Process done, epilog/cleanup in progress |
| COMPLETED | CD | Finished successfully (exit 0) |
| FAILED | F | Exited non-zero |
| CANCELLED | CA | Killed by user/admin/dependency |
| TIMEOUT | TO | Hit wall-time limit |
| NODE_FAIL | NF | Node died mid-execution |
| OUT_OF_MEMORY | OOM | Killed by cgroup OOM |
| PREEMPTED | PR | Evicted by higher-priority job |
| SUSPENDED | S | Paused (admin or preempt-suspend) |
| DEADLINE | DL | Missed --deadline window |
| REQUEUED | RQ | Transient — returns to PENDING |

## Node state reference

| State | Display | Schedulable? | Meaning |
|-------|---------|-------------|---------|
| Idle | idle | Yes | No jobs running |
| Allocated | alloc | No | All CPUs in use |
| Mixed | mix | Yes (partial) | Some CPUs free |
| Down | down | No | Heartbeat lost or admin-set |
| Drain | drain | No | Admin hold, no new jobs |
| Draining | drng | No | Finishing current jobs, then drain |
| Error | err | No | Agent error / crash loop |
| Unknown | unk | No | Not yet registered |
| Suspended | susp | No | Power-managed / suspended |

**Overlays** (displayed as suffixes in `sinfo`, e.g. `idle+PLANNED`):
- `+PLANNED` (`plnd`) — backfill scheduler reserved a future slot on this node
- `+RESERVED` (`resv`) — held by an admin reservation
- `+MAINTENANCE` (`maint`) — reserved for maintenance

---

## Step 0 — Gather context

Ask the user only what you cannot infer:

| Need | How to get it |
|------|---------------|
| **Job ID** | Ask if not provided. If "all my jobs" or "everything is broken", scan broadly. |
| **Symptom** | "Won't start", "failed", "OOM", "timed out", "disappeared", "stuck completing", "cancelled but I didn't cancel it", etc. This determines which diagnostic path to follow. |
| **Access method** | Local CLI or SSH? If SSH, get hostname + jump host. |
| **Controller endpoint** | Default `http://localhost:6817`. Only ask if non-standard. |

```bash
JOB_ID=<id>   # leave empty for broad scan
```

---

## Step 1 — Snapshot cluster and job state

Run these in parallel:

```bash
# 1a. Full job details
if [ -n "${JOB_ID}" ]; then
  scontrol show job "${JOB_ID}"
else
  squeue --format="%18i %12j %10u %8T %12r %10M %10l %6D %R"
fi

# 1b. Node overview (all states including alloc, mix, idle overlays)
sinfo -N --format="%20N %10P %6t %10e %10m %20G %30f %20E"

# 1c. Partition state
scontrol show partition

# 1d. Scheduler health
sdiag
```

For a specific job, also run:

```bash
# 1e. Accounting record (works for finished jobs too)
# Note: sacct supported fields are JobID,JobName,User,Account,Partition,State,
#       ExitCode,DerivedExitCode,Elapsed,NNodes,NCPUS,QOS,TimeLimit,NodeList,
#       Start,End,Submit,PreemptedBy,PreemptMode,PreemptQOS
sacct -j "${JOB_ID}" --format=JobID,JobName,State,ExitCode,Elapsed,NodeList,Start,End

# 1f. Live resource usage for RUNNING jobs (MaxRSS, MaxVMSize are sstat fields, not sacct)
sstat -j "${JOB_ID}" 2>/dev/null

# 1g. Priority breakdown (pending only)
sprio -j "${JOB_ID}" 2>/dev/null

# 1h. Projected start time (pending only)
squeue --start -j "${JOB_ID}" 2>/dev/null
```

> **If `scontrol show job` returns "Invalid job id"**: the job already completed and
> was purged from controller memory (`terminal_job_retention_secs`, default 3600).
> Use `sacct -j <id>` instead — it queries the accounting database.

Now branch on the job's **state**.

---

## PENDING jobs (State=PD)

Extract the `Reason=` field from `scontrol show job`. This is the primary diagnostic
signal. Match it to one of these categories:

### A. Resource shortage — `Resources`

All suitable nodes are busy. The job is next in line but no capacity exists.

```bash
squeue --states=R --sort=-T --format="%18i %12j %10u %10M %10l %6D %R" | head -30
# Note: plnd is an OVERLAY on idle nodes (+PLANNED), not a separate state
sinfo -N --format="%20N %10P %6t %20G %30f"
```

**Diagnosis:** Show busy (alloc/mix) vs idle vs backfill-reserved (idle+PLANNED) nodes.
Report projected start from `squeue --start`. Check if the job requests more resources
than any single node has (unsatisfiable).

**Fixes:**
1. Wait — report projected start time.
2. Tighter `--time` estimates let backfill pack more efficiently.
3. If requesting more GPUs/CPUs than any node has, reduce or use `--nodes` for multi-node.
4. Preemptible QOS if the user can tolerate eviction.

### B. Priority starvation — `Priority`

Higher-priority jobs keep being scheduled first.

```bash
sprio -j "${JOB_ID}"
sprio --sort=-Y | head -20
sshare --user="${USER}" 2>/dev/null
```

**Fixes:**
1. Wait — priority increases with age.
2. Admin raises priority: `scontrol update JobId=${JOB_ID} Priority=<N>`
3. Move to higher `priority_tier` partition.
4. Enable preemption.

### C. Dependency — `Dependency` / `DependencyNeverSatisfied`

```bash
scontrol show job "${JOB_ID}" | grep -E "Dependency|JobId"
scontrol show job <dep_id> | grep -E "JobId|JobState|ExitCode"
```

- `DependencyNeverSatisfied`: target failed/cancelled/timed out. Fix: `scontrol release ${JOB_ID}` or resubmit.
- Circular dependency: cancel one leg.
- `afterok` on a job that exited non-zero: will never satisfy.

### D. Hold — `JobHeldUser` / `JobHeldAdmin` / `JobHoldMaxRequeue`

Internal enum names: `Held` (displays as `JobHeldUser`), `JobHeldAdmin`, `JobHoldMaxRequeue`.

```bash
scontrol release "${JOB_ID}"
```

`JobHoldMaxRequeue`: job hit `max_batch_requeue` (default 5). Check controller logs
for the root cause (prolog failure? node crash?) before releasing.

### E. Partition problems — `PartitionDown` / `PartitionInactive` / `PartitionNodeLimit` / `PartitionTimeLimit` / `PartitionConfig`

```bash
scontrol show partition <partition_name>
```

- `PartitionDown`/`PartitionInactive`: admin brings it up or user moves partitions.
- `PartitionNodeLimit`: `--nodes` exceeds partition `MaxNodes`.
- `PartitionTimeLimit`: `--time` exceeds partition `MaxTime`.
- `PartitionConfig`: partition configuration mismatch (ACLs, features, etc.).

### F. Node problems — `NodeDown` / `ReqNodeNotAvail` / `BadConstraints`

```bash
scontrol show job "${JOB_ID}" | grep -E "ReqNodeList|ExcNodeList|Features|ReqTRES"
sinfo -N --format="%20N %10P %6t %10c %10m %20G %30f %20E"
```

- `BadConstraints`: `--constraint` features match zero nodes. Show the mismatch.
- `ReqNodeNotAvail`: named nodes down/draining/wrong partition.
- Fix: remove constraints, or admin recovers nodes.

### G. QOS / Association limits

Each QOS/Association limit has its own pending reason with a specific display string.

**QOS limit reasons** (note: GPU limits display as `GRES`, not `GPU`):

| Reason display | Meaning |
|---------------|---------|
| `QOSMaxJobsPerUserLimit` | Running jobs per user exceeded |
| `QOSMaxCpuPerJobLimit` | CPUs per job exceeded |
| `QOSMaxNodePerJobLimit` | Nodes per job exceeded |
| `QOSMaxMemoryPerJob` | Memory per job exceeded |
| `QOSMaxGRESPerJob` | GPUs per job exceeded |
| `QOSMaxWallDurationPerJobLimit` | Wall-time per job exceeded |
| `QOSMaxCpuPerUserLimit` | Total CPUs across user's jobs exceeded |
| `QOSMaxNodePerUserLimit` | Total nodes across user's jobs exceeded |
| `QOSMaxMemoryPerUser` | Total memory across user's jobs exceeded |
| `QOSMaxGRESPerUser` | Total GPUs across user's jobs exceeded |
| `QOSMaxSubmitJobPerUserLimit` | Submit count per user exceeded |
| `QOSMaxSubmitJobPerAccountLimit` | Submit count per account exceeded |
| `QOSGrpCpuLimit` | Group-wide CPU aggregate exceeded |
| `QOSGrpNodeLimit` | Group-wide node aggregate exceeded |
| `QOSGrpMemLimit` | Group-wide memory aggregate exceeded |
| `QOSGrpGRES` | Group-wide GPU aggregate exceeded |
| `QOSGrpWallLimit` | Group-wide wall-time aggregate exceeded |
| `QOSGrpSubmitJobsLimit` | Group-wide submit count exceeded |

**Association limit reasons:**

| Reason display | Meaning |
|---------------|---------|
| `AssocMaxJobsLimit` | Running jobs for this association exceeded |
| `AssocMaxSubmitJobLimit` | Submit count exceeded |
| `AssocMaxCpuPerJobLimit` | CPUs per job exceeded |
| `AssocMaxNodePerJobLimit` | Nodes per job exceeded |
| `AssocMaxMemPerJob` | Memory per job exceeded |
| `AssocMaxGRESPerJob` | GPUs per job exceeded |
| `AssocMaxWallDurationPerJobLimit` | Wall-time per job exceeded |
| `AssocGrpCpuLimit` | Group CPU aggregate exceeded |
| `AssocGrpNodeLimit` | Group node aggregate exceeded |
| `AssocGrpMemLimit` | Group memory aggregate exceeded |
| `AssocGrpGRES` | Group GPU aggregate exceeded |
| `AssocGrpSubmitJobsLimit` | Group submit count exceeded |

**Checks:**
```bash
sacctmgr show user "${USER}" withassoc format=User,Account,MaxJobs,MaxSubmit,MaxTRES,GrpTRES
sacctmgr show qos format=Name,MaxJobsPU,MaxSubmitPU,MaxTRES,MaxTRESPU,GrpTRES,GrpJobs
squeue --user="${USER}" --states=R | wc -l
```

Identify which limit is hit, show current usage vs configured cap. Fix: wait, reduce
request, or admin raises limit.

### H. Deferred start — `BeginTime`

```bash
scontrol show job "${JOB_ID}" | grep -E "EligibleTime|StartTime"
```

Fix: `scontrol update JobId=${JOB_ID} EligibleTime=now`

### I. Reservation — `Reservation` / `ReqNodeNotAvail, Reserved for maintenance` / `ReservationDeleted`

```bash
scontrol show reservation
```

- `Reservation`: nodes held by a reservation the job can't access.
- `ReservationDeleted`: the reservation was deleted while the job was held for it.
- Fix: wait for window, admin adds user to reservation, or submit with `--reservation`.

### J. Job array throttle — `JobArrayTaskLimit`

Array `%N` concurrency cap reached. Fix: wait, or
`scontrol update JobId=${JOB_ID} ArrayTaskThrottle=<N>`

### K. License shortage — `Licenses`

```bash
scontrol show license
```

Fix: wait or reduce `--licenses` request.

### L. Launch failure — `JobLaunchFailure`

```bash
scontrol show job "${JOB_ID}" | grep -E "Reason|AdminComment"
```

Prolog failed, container pull failed, or agent rejected. Check controller logs, fix
root cause, then `scontrol release ${JOB_ID}`.

### M. Kubernetes reserved — `K8sReserved`

Nodes in managed k0s cluster are excluded. Use non-k0s nodes.

### N. Accounting / validation — `AccountingUnavailable` / `InvalidAccount` / `InvalidQOS`

- `AccountingUnavailable`: PostgreSQL down or association caches still loading after
  controller restart (10-30s). Check `systemctl status spurctld` and DB connectivity.
- `InvalidAccount`: account doesn't exist or user has no association. Can appear as a
  pending reason (not just submit-time rejection) when accounting loads late.
- `InvalidQOS`: QOS doesn't exist or isn't authorized for the user/account/partition.

### O. Scheduler depth limit

`Reason=Priority` or `Resources` but `LastSchedEval` is stale.

```bash
sdiag | grep -E "depth|max_jobs"
```

Fix: admin increases `scheduler.max_jobs_per_cycle` and runs `scontrol reconfigure`.

### P. Requeue-related pending reasons

When a job is requeued (after failure, timeout, OOM, signal, preemption, or node boot
failure), the pending reason reflects WHY it was requeued:

| Reason display | Meaning |
|---------------|---------|
| `TimeLimit` | Previous run exceeded wall-time (requeued after timeout) |
| `NonZeroExitCode` | Previous run exited non-zero (requeued after failure) |
| `RaisedSignal` | Previous run was killed by a signal |
| `OutOfMemory` | Previous run was OOM-killed (requeued) |
| `BootFailure` | Node boot failure prevented start (requeued) |
| `Preempted` | Job was preempted, now re-pending in requeue mode |

These differ from the terminal states (TIMEOUT, FAILED, OOM, etc.) — they appear on
jobs that are back in PENDING after a requeue. Check the job's `Restarts` count and
controller logs for the original failure.

### Q. Deadline — `DeadLine`

Job has `--deadline` set and is approaching or past its deadline window while still
pending. If the deadline passes before the job starts, it moves to terminal DEADLINE state.

```bash
scontrol show job "${JOB_ID}" | grep -E "Deadline|EligibleTime"
```

Fix: extend deadline or remove it: `scontrol update JobId=${JOB_ID} Deadline=<new>`

### R. Burst buffer — `BurstBufferResources` / `BurstBufferStageIn`

- `BurstBufferResources`: burst buffer capacity unavailable.
- `BurstBufferStageIn`: burst buffer is staging data in.

Fix: wait for burst buffer capacity, or reduce `--bb` request.

---

## FAILED jobs (State=F)

The job ran but exited non-zero.

### Checks

```bash
# Exit code (format is exit:signal — signal info is encoded in ExitCode)
sacct -j "${JOB_ID}" --format=JobID,JobName,State,ExitCode,Elapsed,NodeList,Start,End

# Job's stdout/stderr (if StdOut/StdErr paths are set)
scontrol show job "${JOB_ID}" | grep -E "StdOut|StdErr|Command|WorkDir"

# Live resource usage (only for still-running jobs)
sstat -j "${JOB_ID}" 2>/dev/null

# Node health at the time
scontrol show node <nodelist> | grep -E "NodeName|State|Reason|LastBusy"
```

### Interpreting ExitCode

`ExitCode` format is `exit:signal`. Examples:
- `1:0` — script exited with code 1 (user error in the script).
- `0:9` — killed by SIGKILL (OOM killer, admin, or timeout).
- `0:15` — killed by SIGTERM (cancellation or preemption).
- `0:11` — SIGSEGV (segfault in the application).
- `127:0` — command not found (bad PATH or missing binary).
- `126:0` — permission denied on the executable.
- `2:0` — bash syntax error or misuse of shell builtin.

### Common causes and fixes

| ExitCode | Likely cause | Fix |
|----------|-------------|-----|
| `1:0` | Application error | Read stderr; fix the script/program |
| `127:0` | Binary not found | Check PATH, module loads, container image |
| `126:0` | Permission denied | `chmod +x` the script; check filesystem mounts |
| `0:9` (SIGKILL) | OOM kill or admin cancel | Check State — if OOM, increase `--mem` |
| `0:11` (SIGSEGV) | Segfault | Debug the application; check GPU driver compat |
| `0:15` (SIGTERM) | Cancelled/preempted | Check sacct State and PreemptedBy fields |
| `2:0` | Shell syntax error | `bash -n script.sh` to syntax-check |

### GPU job failures

For GPU jobs, also check:
```bash
scontrol show node <node> | grep -E "Gres|GresUsed|Reason"
ssh <node> "dmesg | grep -iE 'amdgpu|error|fault|xid|ECC' | tail -20"
```

---

## TIMEOUT jobs (State=TO)

The job exceeded its wall-time limit.

### Checks

```bash
sacct -j "${JOB_ID}" --format=JobID,Elapsed,TimeLimit,State,ExitCode,NodeList
scontrol show job "${JOB_ID}" | grep -E "TimeLimit|RunTime|EndTime"
```

### Diagnosis

- Compare `Elapsed` vs `TimeLimit`. If nearly equal, the job genuinely ran out of time.
- If `Elapsed` is much less than `TimeLimit`, the partition's `MaxTime` may have capped it.

### Fixes

1. Increase wall-time: resubmit with `--time=<longer>`.
2. If the partition caps it: move to a partition with higher `MaxTime`.
3. If the job just needs more time occasionally: use `--requeue` so timeouts auto-retry.
4. Profile the workload — is it actually slower than expected? (data staging, I/O, bad parallelism)
5. Admin can extend a running job: `scontrol update JobId=${JOB_ID} TimeLimit=<new>`

---

## OUT_OF_MEMORY jobs (State=OOM)

The cgroup OOM killer terminated the job.

### Checks

```bash
sacct -j "${JOB_ID}" --format=JobID,State,ExitCode,Elapsed,NodeList

# For RUNNING jobs, sstat has live memory stats (MaxRSS, MaxVMSize)
sstat -j "${JOB_ID}" 2>/dev/null

# Check what memory was requested
scontrol show job "${JOB_ID}" | grep -E "MinMemory|Mem|ReqTRES"
```

### Diagnosis

If `sstat` was captured before OOM, compare MaxRSS (peak resident memory) vs the
requested `--mem`. If MaxRSS is at or near the limit, the job genuinely ran out.
For completed OOM jobs, sstat is unavailable — rely on the State=OOM indicator.

### Fixes

1. Increase memory: `--mem=<larger>` or `--mem-per-cpu=<larger>`.
2. If using GPUs with large models: check if the model fits in GPU VRAM vs spilling to system RAM.
3. Reduce parallelism to lower per-node memory footprint.
4. For multi-step jobs: identify which step OOM'd and increase only that step's memory.
5. Check for memory leaks in long-running jobs.

---

## NODE_FAIL jobs (State=NF)

A node died, lost heartbeat, or was drained while the job was running.

### Checks

```bash
sacct -j "${JOB_ID}" --format=JobID,State,ExitCode,NodeList,Elapsed,Start,End

scontrol show node <failed_node> | grep -E "NodeName|State|Reason|SlurmdStartTime|LastBusy"

journalctl -u spurctld --since "1 hour ago" | grep -i "<failed_node>"

ssh <failed_node> "journalctl -u spurd --since '1 hour ago' | tail -50"
```

### Common causes

- **Heartbeat timeout** (`heartbeat_timeout_secs`, default 90s): agent crashed, network partition, or node rebooted.
- **Admin drain during job**: `scontrol update NodeName=<n> State=drain` kills running jobs.
- **Hardware failure**: check dmesg, BMC logs, GPU ECC errors.

### Fixes

1. If the job is requeue-able (`--requeue`), it will auto-restart on a healthy node.
2. Admin investigates the node: `dmesg`, `ipmitool sel list`, GPU health checks.
3. Resume the node after recovery: `scontrol update NodeName=<n> State=resume`
4. If persistent: exclude the node via `--exclude=<n>` in future submissions.

---

## CANCELLED jobs (State=CA)

### Checks

```bash
sacct -j "${JOB_ID}" --format=JobID,State,ExitCode,User,Start,End,NodeList

# Who cancelled it? (AdminComment only available via scontrol, not sacct)
scontrol show job "${JOB_ID}" | grep -E "UserId|AdminComment|Reason"
```

### Interpreting who cancelled

- **User cancel**: `scancel <id>` — ExitCode `0:15` (SIGTERM).
- **Admin cancel**: `scancel` by admin — check `AdminComment` via scontrol.
- **Dependency cancel**: `DependencyNeverSatisfied` — dependency target failed.
- **Deadline cancel**: `--deadline` passed before job could start.
- **Preemption cancel**: higher-priority job needed the resources (Reason=`Preempted`).
- **QOS DenyOnLimit**: submit rejected at submission time (never ran).
- **Controller restart**: jobs in weird states may be cleaned up.

### Fixes

Depends on who cancelled and why. If unintentional, resubmit. If preempted, consider
a non-preemptible QOS or `preempt_exempt_time`.

---

## PREEMPTED jobs (State=PR)

### Checks

```bash
sacct -j "${JOB_ID}" --format=JobID,State,ExitCode,Elapsed,Start,End,PreemptedBy,PreemptMode,PreemptQOS
scontrol show job "${JOB_ID}" | grep -E "Preempt|Reason|Priority"
```

### Diagnosis

A higher-priority job or QOS evicted this job. Check preempt mode:
- `cancel`: job is killed, must resubmit.
- `requeue`: job returns to PENDING with Reason=`Preempted` and will restart.
- `suspend`: job is paused and will resume when resources free up.

### Fixes

1. If `requeue` mode: the job will restart automatically — no action needed.
2. Use `preempt_exempt_time` to guarantee minimum runtime before eviction.
3. Move to a non-preemptible partition/QOS.
4. Submit with `--requeue` to survive future preemptions.

---

## SUSPENDED jobs (State=S)

### Checks

```bash
scontrol show job "${JOB_ID}" | grep -E "JobState|Reason|PreemptTime|SuspendTime"
squeue -j "${JOB_ID}"
```

### Causes

- **Preempt-suspend**: the scheduler suspended it to free resources for a higher-priority job.
- **Admin suspend**: `scontrol suspend ${JOB_ID}`

### Fixes

- Preempt-suspend: it will auto-resume when resources free up. Wait.
- Admin suspend: `scontrol resume ${JOB_ID}` (admin).

> **Note:** `inactive_limit_secs` does NOT cause suspension. It sends SIGTERM then
> SIGKILL to idle allocations, leading to TIMEOUT state.

---

## COMPLETING (stuck in CG state)

Job process finished but the job is stuck in COMPLETING for too long.

### Checks

```bash
scontrol show job "${JOB_ID}" | grep -E "JobState|EndTime|NodeList|BatchHost"
sinfo -N --nodes=<nodelist> --format="%20N %6t %20E"

ssh <node> "ps aux | grep -i epilog"
ssh <node> "journalctl -u spurd --since '10 min ago' | grep -i '${JOB_ID}\|epilog\|cleanup'"
```

### Common causes

- **Epilog script hung**: a cleanup script is blocking.
- **Process won't die**: orphan processes still using the cgroup.
- **Filesystem unmount stuck**: network filesystem won't release.
- **Agent crashed mid-cleanup**: spurd restarted but lost the cleanup state.

### Fixes

1. Wait up to `complete_wait_secs` (default 300s) — controller auto-finalizes after that.
2. Admin forces completion: `scontrol update JobId=${JOB_ID} JobState=completed`
3. SSH to the node and kill orphan processes: `kill -9 <pids>`
4. Fix the epilog script.
5. If agent crashed: `systemctl restart spurd` on the node.

---

## Job disappeared / not visible

User says "my job is gone" or `scontrol show job` says "Invalid job id."

### Checks

```bash
sacct -j "${JOB_ID}" --format=JobID,JobName,State,ExitCode,Elapsed,Start,End,NodeList
```

### Diagnosis

- Controller keeps finished jobs in memory for `terminal_job_retention_secs` (default 1h).
  After that, `scontrol show job` fails. Use `sacct` instead.
- If accounting is not configured (`database_url` not set), finished jobs are gone forever
  after retention expires.
- If the job was never submitted successfully (exit code from sbatch/srun), it has no ID.

---

## Submit-time rejections

The user says `sbatch` or `srun` failed immediately (non-zero exit) and no job ID was assigned.

### Account / association errors

| Error message | Cause | Fix |
|---------------|-------|-----|
| `account associations are temporarily unavailable` | Accounting cache not loaded yet after controller restart | Wait 10-30s and retry |
| `no default account resolved for user` | `require_association=true` and user has no default account | `sacctmgr add user <name> account=<acct>` |
| `user has no associations` | User not in any account | `sacctmgr add user <name> account=<acct>` |
| `user is not a member of account` | Explicit `--account` not associated | `sacctmgr add user <name> account=<acct>` |

### QOS / partition ACL errors

| Error message | Cause | Fix |
|---------------|-------|-----|
| `QOS not permitted for association` | QOS not authorized for user/account | `sacctmgr modify user <name> set qos+=<qos>` |
| `default QOS does not exist` | Configured default QOS missing | `sacctmgr add qos <name>` |
| `QOS not in AllowQos` | Partition restricts QOS list | Use an allowed QOS or admin updates partition |
| `QOS in DenyQos` | Partition blocks this QOS | Use a different QOS |
| `account not in AllowAccounts` | Partition restricts account list | Use allowed account or admin updates partition |
| `account in DenyAccounts` | Partition blocks this account | Use a different account |

### Resource / limit errors

| Error message | Cause | Fix |
|---------------|-------|-----|
| `exceeds ... limit` (various) | QOS/Association `DenyOnLimit` flag rejects over-limit jobs at submit | Reduce resources or admin raises limit |
| `requested time limit must not be negative` | Negative `--time` value | Fix the time specification |
| `requested node configuration is not available` | No node matches CPU/mem/GPU request | Check `sinfo -N` for actual resources |
| `node count outside partition bounds` | `--nodes` outside partition min/max | Adjust node count |
| `wall-time exceeds partition MaxTime` | `enforce_part_limits=on` and `--time` too high | Reduce `--time` or use different partition |

### GPU request errors

| Error message | Cause | Fix |
|---------------|-------|-----|
| `GPU demand conflict` | Multiple conflicting GPU request forms | Use only one of `--gpus`, `--gres`, `--gpus-per-node` |
| `GPU gres type conflict` | Conflicting GPU types in gres entries | Ensure consistent GPU type across all gres flags |
| `GPU total less than node count` | Fewer GPUs requested than nodes | Request at least 1 GPU per node |

### Validation / hook errors

| Error message | Cause | Fix |
|---------------|-------|-----|
| `invalid partition` | Partition doesn't exist | `sinfo` to list valid partitions |
| `invalid dependency syntax` | Bad `--dependency` expression | Fix dependency format (e.g. `afterok:123`) |
| `invalid array spec` / `array too large` | Bad `--array` expression or >100000 tasks | Fix array spec |
| `batch script contains DOS line endings` | Windows CRLF in script | `dos2unix script.sh` |
| `permission denied` | Script not executable or path inaccessible | `chmod +x`, check NFS mounts |
| `job submit hook rejected` | Shell or Lua job_submit hook vetoed | Check hook scripts on controller |
| `job submission too large` | Serialized job spec >4 MiB | Reduce embedded data / env vars |
| Connection refused / timeout | Controller down or wrong endpoint | Check `spurctld` status, verify `SPUR_CONTROLLER_ADDR` |

---

## Step 2 — Check for hidden blockers (all states)

Even after the primary issue is clear, check for compounding problems:

```bash
# Down, draining, error nodes reducing capacity
sinfo -N --states=down,drain,drng,err --format="%20N %6t %20E"

# Nodes in alloc/mix — are they running jobs or stuck?
sinfo -N --states=alloc,mix --format="%20N %6t %20G"

# Unknown nodes — never registered or lost after restart
sinfo -N --states=unk --format="%20N %6t %20E"

# Suspended nodes — power-managed, unavailable
sinfo -N --states=susp --format="%20N %6t %20E"

# Reserved/maintenance nodes — held by admin reservations
scontrol show reservation

# Scheduler cycling?
sdiag | grep -E "cycle|last_cycle|jobs_started"

# Resource mismatch — does the job ask for more than any node has?
scontrol show job "${JOB_ID}" | grep ReqTRES
sinfo -N --format="%20N %10c %10m %20G" | sort -k2 -rn | head -5

# Agent health on relevant nodes
scontrol show node <nodelist> | grep -E "State|Reason|SlurmdStartTime"
```

Flag:
- Nodes stuck in `drain` with stale reasons.
- Scheduler `last_cycle` far in the past (controller stalled).
- Job requesting 8 GPUs but max GPUs per node is 4 (unsatisfiable).
- Nodes in `err` state (agent crash loop).
- Nodes in `unk` state (never registered — agent not started or network issue).

---

## Step 3 — Report to the user

Structure your response as:

### Diagnosis

**Job:** `<id>` | **State:** `<state>` | **Reason/ExitCode:** `<value>`

**Root cause:** One-sentence plain-English explanation.

Evidence: what commands you ran, what the output showed, how it connects to the root
cause. Be specific — quote node names, resource numbers, time estimates, exit codes.

### Recommended fix

Numbered list, most effective first. Exact commands. Mark admin-only actions.

### If the fix doesn't work

- Controller logs: `journalctl -u spurctld --since "10 min ago" | grep -i "job ${JOB_ID}\|backfill\|dispatch\|error"`
- Agent logs: `ssh <node> "journalctl -u spurd --since '10 min ago' | tail -50"`
- Full diagnostic bundle: `spur-techsupport.sh` (from spur-toolkit).

---

## Gotchas

- **`Resources` vs `Priority`:** Both mean "waiting," but `Priority` = other pending
  jobs go first; `Resources` = this job IS next but no capacity exists. Different fixes.

- **`+PLANNED` overlay (`plnd`):** Nodes showing `idle+PLANNED` in `sinfo` look idle
  but have a backfill shadow reservation for a future higher-priority job. They won't
  accept new work until that reservation is honored or the reserved job cancels. This is
  an overlay on the `idle` base state, not a separate node state.

- **Multi-node fragmentation:** A 4-node job needs 4 simultaneous free nodes. Aggregate
  free capacity may exceed the request but the job still pends on `Resources`.

- **`DenyOnLimit` QOS flag:** Rejects at submit time (exit 1) instead of pending.
  If "my submit failed," check this. Without `DenyOnLimit`, jobs pend with the
  corresponding QOS/Assoc limit reason instead.

- **Accounting bootstrap:** After controller restart, jobs pend on
  `AccountingUnavailable` for 10-30s while association caches load. `InvalidAccount`
  and `InvalidQOS` can also appear as pending reasons (not just submit rejections)
  during this window.

- **`max_batch_requeue` trap:** Prolog keeps failing -> 5 requeues -> `JobHoldMaxRequeue`.
  User sees a held job with no obvious cause. Check controller logs.

- **Feature constraints are AND by default:** `--constraint="gpu,nvlink"` = BOTH on
  SAME node. `--constraint="gpu|nvlink"` = OR. Typo causes `BadConstraints`.

- **OOM vs SIGKILL ambiguity:** Both show signal 9 in ExitCode. Check `sacct --format=State`
  — if state is `OUT_OF_MEMORY`, it was OOM. If state is `FAILED` with signal 9, it could
  be admin kill or cgroup enforcement. Check `dmesg | grep -i oom` on the node.

- **TIMEOUT grace period:** When wall-time expires, Spur sends SIGTERM first, then
  SIGKILL after `KillWait` seconds (default 30). If the job traps SIGTERM and doesn't
  exit, it gets hard-killed. ExitCode will show signal 9, but state is TIMEOUT.

- **InactiveLimit is NOT suspension:** `inactive_limit_secs` sends SIGTERM then SIGKILL
  to idle allocations, resulting in TIMEOUT state — not SUSPENDED. Only preempt-suspend
  and admin `scontrol suspend` cause SUSPENDED state.

- **Ghost jobs in COMPLETING:** If a node reboots mid-job, the job can stick in CG
  until `complete_wait_secs` expires. Admin can force-complete it.

- **sacct vs scontrol:** `scontrol show job` only works while the job is in controller
  memory (up to `terminal_job_retention_secs` after completion). `sacct` queries the
  accounting database and works indefinitely — but only if accounting is configured.

- **sacct vs sstat:** `sacct` is for finished/historical jobs. `sstat` is for RUNNING
  jobs (live resource usage like MaxRSS, MaxVMSize). Do not use sstat fields in sacct
  format strings — they are different commands with different field sets.

- **Requeued jobs have requeue-specific pending reasons:** A job back in PENDING after
  a timeout shows `Reason=TimeLimit`, after OOM shows `Reason=OutOfMemory`, after
  preemption shows `Reason=Preempted`, etc. These are distinct from the terminal states
  of the same name — they indicate why the job was requeued, not what state it's in now.

- **QOS GRES display strings:** The pending reason for GPU limits displays as `GRES`
  (e.g. `QOSMaxGRESPerJob`, `AssocGrpGRES`), not `GPU`. Searching for "GPU" in reason
  strings will miss them.
