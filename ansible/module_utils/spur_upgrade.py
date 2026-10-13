"""Artifact preparation and activation for an already provisioned Spur host."""

from contextlib import ExitStack
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tarfile
import tempfile
import time


class ConfigurationRequired(ValueError):
    """An older installation needs the existing provisioning roles."""


class ActivationFailed(RuntimeError):
    """Activation failed after installed files or service state changed."""


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


class ArtifactBuilder:
    """Share source hashing and archive creation across a fleet's specifications."""

    def __init__(self, cache_dir):
        self.cache = Path(cache_dir).expanduser()
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.hashes = {}
        self.bundles = {}

    def bundle(self, files):
        for path in files.values():
            if not path.is_file():
                raise ValueError("Missing Spur artifact: %s" % path)
            if path not in self.hashes:
                self.hashes[path] = digest(path)
        manifest = {name: self.hashes[path] for name, path in files.items()}
        key = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        if key not in self.bundles:
            archive = self.cache / (key + ".tar.gz")
            # Rebuild once per run so damaged cache files cannot mask source bytes.
            fd, temporary = tempfile.mkstemp(dir=self.cache, prefix=".build-")
            try:
                with os.fdopen(fd, "wb") as raw:
                    with gzip.GzipFile(
                        fileobj=raw, mode="wb", mtime=0, filename="", compresslevel=1
                    ) as zipped:
                        with tarfile.open(fileobj=zipped, mode="w") as tar:
                            for name, path in sorted(files.items()):
                                data = path.read_bytes()
                                if hashlib.sha256(data).hexdigest() != manifest[name]:
                                    raise ValueError(
                                        "Build changed during packaging: %s" % path
                                    )
                                info = tarfile.TarInfo(name)
                                info.size, info.mode = len(data), 0o755
                                tar.addfile(info, io.BytesIO(data))
                os.replace(temporary, archive)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.bundles[key] = {"archive": str(archive), "manifest": manifest}
        return self.bundles[key]

    def result(self, artifacts):
        return {
            "artifacts": artifacts,
            "source_files_hashed": len(self.hashes),
            "bundles_built": len(self.bundles),
        }


def mpi_source(source):
    return next(
        (
            p
            for p in (
                source / "spur_mpi_pmix.so",
                source / "libspur_mpi_pmix.so",
                source / "lib/spur/spur_mpi_pmix.so",
                source.parent / "lib/spur/spur_mpi_pmix.so",
            )
            if p.is_file()
        ),
        None,
    )


def prepare(targets, cache_dir):
    """Prepare separate activation bundles for each daemon role."""
    builder = ArtifactBuilder(cache_dir)
    artifacts, prepared_specs = {}, {}
    for host, spec in targets.items():
        if not spec["in_place"] or spec["overwrite_conf"] or not spec["source"]:
            raise ValueError(
                "Binary-only upgrades require in-place mode, a local build, and preserved configuration"
            )
        source = Path(spec["source"]).resolve()
        artifacts[host] = {}
        for role in spec["roles"]:
            spec_key = (source, role, spec["auth"], spec["mpi"])
            if spec_key not in prepared_specs:
                names = (
                    ["spur", "spurctld"] if role == "controller" else ["spur", "spurd"]
                )
                if role == "agent" and (source / "spurstepd").is_file():
                    names.append("spurstepd")
                if spec["auth"] and (source / "spurauthd").is_file():
                    names.append("spurauthd")
                files = {name: source / name for name in names}
                if role == "agent" and spec["mpi"]:
                    plugin = mpi_source(source)
                    if plugin:
                        files["spur_mpi_pmix.so"] = plugin
                prepared_specs[spec_key] = builder.bundle(files)
            artifacts[host][role] = prepared_specs[spec_key]
    return builder.result(artifacts)


def prepare_install(targets, cache_dir):
    """Prepare role unions for provisioning, retaining optional older binaries."""
    builder = ArtifactBuilder(cache_dir)
    artifacts, prepared_specs = {}, {}
    for host, spec in targets.items():
        if not spec["source"]:
            continue
        source = Path(spec["source"]).absolute()
        roles = tuple(sorted(spec["roles"]))
        key = (source, roles, spec["auth"], spec["mpi"])
        if key not in prepared_specs:
            names = ["spur"]
            if "controller" in roles:
                names.append("spurctld")
            if "agent" in roles:
                names.append("spurd")
                if (source / "spurstepd").is_file():
                    names.append("spurstepd")
            if spec["auth"] and (source / "spurauthd").is_file():
                names.append("spurauthd")
            plugin = mpi_source(source) if "agent" in roles and spec["mpi"] else None
            files = {name: source / name for name in names}
            if plugin:
                files["spur_mpi_pmix.so"] = plugin
            prepared_specs[key] = dict(
                builder.bundle(files),
                mpi_source=str(plugin) if plugin else "",
            )
        artifacts[host] = prepared_specs[key]
    return builder.result(artifacts)


def command(argv, timeout=30, environment=None):
    result = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=environment,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "Command failed: %s (exit %s): %s"
            % (
                argv[0],
                result.returncode,
                (result.stderr or result.stdout).strip()[-2000:],
            )
        )
    return result.stdout.strip()


def service(name):
    output = command(
        [
            "systemctl",
            "show",
            name,
            "--property=MainPID,KillMode,Environment,ActiveState,SubState,Result,ExecMainStatus",
        ]
    )
    properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    pid = int(properties.get("MainPID", "0"))
    if properties.get("ActiveState") != "active" or pid <= 0:
        raise RuntimeError(
            "%s is unhealthy before binary activation (state=%s/%s, MainPID=%s, result=%s, exit=%s). "
            "Repair the service before retrying; an unhealthy service does not require full provisioning."
            % (
                name,
                properties.get("ActiveState", "unknown"),
                properties.get("SubState", "unknown"),
                pid,
                properties.get("Result", "unknown"),
                properties.get("ExecMainStatus", "unknown"),
            )
        )
    properties["pid"] = pid
    return properties


def process_args(pid):
    return Path("/proc/%s/cmdline" % pid).read_bytes().decode().rstrip("\0").split("\0")


def option(args, name):
    for index, arg in enumerate(args):
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
        if arg == name and index + 1 < len(args):
            return args[index + 1]
    return None


def running_digest(state, expected_path):
    executable = Path("/proc/%s/exe" % state["pid"])
    actual = os.readlink(executable)
    if actual.endswith(" (deleted)"):
        actual = actual[:-10]
    if os.path.realpath(actual) != os.path.realpath(expected_path):
        raise ValueError("Running service executable does not match spur_install_dir")
    return digest(executable)


def wait_ready(
    name,
    expected_hash,
    expected_path,
    port,
    timeout,
    health_command=None,
    auth_socket=None,
    health_environment=None,
):
    deadline = time.monotonic() + timeout
    while True:
        try:
            state = service(name)
            if running_digest(state, expected_path) != expected_hash:
                raise ValueError("Running executable is stale")
            if port:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
            if auth_socket:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(1)
                    connection.connect(auth_socket)
            if health_command:
                command(
                    health_command,
                    timeout=max(1, min(5, deadline - time.monotonic())),
                    environment=health_environment,
                )
            return state
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "%s did not become healthy on the target binary: %s" % (name, error)
                )
            time.sleep(0.2)


def activate(
    archive,
    manifest,
    install_dir,
    home,
    role,
    port,
    timeout,
    plugin_dir,
    auth,
    cluster,
    check=False,
):
    """Stage and verify the entire bundle before replacing any executable."""
    install = Path(install_dir)
    daemon = "spurctld" if role == "controller" else "spurd"
    required = {"spur", daemon} | ({"spurstepd"} if role == "agent" else set())
    if auth:
        required.add("spurauthd")
    allowed = required | ({"spur_mpi_pmix.so"} if role == "agent" else set())
    if not manifest.keys() <= allowed:
        raise ValueError("Unexpected or missing binaries in upgrade manifest")
    if not required <= manifest.keys():
        raise ConfigurationRequired(
            "Older build requires the existing installation roles"
        )
    if not (Path(home) / "etc/spur.conf").is_file():
        raise ConfigurationRequired("Existing configuration must be provisioned")
    try:
        state = service(daemon)
    except ValueError as error:
        raise ConfigurationRequired(str(error)) from error
    current_hash = running_digest(state, install / daemon)
    node_name = ""
    if role == "agent":
        if state.get("KillMode") != "process":
            raise ConfigurationRequired(
                "In-place upgrade requires the existing spurd unit to use KillMode=process"
            )
        environment = shlex.split(state.get("Environment", ""))
        if not any(
            entry.startswith("SPUR_STEPD_STATE_DIR=") and entry.split("=", 1)[1]
            for entry in environment
        ):
            raise ConfigurationRequired(
                "Existing spurd unit must define SPUR_STEPD_STATE_DIR"
            )
        node_name = option(process_args(state["pid"]), "--hostname")
        if not node_name:
            raise ConfigurationRequired(
                "Cannot determine the running agent's --hostname"
            )
    try:
        auth_state = service("spurauthd") if auth else None
    except ValueError as error:
        raise ConfigurationRequired(str(error)) from error
    auth_hash = running_digest(auth_state, install / "spurauthd") if auth else None
    destinations = {
        name: (Path(plugin_dir) if name == "spur_mpi_pmix.so" else install) / name
        for name in manifest
    }
    for name, dest in destinations.items():
        if not dest.is_file() or (
            name != "spur_mpi_pmix.so" and not os.access(dest, os.X_OK)
        ):
            raise ConfigurationRequired(
                "Binary-only upgrade requires an existing executable: %s" % dest
            )
        if dest.is_symlink():
            raise ConfigurationRequired(
                "Binary-only upgrade does not replace symlinked executables: %s" % dest
            )
    # Check mode compares the prepared manifest without requiring a remote archive.
    # Return before extraction so even temporary executable writes are avoided.
    if check:
        return {
            "changed": bool(
                any(
                    digest(dest) != manifest[name]
                    for name, dest in destinations.items()
                )
                or current_hash != manifest[daemon]
                or (auth and auth_hash != manifest["spurauthd"])
            )
        }
    staged, replacements, restarted = {}, [], []
    mutated = False
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) != len(manifest) or {m.name for m in members} != set(
                manifest
            ):
                raise ValueError("Archive does not match upgrade manifest")
            for member in members:
                if not member.isfile():
                    raise ValueError(
                        "Only regular files are allowed in upgrade archives"
                    )
                dest = destinations[member.name]
                checksum = hashlib.sha256()
                with ExitStack() as stack:
                    source = stack.enter_context(tar.extractfile(member))
                    out = None
                    if digest(dest) != manifest[member.name]:
                        fd, temporary = tempfile.mkstemp(
                            dir=dest.parent, prefix=".spur-upgrade-"
                        )
                        staged[member.name] = temporary
                        out = stack.enter_context(os.fdopen(fd, "wb"))
                        replacements.append(member.name)
                    # Validate every archive member, but write only changed files.
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        checksum.update(chunk)
                        if out is not None:
                            out.write(chunk)
                    if out is not None:
                        os.fchmod(out.fileno(), 0o755)
                if checksum.hexdigest() != manifest[member.name]:
                    raise ValueError("Archive checksum mismatch: %s" % member.name)
        # Catch an incompatible executable before changing the installed service.
        version_output = command([staged.get(daemon, str(destinations[daemon])), "-V"])
        if not version_output:
            raise ValueError("Target daemon did not report its version")
        version = version_output.splitlines()[0]
        for name in replacements:
            os.replace(staged[name], destinations[name])
            mutated = True
        if auth and auth_hash != manifest["spurauthd"]:
            mutated = True
            command(["systemctl", "restart", "spurauthd"], timeout=timeout)
            restarted.append("spurauthd")
            wait_ready(
                "spurauthd",
                manifest["spurauthd"],
                install / "spurauthd",
                None,
                timeout,
                auth_socket="/run/spur/%s/auth.sock" % cluster,
            )
        if current_hash != manifest[daemon] or "spur_mpi_pmix.so" in replacements:
            mutated = True
            command(["systemctl", "restart", daemon], timeout=timeout)
            restarted.append(daemon)
        health = [str(install / "spur"), "nodes"] if role == "controller" else None
        health_environment = None
        if health:
            # Match the preserved daemon config, not a leftover CLI auth configuration.
            health_environment = dict(os.environ)
            health_environment.pop("SPUR_AUTH_PLUGIN", None)
            health_environment.pop("SPUR_CLUSTER_NAME", None)
            health_environment["SPUR_CONF"] = str(Path(home) / "etc/spur.conf")
            health_environment["SPUR_CONTROLLER_ADDR"] = "http://127.0.0.1:%s" % port
        state = wait_ready(
            daemon,
            manifest[daemon],
            install / daemon,
            port,
            timeout,
            health_command=health,
            health_environment=health_environment,
        )
        return {
            "changed": bool(replacements or restarted),
            "replaced": replacements,
            "restarted": restarted,
            "pid": state["pid"],
            "version": version,
            "node_name": node_name,
            "listening": True,
            "binary_current": True,
        }
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        if mutated:
            raise ActivationFailed(str(error)) from error
        raise
    finally:
        for temporary in staged.values():
            if os.path.exists(temporary):
                os.unlink(temporary)
