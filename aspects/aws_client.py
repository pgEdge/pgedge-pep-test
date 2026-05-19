"""
AWS Instance Client — drop-in replacement for docker.DockerClient.

When AWS_MODE=true the conftest.py monkey-patches docker.from_env() to return
an AWSInstanceClient so every test file's `client.containers.get(name)` call
transparently returns an SSHExecutor instead of a Docker container.

Instance configuration is loaded from configuration/aws_instances.json.
"""
import json
import os
from pathlib import Path

from aspects.ssh_executor import SSHExecutor

# Import docker.errors so AWSInstanceClient raises the same exception types
# that test files already catch (docker.errors.NotFound).
try:
    import docker.errors as _docker_errors
    _NotFound = _docker_errors.NotFound
except ImportError:
    _NotFound = KeyError


def load_aws_instances():
    """
    Parse configuration/aws_instances.json and return a flat dict:
        { instance_name: instance_config_dict, ... }
    Only instances with "enabled": true are included.
    """
    config_path = Path(__file__).resolve().parent.parent / "configuration" / "aws_instances.json"
    with open(config_path) as fh:
        data = json.load(fh)

    instances = {}
    for section in ("rhel", "deb"):
        for inst in data.get(section, []):
            if inst.get("enabled", False):
                instances[inst["name"]] = inst
    return instances


class _ContainerProxy:
    """Mirrors docker-py's client.containers namespace."""

    def __init__(self, instances: dict):
        self._instances = instances
        self._pool: dict[str, SSHExecutor] = {}  # connection cache

    def get(self, name: str) -> SSHExecutor:
        """
        Return a cached SSHExecutor for the named AWS instance.
        Raises docker.errors.NotFound if the instance is unknown,
        matching the exception test files already catch.
        """
        name = name.strip()
        if name not in self._pool:
            inst = self._instances.get(name)
            if inst is None:
                raise _NotFound(
                    f"AWS instance '{name}' not found in aws_instances.json "
                    f"(or not enabled). Available: {list(self._instances)}"
                )
            self._pool[name] = SSHExecutor(
                name=name,
                host=inst["host"],
                username=inst["username"],
                key_path=inst.get("key_file") or None,
            )
        return self._pool[name]

    def close_all(self):
        for executor in self._pool.values():
            executor.close()
        self._pool.clear()


class AWSInstanceClient:
    """
    Replaces docker.DockerClient when running tests against AWS EC2 instances.

    Exposes:
        client.containers.get(name)  →  SSHExecutor

    Usage (handled automatically via conftest monkey-patch):
        import docker
        docker.from_env = lambda **kw: AWSInstanceClient()
    """

    def __init__(self):
        self._instances = load_aws_instances()
        self.containers = _ContainerProxy(self._instances)

    def close(self):
        self.containers.close_all()

    def __repr__(self):
        return f"AWSInstanceClient(instances={list(self._instances)})"