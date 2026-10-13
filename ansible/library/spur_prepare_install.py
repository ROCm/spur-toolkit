#!/usr/bin/python
"""Discover and package local install sources once for the selected hosts."""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec={"targets": {"type": "dict", "required": True}},
        supports_check_mode=True,
    )
    from ansible.module_utils.spur_upgrade import prepare_install

    try:
        result = prepare_install(
            module.params["targets"], "~/.cache/spur-ansible-install"
        )
    except (OSError, ValueError) as error:
        module.fail_json(msg=str(error))
    module.exit_json(changed=False, ansible_facts={"spur_install_prepared": result})


if __name__ == "__main__":
    main()
