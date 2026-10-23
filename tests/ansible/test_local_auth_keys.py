"""Exercise auth directory preparation, fetch and distribution with inventory sudo."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import ansible
import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "ansible"


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires ansible-core")
class LocalAuthKeysTests(unittest.TestCase):
    def test_local_keys_reuse_and_generation_with_inventory_become(self):
        for existing in (False, True):
            with self.subTest(
                existing=existing
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                keys = root / "keys"
                remote = root / "remote"
                remote.mkdir()
                files = yaml.safe_load(
                    (ANSIBLE / "roles/spur_auth/defaults/main.yml").read_text()
                )["spur_auth_key_files"]
                if existing:
                    keys.mkdir(mode=0o700)
                    for name in files:
                        (keys / name).write_text("existing " + name)
                        (keys / name).chmod(0o600)
                cli = remote / "spur"
                cli.write_text(
                    "#!/usr/bin/env python3\nimport pathlib, sys\n"
                    'for flag in ("--out", "--signing", "--verify"):\n'
                    "    if flag in sys.argv:\n"
                    "        path = pathlib.Path(sys.argv[sys.argv.index(flag) + 1])\n"
                    '        path.write_text("generated " + path.name)\n'
                    "        path.chmod(0o600)\n"
                )
                cli.chmod(0o755)
                guard = root / "forbid-become"
                guard.write_text(
                    "#!/bin/sh\necho AUTH_LOCAL_BECOME_FORBIDDEN >&2\nexit 99\n"
                )
                guard.chmod(0o755)
                role_tasks = yaml.safe_load(
                    (ANSIBLE / "roles/spur_auth/tasks/main.yml").read_text()
                )
                start = next(
                    i
                    for i, t in enumerate(role_tasks)
                    if t["name"] == "Ensure the control-node keys dir exists"
                )
                end = next(
                    i
                    for i, t in enumerate(role_tasks)
                    if t["name"]
                    == "Distribute this host's key files (mode 0600, root-owned)"
                )
                tasks = role_tasks[start : end + 1]
                # Destination ownership is a provisioning concern; keep this test unprivileged.
                tasks[-1]["ansible.builtin.copy"].pop("owner")
                tasks[-1]["ansible.builtin.copy"].pop("group")
                tasks.insert(
                    0,
                    {
                        "ansible.builtin.set_fact": {
                            "spur_auth_is_controller": True,
                            "spur_auth_is_agent": False,
                        }
                    },
                )
                variables = {
                    "ansible_connection": "ssh",
                    "ansible_become": True,
                    "ansible_become_exe": str(guard),
                    "ansible_python_interpreter": sys.executable,
                    "spur_auth_keys_dir": str(keys),
                    "spur_auth_key_dir": str(remote),
                    "spur_auth_key_files": files,
                    "spur_install_dir": str(remote),
                }
                inventory = {
                    "all": {
                        "vars": variables,
                        "children": {
                            "spur_controllers": {
                                "hosts": {
                                    "ctl": {
                                        "ansible_connection": "local",
                                        "ansible_become": False,
                                    }
                                }
                            },
                            "spur_agents": {"hosts": {}},
                        },
                    }
                }
                (root / "inventory.yml").write_text(yaml.safe_dump(inventory))
                (root / "play.yml").write_text(
                    yaml.safe_dump(
                        [
                            {
                                "hosts": "spur_controllers",
                                "become": True,
                                "gather_facts": False,
                                "tasks": tasks,
                            }
                        ]
                    )
                )
                result = subprocess.run(
                    [
                        "ansible-playbook",
                        "-i",
                        str(root / "inventory.yml"),
                        str(root / "play.yml"),
                    ],
                    cwd=ANSIBLE,
                    env=dict(
                        os.environ,
                        ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg"),
                        ANSIBLE_NOCOLOR="1",
                        PYTHONPATH=str(Path(ansible.__file__).resolve().parent.parent)
                        + os.pathsep
                        + os.environ.get("PYTHONPATH", ""),
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("AUTH_LOCAL_BECOME_FORBIDDEN", result.stdout)
                self.assertEqual(keys.stat().st_uid, os.getuid())
                self.assertEqual(keys.stat().st_mode & 0o777, 0o700)
                for name in files:
                    self.assertEqual(
                        (keys / name).read_text(),
                        ("existing " if existing else "generated ") + name,
                    )
                for name in (
                    "auth.jwks",
                    "controller-signing.jwks",
                    "cred-signing.jwks",
                    "node-signing.jwks",
                ):
                    self.assertEqual(
                        (remote / name).read_bytes(), (keys / name).read_bytes()
                    )
                    self.assertEqual((remote / name).stat().st_mode & 0o777, 0o600)
                self.assertFalse((remote / "controller-verification.jwks").exists())

    def test_source_stepd_probe_ignores_inventory_become(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "spurstepd").write_text("fixture")
            guard = root / "forbid-become"
            guard.write_text(
                "#!/bin/sh\necho SOURCE_LOCAL_BECOME_FORBIDDEN >&2\nexit 99\n"
            )
            guard.chmod(0o755)
            task = next(
                t
                for t in yaml.safe_load(
                    (ANSIBLE / "playbooks/tasks/upgrade_agent_full.yml").read_text()
                )
                if t["name"] == "Does the new build ship spurstepd?"
            )
            inventory = {
                "all": {
                    "vars": {
                        "ansible_become": True,
                        "ansible_become_exe": str(guard),
                        "spur_binary_src": str(root),
                        "ansible_python_interpreter": sys.executable,
                    },
                    "hosts": {
                        "worker": {
                            "ansible_connection": "local",
                            "ansible_become": False,
                        }
                    },
                }
            }
            (root / "inventory.yml").write_text(yaml.safe_dump(inventory))
            (root / "play.yml").write_text(
                yaml.safe_dump(
                    [
                        {
                            "hosts": "worker",
                            "gather_facts": False,
                            "tasks": [
                                task,
                                {
                                    "ansible.builtin.assert": {
                                        "that": ["ru_src_stepd.stat.exists"]
                                    }
                                },
                            ],
                        }
                    ]
                )
            )
            result = subprocess.run(
                [
                    "ansible-playbook",
                    "-i",
                    str(root / "inventory.yml"),
                    str(root / "play.yml"),
                ],
                cwd=ANSIBLE,
                env=dict(
                    os.environ,
                    ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg"),
                    ANSIBLE_NOCOLOR="1",
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
