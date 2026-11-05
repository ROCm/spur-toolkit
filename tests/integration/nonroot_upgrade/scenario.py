"""Executed as ubuntu inside the isolated runner; all targets are disposable."""

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time

import yaml

WORK = Path.cwd()
ANSIBLE = WORK / "ansible"
LOGS = WORK / "logs"
HOME = Path.home()
HOSTS = ("controller", "agent")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def command(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, timeout=30, **kwargs)
    require(
        result.returncode == 0, shlex.join(args) + "\n" + result.stdout + result.stderr
    )
    return result.stdout.strip()


def remote(host, *args, root=False, **kwargs):
    argv = (["sudo", "-n"] if root else []) + list(args)
    return command(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "ubuntu@" + host,
            shlex.join(argv),
        ],
        **kwargs,
    )


def inventory(workaround, source):
    defaults = yaml.safe_load((ANSIBLE / "inventory/group_vars/all.yml").read_text())
    defaults.update(
        ansible_connection="ssh",
        ansible_user="ubuntu",
        ansible_become=True,
        ansible_python_interpreter="/usr/bin/python3",
        spur_binary_src=str(source),
        spur_install_dir="/usr/local/bin",
        spur_home="/opt/spur",
        spur_transport="direct",
        spur_accounting_enabled=False,
        spur_auth_enabled=False,
        spur_mpi_plugin_enabled=False,
        spur_verify_enabled=False,
        spur_in_place_upgrade=True,
        spur_ignore_unreachable_agents=False,
        spur_overwrite_conf=False,
    )
    addresses = json.loads((WORK / "addresses.json").read_text())
    children = {
        "spur_controllers": {
            "hosts": {"controller": {"ansible_host": addresses["controller"]}}
        },
        "spur_agents": {"hosts": {"agent": {"ansible_host": addresses["agent"]}}},
        "spur_login": {"hosts": {}},
        "spur_accounting_node": {"hosts": {}},
    }
    if workaround:
        children["ansible_control"] = {
            "hosts": {
                "localhost": {
                    "ansible_connection": "local",
                    "ansible_become": False,
                }
            }
        }
    path = WORK / ("inventory-explicit.yml" if workaround else "inventory-implicit.yml")
    path.write_text(yaml.safe_dump({"all": {"vars": defaults, "children": children}}))
    return path


def play(label, path, inv, variables=None, directory=ANSIBLE, expect_failure=False):
    args = ["ansible-playbook", "-i", str(inv), str(path)]
    if variables:
        args += ["-e", json.dumps(variables)]
    log = LOGS / (label + ".log")
    with log.open("w") as stream:
        result = subprocess.run(
            args,
            cwd=directory,
            text=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=360,
            env=dict(
                os.environ,
                ANSIBLE_CONFIG=str(directory / "ansible.cfg"),
                ANSIBLE_NOCOLOR="1",
            ),
        )
    output = log.read_text()
    if expect_failure:
        require(result.returncode != 0, label + " unexpectedly succeeded")
        require(
            "/root/.cache/spur-ansible-" in output
            and "Could not find or access" in output,
            label + " did not reproduce the unreadable root-local archive:\n" + output,
        )
    else:
        require(result.returncode == 0, label + " failed:\n" + output)
    print("PASS: " + label, flush=True)
    return output


def root_caches(clear=False):
    script = """import json, pathlib, shutil
paths=[pathlib.Path('/root/.cache')/name for name in ('spur-ansible-install','spur-ansible-upgrade')]
print(json.dumps({str(p):[str(x) for x in p.glob('*')] for p in paths if p.exists()}))
"""
    if clear:
        script += "for p in paths:\n    if p.exists(): shutil.rmtree(p)\n"
    return json.loads(command(["sudo", "-n", "python3", "-c", script]))


def cache_snapshot():
    snapshot = {}
    for name in ("spur-ansible-install", "spur-ansible-upgrade"):
        directory = HOME / ".cache" / name
        if not directory.exists():
            continue
        require(
            directory.stat().st_uid == os.getuid()
            and directory.stat().st_mode & 0o777 == 0o700,
            "Local cache directory lost private user ownership",
        )
        for archive in directory.glob("*.tar.gz"):
            stat = archive.stat()
            require(
                stat.st_uid == os.getuid() and stat.st_mode & 0o777 == 0o600,
                "Local archive lost private user ownership: " + str(archive),
            )
            snapshot[str(archive)] = [
                stat.st_ino,
                stat.st_mtime_ns,
                hashlib.sha256(archive.read_bytes()).hexdigest(),
            ]
    require(not root_caches(), "Positive run created root-local bundle caches")
    return snapshot


def boundary_tasks(mode):
    if mode == "upgrade":
        plays = yaml.safe_load((ANSIBLE / "playbooks/rolling_upgrade.yml").read_text())
        preparation = next(
            p for p in plays if p["name"] == "Prepare and stage local in-place upgrades"
        )["tasks"][0]
        stage = yaml.safe_load(
            (ANSIBLE / "playbooks/tasks/stage_upgrade.yml").read_text()
        )[0]
        return [preparation, stage]
    tasks = yaml.safe_load((ANSIBLE / "roles/spur_install/tasks/main.yml").read_text())
    names = [
        "Prepare local install artifacts for this batch",
        "Select this host's prepared installation artifact",
        "Copy the binaries tarball to the remote host",
    ]
    return [next(t for t in tasks if t["name"] == name) for name in names]


def check_staging(mode):
    cache = HOME / ".cache" / ("spur-ansible-" + mode)
    for host, role in [("controller", "controller"), ("agent", "agent")]:
        expected = (
            {"spur", "spurctld"}
            if role == "controller"
            else {"spur", "spurd", "spurstepd"}
        )
        matches = []
        for archive in cache.glob("*.tar.gz"):
            with tarfile.open(archive) as bundle:
                if set(bundle.getnames()) == expected:
                    matches.append(archive)
        require(len(matches) == 1, "Unexpected boundary bundle set")
        archive = matches[0]
        destination = "/usr/local/bin/" + (
            ".spur-upgrade-" + role + ".tar.gz"
            if mode == "upgrade"
            else ".bins-" + archive.name
        )
        expected_mode = "600" if mode == "upgrade" else "644"
        require(
            remote(host, "stat", "-c", "%u:%g:%a", destination, root=True)
            == "0:0:" + expected_mode,
            "Remote staging did not preserve root ownership and mode",
        )
        require(
            remote(host, "sha256sum", destination, root=True).split()[0]
            == hashlib.sha256(archive.read_bytes()).hexdigest(),
            "Remote archive differs from the unprivileged source",
        )


def integration():
    source = WORK / "boundary-source"
    source.mkdir(mode=0o700)
    for name in ("spur", "spurctld", "spurd", "spurstepd"):
        (source / name).write_text("boundary fixture " + name)
    for mode in ("upgrade", "install"):
        tasks = boundary_tasks(mode)
        path = WORK / "boundary.yml"
        broken = copy.deepcopy(tasks)
        del broken[0]["vars"]["ansible_become"]
        path.write_text(
            yaml.safe_dump(
                [
                    dict(
                        hosts="spur_controllers:spur_agents",
                        gather_facts=False,
                        become=True,
                        tasks=broken,
                    )
                ]
            )
        )
        play(
            "boundary-" + mode + "-negative",
            path,
            inventory(False, source),
            expect_failure=True,
        )
        require(
            root_caches(clear=True),
            "Negative control did not create a root-local archive",
        )
        path.write_text(
            yaml.safe_dump(
                [
                    dict(
                        hosts="spur_controllers:spur_agents",
                        gather_facts=False,
                        become=True,
                        tasks=tasks,
                    )
                ]
            )
        )
        previous = None
        for workaround in (False, True):
            play(
                f"boundary-{mode}-localhost-{workaround}",
                path,
                inventory(workaround, source),
            )
            check_staging(mode)
            current = cache_snapshot()
            if previous is not None:
                require(
                    previous == current,
                    "Workaround rerun rebuilt or changed cached artifacts",
                )
            previous = current


@contextmanager
def missing_local_override():
    with tempfile.TemporaryDirectory(
        prefix="without-local-override-", dir=WORK
    ) as directory:
        clone = Path(directory) / "ansible"
        shutil.copytree(ANSIBLE, clone)
        path = clone / "playbooks/rolling_upgrade.yml"
        plays = yaml.safe_load(path.read_text())
        task = next(
            p for p in plays if p["name"] == "Prepare and stage local in-place upgrades"
        )["tasks"][0]
        del task["vars"]["ansible_become"]
        path.write_text(yaml.safe_dump(plays))
        yield clone


def service_snapshot():
    result = {}
    for host, daemon in [("controller", "spurctld"), ("agent", "spurd")]:
        pid = remote(host, "systemctl", "show", daemon, "-p", "MainPID", "--value")
        require(pid.isdigit() and pid != "0", host + " has no running daemon")
        result[host] = dict(
            pid=pid,
            binary=remote(
                host, "sha256sum", "/usr/local/bin/" + daemon, root=True
            ).split()[0],
            running=remote(
                host, "sha256sum", "/proc/" + pid + "/exe", root=True
            ).split()[0],
            config=remote(
                host, "sha256sum", "/opt/spur/etc/spur.conf", root=True
            ).split()[0],
        )
    return result


def check_installation(source):
    snapshot = service_snapshot()
    for host, names in [
        ("controller", ("spur", "spurctld")),
        ("agent", ("spur", "spurd", "spurstepd")),
    ]:
        for name in names:
            path = "/usr/local/bin/" + name
            require(
                remote(host, "stat", "-c", "%u:%g:%a", path) == "0:0:755",
                "Incorrect installed binary permissions",
            )
            require(
                remote(host, "sha256sum", path).split()[0]
                == hashlib.sha256((source / name).read_bytes()).hexdigest(),
                "Installed binary does not match requested build",
            )
        require(
            snapshot[host]["binary"] == snapshot[host]["running"],
            "Running daemon differs from disk",
        )
        daemon = "spurctld" if host == "controller" else "spurd"
        require(
            remote(
                host,
                "stat",
                "-c",
                "%u:%g:%a",
                "/etc/systemd/system/" + daemon + ".service",
            )
            == "0:0:644",
            "Incorrect service unit ownership/mode",
        )
        require(
            remote(host, "systemctl", "show", daemon, "-p", "User", "--value")
            == "root",
            "Daemon is not running as root",
        )
        require(
            remote(host, "stat", "-c", "%u:%g:%a", "/opt/spur") == "0:0:755",
            "State parent permissions changed",
        )
        remote(host, "bash", "-c", "test ! -w /usr/local/bin && test ! -x /root")
        require(
            remote(host, "readlink", "/usr/local/bin/squeue") == "/usr/local/bin/spur",
            "CLI alias is wrong",
        )
    return snapshot


def smoke_job(label):
    script = f"/tmp/nonroot-{label}.sh"
    output = f"/tmp/nonroot-{label}.out"
    remote("controller", "tee", script, input="#!/bin/sh\nid -u\n")
    submitted = remote(
        "controller",
        "bash",
        "-c",
        "cd /tmp && /usr/local/bin/spur submit --output=" + output + " " + script,
    )
    match = re.search(r"\b(\d+)\b", submitted)
    require(match is not None, "Could not parse submitted job: " + submitted)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = remote("controller", "/usr/local/bin/spur", "show", "job", match.group(1))
        if "JobState=COMPLETED" in job:
            require(
                remote("agent", "cat", output) == "1000",
                "Job did not execute as ubuntu",
            )
            print("PASS: unprivileged job " + label, flush=True)
            return
        require(
            not any(
                "JobState=" + state in job
                for state in ("FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL")
            ),
            job,
        )
        time.sleep(1)
    raise AssertionError("Job did not complete: " + job)


def end_to_end():
    old, new = WORK / "old", WORK / "new"
    play("deploy-old-as-ubuntu", "playbooks/deploy.yml", inventory(False, old))
    check_installation(old)
    smoke_job("before")
    for host in HOSTS:
        remote(
            host,
            "bash",
            "-c",
            "mkdir -p /opt/spur/shared && mv /opt/spur/etc/spur.conf /opt/spur/shared/spur.conf && ln -s /opt/spur/shared/spur.conf /opt/spur/etc/spur.conf",
            root=True,
        )
    before = service_snapshot()
    inv = inventory(False, new)
    with missing_local_override() as clone:
        play(
            "rolling-negative",
            "playbooks/rolling_upgrade.yml",
            inv,
            directory=clone,
            expect_failure=True,
        )
    require(before == service_snapshot(), "Failed staging changed services or config")
    require(
        root_caches(clear=True),
        "Negative rolling upgrade did not create a root-local archive",
    )
    play("rolling-as-ubuntu", "playbooks/rolling_upgrade.yml", inv)
    after = check_installation(new)
    for host in HOSTS:
        require(before[host]["pid"] != after[host]["pid"], "Daemon was not restarted")
        require(
            before[host]["binary"] != after[host]["binary"],
            "No actual version upgrade occurred",
        )
        require(
            before[host]["config"] == after[host]["config"],
            "Upgrade changed preserved config",
        )
        require(
            remote(host, "readlink", "/opt/spur/etc/spur.conf")
            == "/opt/spur/shared/spur.conf",
            "Config symlink was replaced",
        )
    smoke_job("after")
    previous = cache_snapshot()
    play(
        "rolling-with-localhost-workaround",
        "playbooks/rolling_upgrade.yml",
        inventory(True, new),
    )
    check_installation(new)
    require(previous == cache_snapshot(), "Rolling rerun rebuilt local archives")
    smoke_job("rerun")


def main():
    LOGS.mkdir()
    os.umask(0o022)
    require(
        os.getuid() == 1000 and HOME == Path("/home/ubuntu"),
        "Runner must be actual ubuntu, not root",
    )
    require(
        not os.access("/root", os.X_OK) and not os.access("/usr/local/bin", os.W_OK),
        "Runner is overprivileged",
    )
    require(
        command(["sudo", "-n", "sh", "-c", "id -u; printf '%s' \"$HOME\""])
        == "0\n/root",
        "Real sudo must reset HOME",
    )
    require(not root_caches(), "Fixture starts with unexpected root bundle caches")
    for host in HOSTS:
        require(remote(host, "id", "-u") == "1000", "SSH login is not ubuntu")
        require(
            remote(host, "id", "-u", root=True) == "0", "Remote sudo is unavailable"
        )
    integration()
    end_to_end()
    print(
        "PASS: integration and real SSH/sudo end-to-end permission scenarios",
        flush=True,
    )


if __name__ == "__main__":
    main()
