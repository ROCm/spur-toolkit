"""Exercise the legacy-path guard without touching system installation paths."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ANSIBLE = Path(__file__).resolve().parents[2] / "ansible"


class InstallMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.legacy = self.root / "legacy"
        self.destination = self.root / "destination"
        self.units = self.root / "units"
        for directory in (self.legacy, self.destination, self.units):
            directory.mkdir()
        (self.legacy / "spur").write_text("old CLI")

    def params(self, **overrides):
        return dict(
            install_dir=str(self.destination),
            legacy_dir=str(self.legacy),
            unit_dir=str(self.units),
            **overrides,
        )

    def run_module(self, success=True, **overrides):
        params = self.params()
        params.update(overrides)
        result = subprocess.run(
            [sys.executable, str(ANSIBLE / "library/spur_check_install_path.py")],
            input=json.dumps({"ANSIBLE_MODULE_ARGS": params}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_legacy_install_requires_explicit_path_or_migration(self):
        result = self.run_module(success=False, _ansible_check_mode=True)
        self.assertIn("spur_install_dir=" + str(self.legacy), result["msg"])
        self.assertFalse(self.run_module(install_dir=str(self.legacy))["changed"])
        self.assertFalse(self.run_module(allow_migration=True)["changed"])
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_old_service_path_is_detected_even_if_new_binaries_exist(self):
        (self.destination / "spur").write_text("new CLI")
        (self.units / "spurd.service").write_text(
            '[Service]\nExecStart="'
            + str(self.legacy / "spurd")
            + '" -f /etc/spur.conf\n'
        )
        self.run_module(success=False)
        (self.legacy / "spur").unlink()
        self.run_module(success=False)
        (self.units / "spurd.service").write_text(
            "[Service]\nExecStart=" + str(self.destination / "spurd") + "\n"
        )
        self.assertFalse(self.run_module()["changed"])

    def test_fresh_host_and_already_migrated_installations_are_allowed(self):
        (self.legacy / "spur").unlink()
        self.assertFalse(self.run_module()["changed"])
        (self.legacy / "spur").write_text("stale CLI")
        (self.destination / "spur").write_text("new CLI")
        self.assertFalse(self.run_module()["changed"])

    @unittest.skipUnless(shutil.which("ansible-playbook"), "requires ansible-core")
    def test_production_install_guard_fails_before_following_tasks(self):
        install = yaml.safe_load(
            (ANSIBLE / "roles/spur_install/tasks/main.yml").read_text()
        )
        guards = [install[0]]
        for guard in guards:
            with self.subTest(guard=guard):
                # Preserve the production condition; redirect only the probe's filesystem inputs.
                guard["spur_check_install_path"].update(self.params())
                marker = self.root / "mutation"
                playbook = self.root / "guard.yml"
                playbook.write_text(
                    yaml.safe_dump(
                        [
                            dict(
                                hosts="localhost",
                                gather_facts=False,
                                vars=dict(
                                    spur_install_dir="/usr/local/bin",
                                    ansible_python_interpreter=sys.executable,
                                ),
                                tasks=[
                                    guard,
                                    {
                                        "ansible.builtin.copy": {
                                            "content": "changed",
                                            "dest": str(marker),
                                        }
                                    },
                                ],
                            )
                        ]
                    )
                )
                result = subprocess.run(
                    [
                        "ansible-playbook",
                        "-i",
                        "localhost,",
                        "-c",
                        "local",
                        str(playbook),
                    ],
                    cwd=ANSIBLE,
                    env=dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg")),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Existing Spur installation uses", result.stdout)
                self.assertFalse(marker.exists())


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires ansible-core")
class RollingPathPreflightTests(unittest.TestCase):
    def run_preflight(
        self, legacy=None, unreachable=None, tolerate=False, limit=None, dual=False
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hosts = {}
            for name in ("ctl0", "ctl1", "agent"):
                home = root / name
                for subdir in ("bin", "legacy", "units"):
                    (home / subdir).mkdir(parents=True)
                (home / ("legacy" if name == legacy else "bin") / "spur").write_text(
                    "fixture CLI"
                )
                hosts[name] = dict(fixture_home=str(home))
            fake_ssh = root / "ssh"
            fake_ssh.write_text(
                "#!/bin/sh\necho 'fixture SSH unavailable' >&2\nexit 255\n"
            )
            fake_ssh.chmod(0o755)
            if unreachable:
                hosts[unreachable].update(
                    ansible_connection="ssh", ansible_ssh_executable=str(fake_ssh)
                )
            inventory = root / "inventory.yml"
            inventory.write_text(
                yaml.safe_dump(
                    dict(
                        all=dict(
                            hosts=hosts,
                            vars=dict(
                                ansible_connection="local",
                                ansible_become=False,
                                ansible_python_interpreter=sys.executable,
                                spur_install_dir="/usr/local/bin",
                                spur_ignore_unreachable_agents=tolerate,
                            ),
                            children=dict(
                                spur_controllers=dict(
                                    hosts={
                                        name: {}
                                        for name in (
                                            ["ctl0", "ctl1", "agent"]
                                            if dual
                                            else ["ctl0", "ctl1"]
                                        )
                                    }
                                ),
                                spur_agents=dict(hosts={"agent": {}}),
                            ),
                        )
                    )
                )
            )
            plays = yaml.safe_load(
                (ANSIBLE / "playbooks/rolling_upgrade.yml").read_text()
            )
            preflight = [
                p
                for p in plays
                if p["name"] == "Validate install paths before any rolling upgrade"
            ]
            for play in preflight:
                for task in play["tasks"]:
                    if "spur_check_install_path" in task:
                        task["spur_check_install_path"].update(
                            install_dir="{{ fixture_home }}/bin",
                            legacy_dir="{{ fixture_home }}/legacy",
                            unit_dir="{{ fixture_home }}/units",
                        )
            # Entering this later play stands for the first upgrade or drain side effect.
            canary = dict(
                hosts="spur_controllers:spur_agents",
                gather_facts=False,
                tasks=[
                    {
                        "ansible.builtin.copy": {
                            "content": "entered upgrade phase",
                            "dest": "{{ fixture_home }}/mutation",
                        },
                        "delegate_to": "localhost",
                        "become": False,
                        "vars": {
                            "ansible_connection": "local",
                            "ansible_become": False,
                        },
                    }
                ],
            )
            playbook = root / "preflight.yml"
            playbook.write_text(yaml.safe_dump(preflight + [canary]))
            command = ["ansible-playbook", "-i", str(inventory), str(playbook)]
            if limit:
                command += ["--limit", limit]
            result = subprocess.run(
                command,
                cwd=ANSIBLE,
                env=dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg")),
                capture_output=True,
                text=True,
            )
            mutated = {name for name in hosts if (root / name / "mutation").exists()}
            return result.returncode, result.stdout + result.stderr, mutated

    def test_later_controller_or_agent_blocks_all_upgrades_even_with_lenient_policy(
        self,
    ):
        for host in ("ctl1", "agent"):
            with self.subTest(host=host):
                status, output, mutated = self.run_preflight(legacy=host, tolerate=True)
                self.assertNotEqual(status, 0, output)
                self.assertIn(host, output)
                self.assertIn("Existing Spur installation uses", output)
                self.assertEqual(mutated, set(), output)

    def test_limits_check_selected_agents_without_including_excluded_hosts(self):
        status, output, mutated = self.run_preflight(legacy="agent", limit="agent")
        self.assertNotEqual(status, 0, output)
        self.assertEqual(mutated, set(), output)
        status, output, mutated = self.run_preflight(legacy="ctl1", limit="ctl0:agent")
        self.assertEqual(status, 0, output)
        self.assertEqual(mutated, {"ctl0", "agent"}, output)

    def test_only_pure_agents_can_be_ignored_when_unreachable(self):
        for host, tolerate, dual, allowed in (
            ("agent", False, False, False),
            ("agent", True, False, True),
            ("ctl0", True, False, False),
            ("ctl1", True, False, False),
            ("agent", True, True, False),
        ):
            with self.subTest(host=host, tolerate=tolerate, dual=dual):
                status, output, mutated = self.run_preflight(
                    unreachable=host, tolerate=tolerate, dual=dual
                )
                self.assertEqual(status == 0, allowed, output)
                self.assertEqual(bool(mutated), allowed, output)


if __name__ == "__main__":
    unittest.main()
