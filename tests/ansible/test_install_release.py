"""Exercise release selection using real modules and isolated install-role runs."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "ansible"
COMPONENTS = ("spur", "spurctld", "spurd", "spurstepd", "spurauthd")


def binary(directory, name, version="0.15.0", status=0):
    path = directory / name
    path.write_text(
        "#!/bin/sh\nprintf '%s\\n' "
        + shlex.quote("spur " + version + " (a1b2c3d4)")
        + "\nexit "
        + str(status)
        + "\n"
    )
    path.chmod(0o755)
    return path


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in COMPONENTS:
            binary(self.root, name)

    def run_module(self, success=True, **overrides):
        result = subprocess.run(
            [sys.executable, str(ANSIBLE / "library/spur_check_release.py")],
            input=json.dumps(
                {
                    "ANSIBLE_MODULE_ARGS": {
                        "install_dir": str(self.root),
                        "version": "v0.15.0",
                        "required_bins": list(COMPONENTS),
                        **overrides,
                    }
                }
            ),
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_matching_release_and_unprefixed_pin_are_accepted_in_check_mode(self):
        for version in ("v0.15.0", "0.15.0"):
            with self.subTest(version=version):
                result = self.run_module(version=version, _ansible_check_mode=True)
                self.assertFalse(result["needs_install"])
                self.assertFalse(result["changed"])
                self.assertEqual(result["release"], "v0.15.0")

    def test_each_component_is_checked_even_when_cli_matches(self):
        for name in COMPONENTS:
            with self.subTest(name=name):
                binary(self.root, name, "0.14.0")
                result = self.run_module()
                self.assertTrue(result["needs_install"])
                self.assertEqual(len(result["problems"]), 1)
                self.assertIn(str(self.root / name), result["problems"][0])
                failure = self.run_module(success=False, require_match=True)
                self.assertIn("expected v0.15.0", failure["msg"])
                binary(self.root, name)

    def test_version_comparison_is_exact_and_requires_successful_execution(self):
        for version, status in (
            ("0.15.01", 0),
            ("0.15.0-rc1", 0),
            ("not-a-version", 0),
            ("0.15.0", 1),
        ):
            with self.subTest(version=version, status=status):
                binary(self.root, "spur", version, status)
                self.assertTrue(self.run_module()["needs_install"])
        binary(self.root, "spur", "0.15.0-rc1")
        self.assertFalse(
            self.run_module(version="v0.15.0-rc1", required_bins=["spur"])[
                "needs_install"
            ]
        )

    def test_missing_dangling_nonexecutable_and_bad_interpreter_trigger_install(self):
        path = self.root / "spurd"
        for defect in ("missing", "dangling", "permissions", "interpreter"):
            with self.subTest(defect=defect):
                path.unlink(missing_ok=True)
                if defect == "dangling":
                    path.symlink_to(self.root / "absent")
                elif defect == "permissions":
                    binary(self.root, "spurd").chmod(0o644)
                elif defect == "interpreter":
                    path.write_text("#!/nonexistent/interpreter\n")
                    path.chmod(0o755)
                self.assertTrue(self.run_module()["needs_install"])

    def test_optional_components_are_checked_when_present_but_may_be_absent(self):
        for name in ("spurstepd", "spurauthd"):
            with self.subTest(name=name):
                params = dict(required_bins=["spur"], optional_bins=[name])
                binary(self.root, name, "0.14.0")
                self.assertTrue(self.run_module(**params)["needs_install"])
                (self.root / name).unlink()
                self.assertFalse(self.run_module(**params)["needs_install"])

    def test_moving_channels_reuse_complete_installations(self):
        binary(self.root, "spur", "0.14.0")
        for channel in ("latest", "nightly"):
            with self.subTest(channel=channel):
                self.assertFalse(self.run_module(version=channel)["needs_install"])
        (self.root / "spurd").unlink()
        self.assertTrue(self.run_module(version="latest")["needs_install"])

    def test_invalid_release_is_rejected_before_probing_binaries(self):
        sentinel = self.root / "executed"
        (self.root / "spur").write_text("#!/bin/sh\ntouch " + str(sentinel) + "\n")
        for version in ("uninstall", "", "v0.15", "v0.15.0; false"):
            with self.subTest(version=version):
                self.assertIn(
                    "spur_version must be",
                    self.run_module(success=False, version=version)["msg"],
                )
        self.assertFalse(sentinel.exists())


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires ansible-core")
class ReleaseInstallRoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.commands = self.root / "commands"
        self.commands.mkdir()
        self.downloads = self.root / "downloads"
        self.installer = self.root / "installer.sh"
        self.installer.write_text("""#!/bin/bash
set -eu
printf '%s %s\\n' "$INSTALL_DIR" "$1" >> "$FIXTURE_DOWNLOADS"
python3 - <<'PY'
import os
from pathlib import Path
path = Path(os.environ['INSTALL_DIR'])
version = os.environ.get('FIXTURE_VERSION', '0.15.0')
for name in ('spur', 'spurctld', 'spurd', 'spurstepd', 'spurauthd'):
    binary = path / name
    binary.unlink(missing_ok=True)
    binary.write_text('#!/bin/sh\\necho "spur ' + version + ' (a1b2c3d4)"\\n')
    binary.chmod(0o755)
PY
""")
        self.command(
            "curl",
            """#!/bin/bash
set -eu
test "$*" = '-fsSL https://raw.githubusercontent.com/ROCm/spur/main/install.sh'
test "${FIXTURE_DOWNLOAD_FAIL:-false}" = false || exit 22
cat "$FIXTURE_INSTALLER"
""",
        )
        self.command("ss", "#!/bin/sh\nexit 0\n")
        self.command("pgrep", "#!/bin/sh\nexit 1\n")
        self.env = dict(
            os.environ,
            PATH=str(self.commands) + os.pathsep + os.environ["PATH"],
            ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg"),
            ANSIBLE_LOCAL_TEMP=str(self.root / "ansible-local"),
            ANSIBLE_NOCOLOR="1",
            FIXTURE_DOWNLOADS=str(self.downloads),
            FIXTURE_INSTALLER=str(self.installer),
        )
        # Execute the production role; redirect only its global environment write.
        role = self.root / "roles/spur_install"
        shutil.copytree(ANSIBLE / "roles/spur_install", role)
        tasks_file = role / "tasks/main.yml"
        tasks = yaml.safe_load(tasks_file.read_text())
        next(t for t in tasks if "spur_configure_path" in t)["spur_configure_path"][
            "environment_file"
        ] = "{{ spur_home }}/environment"
        tasks_file.write_text(yaml.safe_dump(tasks))
        self.playbook = self.root / "install.yml"
        self.playbook.write_text(
            yaml.safe_dump(
                [dict(hosts="all", gather_facts=False, roles=["spur_install"])]
            )
        )

    def command(self, name, script):
        path = self.commands / name
        path.write_text(script)
        path.chmod(0o755)

    def inventory(self, fleet=False, **overrides):
        defaults = yaml.safe_load(
            (ANSIBLE / "inventory/group_vars/all.yml").read_text()
        )
        defaults.update(
            ansible_connection="local",
            ansible_become=False,
            ansible_python_interpreter=sys.executable,
            spur_version="0.15.0",
            **overrides,
        )
        self.hosts = ["ctl", "worker", "login", "dual"] if fleet else ["dual"]
        for name in self.hosts:
            (self.root / name).mkdir(exist_ok=True)
        hosts = {
            name: dict(
                spur_install_dir=str(self.root / name / "bin"),
                spur_home=str(self.root / name / "home"),
            )
            for name in self.hosts
        }
        children = {
            "spur_controllers": {
                "hosts": {"ctl": {}, "dual": {}} if fleet else {"dual": {}}
            },
            "spur_agents": {
                "hosts": {"worker": {}, "dual": {}} if fleet else {"dual": {}}
            },
            "spur_login": {"hosts": {"login": {}} if fleet else {}},
        }
        self.inventory_path = self.root / "inventory.yml"
        self.inventory_path.write_text(
            yaml.safe_dump(
                dict(all=dict(vars=defaults, hosts=hosts, children=children))
            )
        )

    def seed(self, version="0.14.0"):
        for name in self.hosts:
            directory = self.root / name / "bin"
            directory.mkdir(exist_ok=True)
            for component in COMPONENTS:
                binary(directory, component, version)

    def run_play(self, success=True, *args):
        result = subprocess.run(
            [
                "ansible-playbook",
                "-i",
                str(self.inventory_path),
                str(self.playbook),
                *args,
            ],
            cwd=ANSIBLE,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode == 0, success, output)
        return output

    def test_old_fleet_converges_to_pin_and_rerun_is_unchanged(self):
        self.inventory(fleet=True)
        self.seed()
        self.run_play()
        downloaded = self.downloads.read_text()
        self.assertEqual(len(downloaded.splitlines()), 4)
        self.assertTrue(
            all(line.endswith(" v0.15.0") for line in downloaded.splitlines())
        )
        for host, required in (
            ("ctl", ("spur", "spurctld")),
            ("worker", ("spur", "spurd", "spurstepd")),
            ("login", ("spur",)),
            ("dual", ("spur", "spurctld", "spurd", "spurstepd")),
        ):
            for name in required:
                self.assertIn("0.15.0", (self.root / host / "bin" / name).read_text())
        output = self.run_play()
        self.assertEqual(self.downloads.read_text(), downloaded)
        for host in self.hosts:
            self.assertRegex(output, host + r"\s+: ok=\d+\s+changed=0")

    def test_matching_cli_does_not_hide_missing_or_old_daemons(self):
        self.inventory()
        self.seed("0.15.0")
        directory = self.root / "dual/bin"
        binary(directory, "spurd", "0.14.0")
        (directory / "spurctld").unlink()
        (directory / "spurctld").symlink_to(directory / "missing")
        self.run_play()
        self.assertTrue(self.downloads.exists())
        self.assertIn("0.15.0", (directory / "spurd").read_text())

    def test_successful_installer_with_wrong_version_fails_role(self):
        self.inventory()
        self.seed()
        self.env["FIXTURE_VERSION"] = "0.14.0"
        output = self.run_play(success=False)
        self.assertIn("expected v0.15.0", output)
        self.assertNotIn("Show install result", output)

    def test_failed_download_does_not_succeed_with_existing_binaries(self):
        self.inventory()
        self.seed()
        self.env["FIXTURE_DOWNLOAD_FAIL"] = "true"
        output = self.run_play(success=False)
        self.assertNotIn("Show install result", output)

    def test_force_refresh_still_runs_for_matching_installation(self):
        self.inventory(spur_force_reinstall=True)
        self.seed("0.15.0")
        self.run_play()
        self.assertTrue(self.downloads.exists())

    def test_local_source_overrides_release_pin(self):
        source = self.root / "local-build"
        source.mkdir()
        for name in COMPONENTS:
            binary(source, name, "0.14.0")
        self.inventory(spur_binary_src=str(source))
        self.run_play()
        self.assertFalse(self.downloads.exists())
        self.assertIn("0.14.0", (self.root / "dual/bin/spurd").read_text())


if __name__ == "__main__":
    unittest.main()
