"""Container recreation (aspects/container_management.py).

ensure_container_running(..., force_recreate=True) wants a clean slate for the one container it
recreates. It must not get there with an unfiltered `client.volumes.prune()`: that deletes every
unused volume on the Docker host (anonymous ones on API >= 1.42, named ones too before that),
including volumes that belong to other projects on a shared developer or CI machine.

A fake Docker host (no Docker) tracks containers and volumes, and prunes the way the engine
does: every unused anonymous volume, or only the matching ones when a label filter is given.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import docker
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root: `aspects` imports
from aspects import container_management as M  # noqa: E402

NAME = "auto-rocky9-arm"


class FakeContainer:
    def __init__(self, host, name, status, volumes=()):
        self.host, self.name, self.status, self.volumes = host, name, status, list(volumes)

    def remove(self, force=False, v=False):
        if self.status == "running" and not force:
            raise docker.errors.APIError("container is running")
        del self.host.containers[self.name]
        if v:   # the engine drops the container's anonymous volumes, never named ones
            for vol in self.volumes:
                if self.host.volumes[vol]["anonymous"]:
                    del self.host.volumes[vol]

    def start(self):
        self.status = "running"

    def reload(self):
        pass


class FakeHost:
    def __init__(self):
        self.containers = {}
        self.volumes = {}       # name -> {"anonymous": bool, "labels": dict}
        self.client = SimpleNamespace(
            containers=SimpleNamespace(get=self._get, create=self._create),
            images=SimpleNamespace(pull=lambda *a, **k: None),
            volumes=SimpleNamespace(prune=self._prune),
        )

    def add_volume(self, name, anonymous=True, labels=None):
        self.volumes[name] = {"anonymous": anonymous, "labels": labels or {}}

    def add_container(self, name, status, volumes=()):
        self.containers[name] = FakeContainer(self, name, status, volumes)
        return self.containers[name]

    def _get(self, name):
        if name not in self.containers:
            raise docker.errors.NotFound(name)
        return self.containers[name]

    def _create(self, name, **kwargs):
        return self.add_container(name, "created")

    def _prune(self, filters=None):
        in_use = {vol for c in self.containers.values() for vol in c.volumes}
        wanted = (filters or {}).get("label")
        wanted = [wanted] if isinstance(wanted, str) else (wanted or [])

        def matches(labels):
            return all(labels.get(k) == v if v else k in labels
                       for k, _, v in (w.partition("=") for w in wanted))

        for vol, meta in list(self.volumes.items()):
            if vol not in in_use and meta["anonymous"] and matches(meta["labels"]):
                del self.volumes[vol]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(M, "time", SimpleNamespace(sleep=lambda s: None))


@pytest.mark.parametrize("status", ["running", "exited"])
def test_force_recreate_leaves_other_unused_volumes_on_the_host(status):
    host = FakeHost()
    host.add_volume("other-project-anon")                 # unused, not ours
    host.add_volume("old-anon")
    host.add_container(NAME, status, volumes=["old-anon"])

    M.ensure_container_running(host.client, NAME, "rhel", force_recreate=True)

    assert "other-project-anon" in host.volumes


@pytest.mark.parametrize("status", ["running", "exited"])
def test_force_recreate_removes_the_old_container_with_its_own_volumes(status):
    host = FakeHost()
    host.add_volume("old-anon")
    old = host.add_container(NAME, status, volumes=["old-anon"])

    container, created, _ = M.ensure_container_running(host.client, NAME, "rhel", force_recreate=True)

    assert "old-anon" not in host.volumes
    assert created is True
    assert container is not old and container.status == "running"
    assert host.containers == {NAME: container}
