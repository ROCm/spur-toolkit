"""Local fixtures exercise production helpers and the real rolling playbook."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

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

    def test_auth_enabled_upgrade_requires_authd_in_source_before_staging(self):
        (self.source / "spurauthd").unlink()
        targets = {
            name: dict(self.spec, roles=["controller"], auth=True)
            for name in ["controller-a", "controller-b"]
        }
        with self.assertRaisesRegex(ValueError, "Missing Spur artifact: .*spurauthd"):
            upgrade.prepare(targets, self.root / "cache")

    def test_install_cache_is_reused_and_corruption_is_rebuilt(self):
        targets = {"a": self.spec}
        first = upgrade.prepare_install(targets, self.root / "cache")
        bundle = first["artifacts"]["a"]
        archive = Path(bundle["archive"])
        before = archive.stat()
        with patch.object(upgrade.os, "replace", wraps=os.replace) as replace:
            self.assertEqual(
                first, upgrade.prepare_install(targets, self.root / "cache")
            )
        replace.assert_not_called()
        self.assertEqual(archive.stat().st_ino, before.st_ino)
        self.assertEqual(archive.stat().st_mtime_ns, before.st_mtime_ns)
        for corruption in [b"not a tarball", archive.read_bytes()[:20]]:
            archive.write_bytes(corruption)
            upgrade.prepare_install(targets, self.root / "cache")
            upgrade.validate_archive(archive, bundle["manifest"])
        (self.source / "spurd").write_text("changed source")
        updated = upgrade.prepare_install(targets, self.root / "cache")["artifacts"][
            "a"
        ]
        self.assertNotEqual(updated["archive"], bundle["archive"])
        upgrade.validate_archive(updated["archive"], updated["manifest"])

    def test_read_only_verification_accepts_matching_symlinks_and_rejects_drift(self):
        install, state, args = self.activation_fixture()
        args["verify_only"] = True
        for name in args["manifest"]:
            (install / name).unlink()
            (install / name).symlink_to(self.source / name)
        with (
            patch.object(upgrade, "service", return_value=state),
            patch.object(
                upgrade, "process_args", return_value=["spurd", "--hostname=a"]
            ),
            patch.object(
                upgrade, "running_digest", return_value=args["manifest"]["spurd"]
            ) as running,
            patch.object(upgrade, "command", return_value="new") as command,
            patch.object(upgrade, "wait_ready", return_value=state),
            patch.object(
                upgrade.os, "replace", side_effect=AssertionError("unexpected write")
            ),
        ):
            self.assertFalse(upgrade.activate(**args)["changed"])
            self.assertFalse(upgrade.activate(**args, check=True)["changed"])
            running.return_value = "stale"
            with self.assertRaisesRegex(ValueError, "Running daemon"):
                upgrade.activate(**args)
            running.return_value = args["manifest"]["spurd"]
            (self.source / "spurstepd").write_text("drift")
            with self.assertRaisesRegex(ValueError, "Installed binary"):
                upgrade.activate(**args)
            self.assertTrue(upgrade.activate(**args, check=True)["changed"])
        command.assert_called_once_with([str(install / "spurd"), "-V"])
        self.assertTrue(all((install / name).is_symlink() for name in args["manifest"]))

    def test_staged_archive_validation_rejects_corruption_and_unsafe_members(self):
        _, _, args = self.activation_fixture()
        upgrade.validate_archive(args["archive"], args["manifest"])
        bad_manifest = dict(args["manifest"], spurd="0" * 64)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            upgrade.validate_archive(args["archive"], bad_manifest)
        with tarfile.open(args["archive"], "w:gz") as bundle:
            member = tarfile.TarInfo("../spurd")
            bundle.addfile(member)
        with self.assertRaisesRegex(ValueError, "manifest"):
            upgrade.validate_archive(args["archive"], args["manifest"])

    def test_truncated_archive_returns_structured_failure_before_changes(self):
        install, state, args = self.activation_fixture()
        archive = Path(args["archive"])
        archive.write_bytes(archive.read_bytes()[:20])
        with self.assertRaises(EOFError):
            upgrade.validate_archive(str(archive), args["manifest"])
        before = {p.name: p.read_bytes() for p in install.iterdir()}
        for name in ["spur_validate_upgrade", "spur_activate_upgrade"]:
            with self.subTest(module=name):
                spec = importlib.util.spec_from_file_location(
                    name, ROOT / "ansible/library" / (name + ".py")
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                result = Mock()
                result.params = dict(args, node="a")
                result.check_mode = False
                result.fail_json.side_effect = SystemExit(1)
                with (
                    patch.object(module, "AnsibleModule", return_value=result),
                    patch.dict(
                        sys.modules, {"ansible.module_utils.spur_upgrade": upgrade}
                    ),
                    patch.object(upgrade, "service", return_value=state) as service,
                    patch.object(upgrade, "running_digest", return_value="old"),
                    patch.object(
                        upgrade,
                        "process_args",
                        return_value=["spurd", "--hostname", "a"],
                    ),
                    patch.object(upgrade, "command") as command,
                ):
                    with self.assertRaises(SystemExit):
                        module.main()
                result.fail_json.assert_called_once()
                self.assertIn("end-of-stream", result.fail_json.call_args.kwargs["msg"])
                result.exit_json.assert_not_called()
                command.assert_not_called()
                if name == "spur_validate_upgrade":
                    service.assert_not_called()
                self.assertEqual(
                    before, {p.name: p.read_bytes() for p in install.iterdir()}
                )

    def test_archive_preflight_reports_existing_and_missing_supervisor(self):
        install, state, args = self.activation_fixture()
        spec = importlib.util.spec_from_file_location(
            "spur_validate_upgrade", ROOT / "ansible/library/spur_validate_upgrade.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stepd = install / "spurstepd"
        for exists in [True, False]:
            if not exists:
                stepd.unlink()
            result = Mock()
            result.params = dict(args, stepd_path=str(stepd))
            result.exit_json.side_effect = SystemExit(0)
            with (
                patch.object(module, "AnsibleModule", return_value=result),
                patch.dict(sys.modules, {"ansible.module_utils.spur_upgrade": upgrade}),
                patch.object(upgrade, "service", return_value=state),
            ):
                with self.assertRaises(SystemExit):
                    module.main()
            result.fail_json.assert_not_called()
            result.exit_json.assert_called_once_with(changed=False, stepd_exists=exists)

    def test_final_verification_is_read_only_and_fails_on_disk_or_running_drift(self):
        install, state, args = self.activation_fixture()
        args["verify_only"] = True
        Path(args["archive"]).unlink()
        with (
            patch.object(upgrade, "service", return_value=state),
            patch.object(
                upgrade, "process_args", return_value=["spurd", "--hostname", "a"]
            ),
            patch.object(
                upgrade, "running_digest", return_value=args["manifest"]["spurd"]
            ) as running,
            patch.object(upgrade, "command", return_value="new") as command,
            patch.object(upgrade, "wait_ready", return_value=state),
            patch.object(
                upgrade.tempfile,
                "mkstemp",
                side_effect=AssertionError("verification wrote a file"),
            ),
            patch.object(
                upgrade.os,
                "replace",
                side_effect=AssertionError("verification replaced a file"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Installed binary"):
                upgrade.activate(**args)
            command.assert_not_called()
            shutil.copyfile(self.source / "spurd", install / "spurd")
            running.return_value = "stale"
            with self.assertRaisesRegex(ValueError, "Running daemon"):
                upgrade.activate(**args)
            command.assert_not_called()
            running.return_value = args["manifest"]["spurd"]
            result = upgrade.activate(**args)
            self.assertFalse(result["changed"])
            self.assertEqual(result["restarted"], [])
            command.assert_called_once_with([str(install / "spurd"), "-V"])

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

    def test_unchanged_activation_does_not_write_temporary_binaries(self):
        install, state, args = self.activation_fixture()
        shutil.copyfile(self.source / "spurd", install / "spurd")
        with (
            patch.object(upgrade, "service", return_value=state),
            patch.object(
                upgrade, "running_digest", return_value=args["manifest"]["spurd"]
            ),
            patch.object(
                upgrade, "process_args", return_value=["spurd", "--hostname=worker"]
            ),
            patch.object(
                upgrade, "command", return_value="fixture version"
            ) as commands,
            patch.object(upgrade, "wait_ready", return_value=state),
            patch.object(
                upgrade.tempfile, "mkstemp", wraps=tempfile.mkstemp
            ) as temporary,
        ):
            result = upgrade.activate(**args)
        self.assertFalse(result["changed"])
        temporary.assert_not_called()
        commands.assert_called_once_with([str(install / "spurd"), "-V"])

    def test_check_compares_disk_and_running_hashes_without_archive_or_writes(self):
        self.spec["auth"] = True
        install, state, args = self.activation_fixture()
        args["auth"] = True
        plugin = install / "spur_mpi_pmix.so"
        plugin.write_bytes(b"plugin")
        args["manifest"][plugin.name] = upgrade.digest(plugin)
        shutil.copyfile(self.source / "spurd", install / "spurd")
        Path(args["archive"]).unlink()
        for drift in (
            None,
            "spur",
            "spurstepd",
            "spur_mpi_pmix.so",
            "spurd",
            "spurauthd",
        ):
            with self.subTest(drift=drift):
                disk_drift = drift in ("spur", "spurstepd", "spur_mpi_pmix.so")
                if disk_drift:
                    original = (install / drift).read_bytes()
                    (install / drift).write_bytes(b"old bytes")
                before = {
                    p: (p.read_bytes(), p.stat().st_ino) for p in install.iterdir()
                }
                with (
                    patch.object(upgrade, "service", return_value=state),
                    patch.object(
                        upgrade,
                        "process_args",
                        return_value=["spurd", "--hostname=worker"],
                    ),
                    patch.object(
                        upgrade,
                        "running_digest",
                        side_effect=lambda _, path: (
                            "old" if path.name == drift else args["manifest"][path.name]
                        ),
                    ),
                    patch.object(upgrade.tarfile, "open") as archive,
                    patch.object(upgrade.tempfile, "mkstemp") as temporary,
                    patch.object(upgrade, "command") as commands,
                    patch.object(upgrade, "wait_ready") as ready,
                ):
                    result = upgrade.activate(**args, check=True)
                self.assertEqual(result["changed"], drift is not None)
                archive.assert_not_called()
                temporary.assert_not_called()
                commands.assert_not_called()
                ready.assert_not_called()
                self.assertEqual(
                    {p: (p.read_bytes(), p.stat().st_ino) for p in install.iterdir()},
                    before,
                )
                if disk_drift:
                    (install / drift).write_bytes(original)

    def test_unchanged_install_still_rejects_corrupt_archive_bytes(self):
        install, state, args = self.activation_fixture()
        shutil.copyfile(self.source / "spurd", install / "spurd")
        (self.source / "spurstepd").write_text("corrupted archive member")
        with tarfile.open(args["archive"], "w:gz") as archive:
            for name in args["manifest"]:
                archive.add(self.source / name, arcname=name)
        with (
            patch.object(upgrade, "service", return_value=state),
            patch.object(
                upgrade, "running_digest", return_value=args["manifest"]["spurd"]
            ),
            patch.object(
                upgrade, "process_args", return_value=["spurd", "--hostname=worker"]
            ),
            patch.object(upgrade, "command") as commands,
        ):
            with self.assertRaisesRegex(
                ValueError, "Archive checksum mismatch: spurstepd"
            ):
                upgrade.activate(**args)
        commands.assert_not_called()
        for name, checksum in args["manifest"].items():
            self.assertEqual(upgrade.digest(install / name), checksum)
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

    def test_all_agent_artifacts_and_plugin_only_updates_keep_restart_contract(self):
        self.spec.update(auth=True, mpi=True)
        (self.source / "spur_mpi_pmix.so").write_bytes(b"new plugin")
        install, state, args = self.activation_fixture()
        args["auth"] = True
        plugin_dir = self.root / "plugins"
        plugin_dir.mkdir()
        args["plugin_dir"] = str(plugin_dir)
        manifest = args["manifest"]
        self.assertEqual(
            set(manifest),
            {"spur", "spurd", "spurstepd", "spurauthd", "spur_mpi_pmix.so"},
        )
        destinations = {
            name: (plugin_dir if name == "spur_mpi_pmix.so" else install) / name
            for name in manifest
        }
        for changed, restarts in (
            (set(manifest), ["spurauthd", "spurd"]),
            ({"spur_mpi_pmix.so"}, ["spurd"]),
            ({"spurauthd"}, ["spurauthd"]),
            ({"spurstepd"}, []),
        ):
            with self.subTest(changed=changed):
                for name, destination in destinations.items():
                    shutil.copyfile(self.source / name, destination)
                    destination.chmod(0o755)
                    if name in changed:
                        destination.write_text("old " + name)
                with (
                    patch.object(upgrade, "service", return_value=state),
                    patch.object(
                        upgrade,
                        "process_args",
                        return_value=["spurd", "--hostname=worker"],
                    ),
                    patch.object(
                        upgrade,
                        "running_digest",
                        side_effect=lambda _, path: (
                            "old" if path.name in changed else manifest[path.name]
                        ),
                    ),
                    patch.object(
                        upgrade, "command", return_value="version"
                    ) as commands,
                    patch.object(upgrade, "wait_ready", return_value=state) as ready,
                ):
                    result = upgrade.activate(**args)
                self.assertEqual(set(result["replaced"]), changed)
                self.assertEqual(result["restarted"], restarts)
                self.assertEqual(
                    [call.args[0] for call in commands.call_args_list][1:],
                    [["systemctl", "restart", name] for name in restarts],
                )
                self.assertEqual(
                    [call.args[0] for call in ready.call_args_list],
                    (["spurauthd"] if "spurauthd" in restarts else []) + ["spurd"],
                )
                for name, destination in destinations.items():
                    self.assertEqual(upgrade.digest(destination), manifest[name])
                self.assertEqual((self.root / "etc/spur.conf").read_text(), "preserved")

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
import json, os, shlex, signal, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['SPUR_TEST_STATE'])
action = sys.argv[1]
if action in ('daemon-reload', 'enable'):
    with (root / 'service-operations').open('a') as log: log.write(' '.join(sys.argv[1:]) + '\n')
    if action == 'daemon-reload':
        for unit in (root / 'system-units').glob('*.service'):
            (root / (unit.stem + '.loaded')).write_text(unit.read_text())
    sys.exit(0)
name = sys.argv[-1] if '--value' in sys.argv else sys.argv[2]
path = root / (name + '.json')
data = json.loads(path.read_text())
if action == 'show':
    if '--value' in sys.argv:
        print(data['pid'])
        sys.exit(0)
    print('MainPID=' + str(data['pid']))
    print('ActiveState=active')
    print('KillMode=' + data.get('kill_mode', 'process'))
    print('Environment=SPUR_STEPD_STATE_DIR=' + str(root / 'state'))
elif action == 'restart':
    os.kill(data['pid'], signal.SIGTERM)
    # Match systemctl's stop-before-start contract before rebinding the port.
    for attempt in range(500):
        try:
            state = Path('/proc', str(data['pid']), 'stat').read_text().rsplit(') ', 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            break
        if state in ('Z', 'X'):
            break
        time.sleep(0.01)
    else:
        sys.exit('fixture daemon did not stop')
    unit = (root / (name + '.loaded')).read_text()
    args = shlex.split(next(line.split('=', 1)[1] for line in unit.splitlines() if line.startswith('ExecStart=')))
    args.append(data['args'][-1])
    data['args'] = args
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
elif sys.argv[1:3] == ['show', 'node']:
    idle = (Path(os.environ['SPUR_TEST_STATE']) / 'idle-worker').exists()
    print('NodeName=registered-worker\nState=' + ('IDLE' if idle else 'ALLOCATED'))
    print('CPUAlloc=' + ('0' if idle else '1'))
else: sys.exit('unexpected state mutation')
"""


@unittest.skipUnless(
    shutil.which("ansible-playbook") and shutil.which("cc"),
    "requires ansible-core and a C compiler",
)
class PlaybookTests(unittest.TestCase):
    def test_real_playbook_reconciles_units_plugin_and_binaries_without_draining(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ansible = root / "ansible"
            shutil.copytree(ROOT / "ansible", ansible, symlinks=True)
            (root / "system-units").mkdir()
            environment_file = root / "environment"
            environment_file.write_text(
                'PATH="/usr/bin"\nKEEP=unchanged\nSPUR_ACCOUNTING_ADDR=stale\n'
            )
            # Exercise actual roles while confining every absolute config write.
            for task in (ansible / "roles").rglob("*.yml"):
                task.write_text(
                    task.read_text()
                    .replace("/etc/environment", str(environment_file))
                    .replace("/etc/systemd/system", str(root / "system-units"))
                )
            (root / "fake-bin").mkdir()
            (root / "fake-bin/systemctl").write_text(SYSTEMCTL)
            (root / "fake-bin/systemctl").chmod(0o755)
            (root / "daemon.c").write_text(DAEMON)
            env = dict(
                os.environ,
                SPUR_TEST_STATE=str(root),
                PATH=str(root / "fake-bin") + os.pathsep + os.environ["PATH"],
                ANSIBLE_NOCOLOR="1",
                ANSIBLE_CONFIG=str(ansible / "ansible.cfg"),
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
            (source / "spur_mpi_pmix.so").write_bytes(b"target pmix plugin")
            hosts, processes, saved = {}, [], {}
            try:
                for name, daemon in (
                    ("controller", "spurctld"),
                    ("worker-alias", "spurd"),
                ):
                    home = root / name
                    (home / "bin").mkdir(parents=True)
                    (home / "etc").mkdir()
                    (home / "plugins").mkdir()
                    (home / "plugins/spur_mpi_pmix.so").write_bytes(b"old plugin")
                    (root / "system-units" / (daemon + ".service")).write_text(
                        "stale unit\n"
                    )
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
                        "ansible_host": "127.0.0.1",
                        "spur_node_name": "registered-worker",
                        "spur_agent_state_dir": str(home / "agent-state"),
                        "spur_mpi_plugin_dir": str(home / "plugins"),
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
                            "spur_mpi_plugin_enabled": True,
                        }
                    ),
                ]
                # Defaults must be inventory vars, so per-host paths retain their precedence.
                defaults = ROOT / "ansible/inventory/group_vars/all.yml"
                (root / "group_vars").mkdir()
                shutil.copyfile(defaults, root / "group_vars/all.yml")

                def check_without_staged_archives(changed):
                    for host in hosts.values():
                        for archive in Path(host["spur_install_dir"]).glob(
                            ".spur-upgrade-*.tar.gz"
                        ):
                            archive.unlink()
                    paths = [
                        p
                        for host in hosts.values()
                        for p in Path(host["spur_home"]).rglob("*")
                        if p.is_file()
                    ] + list(root.glob("*.json"))
                    before = {p: (p.read_bytes(), p.stat().st_ino) for p in paths}
                    restarts = root / "restarts"
                    restart_log = restarts.read_bytes() if restarts.exists() else b""
                    result = subprocess.run(
                        command + ["--check"],
                        cwd=ansible,
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=90,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                    activations = result.stdout.split(
                        "TASK [Verify installed and running binaries against the staged build]"
                    )[1:]
                    self.assertEqual(len(activations), 2, result.stdout)
                    for host, activation in zip(hosts, activations):
                        self.assertIn(
                            ("changed" if changed else "ok") + ": [" + host + "]",
                            activation.split("\nTASK [", 1)[0],
                        )
                    self.assertEqual(
                        {p: (p.read_bytes(), p.stat().st_ino) for p in paths}, before
                    )
                    self.assertEqual(
                        restarts.read_bytes() if restarts.exists() else b"", restart_log
                    )
                    for host in hosts.values():
                        self.assertEqual(
                            list(
                                Path(host["spur_install_dir"]).glob(".spur-upgrade-*")
                            ),
                            [],
                        )

                check_without_staged_archives(changed=True)
                for run in range(3):
                    if run == 2:
                        (root / "idle-worker").touch()
                        (
                            Path(hosts["worker-alias"]["spur_install_dir"])
                            / "spurstepd"
                        ).unlink()
                    result = subprocess.run(
                        command,
                        cwd=ansible,
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=90,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertIn("2/2 nodes verified", result.stdout)
                    for role in ("spur_install", "spur_controller", "spur_agent"):
                        self.assertIn(role + " :", result.stdout)
                    if run < 2:
                        self.assertIn("spur_in_place_upgrade=true", result.stdout)
                    if run == 2:
                        self.assertIn(
                            "has no spurstepd yet and this run's source has one",
                            result.stdout,
                        )
                    self.assertEqual(
                        result.stdout.count("TASK [Stage binary bundles"), 2
                    )
                    self.assertEqual(
                        (root / "restarts").read_text().splitlines(),
                        ["spurctld", "spurd"] * (run + 1),
                    )
                    for name, host in hosts.items():
                        daemon = "spurctld" if name == "controller" else "spurd"
                        binaries = ["spur", daemon] + (
                            ["spurstepd"] if daemon == "spurd" else []
                        )
                        for binary in binaries:
                            self.assertEqual(
                                upgrade.digest(Path(host["spur_install_dir"]) / binary),
                                upgrade.digest(source / binary),
                            )
                        unit = (
                            root / "system-units" / (daemon + ".service")
                        ).read_text()
                        self.assertIn("ExecStart=" + host["spur_install_dir"], unit)
                        self.assertEqual(
                            (root / (daemon + ".loaded")).read_text(), unit
                        )
                        self.assertTrue(
                            (Path(host["spur_install_dir"]) / "squeue").is_symlink()
                        )
                    worker = hosts["worker-alias"]
                    self.assertEqual(
                        (
                            Path(worker["spur_mpi_plugin_dir"]) / "spur_mpi_pmix.so"
                        ).read_bytes(),
                        (source / "spur_mpi_pmix.so").read_bytes(),
                    )
                    self.assertTrue(Path(worker["spur_agent_state_dir"]).is_dir())
                    operations = (root / "service-operations").read_text().splitlines()
                    self.assertEqual(operations.count("daemon-reload"), 2 * (run + 1))
                    self.assertEqual(operations.count("enable spurd"), run + 1)
                    self.assertIn("KEEP=unchanged", environment_file.read_text())
                    self.assertNotIn(
                        "SPUR_ACCOUNTING_ADDR", environment_file.read_text()
                    )
                    for path, content in saved.items():
                        self.assertEqual(path.read_bytes(), content)
                    calls = (root / "cli-calls").read_text()
                    self.assertEqual(
                        calls.splitlines().count("show node registered-worker"),
                        run + 1,
                    )
                    self.assertNotIn(
                        "TASK [Wait for the node to re-register", result.stdout
                    )
                    for task in (
                        "Does this agent already run a spurstepd build?",
                        "Does the new build ship spurstepd?",
                        "Check whether this busy node already has a spurstepd supervisor",
                    ):
                        section = result.stdout.split("TASK [" + task + "]", 1)[1]
                        self.assertIn(
                            "skipping: [worker-alias]", section.split("\nTASK [", 1)[0]
                        )
                    self.assertNotIn("drain", calls)
                    self.assertNotIn("RESUME", calls)
                    self.assertNotIn("cancel", calls)
                check_without_staged_archives(changed=False)
                # An excluded controller without ansible_host still supplies endpoints.
                del hosts["controller"]["ansible_host"]
                (root / "inventory.yml").write_text(json.dumps(inventory))
                for extra in (
                    ["--limit", "worker-alias"],
                    ["--limit", "worker-alias", "--check"],
                ):
                    result = subprocess.run(
                        command + extra,
                        cwd=ansible,
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=90,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(
                        (root / "restarts").read_text().splitlines(),
                        ["spurctld", "spurd"] * 3 + ["spurd"],
                    )
                hosts["controller"]["ansible_host"] = "127.0.0.1"
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
                        cwd=ansible,
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
                        ["spurctld", "spurd"] * 3 + ["spurd", "spurctld", "spurd"],
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
