#!/usr/bin/env python3
"""
Component service management for container / instance based testing.

Centralizes systemd service lifecycle operations (start / stop / restart /
status / enable / disable) for pgEdge component services such as
``pgbouncer.service``, ``patroni.service``, ``etcd.service`` etc.

Design notes
------------
* Every function returns ``(success: bool, message: str)`` — callers decide how
  strict to be. They do NOT raise for the common "service/systemd not present"
  cases, so they are safe to call defensively (e.g. stopping a package-started
  service before launching a component manually).
* Works against both docker-py containers and the SSHExecutor used for AWS, via
  the shared ``container.exec_run(...)`` interface.
"""


def _run(container, cmd, user="root"):
    """Run a shell command in the target and return (exit_code, decoded_output)."""
    exit_code, output = container.exec_run(["bash", "-c", cmd], user=user)
    return exit_code, output.decode()


def has_systemctl(container):
    """Return True if systemctl is available in the target."""
    exit_code, _ = _run(container, "command -v systemctl >/dev/null 2>&1")
    return exit_code == 0


def _absent(output):
    """Return True if systemctl output indicates the unit doesn't exist."""
    low = output.lower()
    return any(s in low for s in ("not loaded", "not found", "could not be found", "no such file"))


def stop_service(container, service_name):
    """Stop a systemd service.

    Treated as success when systemd is unavailable or the unit does not exist
    (nothing to stop). Returns (success, message).
    """
    if not has_systemctl(container):
        return True, f"systemctl not available — nothing to stop for {service_name}"

    rc, out = _run(container, f"systemctl stop {service_name} 2>&1")
    if rc == 0:
        return True, f"{service_name} stopped"
    if _absent(out):
        return True, f"{service_name} not present — nothing to stop"
    return False, f"Failed to stop {service_name}: {out.strip()}"


def start_service(container, service_name):
    """Start a systemd service. Returns (success, message)."""
    if not has_systemctl(container):
        return False, f"systemctl not available — cannot start {service_name}"

    rc, out = _run(container, f"systemctl start {service_name} 2>&1")
    if rc == 0:
        return True, f"{service_name} started"
    return False, f"Failed to start {service_name}: {out.strip()}"


def restart_service(container, service_name):
    """Restart a systemd service. Returns (success, message)."""
    if not has_systemctl(container):
        return False, f"systemctl not available — cannot restart {service_name}"

    rc, out = _run(container, f"systemctl restart {service_name} 2>&1")
    if rc == 0:
        return True, f"{service_name} restarted"
    return False, f"Failed to restart {service_name}: {out.strip()}"


def is_active(container, service_name):
    """Return True if the service is currently active (running)."""
    if not has_systemctl(container):
        return False
    rc, out = _run(container, f"systemctl is-active {service_name} 2>/dev/null")
    return rc == 0 and out.strip() == "active"


def service_status(container, service_name):
    """Return (active: bool, status_text: str) for the service."""
    if not has_systemctl(container):
        return False, "systemctl not available"
    _rc, out = _run(container, f"systemctl is-active {service_name} 2>&1")
    active = out.strip() == "active"
    return active, out.strip()


def enable_service(container, service_name):
    """Enable a systemd service (start on boot). Returns (success, message)."""
    if not has_systemctl(container):
        return False, f"systemctl not available — cannot enable {service_name}"
    rc, out = _run(container, f"systemctl enable {service_name} 2>&1")
    if rc == 0:
        return True, f"{service_name} enabled"
    return False, f"Failed to enable {service_name}: {out.strip()}"


def disable_service(container, service_name):
    """Disable a systemd service (do not start on boot). Success if absent."""
    if not has_systemctl(container):
        return True, f"systemctl not available — nothing to disable for {service_name}"
    rc, out = _run(container, f"systemctl disable {service_name} 2>&1")
    if rc == 0 or _absent(out):
        return True, f"{service_name} disabled"
    return False, f"Failed to disable {service_name}: {out.strip()}"
