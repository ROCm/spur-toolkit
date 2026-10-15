"""Exercise the production agent configuration module against real local files."""

import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[2] / "ansible/library/spur_configure_agent.py"


class AgentConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "plugin-source"
        self.source.write_bytes(b"\x7fELF\x00\xffplugin")
        self.params = dict(
            environment_file=str(self.root / "environment"),
            controller_endpoints=[
                "http://controller-a:6817",
                "http://controller-b:6817",
            ],
            config_file=str(self.root / "spur.conf"),
            config_content='cluster_name = "fixture"\n',
            state_dir=str(self.root / "state"),
            unit_file=str(self.root / "spurd.service"),
            unit_content="[Service]\nKillMode=process\n",
            mpi_dir=str(self.root / "plugins"),
            mpi_source=str(self.source),
        )

    def run_module(self, success=True, **overrides):
        result = subprocess.run(
            [sys.executable, str(MODULE)],
            input=json.dumps({"ANSIBLE_MODULE_ARGS": dict(self.params, **overrides)}),
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = json.loads(result.stdout)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return output

    def test_fresh_install_and_unchanged_rerun_preserve_bytes_modes_and_inodes(self):
        self.assertTrue(self.run_module()["changed"])
        expected = {
            "state": 0o700,
            "plugins": 0o755,
            "plugins/spur_mpi_pmix.so": 0o755,
            "spur.conf": 0o644,
            "spurd.service": 0o644,
        }
        for name, mode in expected.items():
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), mode)
        self.assertEqual(
            (self.root / "plugins/spur_mpi_pmix.so").read_bytes(),
            self.source.read_bytes(),
        )
        before = {name: (self.root / name).stat().st_ino for name in expected}
        self.assertFalse(self.run_module()["changed"])
        self.assertEqual(
            before, {name: (self.root / name).stat().st_ino for name in expected}
        )
        (self.root / "state").chmod(0o755)
        (self.root / "spurd.service").chmod(0o600)
        self.assertTrue(self.run_module()["changed"])
        self.assertEqual(stat.S_IMODE((self.root / "state").stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.root / "spurd.service").stat().st_mode), 0o644
        )

    def test_preserve_existing_config_and_environment_then_explicit_overwrite(self):
        config = self.root / "spur.conf"
        config.write_text("operator configuration\n")
        config.chmod(0o600)
        environment = self.root / "environment"
        environment.write_text('PATH="/bin"\nCUSTOM=keep\nSPUR_CONTROLLER_ADDR=old\n')
        environment.chmod(0o640)
        self.run_module()
        self.assertEqual(config.read_text(), "operator configuration\n")
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(
            environment.read_text(),
            'PATH="/bin"\nCUSTOM=keep\nSPUR_CONTROLLER_ADDR=http://controller-a:6817,http://controller-b:6817\n',
        )
        self.assertEqual(stat.S_IMODE(environment.stat().st_mode), 0o640)
        self.run_module(overwrite_config=True)
        self.assertEqual(config.read_text(), self.params["config_content"])
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o644)

    def test_check_mode_reports_diff_without_writing_or_chmod(self):
        before = sorted(p.name for p in self.root.iterdir())
        result = self.run_module(_ansible_check_mode=True, _ansible_diff=True)
        self.assertTrue(result["changed"])
        self.assertTrue(result["diff"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)
        self.run_module()
        state = self.root / "state"
        state.chmod(0o755)
        self.assertTrue(self.run_module(_ansible_check_mode=True)["changed"])
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)

    def test_optional_missing_and_disabled_mpi_keep_existing_plugin(self):
        result = self.run_module(mpi_source="")
        self.assertIn("not found", json.dumps(result["warnings"]))
        self.assertTrue((self.root / "plugins").is_dir())
        self.run_module()
        plugin = self.root / "plugins/spur_mpi_pmix.so"
        before = plugin.read_bytes()
        result = self.run_module(mpi_source="", mpi_enabled=False)
        self.assertFalse(result["changed"])
        self.assertEqual(plugin.read_bytes(), before)
        self.assertNotIn("warnings", result)

    def test_missing_expected_staged_plugin_fails_before_configuration(self):
        self.source.unlink()
        result = self.run_module(success=False, mpi_source_required=True)
        self.assertIn("Staged MPI plugin is missing", result["msg"])
        self.assertFalse((self.root / "environment").exists())
        self.assertFalse((self.root / "state").exists())
        self.run_module(mpi_source_required=True, _ansible_check_mode=True)

    def test_invalid_directory_fails_and_symlinks_match_template_and_lineinfile(self):
        target = self.root / "unit-target"
        target.write_text("stale")
        (self.root / "spurd.service").symlink_to(target)
        environment_target = self.root / "environment-target"
        environment_target.write_text("CUSTOM=keep\n")
        (self.root / "environment").symlink_to(environment_target)
        self.run_module()
        self.assertFalse((self.root / "spurd.service").is_symlink())
        self.assertEqual(
            (self.root / "spurd.service").read_text(), self.params["unit_content"]
        )
        self.assertEqual(target.read_text(), "stale")
        self.assertTrue((self.root / "environment").is_symlink())
        self.assertIn(
            "CUSTOM=keep\nSPUR_CONTROLLER_ADDR=", environment_target.read_text()
        )
        invalid = self.root / "invalid-state"
        invalid.write_text("not a directory")
        result = self.run_module(success=False, state_dir=str(invalid))
        self.assertIn("Not a directory", result["msg"])
        self.assertEqual(invalid.read_text(), "not a directory")


if __name__ == "__main__":
    unittest.main()
