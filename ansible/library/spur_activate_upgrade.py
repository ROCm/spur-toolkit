#!/usr/bin/python
"""Verify target artifacts after provisioning, or activate a standalone bundle."""

import subprocess
import tarfile

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec={
            "archive": {"type": "path", "required": True},
            "manifest": {"type": "dict", "required": True},
            "install_dir": {"type": "path", "required": True},
            "home": {"type": "path", "required": True},
            "role": {"choices": ["controller", "agent"], "required": True},
            "node": {"type": "str", "required": True},
            "port": {"type": "int", "required": True},
            "timeout": {"type": "int", "default": 120},
            "plugin_dir": {"type": "path", "default": "/usr/lib/spur"},
            "auth": {"type": "bool", "default": False},
            "cluster": {"type": "str", "required": True},
            "verify_only": {"type": "bool", "default": False},
        },
        supports_check_mode=True,
    )
    from ansible.module_utils.spur_upgrade import (
        ActivationFailed,
        ConfigurationRequired,
        activate,
    )

    params = dict(module.params)
    node = params.pop("node")
    try:
        result = activate(**params, check=module.check_mode)
    except ConfigurationRequired as error:
        if params["verify_only"] and not module.check_mode:
            module.fail_json(msg="Installation verification failed: %s" % error)
        module.warn("Configuration reconciliation required: %s" % error)
        module.exit_json(changed=False, configuration_required=str(error))
    except ActivationFailed as error:
        module.fail_json(changed=True, msg=str(error))
    except (
        OSError,
        ValueError,
        RuntimeError,
        EOFError,
        subprocess.TimeoutExpired,
        tarfile.TarError,
    ) as error:
        module.fail_json(msg=str(error))
    if not module.check_mode:
        result["ansible_facts"] = {
            "spur_verify_row": {
                "node": node,
                "role": params["role"],
                "port": params["port"],
                "listening": result["listening"],
                "binary_current": result["binary_current"],
                "version": result["version"],
                "registered_state": "",
            }
        }
    module.exit_json(**result)


if __name__ == "__main__":
    main()
