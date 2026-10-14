#!/usr/bin/python
"""Validate a staged build before any provisioning or binary replacement."""

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
        },
        supports_check_mode=True,
    )
    from ansible.module_utils.spur_upgrade import service, validate_archive

    try:
        validate_archive(module.params["archive"], module.params["manifest"])
        service("spurctld" if module.params["role"] == "controller" else "spurd")
        if module.params["auth"]:
            service("spurauthd")
    except (
        OSError,
        ValueError,
        RuntimeError,
        EOFError,
        subprocess.TimeoutExpired,
        tarfile.TarError,
    ) as error:
        module.fail_json(msg=str(error))
    module.exit_json(changed=False)


if __name__ == "__main__":
    main()
