#!/usr/bin/python
"""Check whether installed components satisfy a requested upstream release."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

from ansible.module_utils.basic import AnsibleModule

VERSION = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
COMPONENTS = ("spur", "spurctld", "spurd", "spurstepd", "spurauthd")
SHA256 = re.compile(r"[0-9a-f]{64}")


def digest(stream):
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def download(url, destination):
    result = subprocess.run(
        ["curl", "-fsSL", "--output", str(destination), url],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise ValueError("Cannot verify legacy release: %s" % result.stderr.strip())


def release_manifest(install_dir, release, cache=True):
    """Cache hashes from a checksum-verified archive, never from installed files."""
    receipt = Path(install_dir) / (".spur-release-%s.json" % release)
    try:
        if receipt.is_symlink() or receipt.stat().st_size > 16384:
            raise ValueError("Invalid release receipt")
        manifest = json.loads(receipt.read_text())
        if (
            manifest.get("schema") == 1
            and manifest.get("release") == release
            and SHA256.fullmatch(manifest.get("archive_sha256", ""))
            and isinstance(manifest.get("binaries"), dict)
            and manifest["binaries"]
            and manifest["binaries"].keys() <= set(COMPONENTS)
            and all(
                isinstance(value, str) and SHA256.fullmatch(value)
                for value in manifest["binaries"].values()
            )
        ):
            return manifest["binaries"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass

    filename = "spur-%s-linux-amd64.tar.gz" % release
    url = "https://github.com/ROCm/spur/releases/download/%s/%s" % (release, filename)
    with tempfile.TemporaryDirectory(prefix="spur-release-") as temporary:
        archive = Path(temporary) / filename
        checksum = Path(temporary) / (filename + ".sha256")
        download(url + ".sha256", checksum)
        fields = checksum.read_text().strip().split()
        if (
            len(fields) != 2
            or not SHA256.fullmatch(fields[0])
            or fields[1].lstrip("*") != filename
        ):
            raise ValueError("Invalid release checksum for %s" % release)
        download(url, archive)
        with archive.open("rb") as stream:
            if digest(stream) != fields[0]:
                raise ValueError("Release archive checksum mismatch for %s" % release)
        binaries = {}
        prefix = "spur-%s-linux-amd64/bin/" % release
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle:
                name = member.name[len(prefix) :]
                if not member.name.startswith(prefix) or name not in COMPONENTS:
                    continue
                if not member.isfile() or name in binaries:
                    raise ValueError("Invalid release archive entry: %s" % member.name)
                with bundle.extractfile(member) as stream:
                    binaries[name] = digest(stream)
        if "spur" not in binaries:
            raise ValueError("Release archive has no Spur CLI")
        if cache:
            manifest = dict(
                schema=1, release=release, archive_sha256=fields[0], binaries=binaries
            )
            fd, temporary_receipt = tempfile.mkstemp(
                dir=install_dir, prefix=".release-"
            )
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(manifest, stream)
                os.replace(temporary_receipt, receipt)
            finally:
                if os.path.exists(temporary_receipt):
                    os.unlink(temporary_receipt)
        return binaries


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
    legacy_hashes = None
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
        # These helpers acquired --version in 0.15.0; older releases need byte identity.
        if name in ("spurstepd", "spurauthd") and tuple(
            map(int, expected.split("-")[0].split("+")[0].split("."))
        ) < (0, 15, 0):
            try:
                if legacy_hashes is None:
                    legacy_hashes = release_manifest(
                        module.params["install_dir"],
                        release,
                        cache=not module.check_mode,
                    )
                with open(path, "rb") as stream:
                    actual = digest(stream)
                versions[name] = "sha256:%s" % actual
                if actual != legacy_hashes.get(name):
                    problems.append(
                        "%s does not match the %s release artifact" % (path, release)
                    )
            except (
                OSError,
                ValueError,
                tarfile.TarError,
                subprocess.TimeoutExpired,
            ) as error:
                module.fail_json(msg="Cannot verify %s: %s" % (release, error))
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
