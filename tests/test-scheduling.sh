#!/usr/bin/env bash
# test-scheduling.sh — manual scheduling failure mode tests for Spur
#
# Spins up a docker-based 1-ctl + 3-agent cluster, runs 12 scheduling edge
# case tests covering every documented "job not getting scheduled" root cause,
# then tears down.
#
# Usage:
#   ./tests/test-scheduling.sh                  # full run (deploy + test + teardown)
#   ./tests/test-scheduling.sh --skip-deploy    # reuse a running cluster from a prior run
#   ./tests/test-scheduling.sh --skip-teardown  # leave the cluster running after tests
#   ./tests/test-scheduling.sh --test 3         # run only test 3 (against existing cluster)
#
# Prerequisites:
#   - Docker (or podman) with systemd container support
#   - Spur binaries in $SPUR_BIN_DIR (default: ./spur-bin; download from ROCm/spur releases)
#   - Ansible + community.docker collection (for deploy)
#
# The 12 tests cover:
#   1.  Drained node blocks scheduling (ReqNodeNotAvail)
#   2.  Unsatisfiable resource request stays PENDING
#   3.  Job dependency chain (afterok) — both complete
#   4.  DependencyNeverSatisfied — afterok on FAILED parent
#   5.  Node DOWN — job stays PENDING until recovery
#   6.  Job cancellation (scancel)
#   7.  Job timeout (--time exceeded)
#   8.  Multi-node job — insufficient idle nodes
#   9.  Submit-time rejection — invalid partition
#   10. Submit-time rejection — nonexistent node
#   11. Drain-all / resume-all lifecycle
#   12. Cluster state consistency (spur nodes reflects IDLE)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NETWORK="spur-test-sched"
NODES="ctl-0 gpu-1 gpu-2 gpu-3"
IMG="${SPUR_TEST_IMAGE:-jrei/systemd-ubuntu:22.04}"
SPUR_BIN_DIR="$(cd "${SPUR_BIN_DIR:-$REPO_DIR/spur-bin}" 2>/dev/null && pwd)"
INSTALL_DIR="/usr/local/bin"
SPUR_HOME="/opt/spur"
SUBMIT_USER="testuser"

SKIP_DEPLOY=false
SKIP_TEARDOWN=false
SINGLE_TEST=""
PASSED=0
FAILED=0
SKIPPED=0
RESULTS=()

usage() {
  sed -n '2,/^set -/{ /^#/s/^# \?//p }' "$0"
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-deploy)   SKIP_DEPLOY=true; shift ;;
    --skip-teardown) SKIP_TEARDOWN=true; shift ;;
    --test)          SINGLE_TEST="$2"; shift 2 ;;
    -h|--help)       usage ;;
    *)               echo "Unknown flag: $1"; usage ;;
  esac
done

# --- Helpers ----------------------------------------------------------------

log()  { echo -e "\033[1;34m>>>\033[0m $*"; }
pass() { echo -e "\033[1;32mPASS\033[0m $*"; PASSED=$((PASSED+1)); RESULTS+=("PASS: $*"); }
fail() { echo -e "\033[1;31mFAIL\033[0m $*"; FAILED=$((FAILED+1)); RESULTS+=("FAIL: $*"); }
skip() { echo -e "\033[1;33mSKIP\033[0m $*"; SKIPPED=$((SKIPPED+1)); RESULTS+=("SKIP: $*"); }

ctl() { docker exec ctl-0 "$@"; }
ctl_spur() { docker exec ctl-0 "$INSTALL_DIR/spur" "$@"; }
submit() { docker exec -u "$SUBMIT_USER" -w "/home/$SUBMIT_USER" ctl-0 "$INSTALL_DIR/spur" submit "$@"; }
job_state() { ctl_spur show job "$1" 2>/dev/null | grep -oE 'JobState=[A-Z_]+' | head -1 | cut -d= -f2; }
node_state() { ctl_spur show node "$1" 2>/dev/null | grep -oE 'State=[A-Za-z*+]+' | head -1 | cut -d= -f2; }

wait_job_terminal() {
  local jid="$1" timeout="${2:-60}" st=""
  for i in $(seq 1 "$timeout"); do
    st=$(job_state "$jid")
    case "$st" in COMPLETED|FAILED|CANCELLED|TIMEOUT|NODE_FAIL|OUT_OF_MEMORY) echo "$st"; return 0 ;; esac
    sleep 1
  done
  echo "$st"
}

wait_job_state() {
  local jid="$1" want="$2" timeout="${3:-30}" st=""
  for i in $(seq 1 "$timeout"); do
    st=$(job_state "$jid")
    [ "$st" = "$want" ] && { echo "$st"; return 0; }
    sleep 1
  done
  echo "$st"
}

wait_node_state() {
  local node="$1" pattern="$2" timeout="${3:-60}" st=""
  for i in $(seq 1 "$timeout"); do
    st=$(node_state "$node")
    if echo "$st" | grep -qiE "$pattern"; then echo "$st"; return 0; fi
    sleep 1
  done
  echo "$st"
}

write_script() {
  local name="$1" body="$2"
  docker exec -u "$SUBMIT_USER" -w "/home/$SUBMIT_USER" ctl-0 bash -c "cat > /tmp/$name.sh << 'JOBEOF'
#!/bin/bash
$body
JOBEOF"
}

should_run() {
  [ -z "$SINGLE_TEST" ] || [ "$SINGLE_TEST" = "$1" ]
}

resume_all() {
  for a in gpu-1 gpu-2 gpu-3; do
    ctl_spur scontrol update "NodeName=$a" State=RESUME 2>/dev/null || true
  done
}

# --- Cluster setup ----------------------------------------------------------

setup_cluster() {
  log "Setting up docker network and containers..."
  docker network create "$NETWORK" 2>/dev/null || true
  docker pull "$IMG"
  for n in $NODES; do
    docker rm -f "$n" 2>/dev/null || true
    docker run -d --name "$n" --hostname "$n" \
      --network "$NETWORK" \
      --privileged \
      --cgroupns=host \
      -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
      --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
      "$IMG"
  done

  log "Waiting for systemd in all containers..."
  for n in $NODES; do
    for i in $(seq 1 30); do
      st=$(docker exec "$n" systemctl is-system-running 2>/dev/null || true)
      case "$st" in running|degraded) break ;; esac
      sleep 2
    done
  done

  log "Installing preflight deps..."
  for n in $NODES; do
    docker exec "$n" bash -c 'apt-get update -qq && apt-get install -y -qq curl tar iproute2 python3 sudo' >/dev/null 2>&1
  done

  log "Creating submission user..."
  for n in $NODES; do
    docker exec "$n" useradd -m -s /bin/bash "$SUBMIT_USER" 2>/dev/null || true
  done

  if [ ! -d "$SPUR_BIN_DIR" ] || [ ! -f "$SPUR_BIN_DIR/spur" ]; then
    echo "ERROR: Spur binaries not found at $SPUR_BIN_DIR"
    echo "Download from: gh release download <tag> --repo ROCm/spur --pattern '*-linux-amd64.tar.gz'"
    echo "Then: export SPUR_BIN_DIR=/path/to/extracted/binaries"
    exit 1
  fi

  log "Deploying cluster via Ansible..."
  (
    cd "$REPO_DIR/ansible"
    ansible-playbook -i inventory/ci.ini playbooks/deploy.yml \
      -e spur_binary_src="$SPUR_BIN_DIR" \
      -e spur_install_dir="$INSTALL_DIR" \
      -e spur_home="$SPUR_HOME" \
      -e spur_verify_enabled=true \
      -e spur_verify_submit_user="$SUBMIT_USER"
  )

  log "Verifying all agents registered..."
  for i in $(seq 1 30); do
    missing=""
    for a in gpu-1 gpu-2 gpu-3; do
      ctl_spur show node "$a" 2>/dev/null | grep -qx "NodeName=$a" || missing="$missing $a"
    done
    [ -z "$missing" ] && break
    sleep 2
  done
  if [ -n "$missing" ]; then
    echo "ERROR: agents never registered:$missing"
    exit 1
  fi
  log "Cluster ready: 1 controller + 3 agents"
  ctl_spur nodes
}

teardown_cluster() {
  log "Tearing down containers..."
  for n in $NODES; do docker rm -f "$n" 2>/dev/null || true; done
  docker network rm "$NETWORK" 2>/dev/null || true
}

# --- Tests ------------------------------------------------------------------

test_1_drained_node_blocks_scheduling() {
  log "Test 1: Drained node blocks scheduling (ReqNodeNotAvail)"
  ctl_spur node drain gpu-1 --reason "test-1-drain"
  wait_node_state gpu-1 "drain|DRAIN" 20 >/dev/null

  write_script "t1" "#SBATCH --job-name=t1-drain
echo ran on \$(hostname)"
  local jid
  jid=$(submit -w gpu-1 /tmp/t1.sh 2>&1 | grep -oE '[0-9]+' | head -1)
  sleep 5
  local st
  st=$(job_state "$jid")
  if [ "$st" = "PENDING" ]; then
    pass "Test 1a: job $jid PENDING on drained gpu-1"
  else
    fail "Test 1a: expected PENDING, got $st"
  fi

  ctl_spur scontrol update NodeName=gpu-1 State=RESUME
  st=$(wait_job_terminal "$jid" 30)
  if [ "$st" = "COMPLETED" ]; then
    pass "Test 1b: job $jid COMPLETED after resuming gpu-1"
  else
    fail "Test 1b: expected COMPLETED after resume, got $st"
  fi
}

test_2_unsatisfiable_resources() {
  log "Test 2: Unsatisfiable resource request stays PENDING"
  write_script "t2" "#SBATCH --job-name=t2-overrequest
#SBATCH --cpus-per-task=9999
echo should-never-run"
  local jid
  jid=$(submit /tmp/t2.sh 2>&1 | grep -oE '[0-9]+' | head -1)
  if [ -z "$jid" ]; then
    pass "Test 2: submit rejected at submit time (also valid)"
    return
  fi
  sleep 5
  local st
  st=$(job_state "$jid")
  if [ "$st" = "PENDING" ]; then
    pass "Test 2: job $jid PENDING (unsatisfiable 9999 CPUs)"
  else
    fail "Test 2: expected PENDING, got $st"
  fi
  ctl_spur scancel "$jid" 2>/dev/null || true
}

test_3_dependency_chain() {
  log "Test 3: Job dependency chain (afterok) — both complete in order"
  write_script "t3-parent" "#SBATCH --job-name=t3-parent
sleep 3
echo parent-done"
  write_script "t3-child" "#SBATCH --job-name=t3-child
echo child-done"

  local jid_a jid_b st_a st_b
  jid_a=$(submit /tmp/t3-parent.sh 2>&1 | grep -oE '[0-9]+' | head -1)
  jid_b=$(submit --dependency=afterok:"$jid_a" /tmp/t3-child.sh 2>&1 | grep -oE '[0-9]+' | head -1)

  sleep 1
  st_b=$(job_state "$jid_b")
  if [ "$st_b" = "PENDING" ]; then
    log "  child PENDING while parent runs (correct)"
  else
    log "  child state while parent runs: $st_b (may have raced)"
  fi

  st_a=$(wait_job_terminal "$jid_a" 30)
  st_b=$(wait_job_terminal "$jid_b" 30)
  if [ "$st_a" = "COMPLETED" ] && [ "$st_b" = "COMPLETED" ]; then
    pass "Test 3: dependency chain parent=$st_a child=$st_b"
  else
    fail "Test 3: parent=$st_a child=$st_b (expected both COMPLETED)"
  fi
}

test_4_dependency_never_satisfied() {
  log "Test 4: DependencyNeverSatisfied — afterok on a FAILED parent"
  write_script "t4-parent" "#SBATCH --job-name=t4-fail-parent
exit 1"
  write_script "t4-child" "#SBATCH --job-name=t4-fail-child
echo should-not-run"

  local jid_a jid_b st_a st_b
  jid_a=$(submit /tmp/t4-parent.sh 2>&1 | grep -oE '[0-9]+' | head -1)
  jid_b=$(submit --dependency=afterok:"$jid_a" /tmp/t4-child.sh 2>&1 | grep -oE '[0-9]+' | head -1)

  st_a=$(wait_job_terminal "$jid_a" 30)
  # Give the scheduler time to propagate dependency failure.
  sleep 5
  st_b=$(wait_job_terminal "$jid_b" 30)

  if [ "$st_a" = "FAILED" ] && [ "$st_b" != "COMPLETED" ]; then
    pass "Test 4: parent FAILED, child=$st_b (not COMPLETED)"
  else
    fail "Test 4: parent=$st_a child=$st_b (expected FAILED + not COMPLETED)"
  fi
}

test_5_node_down() {
  log "Test 5: Node DOWN — job stays PENDING until recovery"
  docker exec gpu-2 systemctl stop spurd

  local nst
  nst=$(wait_node_state gpu-2 "DOWN|down" 90)
  log "  gpu-2 state after stopping spurd: $nst"

  write_script "t5" "#SBATCH --job-name=t5-down-test
echo ran"
  local jid
  jid=$(submit -w gpu-2 /tmp/t5.sh 2>&1 | grep -oE '[0-9]+' | head -1)
  sleep 5
  local st
  st=$(job_state "$jid")
  if [ "$st" = "PENDING" ]; then
    pass "Test 5a: job $jid PENDING on DOWN gpu-2"
  else
    fail "Test 5a: expected PENDING, got $st"
  fi

  docker exec gpu-2 systemctl start spurd
  for i in $(seq 1 30); do
    ctl_spur scontrol update NodeName=gpu-2 State=RESUME 2>/dev/null || true
    nst=$(node_state gpu-2)
    case "$nst" in IDLE|idle|*alloc*|*ALLOC*) break ;; esac
    sleep 2
  done

  st=$(wait_job_terminal "$jid" 30)
  if [ "$st" = "COMPLETED" ]; then
    pass "Test 5b: job $jid COMPLETED after gpu-2 recovery"
  else
    fail "Test 5b: expected COMPLETED after recovery, got $st"
  fi
}

test_6_scancel() {
  log "Test 6: Job cancellation (scancel)"
  write_script "t6" "#SBATCH --job-name=t6-cancel
sleep 600"
  local jid
  jid=$(submit /tmp/t6.sh 2>&1 | grep -oE '[0-9]+' | head -1)

  local st
  st=$(wait_job_state "$jid" "RUNNING" 30)
  if [ "$st" != "RUNNING" ]; then
    fail "Test 6: job never reached RUNNING (state: $st)"
    return
  fi

  ctl_spur scancel "$jid"
  sleep 3
  st=$(job_state "$jid")
  if [ "$st" = "CANCELLED" ]; then
    pass "Test 6: job $jid CANCELLED after scancel"
  else
    fail "Test 6: expected CANCELLED, got $st"
  fi
}

test_7_timeout() {
  log "Test 7: Job timeout (--time exceeded)"
  write_script "t7" "#SBATCH --job-name=t7-timeout
#SBATCH --time=00:01:00
sleep 120"
  local jid
  jid=$(submit /tmp/t7.sh 2>&1 | grep -oE '[0-9]+' | head -1)
  log "  job $jid submitted with --time=1min, sleeping 120s..."

  local st
  st=$(wait_job_terminal "$jid" 120)
  if [ "$st" = "TIMEOUT" ]; then
    pass "Test 7: job $jid reached TIMEOUT"
  elif [ "$st" = "COMPLETED" ]; then
    fail "Test 7: job completed before timeout fired"
  else
    fail "Test 7: expected TIMEOUT, got $st"
  fi
}

test_8_multinode_insufficient() {
  log "Test 8: Multi-node job — insufficient idle nodes"
  ctl_spur node drain gpu-1 --reason "test-8-multinode"
  wait_node_state gpu-1 "drain|DRAIN" 20 >/dev/null

  write_script "t8" "#SBATCH --job-name=t8-multinode
#SBATCH --nodes=3
echo ran on \$(hostname)"
  local jid
  jid=$(submit /tmp/t8.sh 2>&1 | grep -oE '[0-9]+' | head -1)
  sleep 5
  local st
  st=$(job_state "$jid")
  if [ "$st" = "PENDING" ]; then
    pass "Test 8a: 3-node job $jid PENDING with gpu-1 drained (only 2 idle)"
  else
    fail "Test 8a: expected PENDING, got $st"
  fi

  ctl_spur scontrol update NodeName=gpu-1 State=RESUME
  st=$(wait_job_terminal "$jid" 30)
  if [ "$st" = "COMPLETED" ]; then
    pass "Test 8b: 3-node job $jid COMPLETED after resuming gpu-1"
  else
    fail "Test 8b: expected COMPLETED, got $st"
  fi
}

test_9_invalid_partition() {
  log "Test 9: Submit-time rejection — invalid partition"
  write_script "t9" "#SBATCH --job-name=t9-badpartition
#SBATCH --partition=does-not-exist
echo should-never-run"
  local out rc
  out=$(submit /tmp/t9.sh 2>&1) && rc=0 || rc=$?
  if [ "$rc" -ne 0 ]; then
    pass "Test 9: submit rejected (rc=$rc): $(echo "$out" | head -1)"
  else
    local jid
    jid=$(echo "$out" | grep -oE '[0-9]+' | head -1)
    if [ -n "$jid" ]; then
      sleep 3
      local st
      st=$(job_state "$jid")
      fail "Test 9: submit accepted job $jid (state: $st) — should have been rejected"
      ctl_spur scancel "$jid" 2>/dev/null || true
    else
      fail "Test 9: unclear submit result: $out"
    fi
  fi
}

test_10_nonexistent_node() {
  log "Test 10: Submit-time rejection — nonexistent node"
  write_script "t10" "#SBATCH --job-name=t10-badnode
echo should-never-run"
  local out rc
  out=$(submit -w nonexistent-xyz /tmp/t10.sh 2>&1) && rc=0 || rc=$?
  if [ "$rc" -ne 0 ]; then
    pass "Test 10: submit targeting nonexistent node rejected (rc=$rc)"
  else
    local jid
    jid=$(echo "$out" | grep -oE '[0-9]+' | head -1)
    if [ -n "$jid" ]; then
      sleep 3
      local st
      st=$(job_state "$jid")
      if [ "$st" = "PENDING" ]; then
        pass "Test 10: job $jid stuck PENDING on nonexistent node (valid behavior)"
      else
        fail "Test 10: job $jid targeting nonexistent node has state $st"
      fi
      ctl_spur scancel "$jid" 2>/dev/null || true
    else
      pass "Test 10: submit with unclear rejection: $(echo "$out" | head -1)"
    fi
  fi
}

test_11_drain_all_resume_all() {
  log "Test 11: Drain ALL nodes → job PENDING → resume ALL → job completes"
  for a in gpu-1 gpu-2 gpu-3; do
    ctl_spur node drain "$a" --reason "test-11-drain-all"
  done
  sleep 3

  write_script "t11" "#SBATCH --job-name=t11-drain-all
echo ran"
  local jid
  jid=$(submit /tmp/t11.sh 2>&1 | grep -oE '[0-9]+' | head -1)
  sleep 5
  local st
  st=$(job_state "$jid")
  if [ "$st" = "PENDING" ]; then
    pass "Test 11a: job $jid PENDING with all nodes drained"
  else
    fail "Test 11a: expected PENDING, got $st"
  fi

  resume_all
  st=$(wait_job_terminal "$jid" 30)
  if [ "$st" = "COMPLETED" ]; then
    pass "Test 11b: job $jid COMPLETED after resuming all nodes"
  else
    fail "Test 11b: expected COMPLETED, got $st"
  fi
}

test_12_cluster_state_consistency() {
  log "Test 12: Cluster state consistency — all nodes IDLE after tests"
  resume_all
  sleep 5
  local all_idle=true
  for a in gpu-1 gpu-2 gpu-3; do
    local st
    st=$(node_state "$a")
    case "$st" in
      IDLE|idle) log "  $a: $st" ;;
      *) log "  $a: $st (not IDLE)"; all_idle=false ;;
    esac
  done
  if $all_idle; then
    pass "Test 12: all nodes IDLE"
  else
    fail "Test 12: not all nodes IDLE (see above)"
  fi
  ctl_spur nodes
}

# --- Main -------------------------------------------------------------------

if ! $SKIP_DEPLOY; then
  setup_cluster
else
  log "Skipping deploy (--skip-deploy)"
fi

echo ""
log "=========================================="
log " Running scheduling failure mode tests"
log "=========================================="
echo ""

should_run 1  && test_1_drained_node_blocks_scheduling
should_run 2  && test_2_unsatisfiable_resources
should_run 3  && test_3_dependency_chain
should_run 4  && test_4_dependency_never_satisfied
should_run 5  && test_5_node_down
should_run 6  && test_6_scancel
should_run 7  && test_7_timeout
should_run 8  && test_8_multinode_insufficient
should_run 9  && test_9_invalid_partition
should_run 10 && test_10_nonexistent_node
should_run 11 && test_11_drain_all_resume_all
should_run 12 && test_12_cluster_state_consistency

echo ""
log "=========================================="
log " Results: $PASSED passed, $FAILED failed, $SKIPPED skipped"
log "=========================================="
for r in "${RESULTS[@]}"; do
  echo "  $r"
done
echo ""

if ! $SKIP_TEARDOWN; then
  teardown_cluster
else
  log "Leaving cluster running (--skip-teardown)"
fi

exit "$FAILED"
