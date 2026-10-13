#!/usr/bin/python
"""Converge agent files in one remote call, before the existing restart barrier."""

import os
from pathlib import Path
import re
import tempfile

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec={
            "environment_file": {"type": "path", "required": True},
            "controller_endpoints": {
                "type": "list",
                "elements": "str",
                "required": True,
            },
            "config_file": {"type": "path", "required": True},
            "config_content": {"type": "str", "required": True},
            "overwrite_config": {"type": "bool", "default": False},
            "state_dir": {"type": "path", "required": True},
            "unit_file": {"type": "path", "required": True},
            "unit_content": {"type": "str", "required": True},
            "mpi_enabled": {"type": "bool", "default": True},
            "mpi_dir": {"type": "path", "required": True},
            "mpi_source": {"type": "path", "default": ""},
            "mpi_source_required": {"type": "bool", "default": False},
        },
        supports_check_mode=True,
    )
    params = module.params
    changed = False
    diffs = []

    def directory(name, mode):
        nonlocal changed
        path = Path(name)
        if path.exists() and not path.is_dir():
            module.fail_json(msg="Not a directory: %s" % path, changed=changed)
        if not path.exists():
            changed = True
            if not module.check_mode:
                path.mkdir(mode=mode, parents=True)
        if path.exists():
            changed = module.set_mode_if_different(str(path), mode, changed)

    def write(name, data, mode=None, preserve=False, binary=False, follow=False):
        nonlocal changed
        path = Path(name)
        if preserve and path.exists():
            return
        before = path.read_bytes() if path.exists() else None
        if before != data or (path.is_symlink() and not follow):
            changed = True
            if module._diff:
                if binary:
                    diffs.append(
                        {
                            "before_header": str(path),
                            "after_header": str(path),
                            "dst_binary": 1,
                            "src_binary": 1,
                        }
                    )
                else:
                    diffs.append(
                        {
                            "before_header": str(path),
                            "after_header": str(path),
                            "before": (before or b"").decode("utf-8", "replace"),
                            "after": data.decode("utf-8", "replace"),
                        }
                    )
            if not module.check_mode:
                fd, temporary = tempfile.mkstemp(dir=module.tmpdir)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                    destination = os.path.realpath(path) if follow else str(path)
                    module.atomic_move(temporary, destination, keep_dest_attrs=True)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        if path.exists() and mode is not None:
            changed = module.set_mode_if_different(str(path), mode, changed)

    try:
        source = Path(params["mpi_source"]) if params["mpi_source"] else None
        if (
            params["mpi_enabled"]
            and params["mpi_source_required"]
            and source
            and not source.is_file()
            and not module.check_mode
        ):
            module.fail_json(msg="Staged MPI plugin is missing: %s" % source)
        environment = Path(params["environment_file"])
        lines = (
            environment.read_bytes().splitlines(keepends=True)
            if environment.exists()
            else []
        )
        value = (
            "SPUR_CONTROLLER_ADDR=" + ",".join(params["controller_endpoints"]) + "\n"
        ).encode()
        matches = [
            i
            for i, line in enumerate(lines)
            if re.match(b"^SPUR_CONTROLLER_ADDR=", line)
        ]
        if matches:
            lines[matches[-1]] = value
        else:
            if lines and not lines[-1].endswith(b"\n"):
                lines[-1] += b"\n"
            lines.append(value)
        write(environment, b"".join(lines), follow=True)
        directory(params["state_dir"], 0o700)
        write(
            params["config_file"],
            params["config_content"].encode(),
            0o644,
            preserve=not params["overwrite_config"],
        )
        write(params["unit_file"], params["unit_content"].encode(), 0o644)
        if params["mpi_enabled"]:
            directory(params["mpi_dir"], 0o755)
            if source and source.is_file():
                write(
                    Path(params["mpi_dir"]) / "spur_mpi_pmix.so",
                    source.read_bytes(),
                    0o755,
                    binary=True,
                )
            else:
                module.warn(
                    "spur_mpi_pmix.so not found; MPI plugin installation skipped"
                )
    except (OSError, ValueError) as error:
        module.fail_json(msg=str(error), changed=changed)
    module.exit_json(changed=changed, diff=diffs)


if __name__ == "__main__":
    main()
