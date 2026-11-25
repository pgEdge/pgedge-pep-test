"""package_management.install_package installs every requested package in ONE transaction
(6d41938): installed one by one, packages sharing mutually exclusive dependencies (the
enterprise-* metas both pull in PostGIS) could pick postgis36 in one transaction and postgis35 in
the next, whose files conflict. Driven with a fake container that records each shell command."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from aspects import configure_repository, package_management  # noqa: E402


class _Box:
    def __init__(self, family, install_exit=0):
        self.family, self.install_exit, self.commands = family, install_exit, []

    def exec_run(self, cmd, user=None, **kwargs):
        script = cmd[-1]
        self.commands.append(script)
        if script == "command -v dnf":
            return (0 if self.family == "rpm" else 1), b""
        if script == "command -v apt-get":
            return (0 if self.family == "deb" else 1), b""
        if " install -y " in script:
            return self.install_exit, b"E: conflicting files" if self.install_exit else b""
        return 0, b""


@pytest.fixture(autouse=True)
def no_apt_lock_wait(monkeypatch):
    monkeypatch.setattr(configure_repository, "_wait_for_apt_lock", lambda container: None)


def _installs(box):
    return [c for c in box.commands if " install -y " in c]


@pytest.mark.parametrize("requested", ["a, b,c", ["a", " b", "c "], ("a", "b", "c")],
                         ids=["comma-string", "list", "tuple"])
def test_rhel_installs_all_requested_packages_in_one_dnf_transaction(requested):
    box = _Box("rpm")
    ok, platform, _ = package_management.install_package(box, requested)
    assert ok and platform == "rhel"
    assert _installs(box) == ["dnf install -y a b c"]
    assert box.commands.index("dnf clean expire-cache") < box.commands.index("dnf install -y a b c")


def test_deb_installs_all_requested_packages_in_one_apt_transaction_after_update():
    box = _Box("deb")
    ok, platform, _ = package_management.install_package(box, "pgedge-enterprise-all-17,pgedge-enterprise-postgres-17")
    assert ok and platform == "debian"
    want = "DEBIAN_FRONTEND=noninteractive apt-get install -y pgedge-enterprise-all-17 pgedge-enterprise-postgres-17"
    assert _installs(box) == [want]
    assert box.commands.index("apt-get update") < box.commands.index(want)


@pytest.mark.parametrize("family", ["rpm", "deb"])
def test_a_failed_transaction_fails_the_install_naming_every_package_and_retries_nothing(family):
    box = _Box(family, install_exit=100)
    with pytest.raises(Exception) as err:
        package_management.install_package(box, "a,b")
    assert "Failed to install a b" in str(err.value) and "conflicting files" in str(err.value)
    assert len(_installs(box)) == 1


def test_a_single_package_is_still_one_plain_install():
    box = _Box("rpm")
    package_management.install_package(box, "pgedge-spock50_17")
    assert _installs(box) == ["dnf install -y pgedge-spock50_17"]
