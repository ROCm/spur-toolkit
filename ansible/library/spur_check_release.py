#!/usr/bin/python
"""Check whether installed components satisfy a requested upstream release."""

import os
import re
import subprocess

from ansible.module_utils.basic import AnsibleModule

VERSION = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
COMPONENTS = ("spur", "spurctld", "spurd", "spurstepd", "spurauthd")


def main():
    module = AnsibleModule(
        argument_spec={
            "install_dir": {"type": "path", "required": True},
            "version": {"type": "str", "required": True},
            "required_bins": {
                "type": "list",
                "elements": "str",
                "choices": COMPONENTS,
                "required": True,
            },
            "optional_bins": {
                "type": "list",
                "elements": "str",
                "choices": COMPONENTS,
                "default": [],
            },
            "require_match": {"type": "bool", "default": False},
        },
        supports_check_mode=True,
    )
    requested = module.params["version"]
    pinned = requested not in ("latest", "nightly")
    if pinned and not re.fullmatch("v?" + VERSION, requested):
        module.fail_json(
            msg="spur_version must be latest, nightly, or a release such as v0.15.0"
        )
    expected = requested[1:] if requested.startswith("v") else requested
    release = "v" + expected if pinned else requested
    problems = []
    versions = {}
    required = module.params["required_bins"]
    for name in dict.fromkeys(required + module.params["optional_bins"]):
        path = os.path.join(module.params["install_dir"], name)
        if name not in required and not os.path.lexists(path):
            continue
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            problems.append("%s is missing or not executable" % path)
            continue
        # Moving channels retain existing installations until explicitly refreshed.
        if not pinned:
            continue
        try:
            result = subprocess.run(
                [path, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            problems.append("%s --version failed: %s" % (path, error))
            continue
        output = result.stdout.strip()
        match = re.fullmatch(
            r"(?:spur|" + name + r") v?(" + VERSION + r")(?: \([^\r\n]*\))?",
            output,
        )
        versions[name] = output
        if result.returncode != 0 or not match or match.group(1) != expected:
            problems.append(
                "%s: expected %s, got %r (exit %s)"
                % (path, release, output, result.returncode)
            )
    result = dict(
        changed=False,
        release=release,
        needs_install=bool(problems),
        problems=problems,
        versions=versions,
    )
    if problems and module.params["require_match"]:
        module.fail_json(
            msg="Installed Spur does not satisfy %s: %s"
            % (release, "; ".join(problems)),
            **result,
        )
    module.exit_json(**result)


if __name__ == "__main__":
    main()
