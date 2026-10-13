"""Exercise consolidated operations and real playbooks using local fixtures."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "ansible"


def helper(name):
    spec = importlib.util.spec_from_file_location(
        name, ANSIBLE / "module_utils" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifacts = helper("spur_upgrade")
nodes = helper("spur_nodes")
health = helper("spur_health")


class FleetHelpers(unittest.TestCase):
    def test_workload_cleanup_ignores_pids_hidden_by_the_pid_namespace(self):
        task = yaml.safe_load(
            (ANSIBLE / "playbooks/tasks/kill_workload_processes.yml").read_text()
        )[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cgroup.procs").write_text("0\n1\n")
            (root / "find").write_text(
                "#!/bin/sh\nprintf '%s\\n' '" + str(root / "cgroup.procs") + "'\n"
            )
            (root / "find").chmod(0o755)
            result = subprocess.run(
                ["bash", "-c", task["ansible.builtin.shell"]],
                env=dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"]),
                capture_output=True,
                text=True,
                timeout=10,
                start_new_session=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("nothing to kill", result.stdout)
            self.assertNotIn("AFFECTED_JOB_IDS", result.stdout)

    def test_install_hashes_once_with_mixed_sources_roles_and_optional_binaries(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            for name in (
                "spur",
                "spurctld",
                "spurd",
                "spurstepd",
                "spurauthd",
                "libspur_mpi_pmix.so",
            ):
                (source / name).write_text(name)
            spec = {
                "source": str(source),
                "roles": ["agent"],
                "auth": False,
                "mpi": True,
            }
            targets = {"worker-%s" % n: spec for n in range(200)}
            targets["dual"] = dict(spec, roles=["controller", "agent"], auth=True)
            targets["login"] = dict(spec, roles=[], auth=True)
            older = Path(directory) / "older"
            shutil.copytree(source, older)
            (older / "spurstepd").unlink()
            (older / "spurauthd").unlink()
            (older / "spurd").write_text("older")
            targets["older"] = dict(spec, source=str(older), auth=True)
            targets["upstream"] = dict(spec, source="")
            with patch.object(artifacts, "digest", wraps=artifacts.digest) as hashed:
                result = artifacts.prepare_install(targets, Path(directory) / "cache")
            self.assertEqual(hashed.call_count, 7)
            self.assertEqual(result["bundles_built"], 4)
            self.assertEqual(
                set(result["artifacts"]["dual"]["manifest"]),
                {"spur", "spurctld", "spurd", "spurstepd", "spurauthd"},
            )
            self.assertEqual(
                set(result["artifacts"]["login"]["manifest"]), {"spur", "spurauthd"}
            )
            self.assertNotIn("spurstepd", result["artifacts"]["older"]["manifest"])
            self.assertNotIn("upstream", result["artifacts"])
            self.assertEqual(
                result["artifacts"]["worker-0"], result["artifacts"]["worker-199"]
            )
            self.assertTrue(
                result["artifacts"]["worker-0"]["mpi_source"].endswith(
                    "libspur_mpi_pmix.so"
                )
            )

    def test_snapshot_preserves_exact_names_and_queries_once_for_200_nodes(self):
        output = "\n".join("NodeName=worker-%s\n   State=IDLE" % n for n in range(200))
        result = subprocess.CompletedProcess([], 0, output, "")
        with patch.object(nodes.subprocess, "run", return_value=result) as called:
            snapshot = nodes.snapshot(
                "/fake/spur", ["worker-%s" % n for n in range(200)]
            )
        self.assertEqual(snapshot["pending"], [])
        self.assertEqual(called.call_count, 1)
        self.assertEqual(called.call_args.args[0], ["/fake/spur", "show", "node"])
        self.assertEqual(
            nodes.parse_nodes("NodeName=worker-10\n State=IDLE+DRAIN\n"),
            {"worker-10": "IDLE+DRAIN"},
        )

    def test_drain_snapshot_uses_shared_deadline_and_keeps_missing_nodes_busy(self):
        responses = [
            subprocess.CompletedProcess(
                [], 0, "NodeName=a\n State=DRAINING\nNodeName=b\n State=DOWN", ""
            ),
            subprocess.CompletedProcess(
                [], 0, "NodeName=a\n State=DRAINED\nNodeName=b\n State=DOWN", ""
            ),
        ]
        with patch.object(
            nodes.subprocess, "run", side_effect=responses
        ) as called, patch.object(nodes.time, "sleep"), patch.object(
            nodes.time, "monotonic", return_value=0
        ):
            result = nodes.snapshot("spur", ["a", "b"], ["DRAINED", "DOWN"], 5)
        self.assertEqual(result["pending"], [])
        self.assertEqual(called.call_count, 2)
        with patch.object(nodes.subprocess, "run", return_value=responses[1]):
            result = nodes.snapshot("spur", ["a", "b", "absent"], ["DRAINED", "DOWN"])
        self.assertEqual(result["pending"], ["absent"])

    def test_snapshot_retries_transient_query_failures_within_the_shared_deadline(self):
        responses = [
            subprocess.CompletedProcess([], 1, "", "no leader"),
            subprocess.CompletedProcess([], 0, "NodeName=a\n State=DRAINED+FLAG", ""),
        ]
        with patch.object(nodes.subprocess, "run", side_effect=responses), patch.object(
            nodes.time, "sleep"
        ), patch.object(nodes.time, "monotonic", return_value=0):
            result = nodes.snapshot("spur", ["a"], ["DRAINED"], 5)
        self.assertEqual(result["pending"], [])

    def test_snapshot_cli_failure_cannot_appear_as_success(self):
        with patch.object(
            nodes.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 1, "NodeName=a", "unavailable"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                nodes.snapshot("spur", ["a"])

    def test_health_keeps_disabled_checks_disabled_and_reports_all_failures(self):
        kwargs = dict(
            role="agent",
            install_dir="/missing",
            home="/missing",
            port=1,
            auth=True,
            cluster="fixture",
            key_dir="/missing",
            accounting=False,
            cgroup=False,
            require_stepd=True,
        )
        with patch.object(health, "probe", return_value=(1, "")), patch.object(
            health, "listening", return_value=False
        ):
            problems = health.health(**kwargs)
        for fragment in (
            "spurd not active",
            "port 1",
            "spurstepd missing",
            "low disk",
            "spurauthd",
            "no-mint-socket",
            "no-auth.jwks",
        ):
            self.assertIn(fragment, " ".join(problems))
        with tempfile.TemporaryDirectory() as directory, patch.object(
            health, "probe", return_value=(0, "active")
        ) as commands, patch.object(health, "listening", return_value=True):
            problems = health.health(
                **dict(kwargs, home=directory, auth=False, require_stepd=False)
            )
        self.assertEqual(problems, [])
        self.assertEqual(
            commands.call_args_list,
            [unittest.mock.call(["systemctl", "is-active", "spurd"])],
        )


class LocalPlaybooks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(
            os.environ,
            ANSIBLE_CONFIG=str(ANSIBLE / "ansible.cfg"),
            ANSIBLE_LOCAL_TEMP=str(self.root / "ansible-local"),
            ANSIBLE_NOCOLOR="1",
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
        )
        self.inventory = self.root / "inventory.yml"

    def executable(self, name, content):
        path = self.bin / name
        path.write_text("#!/usr/bin/env python3\n" + content)
        path.chmod(0o755)
        return path

    def inventory_file(self, hosts, agents=None, controllers=None, **variables):
        data = {
            "all": {
                "vars": dict(
                    ansible_connection="local",
                    ansible_python_interpreter=shutil.which("python3"),
                    ansible_become=False,
                    spur_install_dir=str(self.bin),
                    spur_home=str(self.root),
                    spur_cluster_name="fixture",
                    spur_auth_key_dir=str(self.root / "keys"),
                    spur_accounting_enabled=False,
                    spur_transport="direct",
                    spur_gather_subset=["!all", "!min", "platform"],
                    spur_controller_port=6817,
                    **variables
                ),
                "hosts": hosts,
                "children": {
                    "spur_controllers": {
                        "hosts": {h: {} for h in (controllers or ["ctl"])}
                    },
                    "spur_agents": {"hosts": {h: {} for h in (agents or ["worker"])}},
                },
            }
        }
        self.inventory.write_text(yaml.safe_dump(data))

    def run_play(self, playbook, *extra, success=True):
        result = subprocess.run(
            ["ansible-playbook", "-i", str(self.inventory), str(playbook), *extra],
            cwd=ANSIBLE,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=90,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_idle_cleanup_does_not_expand_job_reconciliation_tasks(self):
        procs = self.root / "cgroup.procs"
        procs.write_text("0\n")
        self.executable("find", "print(" + repr(str(procs)) + ")\n")
        self.inventory_file({"ctl": {}, "worker": {}})
        playbook = self.root / "cleanup.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    dict(
                        hosts="worker",
                        gather_facts=False,
                        tasks=[
                            {
                                "ansible.builtin.include_tasks": str(
                                    ANSIBLE
                                    / "playbooks/tasks/kill_workload_processes.yml"
                                )
                            }
                        ],
                    )
                ]
            )
        )
        output = self.run_play(playbook)
        self.assertNotIn("Extract job IDs", output)
        self.assertNotIn("Look up each affected job", output)
        self.assertNotIn("Force-cancel jobs", output)

    def test_auth_preflight_keeps_warnings_and_fails_missing_binary_in_check_mode(self):
        self.executable("timedatectl", "print('no')\n")
        self.executable("systemctl", "import sys\nsys.exit(1)\n")
        self.executable("getent", "import sys\nsys.exit(1)\n")
        self.inventory_file(
            {"ctl": {}, "worker": {}}, spur_cluster_admins=["fixture-user"]
        )
        tasks = yaml.safe_load((ANSIBLE / "roles/spur_auth/tasks/main.yml").read_text())
        names = [
            "Check clock, RBAC principals, and auth binary in one remote call",
            "Warn if no time synchronization is detected",
            "Warn about RBAC principals that don't resolve on this host",
            "Fail early if spurauthd is missing",
        ]
        chosen = [t for t in tasks if t["name"] in names]
        self.assertEqual(len(chosen), 4)
        playbook = self.root / "auth-preflight.yml"
        playbook.write_text(
            yaml.safe_dump([dict(hosts="worker", gather_facts=False, tasks=chosen)])
        )
        output = self.run_play(playbook, "--check", success=False)
        self.assertIn("no time sync", output)
        self.assertIn("fixture-user", output)
        self.assertIn("spurauthd is missing", output)
        self.executable("spurauthd", "pass\n")
        self.run_play(playbook, "--check")

    def test_real_healthcheck_and_check_mode_fail_on_missing_stepd(self):
        self.executable("systemctl", "print('active')\n")
        self.executable("spur", "print('healthy')\n")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        self.addCleanup(listener.close)
        self.inventory_file(
            {"ctl": {}, "worker": {}},
            spur_agent_port=listener.getsockname()[1],
            spur_cgroup_enabled=False,
        )
        output = self.run_play(ANSIBLE / "playbooks/healthcheck.yml")
        self.assertIn("worker: OK", output)
        self.assertNotIn("Collect PostgreSQL health", output)
        output = self.run_play(
            ANSIBLE / "playbooks/healthcheck.yml",
            "--check",
            "-e",
            "spur_require_stepd=true",
            success=False,
        )
        self.assertIn("spurstepd missing", output)
        self.assertNotIn("changed=1", output)

    def test_real_healthcheck_skips_only_unreachable_agents(self):
        self.executable("systemctl", "print('active')\n")
        self.executable("spur", "print('healthy')\n")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        down = {
            "ansible_connection": "ssh",
            "ansible_host": "127.0.0.1",
            "ansible_port": port,
            "ansible_ssh_common_args": "-o ConnectionAttempts=1 -o ConnectTimeout=1",
        }
        self.inventory_file(
            {"ctl": {}, "worker": down}, spur_agent_port=1, spur_cgroup_enabled=False
        )
        self.run_play(ANSIBLE / "playbooks/healthcheck.yml", success=False)
        output = self.run_play(
            ANSIBLE / "playbooks/healthcheck.yml",
            "-e",
            "spur_ignore_unreachable_agents=true",
        )
        self.assertIn("SKIPPED (unreachable", output)

    def test_real_k8s_playbooks_batch_registration_and_reuse_ready_snapshot(self):
        log = self.root / "calls"
        self.executable(
            "spur",
            "import sys\nfrom pathlib import Path\n"
            + "with Path(%r).open('a') as f: f.write(' '.join(sys.argv[1:])+'\\n')\n"
            % str(log)
            + "if sys.argv[1:3] == ['show', 'node']: print('NodeName=worker\\n State=IDLE\\nNodeName=second\\n State=IDLE')\n"
            + "elif sys.argv[1:3] == ['k8s', 'status']: print('phase: ready\\n  worker: active')\n"
            + "else: print('up requested; add-nodes requested')\n",
        )
        self.inventory_file(
            {"ctl": {}, "worker": {}, "second": {}},
            agents=["worker", "second"],
            spur_k8s_enabled=True,
        )
        for name in ("k8s_up.yml", "k8s_add_nodes.yml"):
            log.write_text("")
            output = self.run_play(
                ANSIBLE / "playbooks" / name, "-e", "k8s_new_nodes=worker,second"
            )
            calls = log.read_text().splitlines()
            self.assertEqual(sum(c.startswith("k8s status") for c in calls), 1)
            self.assertEqual(calls.count("show node"), int(name == "k8s_add_nodes.yml"))
            self.assertIn("phase: ready", output)
        self.executable("spur", "import sys\nprint('phase: ready')\nsys.exit(1)\n")
        self.run_play(
            ANSIBLE / "playbooks/k8s_up.yml",
            "-e",
            "spur_k8s_enabled=true",
            success=False,
        )

    def test_real_add_nodes_skips_existing_and_disabled_roles(self):
        marker = self.root / "registered"
        self.executable(
            "spur",
            "from pathlib import Path\nimport sys\n"
            + "print('NodeName=existing\\n State=IDLE')\n"
            + "if Path(%r).exists(): print('NodeName=added\\n State=IDLE')\n"
            % str(marker),
        )
        source = self.root / "source"
        source.mkdir()
        for name in ("spur", "spurd", "spurstepd"):
            (source / name).write_text(name)
        existing, added = self.root / "existing", self.root / "added"
        existing.mkdir()
        added.mkdir()
        self.inventory_file(
            {
                "ctl": {},
                "worker": {
                    "spur_node_name": "existing",
                    "spur_install_dir": str(existing),
                },
                "second": {"spur_node_name": "added", "spur_install_dir": str(added)},
            },
            agents=["worker", "second"],
            spur_binary_src=str(source),
        )
        roles = self.root / "roles"
        install = yaml.safe_load(
            (ANSIBLE / "roles/spur_install/tasks/main.yml").read_text()
        )
        names = [
            "Determine which Spur components this host needs",
            "Prepare local install artifacts for this batch",
            "Copy the binaries tarball to the remote host",
            "Extract changed binaries, verify components, and maintain CLI symlinks",
        ]
        role_tasks = {
            "spur_install": [t for t in install if t["name"] in names],
            "spur_agent": [
                {
                    "name": "Register fixture agent",
                    "ansible.builtin.copy": {
                        "dest": str(marker),
                        "content": "registered",
                    },
                }
            ],
            "spur_controller": [
                {
                    "name": "Controller restart remains disabled",
                    "ansible.builtin.assert": {
                        "that": "spur_controller_skip_restart | bool"
                    },
                }
            ],
            "spur_auth": [
                {
                    "name": "Disabled auth role must not run",
                    "ansible.builtin.fail": {"msg": "disabled auth entered"},
                }
            ],
            "spur_wireguard": [
                {
                    "name": "Direct transport must not enter mesh",
                    "ansible.builtin.fail": {"msg": "disabled mesh entered"},
                }
            ],
        }
        for role, tasks in role_tasks.items():
            path = roles / role / "tasks/main.yml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(tasks))
        self.env["ANSIBLE_ROLES_PATH"] = str(roles)
        playbook = self.root / "add.yml"
        playbook.write_text((ANSIBLE / "playbooks/add_nodes.yml").read_text())
        output = self.run_play(playbook, "-e", "new_nodes=worker,second")
        self.assertTrue((added / "spurd").is_file())
        self.assertEqual(list(existing.iterdir()), [])
        self.assertNotIn("Disabled auth role must not run", output)
        self.assertNotIn("Direct transport must not enter mesh", output)
        self.assertIn("already registered", output)
        marker_time = marker.stat().st_mtime_ns
        self.run_play(playbook, "-e", "new_nodes=worker,second")
        self.assertEqual(marker.stat().st_mtime_ns, marker_time)

    def test_verification_accepts_reextracted_identical_binary_but_rejects_stale_bytes(
        self,
    ):
        self.executable("systemctl", "print(%r)\n" % str(os.getpid()))
        shutil.copyfile("/proc/self/exe", self.bin / "spurd")
        (self.bin / "spurd").chmod(0o755)
        self.inventory_file({"ctl": {}, "worker": {}})
        playbook = self.root / "verify-identical.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    dict(
                        hosts="worker",
                        gather_facts=False,
                        tasks=[
                            {
                                "ansible.builtin.include_tasks": str(
                                    ANSIBLE
                                    / "playbooks/tasks/collect_verification_facts.yml"
                                ),
                                "vars": {
                                    "spur_verify_daemon_name": "spurd",
                                    "spur_verify_daemon_port": 1,
                                    "spur_verify_role_label": "agent",
                                },
                            },
                            {
                                "ansible.builtin.assert": {
                                    "that": "spur_verify_row.binary_current == (expected_current | bool)"
                                }
                            },
                        ],
                    )
                ]
            )
        )
        self.run_play(playbook, "-e", "expected_current=true")
        self.executable("spurd", "print('stale build')\n")
        self.run_play(playbook, "-e", "expected_current=false")

    def test_verification_queries_once_and_clears_stale_registration_on_failure(self):
        calls, fail = self.root / "queries", self.root / "fail-query"
        self.executable("systemctl", "print('0')\n")
        self.executable("spurd", "print('fixture version')\n")
        self.executable(
            "spur",
            "import sys\nfrom pathlib import Path\n"
            + "with Path(%r).open('a') as f: f.write('query\\n')\n" % str(calls)
            + "if Path(%r).exists(): sys.exit('query failed')\n" % str(fail)
            + "print('NodeName=first\\n State=IDLE\\nNodeName=second\\n State=ALLOCATED')\n",
        )
        self.inventory_file(
            {
                "ctl": {},
                "worker": {"spur_node_name": "first"},
                "second": {"spur_node_name": "second"},
            },
            agents=["worker", "second"],
        )
        collect = {
            "name": "Collect verification",
            "ansible.builtin.include_tasks": str(
                ANSIBLE / "playbooks/tasks/collect_verification_facts.yml"
            ),
            "vars": {
                "spur_verify_daemon_name": "spurd",
                "spur_verify_daemon_port": 1,
                "spur_verify_role_label": "agent",
                "spur_verify_check_registration": True,
            },
        }
        first = {
            "hosts": "spur_agents",
            "gather_facts": False,
            "tasks": [
                collect,
                {
                    "name": "Validate each row",
                    "ansible.builtin.assert": {
                        "that": "spur_verify_row.registered_state == ('IDLE' if inventory_hostname == 'worker' else 'ALLOCATED')"
                    },
                },
            ],
        }
        fail_query = {
            "hosts": "ctl",
            "gather_facts": False,
            "tasks": [
                {
                    "name": "Make fixture query fail",
                    "ansible.builtin.copy": {"dest": str(fail), "content": "fail"},
                }
            ],
        }
        second = {
            "hosts": "spur_agents",
            "gather_facts": False,
            "tasks": [
                collect,
                {
                    "name": "Do not reuse stale state",
                    "ansible.builtin.assert": {
                        "that": "spur_verify_row.registered_state == ''"
                    },
                },
            ],
        }
        playbook = self.root / "verification.yml"
        playbook.write_text(yaml.safe_dump([first, fail_query, second]))
        self.run_play(playbook)
        self.assertEqual(calls.read_text().splitlines(), ["query", "query"])

    def test_real_remove_guard_preserves_refuse_skip_and_force(self):
        self.executable(
            "spur",
            "import sys\n"
            + "if sys.argv[1:3] == ['show', 'node']: print('NodeName=idle\\n State=DRAINED\\nNodeName=busy\\n State=DRAINING')\n",
        )
        self.inventory_file({"ctl": {}, "worker": {}})
        # Execute the production decision phase; no daemon-stop or removal phase is run.
        play = yaml.safe_load((ANSIBLE / "playbooks/remove_nodes.yml").read_text())[0]
        play["tasks"].append(
            {
                "name": "Assert removal decision",
                "ansible.builtin.assert": {
                    "that": "rn_actual_remove_list | sort == expected_removals | sort"
                },
            }
        )
        playbook = self.root / "remove-guard.yml"
        playbook.write_text(yaml.safe_dump([play]))
        extra = ["-e", "nodes_to_remove=idle,busy", "-e", "spur_drain_wait_secs=0"]
        output = self.run_play(playbook, *extra, success=False)
        self.assertIn("These nodes still have a running job", output)
        self.run_play(
            playbook,
            *extra,
            "-e",
            json.dumps({"spur_skip_busy_agents": True, "expected_removals": ["idle"]})
        )
        self.run_play(
            playbook,
            *extra,
            "-e",
            json.dumps(
                {
                    "spur_force_remove_busy_nodes": True,
                    "expected_removals": ["idle", "busy"],
                }
            )
        )

    def test_install_tasks_use_per_host_sources_and_do_not_recopy_on_rerun(self):
        source = self.root / "source"
        source.mkdir()
        for name in ("spur", "spurd", "spurstepd", "spurctld", "libspur_mpi_pmix.so"):
            (source / name).write_text("first " + name)
        other = self.root / "other"
        shutil.copytree(source, other)
        (other / "spurd").write_text("second spurd")
        hosts = {}
        for name, src in (("ctl", source), ("worker", source), ("second", other)):
            install = self.root / name
            install.mkdir()
            plugin = install / "plugins"
            plugin.mkdir()
            hosts[name] = dict(
                spur_binary_src=str(src),
                spur_install_dir=str(install),
                spur_mpi_plugin_dir=str(plugin),
            )
        self.inventory_file(hosts, agents=["worker", "second"])
        tasks = yaml.safe_load(
            (ANSIBLE / "roles/spur_install/tasks/main.yml").read_text()
        )
        names = [
            "Determine which Spur components this host needs",
            "Prepare local install artifacts for this batch",
            "Copy the binaries tarball to the remote host",
            "Extract changed binaries, verify components, and maintain CLI symlinks",
        ]
        chosen = [t for t in tasks if t["name"] in names]
        self.assertEqual(len(chosen), len(names))
        mpi = yaml.safe_load((ANSIBLE / "roles/spur_agent/tasks/main.yml").read_text())
        mpi_names = [
            "Reuse the MPI source discovered during installation",
            "Install MPI plugin from spur_binary_src",
        ]
        mpi_tasks = [t for t in mpi if t["name"] in mpi_names]
        playbook = self.root / "install.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    dict(hosts="all", gather_facts=False, tasks=chosen),
                    dict(hosts="spur_agents", gather_facts=False, tasks=mpi_tasks),
                ]
            )
        )
        self.run_play(playbook)
        self.assertEqual((self.root / "worker/spurd").read_text(), "first spurd")
        self.assertEqual((self.root / "second/spurd").read_text(), "second spurd")
        self.assertFalse((self.root / "worker/spurctld").exists())
        self.assertTrue((self.root / "ctl/spurctld").exists())
        self.assertEqual(
            (self.root / "worker/plugins/spur_mpi_pmix.so").read_text(),
            "first libspur_mpi_pmix.so",
        )
        output = self.run_play(playbook)
        self.assertRegex(output, r"worker\s+: ok=\d+\s+changed=0")


if __name__ == "__main__":
    unittest.main()
