#!/usr/bin/python
"""Refuse an accidental migration from the former default binary directory."""

import os
from pathlib import Path
import shlex

from ansible.module_utils.basic import AnsibleModule


def legacy_install_in_use(install_dir, legacy_dir, unit_dir):
    if os.path.realpath(install_dir) == os.path.realpath(legacy_dir):
        return False
    legacy = Path(legacy_dir)
    for name in ("spurctld", "spurd", "spurauthd"):
        unit = Path(unit_dir) / (name + ".service")
        if not unit.exists():
            continue
        for line in unit.read_text().splitlines():
            if line.strip().startswith("ExecStart="):
                command = shlex.split(line.partition("=")[2])
                if command and command[0].lstrip("-+!:@") == str(legacy / name):
                    return True
    return (legacy / "spur").exists() and not (Path(install_dir) / "spur").exists()


def main():
    module = AnsibleModule(
        argument_spec={
            "install_dir": {"type": "path", "required": True},
            "legacy_dir": {"type": "path", "default": "/root/.local/bin"},
            "unit_dir": {"type": "path", "default": "/etc/systemd/system"},
            "allow_migration": {"type": "bool", "default": False},
        },
        supports_check_mode=True,
    )
    try:
        if not module.params["allow_migration"] and legacy_install_in_use(
            module.params["install_dir"],
            module.params["legacy_dir"],
            module.params["unit_dir"],
        ):
            module.fail_json(
                msg=(
                    "Existing Spur installation uses %s, but spur_install_dir is %s. "
                    "Retain spur_install_dir=%s for upgrades and admin playbooks. "
                    "For a planned path migration, use deploy.yml with "
                    "spur_allow_install_dir_migration=true during a maintenance window."
                )
                % (
                    module.params["legacy_dir"],
                    module.params["install_dir"],
                    module.params["legacy_dir"],
                )
            )
    except (OSError, ValueError) as error:
        module.fail_json(msg=str(error))
    module.exit_json(changed=False)


if __name__ == "__main__":
    main()
