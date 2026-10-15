"""CI-only drift injection and assertions against real systemd containers."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def docker(node, *args):
    return subprocess.check_output(["docker", "exec", node, *args], text=True).strip()


def checksum(node, path):
    return docker(node, "sha256sum", path).split()[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["before", "after"])
    parser.add_argument("source", type=Path)
    parser.add_argument("--auth", action="store_true")
    parser.add_argument(
        "--state", type=Path, default=Path("/tmp/upgrade-convergence.json")
    )
    args = parser.parse_args()
    nodes = os.environ["NODES"].split()
    if args.mode == "before":
        state = {}
        for node in nodes:
            daemon = "spurctld" if node.startswith("ctl-") else "spurd"
            services = ["spurauthd", daemon] if args.auth else [daemon]
            state[node] = {"daemon": daemon, "services": {}, "keys": {}}
            state[node]["config"] = checksum(node, "/opt/spur/etc/spur.conf")
            for service in services:
                unit = "/etc/systemd/system/" + service + ".service"
                state[node]["services"][service] = {
                    "pid": docker(
                        node, "systemctl", "show", service, "-p", "MainPID", "--value"
                    ),
                    "unit": checksum(node, unit),
                }
                docker(
                    node,
                    "bash",
                    "-c",
                    'printf "\\n[Service]\\nEnvironment=SPUR_CI_DRIFT=1\\n" >> "$1"',
                    "fixture",
                    unit,
                )
                docker(node, "systemctl", "disable", service)
            docker(
                node,
                "bash",
                "-c",
                "sed -i '/^SPUR_CONTROLLER_ADDR=/d' /etc/environment; rm -f /usr/local/bin/squeue",
            )
            if daemon == "spurctld":
                docker(
                    node,
                    "bash",
                    "-c",
                    "echo SPUR_ACCOUNTING_ADDR=stale >> /etc/environment",
                )
            if daemon == "spurd" and (args.source / "spur_mpi_pmix.so").is_file():
                docker(
                    node,
                    "bash",
                    "-c",
                    "mkdir -p /usr/lib/spur; printf stale > /usr/lib/spur/spur_mpi_pmix.so",
                )
            if args.auth:
                paths = docker(
                    node, "bash", "-c", "printf '%s\\n' /etc/spur/*.jwks"
                ).splitlines()
                state[node]["keys"] = {path: checksum(node, path) for path in paths}
                docker(
                    node,
                    "bash",
                    "-c",
                    "chmod 0644 /etc/spur/*.jwks; echo '# CI_CONFIG_DRIFT' >> /etc/spur/spur.conf; sed -i '/^SPUR_AUTH_PLUGIN=/d; /^SPUR_CLUSTER_NAME=/d' /etc/environment",
                )
        args.state.write_text(json.dumps(state))
        return
    state = json.loads(args.state.read_text())
    for node in nodes:
        before = state[node]
        daemon = before["daemon"]
        binaries = ["spur", daemon]
        if daemon == "spurd" and (args.source / "spurstepd").is_file():
            binaries.append("spurstepd")
        if args.auth:
            binaries.append("spurauthd")
        for binary in binaries:
            expected = hashlib.sha256((args.source / binary).read_bytes()).hexdigest()
            assert checksum(node, "/usr/local/bin/" + binary) == expected, (
                node,
                binary,
            )
        for service, old in before["services"].items():
            pid = docker(node, "systemctl", "show", service, "-p", "MainPID", "--value")
            assert pid not in ("0", old["pid"]), (node, service, "not restarted")
            assert checksum(node, "/proc/" + pid + "/exe") == checksum(
                node, "/usr/local/bin/" + service
            )
            assert (
                checksum(node, "/etc/systemd/system/" + service + ".service")
                == old["unit"]
            )
            assert "SPUR_CI_DRIFT" not in docker(
                node, "systemctl", "show", service, "-p", "Environment"
            )
            assert docker(node, "systemctl", "is-enabled", service) == "enabled"
            assert docker(node, "systemctl", "is-active", service) == "active"
        assert checksum(node, "/opt/spur/etc/spur.conf") == before["config"]
        environment = docker(node, "cat", "/etc/environment")
        assert (
            "SPUR_CONTROLLER_ADDR=" in environment
            and "SPUR_ACCOUNTING_ADDR=" not in environment
        )
        assert (
            docker(node, "readlink", "/usr/local/bin/squeue") == "/usr/local/bin/spur"
        )
        if daemon == "spurd":
            assert (
                docker(node, "systemctl", "show", daemon, "-p", "KillMode", "--value")
                == "process"
            )
            plugin = args.source / "spur_mpi_pmix.so"
            if plugin.is_file():
                assert (
                    checksum(node, "/usr/lib/spur/spur_mpi_pmix.so")
                    == hashlib.sha256(plugin.read_bytes()).hexdigest()
                )
        if args.auth:
            for path, expected in before["keys"].items():
                assert checksum(node, path) == expected
                assert docker(node, "stat", "-c", "%a:%U:%G", path) == "600:root:root"
            docker(node, "test", "-S", "/run/spur/spur-cluster/auth.sock")
            cli_config = docker(node, "cat", "/etc/spur/spur.conf")
            assert (
                'plugin = "spur"' in cli_config and "CI_CONFIG_DRIFT" not in cli_config
            )
            assert "SPUR_AUTH_PLUGIN=spur" in environment
            assert "SPUR_CLUSTER_NAME=spur-cluster" in environment
        print(
            node
            + ": all artifacts, unit reload/enable/restart, configuration and permissions verified"
        )


if __name__ == "__main__":
    main()
