"""Exercise the production rolling play's batch and failure policy locally."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "ansible"


@unittest.skipUnless(shutil.which("ansible-playbook"), "requires ansible-core")
class FailurePolicy(unittest.TestCase):
    def test_failed_agent_does_not_abort_later_batches_only_when_tolerated(self):
        plays = yaml.safe_load((ANSIBLE / "playbooks/rolling_upgrade.yml").read_text())
        play = next(
            p
            for p in plays
            if p.get("hosts") == "spur_agents" and "Rolling upgrade" in p["name"]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ANSIBLE / "playbooks/tasks", root / "tasks")
            (root / "tasks/upgrade_agent_full.yml").write_text("[]\n")
            (root / "library").mkdir()
            (root / "library/spur_validate_upgrade.py").write_text(
                "from ansible.module_utils.basic import AnsibleModule\n"
                "m = AnsibleModule(argument_spec={'archive': {}, 'manifest': {'type': 'dict'}, 'role': {}, 'auth': {'type': 'bool'}})\n"
                "m.exit_json(changed=False)\n"
            )
            # Keep the actual module interface; substitute only daemon activation.
            module = (ANSIBLE / "library/spur_activate_upgrade.py").read_text()
            module = module.replace(
                "    try:\n        result = activate",
                """    from pathlib import Path
    if node == "bad":
        module.fail_json(msg="fixture unhealthy spurd")
    Path(params["home"], node).touch()
    module.exit_json(changed=False, node_name=node)
    try:
        result = activate""",
            )
            (root / "library/spur_activate_upgrade.py").write_text(module)
            (root / "spur").write_text(
                "#!/bin/sh\nprintf 'NodeName=good\\n State=IDLE\\nNodeName=later\\n State=ALLOCATED\\n'\n"
            )
            (root / "spur").chmod(0o755)
            (root / "play.yml").write_text(yaml.safe_dump([play, plays[-1]]))
            defaults = yaml.safe_load(
                (ANSIBLE / "inventory/group_vars/all.yml").read_text()
            )
            defaults.update(
                ansible_connection="local",
                ansible_become=False,
                ansible_python_interpreter=shutil.which("python3"),
                spur_install_dir=str(root),
                spur_home=str(root),
            )
            inventory = {
                "all": {
                    "vars": defaults,
                    "children": {
                        "spur_controllers": {"hosts": {"controller": {}}},
                        "spur_agents": {
                            "hosts": {name: {} for name in ["bad", "good", "later"]}
                        },
                        "preparation": {
                            "hosts": {
                                "localhost": {
                                    "ru_prepared": {
                                        "artifacts": {
                                            name: {"agent": {"manifest": {}}}
                                            for name in ["bad", "good", "later"]
                                        }
                                    }
                                }
                            }
                        },
                    },
                }
            }
            (root / "inventory.yml").write_text(json.dumps(inventory))
            env = dict(
                os.environ,
                ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg"),
                ANSIBLE_LIBRARY=str(root / "library")
                + os.pathsep
                + str(ANSIBLE / "library"),
            )
            for batch in [1, 2]:
                for tolerate in [False, True]:
                    with self.subTest(batch=batch, tolerate=tolerate):
                        for name in ["good", "later"]:
                            (root / name).unlink(missing_ok=True)
                        result = subprocess.run(
                            [
                                "ansible-playbook",
                                "-i",
                                str(root / "inventory.yml"),
                                str(root / "play.yml"),
                                "-e",
                                json.dumps(
                                    {
                                        "spur_rolling_batch_size": batch,
                                        "spur_ignore_unreachable_agents": tolerate,
                                    }
                                ),
                            ],
                            cwd=ANSIBLE,
                            env=env,
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            timeout=60,
                        )
                        self.assertEqual(result.returncode, 2, result.stdout)
                        self.assertIn("fixture unhealthy spurd", result.stdout)
                        self.assertEqual(
                            (root / "later").exists(), tolerate, result.stdout
                        )
                        if tolerate:
                            self.assertTrue((root / "good").exists(), result.stdout)
                            self.assertIn(
                                "Verify this upgraded agent registered", result.stdout
                            )


if __name__ == "__main__":
    unittest.main()
