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
    def test_production_guards_fail_before_following_tasks(self):
        install = yaml.safe_load(
            (ANSIBLE / "roles/spur_install/tasks/main.yml").read_text()
        )
        rolling = yaml.safe_load(
            (ANSIBLE / "playbooks/rolling_upgrade.yml").read_text()
        )
        guards = [
            install[0],
            next(
                task
                for task in rolling[0]["tasks"]
                if "spur_check_install_path" in task
            ),
        ]
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


if __name__ == "__main__":
    unittest.main()
