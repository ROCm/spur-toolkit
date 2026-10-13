#!/usr/bin/python
"""Prepare local binary bundles once before entering the serial upgrade plays."""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec={
            "targets": {"type": "dict", "required": True},
        },
        supports_check_mode=True,
    )
    from ansible.module_utils.spur_upgrade import prepare

    try:
        result = prepare(module.params["targets"], "~/.cache/spur-ansible-upgrade")
    except (OSError, ValueError) as error:
        module.fail_json(msg=str(error))
    # Keep the fleet manifest on the delegated host instead of copying it to every host.
    module.exit_json(changed=False, ansible_facts={"ru_prepared": result})


if __name__ == "__main__":
    main()
