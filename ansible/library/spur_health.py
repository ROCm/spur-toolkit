#!/usr/bin/python
"""Collect all enabled health checks without changing host state."""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec={
            "role": {
                "choices": ["controller", "agent", "accounting"],
                "required": True,
            },
            "install_dir": {"type": "path", "required": True},
            "home": {"type": "path", "required": True},
            "port": {"type": "int", "default": 6818},
            "auth": {"type": "bool", "default": False},
            "cluster": {"type": "str", "default": "spur"},
            "key_dir": {"type": "path", "default": "/etc/spur"},
            "accounting": {"type": "bool", "default": True},
            "cgroup": {"type": "bool", "default": True},
            "require_stepd": {"type": "bool", "default": False},
        },
        supports_check_mode=True,
    )
    from ansible.module_utils.spur_health import health

    module.exit_json(changed=False, problems=health(**module.params))


if __name__ == "__main__":
    main()
