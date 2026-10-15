"""Exercise rollout boundaries with real preparation, staging and failure handling."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "ansible"


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires ansible-core")
class UpgradeBoundaries(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copytree(ANSIBLE / "playbooks/tasks", self.root / "tasks")
        self.controllers = ["controller-a", "controller-b"]
        self.agents = ["first", "second", "later"]
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ["spur", "spurctld", "spurd", "spurstepd", "spurauthd"]:
            (self.source / name).write_text(name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        systemctl = self.bin / "systemctl"
        systemctl.write_text(
            "#!/usr/bin/env python3\nimport os, pathlib\n"
            f"bad = pathlib.Path({str(self.root / 'unhealthy')!r})\n"
            "healthy = not bad.exists() or bad.read_text() != os.environ['SPUR_PROBE_HOST']\n"
            "print('ActiveState=active\\nMainPID=123' if healthy else 'ActiveState=failed\\nMainPID=0')\n"
        )
        systemctl.chmod(0o755)
        self.inventory = {"all": {"children": {}}}
        for group, names in [
            ("spur_controllers", self.controllers),
            ("spur_agents", self.agents),
        ]:
            hosts = {}
            for name in names:
                home = self.root / name
                home.mkdir()
                (home / "spurstepd").write_text("existing supervisor")
                cli = home / "spur"
                cli.write_text(
                    "#!/usr/bin/env python3\nfrom pathlib import Path\nimport sys\n"
                    f"if Path({str(self.root / 'rpc-error')!r}).exists(): sys.exit('fixture RPC error')\n"
                    + "print("
                    + repr(
                        "\n".join("NodeName=" + n + "\nState=IDLE" for n in self.agents)
                    )
                    + ")\n"
                )
                cli.chmod(0o755)
                hosts[name] = {"spur_install_dir": str(home), "spur_home": str(home)}
            self.inventory["all"]["children"][group] = {"hosts": hosts}
        defaults = yaml.safe_load(
            (ANSIBLE / "inventory/group_vars/all.yml").read_text()
        )
        defaults.update(
            ansible_connection="local",
            ansible_become=False,
            ansible_python_interpreter=sys.executable,
            spur_binary_src=str(self.source),
            spur_in_place_upgrade=True,
            spur_ignore_unreachable_agents=True,
            spur_rolling_batch_size=2,
            spur_auth_enabled=False,
            spur_mpi_plugin_enabled=False,
        )
        defaults.pop("spur_install_dir")
        defaults.pop("spur_home")
        self.variables = defaults
        self.plays = [
            p
            for p in yaml.safe_load(
                (ANSIBLE / "playbooks/rolling_upgrade.yml").read_text()
            )
            if p["name"].startswith(
                (
                    "Prepare and stage",
                    "Stage controller",
                    "Stage agent",
                    "Rolling upgrade",
                )
            )
        ]
        for play in self.plays:
            play["environment"] = {"SPUR_PROBE_HOST": "{{ inventory_hostname }}"}
        for role in ["agent", "controller"]:
            (self.root / f"tasks/upgrade_{role}_full.yml").write_text(
                yaml.safe_dump(
                    [
                        {
                            "name": "Record provisioning entry",
                            "ansible.builtin.file": {
                                "path": "{{ spur_home }}/provisioned",
                                "state": "touch",
                            },
                        }
                    ]
                )
            )
        library = self.root / "library"
        library.mkdir()
        module = (
            (ANSIBLE / "library/spur_activate_upgrade.py")
            .read_text()
            .replace(
                "    try:\n        result = activate",
                "    module.exit_json(changed=False, node_name=node)\n    try:\n        result = activate",
            )
        )
        (library / "spur_activate_upgrade.py").write_text(module)
        # Exercise the same RPC-error handling without spending its production retry window.
        query = self.root / "tasks/verify_binary_registration.yml"
        query.write_text(query.read_text().replace("timeout: 30", "timeout: 0"))
        self.env = dict(
            os.environ,
            ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg"),
            ANSIBLE_LIBRARY=str(library) + os.pathsep + str(ANSIBLE / "library"),
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            ANSIBLE_NOCOLOR="1",
        )

    def run_play(self):
        inventory = self.root / "inventory.yml"
        inventory.write_text(json.dumps(self.inventory))
        playbook = self.root / "play.yml"
        playbook.write_text(yaml.safe_dump(self.plays))
        variables = self.root / "vars.json"
        variables.write_text(json.dumps(self.variables))
        return subprocess.run(
            [
                "ansible-playbook",
                "-i",
                str(inventory),
                str(playbook),
                "-e",
                "@" + str(variables),
            ],
            cwd=ANSIBLE,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=90,
        )

    def unreachable(self):
        bound = socket.socket()
        bound.bind(("127.0.0.1", 0))
        self.addCleanup(bound.close)
        return {
            "ansible_connection": "ssh",
            "ansible_host": "127.0.0.1",
            "ansible_port": bound.getsockname()[1],
            "ansible_ssh_timeout": 1,
        }

    def change_connection(self, host, variables):
        return {
            "hosts": host,
            "gather_facts": False,
            "tasks": [{"ansible.builtin.set_fact": variables}],
        }

    def test_missing_auth_build_fails_before_any_host_is_provisioned(self):
        self.variables["spur_auth_enabled"] = True
        (self.source / "spurauthd").unlink()
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Missing Spur artifact", result.stdout)
        self.assertFalse(list(self.root.glob("*/provisioned")), result.stdout)

    def test_external_playbook_discovers_role_modules_and_utilities(self):
        config = self.root / "external.cfg"
        config.write_text("[defaults]\nroles_path = " + str(ANSIBLE / "roles") + "\n")
        playbook = self.root / "external.yml"
        tasks = [
            {"ansible.builtin.import_role": {"name": role}, "when": False}
            for role in ["spur_install", "spur_agent", "spur_verify"]
        ]
        tasks.extend(
            [
                {"spur_prepare_install": {"targets": {}}},
                {
                    "spur_node_snapshot": {
                        "binary": str(self.root / "controller-a/spur"),
                        "expected": self.agents,
                    }
                },
            ]
        )
        playbook.write_text(
            yaml.safe_dump(
                [{"hosts": "localhost", "gather_facts": False, "tasks": tasks}]
            )
        )
        env = dict(os.environ, ANSIBLE_CONFIG=str(config), ANSIBLE_NOCOLOR="1")
        env.pop("ANSIBLE_LIBRARY", None)
        env.pop("ANSIBLE_MODULE_UTILS", None)
        result = subprocess.run(
            [
                "ansible-playbook",
                "-i",
                "localhost,",
                "-c",
                "local",
                str(playbook),
                "-e",
                "ansible_python_interpreter=" + sys.executable,
            ],
            env=env,
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_unhealthy_last_agent_fails_before_any_host_is_provisioned(self):
        self.variables["spur_ignore_unreachable_agents"] = False
        (self.root / "unhealthy").write_text("later")
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("spurd is unhealthy", result.stdout)
        self.assertFalse(list(self.root.glob("*/provisioned")), result.stdout)

    def test_controller_connection_failure_stops_subsequent_batches(self):
        self.variables.pop("ansible_connection")
        self.inventory["all"]["vars"] = {"ansible_connection": "local"}
        self.plays.insert(
            -1, self.change_connection("controller-a", self.unreachable())
        )
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "Controller controller-a registration query failed", result.stdout
        )
        self.assertNotIn("did not register", result.stdout)
        self.assertNotIn("is undefined", result.stdout)
        self.assertTrue((self.root / "first/provisioned").exists(), result.stdout)
        self.assertFalse((self.root / "later/provisioned").exists(), result.stdout)

    def test_drained_busy_refusal_stops_rollout_even_when_unreachable_are_tolerated(
        self,
    ):
        self.variables.update(
            spur_in_place_upgrade=False,
            spur_rolling_batch_size=1,
            spur_drain_wait_secs=0,
        )
        full = self.root / "tasks/upgrade_agent_full.yml"
        tasks = yaml.safe_load(full.read_text())
        tasks.insert(0, {"ansible.builtin.include_tasks": "drain_and_guard_agent.yml"})
        full.write_text(yaml.safe_dump(tasks))
        cli = self.root / "first/spur"
        cli.write_text(
            "#!/usr/bin/env python3\nfrom pathlib import Path\nimport sys\n"
            "if sys.argv[1:3] == ['node', 'drain']:\n"
            f"    Path({str(self.root / 'drained')!r}).touch()\n"
            "else: print('NodeName=first\\nState=DRAINING\\nCPUAlloc=1')\n"
        )
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.root / "drained").exists(), result.stdout)
        self.assertIn("Refuse to disrupt a busy node", result.stdout)
        for name in self.agents:
            self.assertFalse((self.root / name / "provisioned").exists(), result.stdout)

    def test_controller_rpc_failure_stops_subsequent_batches_without_secondary_error(
        self,
    ):
        (self.root / "rpc-error").touch()
        result = self.run_play()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "Controller controller-a registration query failed", result.stdout
        )
        self.assertNotIn("is undefined", result.stdout)
        self.assertFalse((self.root / "later/provisioned").exists(), result.stdout)

    def test_recovered_agent_is_restaged_and_upgraded(self):
        self.variables.pop("ansible_connection")
        self.inventory["all"]["vars"] = {"ansible_connection": "local"}
        self.plays.insert(2, self.change_connection("first", self.unreachable()))
        self.plays.insert(
            4, self.change_connection("first", {"ansible_connection": "local"})
        )
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("UNREACHABLE", result.stdout)
        for name in self.controllers + self.agents:
            self.assertTrue((self.root / name / "provisioned").exists(), result.stdout)
        self.assertTrue(
            (self.root / "first/.spur-upgrade-agent.tar.gz").is_file(), result.stdout
        )


if __name__ == "__main__":
    unittest.main()
