#!/usr/bin/python
"""Make the install directory accessible and maintain its system PATH entry."""

import os
from pathlib import Path
import re
import shlex
import tempfile

from ansible.module_utils.basic import AnsibleModule

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PATH_ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?PATH[ \t]*=[ \t]*(.*)$")


def configure_path(content, install_dir):
    lines = content.splitlines(keepends=True)
    matches = [
        (index, PATH_ASSIGNMENT.match(line.rstrip("\r\n")))
        for index, line in enumerate(lines)
    ]
    matches = [(index, match) for index, match in matches if match]
    before = ""
    value = DEFAULT_PATH
    if matches:
        index, match = matches[-1]
        before = lines[index]
        # Parse literals and comments without evaluating shell substitutions.
        words = shlex.split(match.group(1), comments=True)
        if len(words) > 1:
            raise ValueError("PATH must contain one quoted or unquoted value")
        value = (words[0] if words else "") or DEFAULT_PATH
        if install_dir in value.split(":") and words and words[0]:
            return content, before, before
    if install_dir not in value.split(":"):
        value = install_dir + ":" + value
    if any(character in value for character in '"\r\n\x00'):
        raise ValueError("PATH cannot contain double quotes or line breaks")
    after = 'PATH="' + value + '"\n'
    if matches:
        lines[index] = after
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(after)
    return "".join(lines), before, after


def main():
    module = AnsibleModule(
        argument_spec={
            "install_dir": {"type": "path", "required": True},
            "environment_file": {"type": "path", "required": True},
        },
        supports_check_mode=True,
    )
    changed = False
    try:
        install_dir = module.params["install_dir"]
        if not os.path.isabs(install_dir) or any(
            character in install_dir for character in ':"\r\n\x00'
        ):
            raise ValueError(
                "install_dir must be an absolute path without colons, double quotes or line breaks"
            )
        destination = Path(install_dir)
        if destination.exists() and not destination.is_dir():
            raise ValueError("Not a directory: %s" % destination)
        environment = Path(module.params["environment_file"])
        existed = environment.exists()
        content = (
            environment.read_bytes().decode("utf-8", "surrogateescape")
            if existed
            else ""
        )
        updated, before, after = configure_path(content, install_dir)
        diff = {"before": before, "after": after} if module._diff else {}

        if not destination.exists():
            changed = True
            if not module.check_mode:
                destination.mkdir(mode=0o755, parents=True)
        if destination.exists():
            changed = module.set_mode_if_different(install_dir, 0o755, changed)
        if updated != content:
            changed = True
            if not module.check_mode:
                fd, temporary = tempfile.mkstemp(dir=module.tmpdir)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(updated.encode("utf-8", "surrogateescape"))
                    module.atomic_move(temporary, os.path.realpath(environment))
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                if not existed:
                    module.set_mode_if_different(str(environment), 0o644, changed)
    except (OSError, ValueError) as error:
        module.fail_json(msg=str(error), changed=changed)
    module.exit_json(changed=changed, diff=diff)


if __name__ == "__main__":
    main()
