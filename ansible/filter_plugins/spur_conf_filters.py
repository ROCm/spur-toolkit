from __future__ import annotations

try:
    import tomlkit
except ImportError:  # pragma: no cover
    tomlkit = None

from ansible.errors import AnsibleFilterError


def spur_conf_exclude_sections(master_text):
    """Return the list of top-level section names the master file marks as
    [ansible].exclude_sections (empty list if the table/key is absent)."""
    if tomlkit is None:
        raise AnsibleFilterError("spur_conf_exclude_sections requires the 'tomlkit' Python package")

    doc = tomlkit.parse(master_text)
    return list(doc.get("ansible", {}).get("exclude_sections", []))


def spur_conf_for_push(master_text, node_fact_fallbacks=None, controller_ha_fallback=None, drop_sections=None):
    """Return the spur.conf content that should be pushed to a node: the
    master file with all ansible-only content stripped ([ansible] table,
    [[ansible_controllers]], [[ansible_login_nodes]], and any ansible_* keys
    on [[nodes]]/[[ansible_controllers]] entries).

    Every section — including ones later excluded by spur_conf_splice_excluded
    — is kept here. exclude_sections is handled entirely in that second step
    by deciding whether to keep this (master's) version or the node's current
    one; dropping the section here would mean a section that's never existed
    on the node yet (first-ever push) never gets created at all.

    node_fact_fallbacks: optional {node_name: {"cpus": int, "memory_mb": int}}
    used to fill a [[nodes]] entry's cpus/memory_mb ONLY when the master file
    itself leaves them absent — an explicit value in the file always wins.

    controller_ha_fallback: optional {"peers": [...], "node_id": int} used to
    fill [controller].peers/node_id ONLY when the master's [controller]
    section omits them. node_id is inherently per-target-host (each
    controller has its own), so this must be computed by the caller once per
    host being pushed to, not once for the whole run.

    drop_sections: optional list of section names to unconditionally remove,
    bypassing exclude_sections entirely — used to keep controller-only
    content (e.g. [accounting]'s DB credentials) off every compute agent.
    """
    if tomlkit is None:
        raise AnsibleFilterError("spur_conf_for_push requires the 'tomlkit' Python package")

    node_fact_fallbacks = node_fact_fallbacks or {}
    doc = tomlkit.parse(master_text)

    doc.pop("ansible", None)
    doc.pop("ansible_controllers", None)
    doc.pop("ansible_login_nodes", None)
    for section in drop_sections or []:
        doc.pop(section, None)

    for node in doc.get("nodes", []):
        for key in [k for k in node.keys() if k.startswith("ansible_")]:
            node.pop(key)
        fallback = node_fact_fallbacks.get(node.get("names"), {})
        if "cpus" not in node and "cpus" in fallback:
            node["cpus"] = int(fallback["cpus"])
        if "memory_mb" not in node and "memory_mb" in fallback:
            node["memory_mb"] = int(fallback["memory_mb"])

    # Ansible/Jinja templating of nested vars: dict values can round-trip a
    # native int as a string (e.g. node_id="1") — TOML integers must not be
    # quoted, so coerce explicitly rather than trust the caller's type.
    if controller_ha_fallback and "controller" in doc:
        ctrl = doc["controller"]
        if "peers" not in ctrl and controller_ha_fallback.get("peers"):
            ctrl["peers"] = list(controller_ha_fallback["peers"])
        if "node_id" not in ctrl and controller_ha_fallback.get("node_id") is not None:
            ctrl["node_id"] = int(controller_ha_fallback["node_id"])

    return tomlkit.dumps(doc)


def spur_conf_splice_excluded(desired_text, existing_text, exclude_sections=None):
    """Decide, per excluded section, whether to keep the freshly-computed
    desired content (master's version — used when the node has no prior copy
    of that section, i.e. write-if-missing) or the node's CURRENT on-disk
    version (used whenever that section already exists there, preserving any
    hand edit indefinitely). Non-excluded sections always take the desired
    (master) version — this is the only place exclude_sections is enforced.
    """
    if tomlkit is None:
        raise AnsibleFilterError("spur_conf_splice_excluded requires the 'tomlkit' Python package")

    exclude_sections = set(exclude_sections or [])
    if not exclude_sections or not existing_text:
        return desired_text

    desired = tomlkit.parse(desired_text)
    existing = tomlkit.parse(existing_text)

    for section in exclude_sections:
        if section in existing:
            desired[section] = existing[section]

    return tomlkit.dumps(desired)


def spur_conf_node_facts(agent_names, hostvars):
    """Build the node_fact_fallbacks dict spur_conf_for_push expects, from
    live gathered facts on each agent host (Ansible's hostvars). Missing
    facts default to 0 rather than raising, matching the old template's
    `| default(0)` behavior.
    """
    facts = {}
    for name in agent_names:
        hv = hostvars.get(name, {})
        facts[name] = {
            "cpus": hv.get("ansible_processor_vcpus", 0),
            "memory_mb": int((hv.get("ansible_memtotal_mb", 0) or 0) * 0.9),
        }
    return facts


def spur_conf_remove_node(master_text, node_names):
    """Return the master file with the given node name(s) dropped entirely:
    their [[nodes]] entry, and pruned out of any [[partitions]].nodes
    membership string. Used only by remove_nodes.yml's write-back to the
    master file itself, after the node is confirmed removed live.
    """
    if tomlkit is None:
        raise AnsibleFilterError("spur_conf_remove_node requires the 'tomlkit' Python package")

    to_remove = set(node_names) if not isinstance(node_names, str) else {node_names}
    doc = tomlkit.parse(master_text)

    if "nodes" in doc:
        kept = [n for n in doc["nodes"] if n.get("names") not in to_remove]
        doc["nodes"] = kept

    for partition in doc.get("partitions", []):
        members = [m.strip() for m in partition.get("nodes", "").split(",") if m.strip()]
        remaining = [m for m in members if m not in to_remove]
        if remaining != members:
            partition["nodes"] = ",".join(remaining)

    return tomlkit.dumps(doc)


class FilterModule(object):
    def filters(self):
        return {
            "spur_conf_exclude_sections": spur_conf_exclude_sections,
            "spur_conf_for_push": spur_conf_for_push,
            "spur_conf_splice_excluded": spur_conf_splice_excluded,
            "spur_conf_node_facts": spur_conf_node_facts,
            "spur_conf_remove_node": spur_conf_remove_node,
        }
