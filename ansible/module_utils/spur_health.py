"""Read-only per-host health probes, executed together over one connection."""

import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time


def probe(argv, environment=None):
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def listening(port):
    deadline = time.monotonic() + 3
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def health(
    role,
    install_dir,
    home,
    port,
    auth,
    cluster,
    key_dir,
    accounting,
    cgroup,
    require_stepd,
):
    problems = []
    if role == "accounting":
        if probe(["systemctl", "is-active", "postgresql"])[1] != "active":
            problems.append("postgresql not active")
        else:
            import shutil

            if shutil.which("pg_isready") and probe(["pg_isready", "-q"])[0]:
                problems.append("postgresql not accepting connections (pg_isready)")
        return problems

    daemon = "spurctld" if role == "controller" else "spurd"
    if probe(["systemctl", "is-active", daemon])[1] != "active":
        problems.append(daemon + " not active")
    if role == "controller":
        environment = dict(os.environ)
        environment.pop("SPUR_AUTH_PLUGIN", None)
        environment.pop("SPUR_CLUSTER_NAME", None)
        environment["SPUR_CONF"] = str(Path(home) / "etc/spur.conf")
        environment["SPUR_CONTROLLER_ADDR"] = "http://127.0.0.1:%s" % port
        rc, output = probe([str(Path(install_dir) / "spur"), "nodes"], environment)
        if rc:
            problems.append("`spur nodes` failed (controller/leader down?)")
        elif re.search("no leader|not the Raft leader", output):
            problems.append("no Raft leader elected")
        if probe([str(Path(install_dir) / "spur"), "queue"], environment)[0]:
            problems.append("`spur queue` failed")
        if (
            accounting
            and probe([str(Path(install_dir) / "sacct"), "--limit", "1"], environment)[
                0
            ]
        ):
            problems.append("accounting/sacct unreachable")
    else:
        if not listening(port):
            problems.append("agent port %s not listening" % port)
        if require_stepd and not (Path(install_dir) / "spurstepd").exists():
            problems.append(
                "spurstepd missing from "
                + install_dir
                + " (every job launch here will fail)"
            )
        if cgroup:
            issues = []
            if probe(["stat", "-fc", "%T", "/sys/fs/cgroup"])[1] != "cgroup2fs":
                issues.append("not-cgroup2")
            controllers = Path("/sys/fs/cgroup/cgroup.controllers")
            try:
                if "memory" not in controllers.read_text().split():
                    issues.append("no-memory-controller")
            except OSError:
                pass
            if issues:
                problems.append("cgroup:" + " ".join(issues))
    try:
        disk = os.statvfs(home)
        available = disk.f_bavail * disk.f_frsize
    except OSError:
        available = 0
    if available < 1024**3:
        problems.append("low disk (<1GB free) under " + home)
    if auth:
        if probe(["systemctl", "is-active", "spurauthd"])[1] != "active":
            problems.append("spurauthd not active")
        issues = []
        try:
            is_socket = stat.S_ISSOCK(
                Path("/run/spur", cluster, "auth.sock").stat().st_mode
            )
        except OSError:
            is_socket = False
        if not is_socket:
            issues.append("no-mint-socket")
        if not (Path(key_dir) / "auth.jwks").is_file():
            issues.append("no-auth.jwks")
        if issues:
            problems.append("auth:" + " ".join(issues))
    return problems
