#!/usr/bin/python
"""Validate a staged build before any provisioning or binary replacement."""

import os
import subprocess
import tarfile

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec={
            "archive": {"type": "path", "required": True},
            "manifest": {"type": "dict", "required": True},
            "role": {"choices": ["controller", "agent"], "required": True},
            "auth": {"type": "bool", "default": False},
            "stepd_path": {"type": "path"},
        },
        supports_check_mode=True,
    )
    from ansible.module_utils.spur_upgrade import service, validate_archive

    result = {}
    try:
        required = {
            "spur",
            "spurctld" if module.params["role"] == "controller" else "spurd",
        }
        if module.params["auth"]:
            required.add("spurauthd")
        if not required <= module.params["manifest"].keys():
            raise ValueError(
                "Missing required upgrade artifacts: %s"
                % ", ".join(sorted(required - module.params["manifest"].keys()))
            )
        validate_archive(module.params["archive"], module.params["manifest"])
        service("spurctld" if module.params["role"] == "controller" else "spurd")
        if module.params["auth"]:
            service("spurauthd")
        if module.params.get("stepd_path"):
            try:
                os.lstat(module.params["stepd_path"])
            except FileNotFoundError:
                result["stepd_exists"] = False
            else:
                result["stepd_exists"] = True
    except (
        OSError,
        ValueError,
        RuntimeError,
        EOFError,
        subprocess.TimeoutExpired,
        tarfile.TarError,
    ) as error:
        module.fail_json(msg=str(error))
    module.exit_json(changed=False, **result)


if __name__ == "__main__":
    main()
