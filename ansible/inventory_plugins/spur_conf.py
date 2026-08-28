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
        # Content-sniff rather than a fixed filename: every spur.conf has a
        # cluster_name key and a [controller] section. Deliberately not
        # requiring "[[nodes]]" too — a fresh control-plane-only cluster with
        # zero agents yet renders that array as `nodes = []`, no header at all.
        return "cluster_name" in head and "[controller]" in head

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
        default_connection = ansible_meta.get("default_connection")

        inventory.add_group("spur_controllers")
        inventory.add_group("spur_agents")
        inventory.add_group("spur_login")
        inventory.add_group("spur_accounting_node")

        # [[nodes]] first, so a controller that's ALSO an agent (single-node
        # deploys, or an HA controller that doubles as a compute node) can be
        # detected below rather than getting a second, disconnected identity.
        agent_names = set()
        for node in doc.get("nodes", []):
            name = node.get("names")
            if not name:
                raise AnsibleParserError("a [[nodes]] entry is missing 'names'")
            agent_names.add(name)
            inventory.add_host(name, group="spur_agents")
            inventory.set_variable(name, "ansible_host", node.get("ansible_host", name))
            user = node.get("ansible_user", default_user)
            if user:
                inventory.set_variable(name, "ansible_user", user)
            connection = node.get("ansible_connection", default_connection)
            if connection:
                inventory.set_variable(name, "ansible_connection", connection)
            wg_address = node.get("ansible_wg_address")
            if wg_address:
                inventory.set_variable(name, "spur_wg_address", wg_address)
            inventory.set_variable(name, "spur_node_name", name)

        # [[ansible_controllers]] entries let a controller's SSH target diverge
        # from its listen IP (e.g. behind NAT); keyed by that listen IP.
        controller_overrides = {}
        for entry in doc.get("ansible_controllers", []):
            host = entry.get("host")
            if not host:
                raise AnsibleParserError("an [[ansible_controllers]] entry is missing 'host'")
            controller_overrides[host] = entry

        # A controller's inventory name is always ctl-<index> (position in
        # [controller].hosts) — to make it ALSO an agent, add a [[nodes]]
        # entry with names = "ctl-<index>". When that collision exists, this
        # is the SAME host: keep the [[nodes]] entry's connection info (it's
        # the one with real ansible_host/ansible_connection/wg details) and
        # just add the extra group membership, rather than creating a second,
        # disconnected identity for the same physical/container host.
        controller_hosts = doc.get("controller", {}).get("hosts", [])
        for idx, addr in enumerate(controller_hosts):
            name = "ctl-%d" % idx
            if name in agent_names:
                inventory.add_host(name, group="spur_controllers")
                continue
            override = controller_overrides.get(addr, {})
            inventory.add_host(name, group="spur_controllers")
            inventory.set_variable(name, "ansible_host", override.get("ansible_host", addr))
            user = override.get("ansible_user", default_user)
            if user:
                inventory.set_variable(name, "ansible_user", user)
            connection = override.get("ansible_connection", default_connection)
            if connection:
                inventory.set_variable(name, "ansible_connection", connection)
            wg_address = override.get("ansible_wg_address")
            if wg_address:
                inventory.set_variable(name, "spur_wg_address", wg_address)
            inventory.set_variable(name, "spur_node_name", name)

        self._add_ansible_only_hosts(inventory, doc.get("ansible_login_nodes", []),
                                      "spur_login", default_user, default_connection)
        self._add_ansible_only_hosts(inventory, doc.get("ansible_accounting_nodes", []),
                                      "spur_accounting_node", default_user, default_connection)

    @staticmethod
    def _add_ansible_only_hosts(inventory, entries, group, default_user, default_connection):
        # Shared by [[ansible_login_nodes]] and [[ansible_accounting_nodes]] —
        # both are pure ansible/ops concepts with no natural home elsewhere in
        # spur.conf's runtime schema (no daemon, no [[nodes]] entry).
        for entry in entries:
            name = entry.get("name")
            if not name:
                raise AnsibleParserError("an [[%s]] entry is missing 'name'" % group)
            inventory.add_host(name, group=group)
            inventory.set_variable(name, "ansible_host", entry.get("ansible_host", name))
            user = entry.get("ansible_user", default_user)
            if user:
                inventory.set_variable(name, "ansible_user", user)
            connection = entry.get("ansible_connection", default_connection)
            if connection:
                inventory.set_variable(name, "ansible_connection", connection)
            wg_address = entry.get("ansible_wg_address")
            if wg_address:
                inventory.set_variable(name, "spur_wg_address", wg_address)
            inventory.set_variable(name, "spur_node_name", name)
