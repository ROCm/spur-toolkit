from __future__ import annotations

DOCUMENTATION = r"""
    name: spur_conf
    plugin_type: inventory
    short_description: Use a spur.conf file itself as the Ansible inventory source
    description:
        - Reads cluster topology directly from a spur.conf TOML file (the same file
          spurctld/spurd load) instead of a separate hosts.ini.
        - Controllers come from [controller].hosts, agents from [[nodes]].
        - Ansible-only connection metadata ([ansible] table, ansible_* keys on each
          [[nodes]] entry) is read here but stripped before the file is ever pushed
          back to a node — see the spur_conf_for_push filter.
    options:
        plugin:
            description: Token that ensures this is a source file for the 'spur_conf' plugin.
            required: false
"""

import os

try:
    import tomlkit
except ImportError:  # pragma: no cover
    tomlkit = None

from ansible.errors import AnsibleParserError
from ansible.plugins.inventory import BaseInventoryPlugin


class InventoryModule(BaseInventoryPlugin):

    NAME = "spur_conf"

    def verify_file(self, path):
        if not super(InventoryModule, self).verify_file(path):
            return False
        if os.path.isdir(path):
            return False
        try:
            with open(path, "r") as f:
                head = f.read(4096)
        except OSError:
            return False
        # Content-sniff rather than a fixed filename: any spur.conf has both a
        # cluster_name key and at least one [[nodes]] array-of-tables header.
        return "cluster_name" in head and "[[nodes]]" in head

    def parse(self, inventory, loader, path, cache=True):
        super(InventoryModule, self).parse(inventory, loader, path, cache=cache)

        if tomlkit is None:
            raise AnsibleParserError(
                "the spur_conf inventory plugin requires the 'tomlkit' Python "
                "package on the control node (pip install tomlkit)"
            )

        with open(path, "r") as f:
            doc = tomlkit.parse(f.read())

        ansible_meta = doc.get("ansible", {})
        default_user = ansible_meta.get("default_user")

        inventory.add_group("spur_controllers")
        inventory.add_group("spur_agents")

        controller_hosts = doc.get("controller", {}).get("hosts", [])
        for idx, addr in enumerate(controller_hosts):
            name = "ctl-%d" % idx
            inventory.add_host(name, group="spur_controllers")
            inventory.set_variable(name, "ansible_host", addr)
            if default_user:
                inventory.set_variable(name, "ansible_user", default_user)
            inventory.set_variable(name, "spur_node_name", name)

        for node in doc.get("nodes", []):
            name = node.get("names")
            if not name:
                raise AnsibleParserError("a [[nodes]] entry is missing 'names'")
            inventory.add_host(name, group="spur_agents")
            ansible_host = node.get("ansible_host", name)
            inventory.set_variable(name, "ansible_host", ansible_host)
            ansible_user = node.get("ansible_user", default_user)
            if ansible_user:
                inventory.set_variable(name, "ansible_user", ansible_user)
            inventory.set_variable(name, "spur_node_name", name)
