"""Run permission integration and end-to-end tests in disposable Docker hosts."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
BINARIES = ("spur", "spurctld", "spurd", "spurstepd")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def release(root, tag):
    name = f"spur-{tag}-linux-amd64.tar.gz"
    url = f"https://github.com/ROCm/spur/releases/download/{tag}/{name}"
    archive = root / name
    checksum = root / (name + ".sha256")
    for source, destination in [(url, archive), (url + ".sha256", checksum)]:
        run(
            "curl",
            "-fsSL",
            "--retry",
            "3",
            "--max-time",
            "180",
            source,
            "-o",
            str(destination),
        )
    fields = checksum.read_text().split()
    if len(fields) != 2 or fields[1].lstrip("*") != name:
        raise RuntimeError("Unexpected release checksum format")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != fields[0]:
        raise RuntimeError("Release checksum mismatch")
    destination = root / tag
    destination.mkdir()
    with tarfile.open(archive) as bundle:
        for binary in BINARIES:
            member = bundle.getmember(f"spur-{tag}-linux-amd64/bin/{binary}")
            if not member.isfile():
                raise RuntimeError(f"Not a regular release binary: {binary}")
            with bundle.extractfile(member) as source, (destination / binary).open(
                "wb"
            ) as target:
                shutil.copyfileobj(source, target)
            (destination / binary).chmod(0o755)
    return destination


def wait_systemd(node):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["docker", "exec", node, "systemctl", "is-system-running"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if state in ("running", "degraded"):
            run("docker", "exec", node, "systemctl", "is-active", "--quiet", "ssh")
            return
        time.sleep(1)
    raise RuntimeError(f"systemd did not become ready in {node}: {state}")


def cleanup(nodes, network, logs):
    try:
        if nodes and nodes[-1].endswith("-runner"):
            subprocess.run(
                ["docker", "cp", nodes[-1] + ":/home/ubuntu/work/logs/.", str(logs)],
                check=False,
            )
        for node in nodes:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    node,
                    "journalctl",
                    "--no-pager",
                    "-u",
                    "spurctld",
                    "-u",
                    "spurd",
                    "-u",
                    "ssh",
                    "-n",
                    "100",
                ],
                capture_output=True,
                text=True,
            )
            (logs / (node.rsplit("-", 1)[1] + "-journal.log")).write_text(
                result.stdout + result.stderr
            )
    finally:
        for node in nodes:
            subprocess.run(["docker", "rm", "-f", node], check=False)
        if network:
            run("docker", "network", "rm", network)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ansible-core", default="2.17.14")
    parser.add_argument("--ubuntu", default="24.04", choices=["22.04", "24.04"])
    parser.add_argument("--logs", type=Path, default=Path("nonroot-upgrade-logs"))
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    prefix = "spur-permissions-" + uuid.uuid4().hex[:10]
    image = f"spur-permissions:{args.ubuntu}-{args.ansible_core}"
    nodes = []
    network_created = False
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        root = Path(temporary)
        old = release(root, "v0.14.0")
        new = release(root, "v0.15.0")
        if (old / "spurd").read_bytes() == (new / "spurd").read_bytes():
            raise RuntimeError("End-to-end test needs distinct old and new builds")
        run(
            "docker",
            "build",
            "--build-arg",
            f"UBUNTU_VERSION={args.ubuntu}",
            "--build-arg",
            f"ANSIBLE_CORE={args.ansible_core}",
            "-t",
            image,
            str(HERE),
        )
        try:
            run("docker", "network", "create", prefix)
            network_created = True
            for role in ("controller", "agent", "runner"):
                name = prefix + "-" + role
                command = [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--hostname",
                    role,
                    "--network",
                    prefix,
                    "--network-alias",
                    role,
                ]
                if role != "runner":
                    # Only the managed hosts need root to boot systemd.
                    command += [
                        "--user",
                        "root",
                        "--privileged",
                        "--cgroupns=host",
                        "-v",
                        "/sys/fs/cgroup:/sys/fs/cgroup:rw",
                        "--tmpfs",
                        "/run",
                        "--tmpfs",
                        "/run/lock",
                        "--tmpfs",
                        "/tmp",
                    ]
                command += [image]
                if role != "runner":
                    command += ["/sbin/init"]
                nodes.append(name)
                run(*command)
                if role != "runner":
                    wait_systemd(name)
            controller, agent, runner = nodes
            run(
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(root / "id_ed25519"),
            )
            known_hosts = []
            for node in (controller, agent):
                run(
                    "docker",
                    "exec",
                    node,
                    "install",
                    "-d",
                    "-o",
                    "ubuntu",
                    "-g",
                    "ubuntu",
                    "-m",
                    "0700",
                    "/home/ubuntu/.ssh",
                    "/home/ubuntu/.cache",
                    "/home/ubuntu/.ansible",
                    "/home/ubuntu/.ansible/tmp",
                )
                run(
                    "docker",
                    "cp",
                    str(root / "id_ed25519.pub"),
                    node + ":/home/ubuntu/.ssh/authorized_keys",
                )
                run(
                    "docker",
                    "exec",
                    node,
                    "chown",
                    "ubuntu:ubuntu",
                    "/home/ubuntu/.ssh/authorized_keys",
                )
                run(
                    "docker",
                    "exec",
                    node,
                    "chmod",
                    "0600",
                    "/home/ubuntu/.ssh/authorized_keys",
                )
                role = node.rsplit("-", 1)[1]
                key = output(
                    "docker", "exec", node, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"
                )
                address = output(
                    "docker",
                    "inspect",
                    "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    node,
                )
                known_hosts.append(role + "," + address + " " + key)
            (root / "known_hosts").write_text("\n".join(known_hosts) + "\n")
            payload = root / "work"
            payload.mkdir()
            shutil.copytree(REPO / "ansible", payload / "ansible")
            shutil.copytree(
                HERE, payload / "scenario", ignore=shutil.ignore_patterns("__pycache__")
            )
            for source, name in [(old, "old"), (new, "new")]:
                shutil.copytree(source, payload / name)
            addresses = {
                role: output(
                    "docker",
                    "inspect",
                    "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    node,
                )
                for role, node in [("controller", controller), ("agent", agent)]
            }
            (payload / "addresses.json").write_text(json.dumps(addresses))
            run("docker", "cp", str(payload), runner + ":/home/ubuntu/work")
            run(
                "docker",
                "exec",
                "--user",
                "root",
                runner,
                "install",
                "-d",
                "-m",
                "0700",
                "/home/ubuntu/.ssh",
                "/home/ubuntu/.cache",
                "/home/ubuntu/.ansible",
                "/home/ubuntu/.ansible/tmp",
            )
            for name in ("id_ed25519", "known_hosts"):
                run(
                    "docker",
                    "cp",
                    str(root / name),
                    runner + ":/home/ubuntu/.ssh/" + name,
                )
            run(
                "docker",
                "exec",
                "--user",
                "root",
                runner,
                "chown",
                "-R",
                "ubuntu:ubuntu",
                "/home/ubuntu",
            )
            run(
                "docker",
                "exec",
                runner,
                "chmod",
                "0700",
                "/home/ubuntu/.ssh",
                "/home/ubuntu/work/old",
                "/home/ubuntu/work/new",
            )
            run(
                "docker",
                "exec",
                runner,
                "chmod",
                "0600",
                "/home/ubuntu/.ssh/id_ed25519",
            )
            run(
                "docker",
                "exec",
                "--user",
                "ubuntu",
                "--env",
                "HOME=/home/ubuntu",
                "--workdir",
                "/home/ubuntu/work",
                runner,
                "/opt/ansible/bin/python",
                "scenario/scenario.py",
            )
        finally:
            cleanup(nodes, prefix if network_created else None, args.logs)


if __name__ == "__main__":
    main()
