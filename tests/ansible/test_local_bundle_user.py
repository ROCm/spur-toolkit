"""Run production bundle preparation and copying with inventory sudo enabled."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import ansible
import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "ansible"


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires ansible-core")
class LocalBundleUserTests(unittest.TestCase):
    def test_inventory_become_does_not_escalate_local_bundle_preparation(self):
        for mode in ("upgrade", "install"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source"
                source.mkdir()
                for name in ("spur", "spurctld"):
                    (source / name).write_text("fixture " + name)
                home = root / "home"
                home.mkdir()
                destination = root / "installed"
                destination.mkdir()
                # A forbidden become executable detects escalation without sudo or root writes.
                guard = root / "forbid-become"
                guard.write_text(
                    "#!/bin/sh\necho LOCAL_BUNDLE_BECOME_FORBIDDEN >&2\nexit 99\n"
                )
                guard.chmod(0o755)
                defaults = yaml.safe_load(
                    (ANSIBLE / "inventory/group_vars/all.yml").read_text()
                )
                defaults.update(
                    ansible_connection="ssh",
                    ansible_become=True,
                    ansible_become_exe=str(guard),
                    ansible_python_interpreter=sys.executable,
                    spur_binary_src=str(source),
                    spur_install_dir=str(destination),
                    spur_in_place_upgrade=True,
                    spur_auth_enabled=False,
                    spur_mpi_plugin_enabled=False,
                    spur_overwrite_conf=False,
                )
                # No localhost inventory entry. Only the destination opts out of sudo
                # because this fixture writes to a temporary directory, not /usr/local.
                inventory = {
                    "all": {
                        "vars": defaults,
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
                if mode == "upgrade":
                    plays = yaml.safe_load(
                        (ANSIBLE / "playbooks/rolling_upgrade.yml").read_text()
                    )
                    preparation = next(
                        p
                        for p in plays
                        if p["name"] == "Prepare and stage local in-place upgrades"
                    )["tasks"][0]
                    copy_task = yaml.safe_load(
                        (ANSIBLE / "playbooks/tasks/stage_upgrade.yml").read_text()
                    )[0]
                    fact = "ru_prepared"
                    artifact = (
                        f"hostvars['localhost'].{fact}.artifacts['ctl']['controller']"
                    )
                    cache = "spur-ansible-upgrade"
                    staged = destination / ".spur-upgrade-controller.tar.gz"
                else:
                    tasks = yaml.safe_load(
                        (ANSIBLE / "roles/spur_install/tasks/main.yml").read_text()
                    )
                    preparation = next(
                        t
                        for t in tasks
                        if t["name"] == "Prepare local install artifacts for this batch"
                    )
                    fact = "spur_install_prepared"
                    artifact = f"hostvars['localhost'].{fact}.artifacts['ctl']"
                    cache = "spur-ansible-install"
                    # Use the production install copy task.
                    copy_task = next(
                        t
                        for t in tasks
                        if "ansible.builtin.copy" in t
                        and "spur_bin_tarball_local" in str(t)
                    )
                    staged = None
                play_tasks = [preparation]
                if mode == "install":
                    play_tasks.extend(
                        t
                        for t in tasks
                        if t.get("name")
                        == "Select this host's prepared installation artifact"
                    )
                play_tasks.append(
                    {
                        "ansible.builtin.assert": {
                            "that": [
                                f"({artifact}).archive.startswith('{home}/.cache/{cache}/')",
                            ]
                        }
                    }
                )
                play_tasks.append(copy_task)
                inventory_path = root / "inventory.yml"
                inventory_path.write_text(yaml.safe_dump(inventory))
                playbook = root / "play.yml"
                playbook.write_text(
                    yaml.safe_dump(
                        [
                            {
                                "hosts": "spur_controllers",
                                "gather_facts": False,
                                "become": True,
                                "tasks": play_tasks,
                            }
                        ]
                    )
                )
                result = subprocess.run(
                    ["ansible-playbook", "-i", str(inventory_path), str(playbook)],
                    cwd=ANSIBLE,
                    env=dict(
                        os.environ,
                        HOME=str(home),
                        ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg"),
                        ANSIBLE_NOCOLOR="1",
                        PYTHONPATH=str(Path(ansible.__file__).resolve().parent.parent)
                        + os.pathsep
                        + os.environ.get("PYTHONPATH", ""),
                    ),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=45,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("LOCAL_BUNDLE_BECOME_FORBIDDEN", result.stdout)
                archives = list((home / ".cache" / cache).glob("*.tar.gz"))
                self.assertEqual(len(archives), 1, result.stdout)
                self.assertEqual(archives[0].stat().st_uid, os.getuid())
                if staged is None:
                    copies = list(destination.glob(".bins-*.tar.gz"))
                    self.assertEqual(len(copies), 1, result.stdout)
                    staged = copies[0]
                self.assertEqual(staged.read_bytes(), archives[0].read_bytes())
                with tarfile.open(staged) as archive:
                    self.assertEqual(set(archive.getnames()), {"spur", "spurctld"})
