"""
Host network settings — DNS (`/etc/resolv.conf`) and `/etc/hosts`.

View + edit through the same SIMULATE-aware pattern as nginx_manager: off a real
host (simulation) it reads/writes a stub copy under DATA_DIR so the UI can be
exercised and round-trips; on the host it reads the real files and writes them in
place (the service runs as root by default, so no sudo is needed). Every write is
validated and returns a StepResult so the UI shows exactly what happened.
"""
import ipaddress
import os
import re
import tempfile

import config
from nginx_manager import StepResult

_HOST_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _stub(path):
    """Path of the simulation stub that mirrors a real system file."""
    return os.path.join(config.DATA_DIR, "netstub_" + os.path.basename(path))


def _target(path):
    """The file we actually read/write — the real path, or a DATA_DIR stub in
    simulation mode (seeded once from the real file if it exists)."""
    if not config.SIMULATE:
        return path
    stub = _stub(path)
    if not os.path.exists(stub) and os.path.exists(path):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(path, "r", encoding="utf-8") as src, \
                    open(stub, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        except OSError:
            pass
    return stub


def _read_text(path):
    try:
        with open(_target(path), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _write_text(label, path, text):
    target = _target(path)
    directory = os.path.dirname(target) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, target)
            os.chmod(target, 0o644)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as exc:
        return StepResult(label, False, str(exc))
    note = "[simulated] " if config.SIMULATE else ""
    return StepResult(label, True, f"{note}wrote {len(text)} bytes to {target}",
                      simulated=config.SIMULATE)


# --------------------------------------------------------------------------
# DNS — /etc/resolv.conf nameserver lines
# --------------------------------------------------------------------------
def read_dns():
    """Nameserver IPs currently configured in resolv.conf."""
    servers = []
    for line in _read_text(config.RESOLV_CONF).splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1])
    return servers


def write_dns(servers):
    """Validate and persist the nameserver list, preserving any other directives
    (search / options / comments) already in the file."""
    label = f"Write {config.RESOLV_CONF}"
    clean = []
    for s in servers:
        s = (s or "").strip()
        if not s:
            continue
        try:
            ipaddress.ip_address(s)
        except ValueError:
            return StepResult(label, False, f"Invalid DNS server address: {s!r}")
        if s not in clean:
            clean.append(s)
    if not clean:
        return StepResult(label, False, "Provide at least one DNS server.")
    # Keep non-nameserver lines (search domains, options, comments) as-is.
    kept = [ln for ln in _read_text(config.RESOLV_CONF).splitlines()
            if ln.strip() and ln.strip().split()[0] != "nameserver"]
    body = "\n".join(kept + [f"nameserver {ip}" for ip in clean]) + "\n"
    return _write_text(label, config.RESOLV_CONF, body)


# --------------------------------------------------------------------------
# Hosts — /etc/hosts
# --------------------------------------------------------------------------
def read_hosts():
    return _read_text(config.HOSTS_FILE)


def write_hosts(text):
    """Validate every non-comment line is `IP hostname...` and persist."""
    label = f"Write {config.HOSTS_FILE}"
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        try:
            ipaddress.ip_address(parts[0])
        except (ValueError, IndexError):
            return StepResult(label, False,
                              f"Line {i}: expected 'IP hostname …', got {line!r}")
        if len(parts) < 2:
            return StepResult(label, False, f"Line {i}: missing hostname after IP")
        for host in parts[1:]:
            if not _HOST_RE.match(host):
                return StepResult(label, False, f"Line {i}: invalid hostname {host!r}")
    if text and not text.endswith("\n"):
        text += "\n"
    return _write_text(label, config.HOSTS_FILE, text)
