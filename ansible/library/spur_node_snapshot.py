#!/usr/bin/python
"""Query or wait for a set of nodes without per-node SSH/RPC loops."""

import subprocess
from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec={
            "binary": {"type": "path", "required": True},
            "expected": {"type": "list", "elements": "str", "default": []},
            "states": {"type": "list", "elements": "str", "default": []},
            "timeout": {"type": "int", "default": 0},
        },
        supports_check_mode=True,
    )
    if module.params["timeout"] < 0:
        module.fail_json(msg="timeout must be nonnegative")
    from ansible.module_utils.spur_nodes import snapshot

    try:
        result = snapshot(**module.params)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        module.fail_json(
            msg=str(error),
            ansible_facts={
                "spur_node_snapshot": {
                    "nodes": {},
                    "pending": module.params["expected"],
                }
            },
        )
    module.exit_json(
        changed=False, **result, ansible_facts={"spur_node_snapshot": result}
    )


if __name__ == "__main__":
    main()
