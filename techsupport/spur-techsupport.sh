#!/usr/bin/env bash
# Collect a diagnostic bundle from a running Spur cluster (native-host/systemd
# deployments only — see README.md for the Kubernetes gap) over plain SSH, no
# Ansible required. Runnable directly from the controller node or any admin
# workstation with SSH access to every cluster host.
set -uo pipefail
# Bundle contents (logs, config, hostnames/IPs) are sensitive — default new
# files/dirs to owner-only rather than the ambient umask.
umask 077

SPUR_HOME="${SPUR_HOME:-/root/spur}"
SPUR_INSTALL_DIR="${SPUR_INSTALL_DIR:-/root/.local/bin}"
# Deployments that don't follow the $SPUR_HOME/{etc,state} layout (e.g. FHS
# installs with config in /etc/spur and state in /var/lib/...) can override
# these two directly instead of via SPUR_HOME.
SPUR_CONF="${SPUR_CONF:-$SPUR_HOME/etc/spur.conf}"
SPUR_STATE_DIR="${SPUR_STATE_DIR:-$SPUR_HOME/state}"
ACCT_DB_NAME="${ACCT_DB_NAME:-spur}"
ACCT_DB_PORT="${ACCT_DB_PORT:-5432}"
CONTROLLERS="${CONTROLLERS:-}"
AGENTS="${AGENTS:-}"
ACCT_HOST="${ACCT_HOST:-}"

SINCE="24h"
OUTPUT_DIR="."
NODES_FILTER=""
SKIP_DB=false
SKIP_JOURNAL=false
SKIP_RAFT=false
RAFT_ALL_CONTROLLERS=false
PARTIAL=false

usage() {
  cat <<'EOF'
Usage: spur-techsupport.sh [--controllers <ssh-targets>] [--agents <ssh-targets>] [--acct-host <target>] [options]

At least one of --controllers, --agents, or --acct-host is required — pass
only what you want collected (e.g. --agents alone for just agent logs).

  --controllers <list>   comma-separated SSH targets (user@host), e.g. root@ctl-0
                          (or set $CONTROLLERS). Supports prefix[N-M] ranges,
                          e.g. root@ctl-[1-3] or root@gpu-[001,010-012]

Options:
  --agents <list>         comma-separated SSH targets for compute agents, same
                          prefix[N-M] range syntax as --controllers (or $AGENTS)
  --acct-host <target>    SSH target running PostgreSQL (default: auto-detected among --controllers)
  --nodes <names>         limit AGENT collection to these short hostnames, same
                          range syntax supported (default: all agents)
  --since <duration>      journalctl --since window (default: 24h)
  --skip-db               omit PostgreSQL version/connection info
  --skip-journal          omit journalctl output (spurctld/spurd)
  --skip-raft             omit Raft log/snapshot files
  --raft-all-controllers  collect Raft state from every controller, not just
                           the first (Raft state is replicated, so one is
                           normally enough — this is for comparing/debugging
                           divergence across an HA cluster)
  --partial               emit the archive even if some collection steps failed
  --output <dir>          directory for the .tar.gz (default: cwd)
  -h, --help              this help

Native-host (systemd) deployments only. Kubernetes-deployed clusters need an
equivalent collector living with the K8s deployment tooling — not covered here.
EOF
}

need_arg() {
  [ $# -ge 2 ] || { echo "error: $1 requires a value" >&2; usage >&2; exit 2; }
}

# Expands a comma-separated list into the named array, supporting Slurm-style
# prefix[N-M] / prefix[N,M-K] ranges (e.g. root@gpu-[1-100], gpu-[001,010-012])
# so a big fleet doesn't need spelling out one host at a time. Commas inside
# [...] are part of the range spec, not top-level list separators.
expand_into() {
  local spec="$1"
  local -n _out="$2"
  _out=()
  [ -z "$spec" ] && return 0

  local -a top=()
  local buf="" depth=0 ch i len="${#spec}"
  for (( i=0; i<len; i++ )); do
    ch="${spec:i:1}"
    case "$ch" in
      '[') depth=$((depth+1)); buf+="$ch" ;;
      ']') depth=$((depth-1)); buf+="$ch" ;;
      ',') if [ "$depth" -eq 0 ]; then top+=("$buf"); buf=""; else buf+="$ch"; fi ;;
      *) buf+="$ch" ;;
    esac
  done
  [ -n "$buf" ] && top+=("$buf")

  local token
  for token in "${top[@]}"; do
    if [[ "$token" =~ ^(.*)\[([^]]+)\](.*)$ ]]; then
      local prefix="${BASH_REMATCH[1]}" body="${BASH_REMATCH[2]}" suffix="${BASH_REMATCH[3]}"
      local -a parts
      IFS=',' read -ra parts <<< "$body"
      local part
      for part in "${parts[@]}"; do
        if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
          local start="${BASH_REMATCH[1]}" end="${BASH_REMATCH[2]}" width=${#BASH_REMATCH[1]} n
          if [ "$((10#$start))" -gt "$((10#$end))" ]; then
            echo "error: invalid range [$part] in '$token' (start > end)" >&2
            exit 2
          fi
          for (( n=10#$start; n<=10#$end; n++ )); do
            _out+=("$(printf "%s%0${width}d%s" "$prefix" "$n" "$suffix")")
          done
        elif [[ "$part" =~ ^[0-9]+$ ]]; then
          _out+=("$(printf "%s%0${#part}d%s" "$prefix" "$((10#$part))" "$suffix")")
        else
          echo "error: invalid range syntax '$part' in '$token'" >&2
          exit 2
        fi
      done
    else
      _out+=("$token")
    fi
  done
}

while [ $# -gt 0 ]; do
  case "$1" in
    --controllers) need_arg "$@"; CONTROLLERS="$2"; shift 2 ;;
    --controllers=*) CONTROLLERS="${1#*=}"; shift ;;
    --agents) need_arg "$@"; AGENTS="$2"; shift 2 ;;
    --agents=*) AGENTS="${1#*=}"; shift ;;
    --acct-host) need_arg "$@"; ACCT_HOST="$2"; shift 2 ;;
    --acct-host=*) ACCT_HOST="${1#*=}"; shift ;;
    --nodes) need_arg "$@"; NODES_FILTER="$2"; shift 2 ;;
    --nodes=*) NODES_FILTER="${1#*=}"; shift ;;
    --since) need_arg "$@"; SINCE="$2"; shift 2 ;;
    --since=*) SINCE="${1#*=}"; shift ;;
    --output) need_arg "$@"; OUTPUT_DIR="$2"; shift 2 ;;
    --output=*) OUTPUT_DIR="${1#*=}"; shift ;;
    --skip-db) SKIP_DB=true; shift ;;
    --skip-journal) SKIP_JOURNAL=true; shift ;;
    --skip-raft) SKIP_RAFT=true; shift ;;
    --raft-all-controllers) RAFT_ALL_CONTROLLERS=true; shift ;;
    --partial) PARTIAL=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$CONTROLLERS" ] && [ -z "$AGENTS" ] && [ -z "$ACCT_HOST" ]; then
  echo "error: at least one of --controllers, --agents, or --acct-host is required" >&2
  usage >&2
  exit 2
fi

expand_into "$CONTROLLERS" CONTROLLERS_ARR
expand_into "$AGENTS" AGENTS_ARR
expand_into "$NODES_FILTER" NODES_FILTER_ARR

# journalctl wants a relative duration prefixed with "-" (e.g. "-24h"); accept
# the JIRA/user-facing bare form ("24h") too and translate it.
case "$SINCE" in
  -*) SINCE_ARG="$SINCE" ;;
  *[0-9][hHdDwWmM]) SINCE_ARG="-$SINCE" ;;
  *) SINCE_ARG="$SINCE" ;;
esac

RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
STAGE=$(mktemp -d "/tmp/spur-techsupport-stage.XXXXXX")
MANIFEST="$STAGE/manifest.txt"
: > "$MANIFEST"
FAILED_COUNT=0

progress() {
  echo "==> $*" >&2
}

# Starts a "==> desc... " line with no trailing newline; progress_done (or
# log_item, which calls it) completes it with the result on the same line —
# so a hang shows exactly which step it's stuck on, not just the final tally.
progress_start() {
  printf '==> %s... ' "$*" >&2
}

progress_done() {
  local status="$1" reason="${2:-}"
  if [ -n "$reason" ]; then
    printf '%s (%s)\n' "$status" "$reason" >&2
  else
    printf '%s\n' "$status" >&2
  fi
}

log_item() {
  local status="$1" desc="$2" reason="${3:-}" quiet="${4:-false}"
  if [ -n "$reason" ]; then
    printf '[%s] %s (%s)\n' "$status" "$desc" "$reason" >> "$MANIFEST"
  else
    printf '[%s] %s\n' "$status" "$desc" >> "$MANIFEST"
  fi
  # quiet=true: a manifest-only entry with no matching progress_start (e.g. an
  # aggregate summary whose parts already reported their own progress).
  $quiet || progress_done "$status" "$reason"
  [ "$status" = "FAILED" ] && FAILED_COUNT=$((FAILED_COUNT + 1))
  return 0
}

short_name() {
  local h="${1##*@}"
  case "$h" in
    # IPv4 target (e.g. 10.11.98.147) — truncating at the first "." would
    # collapse distinct hosts sharing an octet into the same name and silently
    # overwrite each other's collected files. Keep it whole, dash for safety.
    [0-9]*) echo "${h//./-}" ;;
    *) echo "${h%%.*}" ;;
  esac
}

ssh_cmd() {
  local target="$1"; shift
  local attempt
  for attempt in 1 2; do
    if [ -n "${SSHPASS:-}" ]; then
      sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$target" "$@" && return 0
    else
      ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$target" "$@" && return 0
    fi
    # One retry after a short pause — covers transient rejections (e.g. a
    # target still finishing boot) without masking a genuinely dead host.
    [ "$attempt" -eq 1 ] && sleep 2
  done
  return 1
}

# True only if the unit is actually known to systemd — distinguishes "no log
# entries in this window" from "not managed by systemd at all" (e.g. run via
# a raw process/nohup), which would otherwise look like a quiet success.
unit_exists() {
  local target="$1" unit="$2" state
  state=$(ssh_cmd "$target" "systemctl show -p LoadState --value $unit 2>/dev/null")
  [ "$state" = "loaded" ]
}

scp_from() {
  local target="$1" remote="$2" local_path="$3"
  if [ -n "${SSHPASS:-}" ]; then
    sshpass -e scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${target}:${remote}" "$local_path"
  else
    scp -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${target}:${remote}" "$local_path"
  fi
}

# Runs a remote command, captures stdout to $3 (outfile) and stderr for the manifest reason.
run_and_capture() {
  local target="$1" remote_cmd="$2" outfile="$3" desc="$4"
  local errfile="$outfile.err"
  progress_start "$desc"
  if ssh_cmd "$target" "$remote_cmd" > "$outfile" 2> "$errfile"; then
    log_item OK "$desc"
    rm -f "$errfile"
    return 0
  else
    log_item FAILED "$desc" "$(tail -1 "$errfile" 2>/dev/null)"
    rm -f "$errfile"
    return 1
  fi
}

# spur --version is not a supported flag (it errors), so build identity is a
# path/size/mtime/sha256 fingerprint of the installed binaries instead.
collect_build_fingerprint() {
  local target="$1" short="$2" hostdir="$3"
  local remote_cmd="found=0; for b in spur spurctld spurd; do p=\"$SPUR_INSTALL_DIR/\$b\"; [ -e \"\$p\" ] || continue; found=1; printf '%s: ' \"\$b\"; stat -c 'path=%n size=%s mtime=%y' \"\$p\"; sha256sum \"\$p\"; done; if [ \"\$found\" -eq 0 ]; then echo \"no spur/spurctld/spurd binaries found under $SPUR_INSTALL_DIR\" >&2; exit 1; fi"
  run_and_capture "$target" "$remote_cmd" "$hostdir/build-fingerprint.txt" "$short: binary fingerprint"
}

collect_controller() {
  local target="$1"
  local short; short=$(short_name "$target")
  local hostdir="$STAGE/controller-$short"
  mkdir -p "$hostdir"
  progress "controller $short: connecting ($target)"

  if $SKIP_JOURNAL; then
    progress_start "controller $short: spurctld journal"
    log_item SKIPPED "controller $short: spurctld journal" "--skip-journal"
  elif ! unit_exists "$target" spurctld; then
    progress_start "controller $short: spurctld journal"
    log_item FAILED "controller $short: spurctld journal" "spurctld is not managed by systemd on this host"
  else
    run_and_capture "$target" "sudo journalctl -u spurctld --since '$SINCE_ARG' --no-pager" \
      "$hostdir/spurctld.journal.log" "controller $short: spurctld journal"
  fi

  if $SKIP_RAFT; then
    progress_start "controller $short: raft state"
    log_item SKIPPED "controller $short: raft state" "--skip-raft"
  elif ! $RAFT_ALL_CONTROLLERS && [ "$target" != "${CONTROLLERS_ARR[0]}" ]; then
    progress_start "controller $short: raft state"
    log_item SKIPPED "controller $short: raft state" "collected from the first controller only; use --raft-all-controllers for every one"
  else
    progress_start "controller $short: raft state (can take a while if the state dir is large)"
    local remote_tar="/tmp/spur-techsupport-$RUN_ID-raft.tar.gz"
    local remote_cmd="sudo tar czf $remote_tar -C \$(dirname $SPUR_STATE_DIR) \$(basename $SPUR_STATE_DIR) && sudo chmod 644 $remote_tar"
    if ssh_cmd "$target" "$remote_cmd" 2>"$hostdir/.raft.err"; then
      if scp_from "$target" "$remote_tar" "$hostdir/raft-state.tar.gz" 2>>"$hostdir/.raft.err"; then
        log_item OK "controller $short: raft state"
      else
        log_item FAILED "controller $short: raft state" "scp failed"
      fi
    else
      log_item FAILED "controller $short: raft state" "$(tail -1 "$hostdir/.raft.err" 2>/dev/null)"
    fi
    rm -f "$hostdir/.raft.err"
    ssh_cmd "$target" "sudo rm -f $remote_tar" >/dev/null 2>&1
  fi

  progress_start "controller $short: spur.conf"
  # Redact only the database_url password segment remotely; keep host/port/dbname/user.
  local remote_conf="/tmp/spur-techsupport-$RUN_ID-conf.sanitized"
  # Greedy .+ up to the LAST "@host/db"" match — a password containing a
  # literal "@" (not just percent-encoded) would stop early otherwise.
  local sanitize_cmd="sudo sed -E 's#(database_url = \"postgresql://[^:]+:).+(@[^@]+/[^\"]+\")#\1REDACTED\2#' $SPUR_CONF > $remote_conf && sudo chmod 644 $remote_conf"
  if ssh_cmd "$target" "$sanitize_cmd" 2>"$hostdir/.conf.err"; then
    if scp_from "$target" "$remote_conf" "$hostdir/spur.conf.sanitized" 2>>"$hostdir/.conf.err"; then
      log_item OK "controller $short: spur.conf (sanitized)"
    else
      log_item FAILED "controller $short: spur.conf" "scp failed"
    fi
  else
    log_item FAILED "controller $short: spur.conf" "$(tail -1 "$hostdir/.conf.err" 2>/dev/null)"
  fi
  rm -f "$hostdir/.conf.err"
  ssh_cmd "$target" "sudo rm -f $remote_conf" >/dev/null 2>&1

  collect_build_fingerprint "$target" "$short" "$hostdir"
}

collect_agent() {
  local target="$1"
  local short; short=$(short_name "$target")

  if [ ${#NODES_FILTER_ARR[@]} -gt 0 ]; then
    local matched=false n
    for n in "${NODES_FILTER_ARR[@]}"; do
      [ "$n" = "$short" ] && { matched=true; break; }
    done
    if ! $matched; then
      progress_start "agent $short"
      log_item SKIPPED "agent $short" "excluded by --nodes filter"
      return 0
    fi
  fi

  local hostdir="$STAGE/agent-$short"
  mkdir -p "$hostdir"
  progress "agent $short: connecting ($target)"

  if $SKIP_JOURNAL; then
    progress_start "agent $short: spurd journal"
    log_item SKIPPED "agent $short: spurd journal" "--skip-journal"
  elif ! unit_exists "$target" spurd; then
    progress_start "agent $short: spurd journal"
    log_item FAILED "agent $short: spurd journal" "spurd is not managed by systemd on this host"
  else
    run_and_capture "$target" "sudo journalctl -u spurd --since '$SINCE_ARG' --no-pager" \
      "$hostdir/spurd.journal.log" "agent $short: spurd journal"
  fi

  # Agents have no config file of their own — every flag lives in the unit.
  run_and_capture "$target" "sudo cat /etc/systemd/system/spurd.service" \
    "$hostdir/spurd.service" "agent $short: spurd.service unit"

  collect_build_fingerprint "$target" "$short" "$hostdir"
}

collect_cluster_state() {
  if [ ${#CONTROLLERS_ARR[@]} -eq 0 ]; then
    progress_start "cluster state: sinfo/squeue/sdiag"
    log_item SKIPPED "cluster state: sinfo/squeue/sdiag" "no --controllers given"
    return 0
  fi
  local target="${CONTROLLERS_ARR[0]}"
  local short; short=$(short_name "$target")
  local hostdir="$STAGE/cluster-state"
  mkdir -p "$hostdir"
  local ok=true
  local name cmd
  for cmd in sinfo "squeue -t all" sdiag; do
    name=$(echo "$cmd" | awk '{print $1}')
    progress_start "cluster state: $name (via $short)"
    if ssh_cmd "$target" ". /etc/environment 2>/dev/null; $SPUR_INSTALL_DIR/$cmd" \
        > "$hostdir/$name.txt" 2>"$hostdir/$name.err"; then
      progress_done OK
    else
      ok=false
      progress_done FAILED "$(tail -1 "$hostdir/$name.err" 2>/dev/null)"
    fi
    rm -f "$hostdir/$name.err"
  done
  if $ok; then
    log_item OK "cluster state: sinfo/squeue/sdiag (via $short)" "" true
  else
    log_item FAILED "cluster state: sinfo/squeue/sdiag" "one or more commands failed on $short" true
  fi
}

# Probes each controller for a live PostgreSQL (active service or listening
# port) and returns the first hit — only used when --acct-host isn't given.
probe_acct_host() {
  local c
  for c in "${CONTROLLERS_ARR[@]}"; do
    [ -n "$c" ] || continue
    progress_start "accounting db: probing $c for PostgreSQL"
    # pg_isready actually attempts a connection, so it won't be fooled by a
    # "postgresql" wrapper/meta-unit reporting active while the real
    # instance-specific service is down, or a stale listener on the port.
    # Falls back to the weaker service/port check if pg_isready is missing.
    if ssh_cmd "$c" "if command -v pg_isready >/dev/null 2>&1; then pg_isready -p $ACCT_DB_PORT -t 3 -q; else systemctl is-active --quiet postgresql 2>/dev/null || ss -tln 2>/dev/null | grep -q \":$ACCT_DB_PORT \"; fi"; then
      progress_done "found"
      echo "$c"
      return 0
    fi
    progress_done "not found"
  done
  return 1
}

collect_db() {
  if $SKIP_DB; then
    progress_start "accounting db"
    log_item SKIPPED "accounting db" "--skip-db"
    return 0
  fi
  local host="$ACCT_HOST"
  if [ -z "$host" ]; then
    host=$(probe_acct_host) || {
      progress_start "accounting db"
      log_item FAILED "accounting db" "no PostgreSQL found on any --controllers host; pass --acct-host"
      return 0
    }
  fi
  local short; short=$(short_name "$host")
  local hostdir="$STAGE/accounting"
  mkdir -p "$hostdir"
  {
    echo "db_name=$ACCT_DB_NAME"
    echo "db_port=$ACCT_DB_PORT"
    echo "host=$short"
  } > "$hostdir/connection-info.txt"
  run_and_capture "$host" "sudo -u postgres psql -p $ACCT_DB_PORT -tAc 'SELECT version();'" \
    "$hostdir/postgres-version.txt" "accounting db: postgres version (via $short)"
  run_and_capture "$host" "sudo -u postgres pg_dump -p $ACCT_DB_PORT $ACCT_DB_NAME" \
    "$hostdir/accounting-db.sql" "accounting db: full dump (via $short)"
}

progress "starting collection: ${#CONTROLLERS_ARR[@]} controller(s), ${#AGENTS_ARR[@]} agent(s)"

for c in "${CONTROLLERS_ARR[@]}"; do
  [ -n "$c" ] && collect_controller "$c"
done

collect_cluster_state
collect_db

for a in "${AGENTS_ARR[@]}"; do
  [ -n "$a" ] && collect_agent "$a"
done

{
  echo "spur-techsupport bundle manifest"
  echo "generated: $RUN_ID"
  echo "flags: skip-db=$SKIP_DB skip-journal=$SKIP_JOURNAL skip-raft=$SKIP_RAFT nodes=${NODES_FILTER:-all} since=$SINCE partial=$PARTIAL"
  echo
  cat "$MANIFEST"
} > "$MANIFEST.tmp"
mv "$MANIFEST.tmp" "$MANIFEST"

echo "---- manifest ----"
cat "$MANIFEST"

if [ "$FAILED_COUNT" -gt 0 ] && ! $PARTIAL; then
  echo "collection failed ($FAILED_COUNT item(s)); not emitting archive (use --partial to override)" >&2
  rm -rf "$STAGE"
  exit 1
fi

if ! mkdir -p "$OUTPUT_DIR"; then
  echo "error: could not create output directory $OUTPUT_DIR" >&2
  rm -rf "$STAGE"
  exit 1
fi
ARCHIVE="$OUTPUT_DIR/spur-techsupport-$RUN_ID.tar.gz"
if ! tar czf "$ARCHIVE" -C "$(dirname "$STAGE")" "$(basename "$STAGE")"; then
  echo "error: failed to create archive $ARCHIVE" >&2
  rm -rf "$STAGE"
  exit 1
fi
rm -rf "$STAGE"
echo "wrote $ARCHIVE"
