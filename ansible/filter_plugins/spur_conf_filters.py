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


def spur_conf_for_push(master_text, exclude_sections=None):
    """Return the spur.conf content that should be pushed to a node: the
    master file with the ansible-only [ansible] table and any ansible_*
    keys on [[nodes]] entries stripped, and any section named in
    exclude_sections removed entirely (the caller splices back whatever
    already exists on the node for those, this filter only computes what
    ansible itself would contribute).
    """
    if tomlkit is None:
        raise AnsibleFilterError("spur_conf_for_push requires the 'tomlkit' Python package")

    exclude_sections = set(exclude_sections or [])
    doc = tomlkit.parse(master_text)

    doc.pop("ansible", None)

    for node in doc.get("nodes", []):
        for key in [k for k in node.keys() if k.startswith("ansible_")]:
            node.pop(key)

    for section in exclude_sections:
        doc.pop(section, None)

    return tomlkit.dumps(doc)


def spur_conf_splice_excluded(desired_text, existing_text, exclude_sections=None):
    """Given the freshly-computed desired push content and whatever's already
    on the node (may be empty on first push), copy each excluded section's
    CURRENT on-node content into the desired output verbatim, so excluded
    sections survive untouched across repeated pushes.
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


class FilterModule(object):
    def filters(self):
        return {
            "spur_conf_exclude_sections": spur_conf_exclude_sections,
            "spur_conf_for_push": spur_conf_for_push,
            "spur_conf_splice_excluded": spur_conf_splice_excluded,
        }
