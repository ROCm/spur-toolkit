"""Local fixtures exercise production helpers and the real rolling playbook."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "spur_upgrade", ROOT / "ansible/module_utils/spur_upgrade.py"
)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("spur", "spurctld", "spurd", "spurstepd", "spurauthd"):
            path = self.source / name
            path.write_text("new " + name)
            path.chmod(0o755)
        self.spec = {
            "source": str(self.source),
            "roles": ["agent"],
            "auth": False,
            "mpi": False,
            "in_place": True,
            "overwrite_conf": False,
        }

    def test_fleet_hashes_sources_once_and_keeps_dual_roles_separate(self):
        targets = {"agent-%s" % n: self.spec for n in range(200)}
        targets["dual"] = dict(self.spec, roles=["controller", "agent"])
        with patch.object(upgrade, "digest", wraps=upgrade.digest) as hashed:
            result = upgrade.prepare(targets, self.root / "cache")
        self.assertEqual(hashed.call_count, 4)
        self.assertEqual(result["bundles_built"], 2)
        dual = result["artifacts"]["dual"]
        self.assertNotIn("spurd", dual["controller"]["manifest"])
        self.assertNotIn("spurctld", dual["agent"]["manifest"])
        self.assertEqual(dual["agent"], result["artifacts"]["agent-199"]["agent"])

    def test_source_variants_auth_plugin_and_missing_stepd(self):
        plugin = self.source / "libspur_mpi_pmix.so"
        plugin.write_bytes(b"plugin")
        plugin.chmod(0o644)
        other = self.root / "other"
        shutil.copytree(self.source, other)
        (other / "spurd").write_text("different build")
        targets = {
            "a": dict(self.spec, auth=True, mpi=True),
            "b": dict(self.spec, source=str(other)),
        }
        result = upgrade.prepare(targets, self.root / "cache")
        self.assertEqual(result["bundles_built"], 2)
        manifest = result["artifacts"]["a"]["agent"]["manifest"]
        self.assertIn("spurauthd", manifest)
        self.assertIn("spur_mpi_pmix.so", manifest)
        (self.source / "spurstepd").unlink()
        result = upgrade.prepare(targets, self.root / "cache")
        self.assertNotIn("spurstepd", result["artifacts"]["a"]["agent"]["manifest"])

    def activation_fixture(self):
        bundle = upgrade.prepare({"a": self.spec}, self.root / "cache")["artifacts"][
            "a"
        ]["agent"]
        install = self.root / "installed"
        shutil.copytree(self.source, install)
        (install / "spurd").write_text("old spurd")
        (self.root / "etc").mkdir()
        (self.root / "etc/spur.conf").write_text("preserved")
        state = {
            "pid": 123,
            "KillMode": "process",
            "Environment": "SPUR_STEPD_STATE_DIR=/state",
        }
        args = dict(
            bundle,
            install_dir=str(install),
            home=str(self.root),
            role="agent",
            port=1234,
            timeout=1,
            plugin_dir=str(install),
            auth=False,
            cluster="fixture",
        )
        return install, state, args

    def test_controller_health_uses_preserved_daemon_config_not_stale_cli_auth(self):
        self.spec["roles"] = ["controller"]
        (self.source / "spur").write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "assert 'SPUR_AUTH_PLUGIN' not in os.environ\n"
            "assert 'SPUR_CLUSTER_NAME' not in os.environ\n"
            "assert pathlib.Path(os.environ['SPUR_CONF']).read_text() == 'preserved'\n"
            "assert os.environ['SPUR_CONTROLLER_ADDR'] == 'http://127.0.0.1:1234'\n"
            "print('healthy')\n"
        )
        bundle = upgrade.prepare({"ctl": self.spec}, self.root / "cache")["artifacts"][
            "ctl"
        ]["controller"]
        install = self.root / "installed"
        shutil.copytree(self.source, install)
        (self.root / "etc").mkdir()
        (self.root / "etc/spur.conf").write_text("preserved")
        real_command = upgrade.command

        def command(argv, **kwargs):
            if argv[0] == str(install / "spur"):
                return real_command(argv, **kwargs)
            if argv[-1] == "-V":
                return "fixture version"
            self.fail("A matching controller binary must not be restarted: %r" % argv)

        with (
            patch.object(upgrade, "service", return_value={"pid": 123}),
            patch.object(
                upgrade, "running_digest", return_value=bundle["manifest"]["spurctld"]
            ),
            patch.object(upgrade.socket, "create_connection"),
            patch.object(upgrade, "command", side_effect=command),
            patch.dict(
                os.environ,
                {
                    "SPUR_AUTH_PLUGIN": "spur",
                    "SPUR_CLUSTER_NAME": "stale",
                    "SPUR_CONF": "/wrong/config",
                },
            ),
        ):
            result = upgrade.activate(
                **bundle,
                install_dir=str(install),
                home=str(self.root),
                role="controller",
                port=1234,
                timeout=1,
                plugin_dir=str(install),
                auth=False,
                cluster="fixture",
            )
        self.assertFalse(result["changed"])
        self.assertTrue(result["binary_current"])
        self.assertEqual(result["restarted"], [])
        self.assertEqual((self.root / "etc/spur.conf").read_text(), "preserved")

    def test_unhealthy_existing_service_fails_without_provisioning_or_binary_changes(
        self,
    ):
        install, _, args = self.activation_fixture()
        before = (install / "spurd").read_bytes()
        with patch.object(
            upgrade,
            "command",
            return_value=(
                "ActiveState=activating\nSubState=auto-restart\nMainPID=0\n"
                "Result=signal\nExecMainStatus=9\n"
            ),
        ) as commands:
            with self.assertRaisesRegex(RuntimeError, "spurd is unhealthy.*exit=9"):
                upgrade.activate(**args)
        self.assertEqual(commands.call_count, 1)
        self.assertEqual(commands.call_args.args[0][:3], ["systemctl", "show", "spurd"])
        self.assertEqual((install / "spurd").read_bytes(), before)
        self.assertEqual(list(install.glob(".spur-upgrade-*")), [])

    def test_readiness_timeout_reports_the_underlying_cli_error(self):
        cli = self.root / "failing-cli"
        cli.write_text(
            "#!/bin/sh\necho 'controller did not advertise a native auth audience' >&2\nexit 1\n"
        )
        cli.chmod(0o755)
        with (
            patch.object(upgrade, "service", return_value={"pid": 123}),
            patch.object(upgrade, "running_digest", return_value="expected"),
        ):
            with self.assertRaisesRegex(RuntimeError, "native auth audience"):
                upgrade.wait_ready(
                    "spurctld",
                    "expected",
                    "/fake/spurctld",
                    None,
                    0,
                    health_command=[str(cli)],
                )

    def test_stale_process_restarts_even_when_disk_matches_and_rerun_is_noop(self):
        install, state, args = self.activation_fixture()
        shutil.copyfile(self.source / "spurd", install / "spurd")
        with (
            patch.object(upgrade, "service", return_value=state),
            patch.object(
                upgrade,
                "process_args",
                return_value=["spurd", "--hostname", "registered-name"],
            ),
            patch.object(upgrade, "running_digest", return_value="old"),
            patch.object(
                upgrade, "command", return_value="fixture version"
            ) as commands,
            patch.object(upgrade, "wait_ready", return_value=state),
        ):
            result = upgrade.activate(**args)
            self.assertEqual(result["replaced"], [])
            self.assertEqual(result["restarted"], ["spurd"])
            self.assertEqual(result["node_name"], "registered-name")
            self.assertIn(
                unittest.mock.call(["systemctl", "restart", "spurd"], timeout=1),
                commands.call_args_list,
            )
            with patch.object(
                upgrade, "running_digest", return_value=args["manifest"]["spurd"]
            ):
                result = upgrade.activate(**args)
                self.assertFalse(result["changed"])
                self.assertEqual(result["restarted"], [])

    def test_unsafe_unit_and_corrupt_archive_fail_before_replacement(self):
        install, state, args = self.activation_fixture()
        before = (install / "spurd").read_bytes()
        with (
            patch.object(
                upgrade, "service", return_value=dict(state, KillMode="control-group")
            ),
            patch.object(upgrade, "running_digest", return_value="old"),
        ):
            with self.assertRaisesRegex(ValueError, "KillMode"):
                upgrade.activate(**args)
        args["manifest"] = dict(args["manifest"], spurstepd="invalid")
        with (
            patch.object(upgrade, "service", return_value=state),
            patch.object(upgrade, "running_digest", return_value="old"),
            patch.object(
                upgrade, "process_args", return_value=["spurd", "--hostname=worker"]
            ),
        ):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                upgrade.activate(**args)
        self.assertEqual((install / "spurd").read_bytes(), before)
        self.assertEqual(list(install.glob(".spur-upgrade-*")), [])

    def test_auth_mint_restarts_before_agent_and_incomplete_units_request_provisioning(
        self,
    ):
        self.spec["auth"] = True
        install, state, args = self.activation_fixture()
        args["auth"] = True
        with (
            patch.object(upgrade, "service", return_value=state),
            patch.object(
                upgrade, "process_args", return_value=["spurd", "--hostname", "worker"]
            ),
            patch.object(upgrade, "running_digest", return_value="old"),
            patch.object(
                upgrade, "command", return_value="fixture version"
            ) as commands,
            patch.object(upgrade, "wait_ready", return_value=state) as ready,
        ):
            result = upgrade.activate(**args)
            self.assertEqual(result["restarted"], ["spurauthd", "spurd"])
            restart_calls = [
                c.args[0]
                for c in commands.call_args_list
                if c.args[0][0] == "systemctl"
            ]
            self.assertEqual(
                restart_calls,
                [
                    ["systemctl", "restart", "spurauthd"],
                    ["systemctl", "restart", "spurd"],
                ],
            )
            self.assertEqual(
                [c.args[0] for c in ready.call_args_list], ["spurauthd", "spurd"]
            )
            with patch.object(
                upgrade, "service", return_value=dict(state, Environment="")
            ):
                with self.assertRaises(upgrade.ConfigurationRequired):
                    upgrade.activate(**args)

    def test_incompatible_build_fails_before_replacing_any_binary(self):
        install, state, args = self.activation_fixture()
        before = (install / "spurd").read_bytes()
        with (
            patch.object(upgrade, "service", return_value=state),
            patch.object(
                upgrade, "process_args", return_value=["spurd", "--hostname", "worker"]
            ),
            patch.object(upgrade, "running_digest", return_value="old"),
            patch.object(upgrade, "command", side_effect=OSError("exec format error")),
        ):
            with self.assertRaisesRegex(OSError, "exec format error"):
                upgrade.activate(**args)
        self.assertEqual((install / "spurd").read_bytes(), before)
        self.assertEqual(list(install.glob(".spur-upgrade-*")), [])

    def test_older_build_requests_existing_roles_before_touching_the_host(self):
        install, state, args = self.activation_fixture()
        args["manifest"].pop("spurstepd")
        with patch.object(upgrade, "service") as service:
            with self.assertRaises(upgrade.ConfigurationRequired):
                upgrade.activate(**args)
            service.assert_not_called()
        self.assertEqual((install / "spurd").read_text(), "old spurd")

    def test_restart_failure_reports_that_activation_changed_the_host(self):
        install, state, args = self.activation_fixture()
        with (
            patch.object(upgrade, "service", return_value=state),
            patch.object(
                upgrade, "process_args", return_value=["spurd", "--hostname", "worker"]
            ),
            patch.object(upgrade, "running_digest", return_value="old"),
            patch.object(
                upgrade,
                "command",
                side_effect=["new version", RuntimeError("restart failed")],
            ),
        ):
            with self.assertRaisesRegex(upgrade.ActivationFailed, "restart failed"):
                upgrade.activate(**args)
        self.assertEqual(upgrade.digest(install / "spurd"), args["manifest"]["spurd"])
        self.assertEqual(list(install.glob(".spur-upgrade-*")), [])


DAEMON = r"""
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
  if (argc == 2 && strcmp(argv[1], "-V") == 0) { puts(VERSION); return 0; }
  int port = atoi(argv[argc - 1]), fd = socket(AF_INET, SOCK_STREAM, 0), yes = 1;
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
  struct sockaddr_in addr = {.sin_family=AF_INET, .sin_addr.s_addr=htonl(INADDR_LOOPBACK), .sin_port=htons(port)};
  if (bind(fd, (void *)&addr, sizeof(addr)) || listen(fd, 16)) return 1;
  for (;;) { int client = accept(fd, NULL, NULL); if (client >= 0) close(client); }
}
"""

SYSTEMCTL = r"""#!/usr/bin/env python3
import json, os, signal, subprocess, sys
from pathlib import Path
root = Path(os.environ['SPUR_TEST_STATE'])
action, name = sys.argv[1:3]
path = root / (name + '.json')
data = json.loads(path.read_text())
if action == 'show':
    print('MainPID=' + str(data['pid']))
    print('ActiveState=active')
    print('KillMode=' + data.get('kill_mode', 'process'))
    print('Environment=SPUR_STEPD_STATE_DIR=' + str(root / 'state'))
elif action == 'restart':
    os.kill(data['pid'], signal.SIGTERM)
    process = subprocess.Popen(data['args'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    data['pid'] = process.pid
    path.write_text(json.dumps(data))
    with (root / 'restarts').open('a') as log: log.write(name + '\n')
else:
    sys.exit('unexpected provisioning operation: ' + action)
"""

CLI = r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
with (Path(os.environ['SPUR_TEST_STATE']) / 'cli-calls').open('a') as log:
    log.write(' '.join(sys.argv[1:]) + '\n')
if sys.argv[1:] == ['nodes']: print('healthy')
elif sys.argv[1:] == ['show', 'node']:
    print('NodeName=registered-worker\nState=ALLOCATED\nCPUAlloc=1')
else: sys.exit('unexpected state mutation')
"""


@unittest.skipUnless(
    shutil.which("ansible-playbook") and shutil.which("cc"),
    "requires ansible-core and a C compiler",
)
class PlaybookTests(unittest.TestCase):
    def test_real_playbook_preserves_provisioning_and_avoids_noop_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fake-bin").mkdir()
            (root / "fake-bin/systemctl").write_text(SYSTEMCTL)
            (root / "fake-bin/systemctl").chmod(0o755)
            (root / "daemon.c").write_text(DAEMON)
            env = dict(
                os.environ,
                SPUR_TEST_STATE=str(root),
                PATH=str(root / "fake-bin") + os.pathsep + os.environ["PATH"],
                ANSIBLE_NOCOLOR="1",
            )
            source = root / "source"
            source.mkdir()
            subprocess.run(
                [
                    "cc",
                    '-DVERSION="new"',
                    str(root / "daemon.c"),
                    "-o",
                    str(source / "spurd"),
                ],
                check=True,
            )
            shutil.copyfile(source / "spurd", source / "spurctld")
            shutil.copyfile(source / "spurd", source / "spurstepd")
            for binary in source.iterdir():
                binary.chmod(0o755)
            (source / "spur").write_text(CLI)
            (source / "spur").chmod(0o755)
            hosts, processes, saved = {}, [], {}
            try:
                for name, daemon in (
                    ("controller", "spurctld"),
                    ("worker-alias", "spurd"),
                ):
                    home = root / name
                    (home / "bin").mkdir(parents=True)
                    (home / "etc").mkdir()
                    config = home / "etc/spur.conf"
                    config.write_text(
                        '[auth]\nplugin = "jwt"\n[cgroup]\nenabled = true\n'
                    )
                    saved[config] = config.read_bytes()
                    with socket.socket() as sock:
                        sock.bind(("127.0.0.1", 0))
                        port = sock.getsockname()[1]
                    shutil.copytree(source, home / "bin", dirs_exist_ok=True)
                    subprocess.run(
                        [
                            "cc",
                            '-DVERSION="old"',
                            str(root / "daemon.c"),
                            "-o",
                            str(home / "bin" / daemon),
                        ],
                        check=True,
                    )
                    args = [
                        str(home / "bin" / daemon),
                        "--hostname",
                        "registered-worker",
                        str(port),
                    ]
                    proc = subprocess.Popen(args)
                    processes.append(proc)
                    (root / (daemon + ".json")).write_text(
                        json.dumps({"pid": proc.pid, "args": args})
                    )
                    hosts[name] = {
                        "ansible_connection": "local",
                        "ansible_become": False,
                        "ansible_python_interpreter": shutil.which("python3"),
                        "spur_home": str(home),
                        "spur_install_dir": str(home / "bin"),
                        (
                            "spur_controller_port"
                            if daemon == "spurctld"
                            else "spur_agent_port"
                        ): port,
                    }
                inventory = {
                    "all": {
                        "children": {
                            "spur_controllers": {
                                "hosts": {"controller": hosts["controller"]}
                            },
                            "spur_agents": {
                                "hosts": {"worker-alias": hosts["worker-alias"]}
                            },
                        }
                    }
                }
                (root / "inventory.yml").write_text(json.dumps(inventory))
                command = [
                    "ansible-playbook",
                    "-i",
                    str(root / "inventory.yml"),
                    "playbooks/rolling_upgrade.yml",
                    "-e",
                    json.dumps(
                        {
                            "spur_in_place_upgrade": True,
                            "spur_binary_src": str(source),
                            "spur_mpi_plugin_enabled": False,
                        }
                    ),
                ]
                # Defaults must be inventory vars, so per-host paths retain their precedence.
                defaults = ROOT / "ansible/inventory/group_vars/all.yml"
                (root / "group_vars").mkdir()
                shutil.copyfile(defaults, root / "group_vars/all.yml")
                for run in range(2):
                    result = subprocess.run(
                        command,
                        cwd=ROOT / "ansible",
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=90,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertIn("2/2 nodes verified", result.stdout)
                    self.assertNotIn("spur_install :", result.stdout)
                    self.assertNotIn("spur_auth :", result.stdout)
                    self.assertNotIn("TASK [Gathering Facts]", result.stdout)
                    agent_play = result.stdout.split(
                        "PLAY [Rolling upgrade — agents", 1
                    )[1].split("\nPLAY [", 1)[0]
                    self.assertEqual(agent_play.count("TASK ["), 5)
                    self.assertEqual(
                        result.stdout.count("TASK [Stage binary bundles"), 2
                    )
                    self.assertEqual(
                        (root / "restarts").read_text().splitlines(),
                        ["spurctld", "spurd"],
                    )
                    for path, content in saved.items():
                        self.assertEqual(path.read_bytes(), content)
                    calls = (root / "cli-calls").read_text()
                    self.assertNotIn("drain", calls)
                    self.assertNotIn("RESUME", calls)
                    self.assertEqual(calls.splitlines().count("show node"), run + 1)
                for extra in (
                    ["--limit", "worker-alias"],
                    ["--limit", "worker-alias", "--check"],
                ):
                    result = subprocess.run(
                        command + extra,
                        cwd=ROOT / "ansible",
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=90,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(
                        (root / "restarts").read_text().splitlines(),
                        ["spurctld", "spurd"],
                    )
                # A bound, non-listening loopback socket deterministically refuses SSH.
                with socket.socket() as refused:
                    refused.bind(("127.0.0.1", 0))
                    inventory["all"]["children"]["spur_agents"]["hosts"]["offline"] = {
                        "ansible_connection": "ssh",
                        "ansible_host": "127.0.0.1",
                        "ansible_port": refused.getsockname()[1],
                        "ansible_become": False,
                    }
                    (root / "inventory.yml").write_text(json.dumps(inventory))
                    result = subprocess.run(
                        command + ["-e", "spur_ignore_unreachable_agents=true"],
                        cwd=ROOT / "ansible",
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=90,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertIn("2/3 nodes verified", result.stdout)
                    self.assertEqual(
                        (root / "restarts").read_text().splitlines(),
                        ["spurctld", "spurd"],
                    )
            finally:
                for path in root.glob("*.json"):
                    pid = json.loads(path.read_text()).get("pid")
                    if pid:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                for proc in processes:
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
