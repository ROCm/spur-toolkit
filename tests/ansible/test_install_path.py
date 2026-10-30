"""Exercise system PATH setup through the production module and install task."""

import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "ansible"
MODULE = ANSIBLE / "library/spur_configure_path.py"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class InstallPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "spur[1].bin"
        self.environment = self.root / "environment"

    def run_module(self, success=True, **overrides):
        params = {
            "install_dir": str(self.destination),
            "environment_file": str(self.environment),
            **overrides,
        }
        result = subprocess.run(
            [sys.executable, str(MODULE)],
            input=json.dumps({"ANSIBLE_MODULE_ARGS": params}),
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = json.loads(result.stdout)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return output

    def assert_rerun_unchanged(self):
        before = self.environment.read_bytes()
        inode = self.environment.stat().st_ino
        self.assertFalse(self.run_module()["changed"])
        self.assertEqual(self.environment.read_bytes(), before)
        self.assertEqual(self.environment.stat().st_ino, inode)

    def test_missing_empty_or_no_path_gets_system_defaults(self):
        for original in (None, "", "CUSTOM=keep\n", "CUSTOM=keep"):
            with self.subTest(original=original):
                self.environment.unlink(missing_ok=True)
                if original is not None:
                    self.environment.write_text(original)
                    self.environment.chmod(0o644)
                self.assertTrue(self.run_module()["changed"])
                prefix = (original.rstrip("\n") + "\n") if original else ""
                self.assertEqual(
                    self.environment.read_text(),
                    prefix + f'PATH="{self.destination}:{SYSTEM_PATH}"\n',
                )
                self.assertEqual(stat.S_IMODE(self.environment.stat().st_mode), 0o644)
                self.assert_rerun_unchanged()

    def test_quoted_unquoted_empty_and_export_assignments(self):
        for assignment in (
            "PATH=/custom/bin:/usr/bin",
            'PATH="/custom/bin:/usr/bin"',
            "PATH='/custom/bin:/usr/bin'",
            "  export PATH = '/custom/bin:/usr/bin' # comment",
            "PATH=",
            'PATH=""',
        ):
            with self.subTest(assignment=assignment):
                self.environment.write_text("CUSTOM=keep\n" + assignment + "\n")
                self.environment.chmod(0o640)
                self.assertTrue(self.run_module()["changed"])
                expected = (
                    "/custom/bin:/usr/bin" if "/custom" in assignment else SYSTEM_PATH
                )
                self.assertEqual(
                    self.environment.read_text(),
                    f'CUSTOM=keep\nPATH="{self.destination}:{expected}"\n',
                )
                self.assertEqual(stat.S_IMODE(self.environment.stat().st_mode), 0o640)
                self.assert_rerun_unchanged()

    def test_comments_other_variables_and_substrings_do_not_count_as_path_entries(self):
        for prefix in (f"# {self.destination}\n", f"OTHER={self.destination}\n", ""):
            with self.subTest(prefix=prefix):
                self.environment.write_text(
                    prefix + f'PATH="{self.destination}-old:/bin"\n'
                )
                self.assertTrue(self.run_module()["changed"])
                self.assertEqual(
                    self.environment.read_text(),
                    prefix + f'PATH="{self.destination}:{self.destination}-old:/bin"\n',
                )
                self.assert_rerun_unchanged()

    def test_existing_exact_entry_keeps_format_and_order(self):
        self.destination.mkdir(mode=0o755)
        original = f"PATH='/custom/bin:{self.destination}:/bin' # keep\nCUSTOM=keep\n"
        self.environment.write_text(original)
        self.assert_rerun_unchanged()
        self.assertEqual(self.environment.read_text(), original)

    def test_last_path_assignment_is_the_effective_one(self):
        prefix = f'PATH="{self.destination}:/bin"\n'
        self.environment.write_text(prefix + 'PATH="/custom/bin:/bin"\n')
        self.assertTrue(self.run_module()["changed"])
        self.assertEqual(
            self.environment.read_text(),
            prefix + f'PATH="{self.destination}:/custom/bin:/bin"\n',
        )
        self.assert_rerun_unchanged()

    def test_restricted_install_directory_is_repaired_without_chmod_of_its_parent(self):
        self.root.chmod(0o700)
        self.destination.mkdir(mode=0o700)
        self.assertTrue(self.run_module()["changed"])
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assert_rerun_unchanged()

    def test_environment_symlink_preserves_target_mode(self):
        target = self.root / "environment-target"
        target.write_text("CUSTOM=keep\n")
        target.chmod(0o640)
        self.environment.symlink_to(target)
        self.assertTrue(self.run_module()["changed"])
        self.assertTrue(self.environment.is_symlink())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertIn("CUSTOM=keep\nPATH=", target.read_text())
        self.assert_rerun_unchanged()

    def test_check_mode_reports_changes_without_writing_or_chmod(self):
        self.destination.mkdir(mode=0o700)
        self.environment.write_text(
            "PRIVATE_SETTING=do-not-include-in-diff\nPATH=/bin\n"
        )
        before = self.environment.read_bytes()
        result = self.run_module(_ansible_check_mode=True, _ansible_diff=True)
        self.assertTrue(result["changed"])
        self.assertIn(str(self.destination), result["diff"]["after"])
        self.assertNotIn("PRIVATE_SETTING", json.dumps(result["diff"]))
        self.assertEqual(self.environment.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)
        self.environment.unlink()
        self.destination.rmdir()
        self.assertTrue(self.run_module(_ansible_check_mode=True)["changed"])
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.environment.exists())

    def test_invalid_environment_fails_before_creating_install_directory(self):
        self.environment.write_text('PATH="unterminated\n')
        before = self.environment.read_bytes()
        self.run_module(success=False)
        self.assertEqual(self.environment.read_bytes(), before)
        self.assertFalse(self.destination.exists())
        self.environment.unlink()
        self.environment.mkdir()
        self.run_module(success=False)
        self.assertFalse(self.destination.exists())

    def test_invalid_install_directory_fails_before_environment_changes(self):
        self.environment.write_text("CUSTOM=keep\n")
        self.destination.write_text("not a directory")
        self.run_module(success=False)
        for invalid in ("relative/bin", str(self.root / "bad:path")):
            with self.subTest(invalid=invalid):
                self.run_module(success=False, install_dir=invalid)
        self.assertEqual(self.environment.read_text(), "CUSTOM=keep\n")
        self.assertEqual(self.destination.read_text(), "not a directory")

    def test_installed_command_is_resolved_using_configured_path(self):
        self.environment.write_text("PATH=/usr/bin:/bin\n")
        self.run_module()
        executable = self.destination / "spur-path-probe"
        executable.write_text("#!/bin/sh\nprintf 'found\\n'\n")
        executable.chmod(0o755)
        value = shlex.split(self.environment.read_text().partition("=")[2])[0]
        result = subprocess.run(
            ["spur-path-probe"],
            env=dict(os.environ, PATH=value),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, "found\n")

    @unittest.skipUnless(shutil.which("ansible-playbook"), "requires ansible-core")
    def test_production_install_task_uses_inventory_destination(self):
        tasks = yaml.safe_load(
            (ANSIBLE / "roles/spur_install/tasks/main.yml").read_text()
        )
        task = next(t for t in tasks if "spur_configure_path" in t)
        task["spur_configure_path"]["environment_file"] = str(self.environment)
        playbook = self.root / "play.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "gather_facts": False,
                        "vars": {
                            "spur_install_dir": str(self.destination),
                            "ansible_python_interpreter": sys.executable,
                        },
                        "tasks": [task],
                    }
                ]
            )
        )
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
            cwd=ANSIBLE,
            env=dict(os.environ, ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg")),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.destination.is_dir())
        self.assertIn(str(self.destination), self.environment.read_text())


if __name__ == "__main__":
    unittest.main()
