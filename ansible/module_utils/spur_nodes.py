"""Read a fleet snapshot with one CLI query per polling attempt."""

import re
import subprocess
import time


def parse_nodes(output):
    nodes = {}
    current = None
    for line in output.splitlines():
        match = re.match(r"^NodeName=(\S+)", line)
        if match:
            current = match.group(1)
            nodes[current] = ""
        if current:
            state = re.search(r"\bState=([A-Z+]+)", line)
            if state:
                nodes[current] = state.group(1)
    return nodes


def snapshot(binary, expected=(), states=(), timeout=0):
    deadline = time.monotonic() + timeout
    while True:
        error = None
        try:
            result = subprocess.run(
                [binary, "show", "node"],
                capture_output=True,
                text=True,
                timeout=max(1, min(30, deadline - time.monotonic())) if timeout else 30,
                check=False,
            )
            if result.returncode:
                error = RuntimeError("spur show node failed: " + result.stderr.strip())
        except (OSError, subprocess.TimeoutExpired) as failure:
            error = failure
        if error:
            if time.monotonic() >= deadline:
                raise error
            time.sleep(min(1, max(0, deadline - time.monotonic())))
            continue
        nodes = parse_nodes(result.stdout)
        pending = [
            name
            for name in expected
            if name not in nodes
            or (states and nodes[name].split("+", 1)[0] not in states)
        ]
        if not pending or time.monotonic() >= deadline:
            return {"nodes": nodes, "pending": pending}
        time.sleep(min(1, max(0, deadline - time.monotonic())))
