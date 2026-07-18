"""
System provisioning for the Layer-4 Nginx stream proxy.

This module is the only place that touches the OS. Every privileged action is
funnelled through `run_cmd`, which honours `config.SIMULATE` so the tool can be
exercised safely off the production host.

All commands are passed as argument *lists* (never a shell string) so there is
no shell-injection surface even though the inputs are user supplied. Inputs are
additionally validated upstream in `validators.py`.
"""
import datetime
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time

import config


# --------------------------------------------------------------------------
# Step result container
# --------------------------------------------------------------------------
class StepResult(dict):
    def __init__(self, name, ok, detail="", command=None, simulated=False):
        super().__init__(
            name=name, ok=ok, detail=detail, command=command, simulated=simulated,
        )


class ProvisionError(Exception):
    """Raised when a provisioning step fails; carries collected step results."""

    def __init__(self, message, steps):
        super().__init__(message)
        self.steps = steps


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Command execution
# --------------------------------------------------------------------------
def run_cmd(name, argv, ok_returncodes=(0,), allow_fail=False, simulate_out=None):
    """Execute argv (a list). Returns a StepResult. No-op in simulation mode."""
    printable = " ".join(shlex.quote(a) for a in argv)

    if config.SIMULATE:
        detail = f"[simulated] {printable}"
        if simulate_out:
            detail += f"\n{simulate_out}"
        return StepResult(name, True, detail, printable, True)

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        return StepResult(name, False, f"command not found: {exc}", printable)
    except subprocess.TimeoutExpired:
        return StepResult(name, False, "command timed out", printable)

    output = (proc.stdout + proc.stderr).strip()
    ok = proc.returncode in ok_returncodes or allow_fail
    detail = output or f"exit code {proc.returncode}"
    return StepResult(name, ok, detail, printable)


def _privileged(argv):
    """Prepend sudo (unless already root / SPLITTER_SUDO="")."""
    return (shlex.split(config.SUDO) + list(argv)) if config.SUDO else list(argv)


def _read_iface_ip(iface):
    """Return 'ip/prefix' of the first IPv4 address on iface, or None."""
    if config.SIMULATE:
        return None
    try:
        proc = subprocess.run(
            _privileged(["ip", "-j", "addr", "show", "dev", iface]),
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(proc.stdout or "[]")
        for entry in data:
            for a in entry.get("addr_info", []):
                if a.get("family") == "inet":
                    return f"{a['local']}/{a['prefixlen']}"
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# STEP A — IP / sub-interface provisioning
# --------------------------------------------------------------------------
def link_exists(dev):
    if config.SIMULATE:
        return False
    try:
        proc = subprocess.run(_privileged(["ip", "link", "show", "dev", dev]),
                              capture_output=True, text=True, timeout=10)
        return proc.returncode == 0
    except Exception:
        return False


def _ensure_vlan(mapping, steps):
    """
    STEP 1 — create the 802.1Q VLAN sub-interface (e.g. eth0.50) on the parent
    if a VLAN ID was requested and it does not already exist. Records whether we
    created it so teardown only removes VLAN devices this tool made.
    """
    vlan_id = mapping.get("vlan_id")
    if not vlan_id:
        mapping["parent_link"] = mapping["interface"]
        return True

    parent = mapping["interface"]
    vlan_iface = f"{parent}.{vlan_id}"
    mapping["vlan_iface"] = vlan_iface
    mapping["parent_link"] = vlan_iface

    if link_exists(vlan_iface):
        mapping["vlan_created"] = False
        steps.append(StepResult(f"VLAN interface {vlan_iface}", True,
                                "already present — reusing"))
    else:
        res = run_cmd(
            f"Create VLAN interface {vlan_iface} (802.1Q id {vlan_id} on {parent})",
            _privileged(["ip", "link", "add", "link", parent, "name", vlan_iface,
                         "type", "vlan", "id", str(vlan_id)]),
            ok_returncodes=(0, 2))
        steps.append(res)
        if not res["ok"]:
            return False
        mapping["vlan_created"] = True

    res = run_cmd(f"Bring up {vlan_iface}",
                  _privileged(["ip", "link", "set", vlan_iface, "up"]))
    steps.append(res)
    return res["ok"]


# VMware virtualises NICs with these OUI prefixes. Their vSwitch drops frames
# whose source MAC differs from the VM's assigned MAC ("Forged Transmits" policy),
# which breaks macvlan DHCP. ipvlan shares the parent MAC and avoids this.
_VMWARE_OUIS = {"00:0c:29", "00:50:56", "00:05:69"}


def _parent_mac(parent):
    """Return the lower-case MAC of a host interface, or '' on failure."""
    try:
        proc = subprocess.run(
            ["ip", "-j", "link", "show", "dev", parent],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(proc.stdout or "[]")
        return (data[0].get("address") or "").lower()
    except Exception:
        return ""


def _use_ipvlan(parent):
    """Return True when ipvlan should be used instead of macvlan for this parent."""
    if config.SUBIFACE_TYPE == "ipvlan":
        return True
    if config.SUBIFACE_TYPE == "macvlan":
        return False
    # "auto": switch to ipvlan when the parent NIC belongs to a VMware adapter
    return _parent_mac(parent)[:8] in _VMWARE_OUIS


def _create_subiface(mapping, steps):
    """
    STEP 2 — create a sub-interface (macvlan or ipvlan) on the parent.

    macvlan gets its own unique MAC and answers ARP independently.
    ipvlan shares the parent MAC — required on VMware where the vSwitch
    drops frames with a source MAC that differs from the VM's assigned MAC.
    """
    if not _ensure_vlan(mapping, steps):
        return False

    parent = mapping["parent_link"]
    dev = mapping["subiface"]

    if _use_ipvlan(parent):
        mapping["_ipvlan"] = True
        cmd = _privileged(["ip", "link", "add", "link", parent, "name", dev,
                           "type", "ipvlan", "mode", "l2"])
        label = f"Create ipvlan {dev} on {parent} (shared MAC)"
    else:
        mapping["_ipvlan"] = False
        mac = mapping["mac"]
        cmd = _privileged(["ip", "link", "add", "link", parent, "name", dev,
                           "address", mac, "type", "macvlan", "mode", config.MACVLAN_MODE])
        label = f"Create macvlan {dev} on {parent} (MAC {mac})"

    res = run_cmd(label, cmd, ok_returncodes=(0, 2))
    steps.append(res)
    if not res["ok"]:
        if "File exists" in res["detail"]:
            res["ok"] = True
            res["detail"] += " (already exists)"
        else:
            return False

    res = run_cmd(f"Bring up {dev}", _privileged(["ip", "link", "set", dev, "up"]))
    steps.append(res)
    return res["ok"]


def _provision_static(mapping, steps):
    """Static: real macvlan sub-interface + user-supplied IP assigned to it."""
    dev = mapping["subiface"]
    if not _create_subiface(mapping, steps):
        _cleanup_subiface(dev, steps)
        return False
    ipcidr = f"{mapping['bind_ip']}/{mapping['bind_prefix']}"
    res = run_cmd(f"Assign {ipcidr} to {dev}",
                  _privileged(["ip", "addr", "add", ipcidr, "dev", dev]),
                  ok_returncodes=(0, 2))
    steps.append(res)
    if not res["ok"] and "File exists" in res["detail"]:
        res["ok"] = True
        res["detail"] += " (already assigned)"
    if not res["ok"]:
        _cleanup_subiface(dev, steps)
        return False
    return True


def _provision_dhcp(mapping, steps):
    """Dynamic: sub-interface (macvlan or ipvlan) + DHCP lease on it."""
    dev = mapping["subiface"]
    if not _create_subiface(mapping, steps):
        _cleanup_subiface(dev, steps)
        return False

    if mapping.get("_ipvlan"):
        return _provision_dhcp_ipvlan(mapping, steps)

    acquire = [a.format(iface=dev) for a in shlex.split(config.DHCP_ACQUIRE_CMD)]
    mock_ip = _simulated_dhcp_ip(mapping)
    res = run_cmd(f"Acquire DHCP lease on {dev}", _privileged(acquire),
                  simulate_out=f"[simulated] leased {mock_ip}/{mapping['bind_prefix']}")
    steps.append(res)
    if not res["ok"]:
        _cleanup_subiface(dev, steps)
        return False

    if config.SIMULATE:
        mapping["bind_ip"] = mock_ip
        leased = f"{mock_ip}/{mapping['bind_prefix']}"
    else:
        leased = _read_iface_ip(dev)
        if not leased:
            steps.append(StepResult("Read leased IP", False,
                                    f"no IPv4 lease found on {dev}"))
            _cleanup_subiface(dev, steps)
            return False
        mapping["bind_ip"] = leased.split("/")[0]
        mapping["bind_prefix"] = leased.split("/")[1]
    steps.append(StepResult("DHCP lease acquired", True,
                            f"{dev} -> {leased}", simulated=config.SIMULATE))
    return True


def _provision_dhcp_ipvlan(mapping, steps):
    """DHCP on an ipvlan device.

    ipvlan shares the parent MAC, so without extra help the DHCP server would
    hand out the same lease it already gave the parent. We inject a unique
    dhcp-client-identifier (option 61) via a temp config so each sub-interface
    gets its own distinct lease regardless of the shared MAC.
    """
    dev = mapping["subiface"]
    client_id = f"splitter:{dev}"
    mock_ip = _simulated_dhcp_ip(mapping)

    if config.SIMULATE:
        steps.append(StepResult(
            f"Acquire DHCP lease on {dev} (ipvlan, id={client_id})", True,
            f"[simulated] leased {mock_ip}/{mapping['bind_prefix']}", simulated=True))
        mapping["bind_ip"] = mock_ip
        return True

    cfg = f'interface "{dev}" {{\n    send dhcp-client-identifier "{client_id}";\n}}\n'
    try:
        fd, cfg_path = tempfile.mkstemp(suffix=".conf", prefix="splitter-dhcp-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(cfg)
            base = shlex.split(config.DHCP_ACQUIRE_CMD.format(iface=dev))
            # Insert -cf <path> right after the dhclient binary in the command list
            idx = next((i for i, a in enumerate(base) if "dhclient" in a), None)
            if idx is not None:
                acquire = base[:idx + 1] + ["-cf", cfg_path] + base[idx + 1:]
            else:
                acquire = base + ["-cf", cfg_path]
            res = run_cmd(f"Acquire DHCP lease on {dev} (ipvlan, id={client_id})",
                          _privileged(acquire))
        finally:
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
    except OSError as exc:
        steps.append(StepResult(f"Write dhclient config for {dev}", False, str(exc)))
        _cleanup_subiface(dev, steps)
        return False

    steps.append(res)
    if not res["ok"]:
        _cleanup_subiface(dev, steps)
        return False

    leased = _read_iface_ip(dev)
    if not leased:
        steps.append(StepResult("Read leased IP", False,
                                f"no IPv4 lease found on {dev}"))
        _cleanup_subiface(dev, steps)
        return False
    mapping["bind_ip"] = leased.split("/")[0]
    mapping["bind_prefix"] = leased.split("/")[1]
    steps.append(StepResult("DHCP lease acquired", True, f"{dev} -> {leased}"))
    return True


def _cleanup_subiface(dev, steps):
    """Best-effort teardown of a half-provisioned sub-interface."""
    steps.append(run_cmd(f"Roll back sub-interface {dev}",
                         _privileged(["ip", "link", "del", dev]),
                         ok_returncodes=(0, 1), allow_fail=True))


def _simulated_dhcp_ip(mapping):
    # Deterministic, plausible address for demos: 192.168.10.(100+index).
    idx = mapping.get("subiface_index", 0)
    return f"192.168.10.{100 + (idx % 150)}"


def provision_ip(mapping, steps):
    """Dispatch on the allocation method. Mutates mapping['bind_ip'] for DHCP."""
    kind = mapping.get("subiface_kind")
    # The IP already exists on the host — directly on the interface ("direct") or
    # on a sub-interface managed elsewhere ("managed"). Nothing to create here.
    if kind == "direct":
        steps.append(StepResult(
            f"Bind directly to {mapping.get('interface')} "
            f"({mapping.get('bind_ip')})", True,
            "sub-interface creation disabled — using the interface's existing IP"))
        return True
    if kind == "managed":
        steps.append(StepResult(
            f"Bind to sub-interface {mapping.get('subiface')} "
            f"({mapping.get('bind_ip')})", True,
            "using a sub-interface managed on the Interfaces page"))
        return True
    if mapping["alloc_method"] == "dhcp":
        return _provision_dhcp(mapping, steps)
    return _provision_static(mapping, steps)


def deprovision_ip(mapping, delete_vlan=False):
    """
    Delete the macvlan sub-interface (which also removes its addresses). The
    caller decides (via `delete_vlan`) whether the VLAN interface should also be
    removed — it owns the policy of "tool-created AND no longer in use".
    """
    steps = []
    dev = mapping.get("subiface")
    # Mappings never own their device: a direct bind has none, and a managed
    # sub-interface is torn down from the Interfaces page, not on mapping delete.
    if not dev or mapping.get("subiface_kind") in ("direct", "managed"):
        return steps
    if mapping.get("alloc_method") == "dhcp":
        release = [a.format(iface=dev) for a in shlex.split(config.DHCP_RELEASE_CMD)]
        steps.append(run_cmd(f"Release DHCP lease on {dev}",
                             _privileged(release), allow_fail=True))
    steps.append(run_cmd(f"Delete macvlan sub-interface {dev}",
                         _privileged(["ip", "link", "del", dev]),
                         ok_returncodes=(0, 1), allow_fail=True))

    vlan_iface = mapping.get("vlan_iface")
    if vlan_iface and delete_vlan:
        steps.append(run_cmd(f"Delete VLAN interface {vlan_iface}",
                             _privileged(["ip", "link", "del", vlan_iface]),
                             ok_returncodes=(0, 1), allow_fail=True))
    elif vlan_iface:
        steps.append(StepResult(f"Keep VLAN interface {vlan_iface}", True,
                                "still in use or operator-managed"))
    return steps


# --------------------------------------------------------------------------
# SSL — upload and openssl self-signed (SAN)
# --------------------------------------------------------------------------
def save_certificate(domain, cert_bytes, key_bytes):
    cert_path = os.path.join(config.SSL_DIR, f"{domain}.crt")
    key_path = os.path.join(config.SSL_DIR, f"{domain}.key")

    if config.SIMULATE:
        return cert_path, key_path, StepResult(
            "Save SSL certificate", True,
            f"[simulated] would write {cert_path} (0644) and {key_path} (0600)",
            simulated=True)
    try:
        os.makedirs(config.SSL_DIR, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            with open(key_path, "wb") as fh:
                fh.write(key_bytes)
            os.chmod(key_path, 0o600)
        finally:
            os.umask(old_umask)
        with open(cert_path, "wb") as fh:
            fh.write(cert_bytes)
        os.chmod(cert_path, 0o644)
    except OSError as exc:
        return cert_path, key_path, StepResult("Save SSL certificate", False, str(exc))
    return cert_path, key_path, StepResult(
        "Save SSL certificate", True, f"wrote {cert_path} and {key_path} (key 0600)")


def generate_self_signed(domain, days=825):
    """
    Generate a self-signed SAN certificate with `openssl` natively.

    Returns (cert_bytes, key_bytes, StepResult). openssl runs even in simulation
    mode (it only writes to a temp dir) so the UI gets a real certificate to
    preview; placement onto the host is still governed by save_certificate().
    """
    san = f"DNS:{domain}"
    if domain.count(".") >= 1 and not domain.startswith("*."):
        san += f", DNS:*.{domain}"

    # A portable extension config that works with both OpenSSL and LibreSSL.
    cnf = (
        "[req]\n"
        "distinguished_name = dn\n"
        "x509_extensions = v3\n"
        "prompt = no\n"
        "[dn]\n"
        f"CN = {domain}\n"
        "[v3]\n"
        f"subjectAltName = {san}\n"
        "basicConstraints = CA:FALSE\n"
        "keyUsage = digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
    )

    tmp = tempfile.mkdtemp(prefix="splitter-ssl-")
    cnf_path = os.path.join(tmp, "openssl.cnf")
    crt_path = os.path.join(tmp, "cert.crt")
    key_path = os.path.join(tmp, "cert.key")
    with open(cnf_path, "w") as fh:
        fh.write(cnf)

    argv = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key_path, "-out", crt_path,
        "-days", str(days), "-config", cnf_path, "-extensions", "v3",
    ]
    printable = " ".join(shlex.quote(a) for a in argv)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None, None, StepResult("Generate self-signed certificate", False,
                                      "openssl not found", printable)
    if proc.returncode != 0:
        return None, None, StepResult("Generate self-signed certificate", False,
                                      proc.stderr.strip() or "openssl failed", printable)
    with open(crt_path, "rb") as fh:
        cert_bytes = fh.read()
    with open(key_path, "rb") as fh:
        key_bytes = fh.read()
    # Clean up temp material.
    for p in (cnf_path, crt_path, key_path):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    return cert_bytes, key_bytes, StepResult(
        "Generate self-signed certificate", True,
        f"openssl SAN cert for {domain} ({san})", printable)


def cert_info(name):
    """Best-effort subject / expiry of a managed cert, read with `openssl`.

    Returns {} when the cert file isn't on disk (e.g. simulation) or openssl
    isn't available — the registry entry is still useful without it."""
    path = os.path.join(config.SSL_DIR, f"{name}.crt")
    info = {}
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", path, "-noout", "-subject", "-enddate"],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return info
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("subject="):
            info["subject"] = line.split("=", 1)[1].strip()
        elif line.startswith("notAfter="):
            info["not_after"] = line.split("=", 1)[1].strip()
    return info


def remove_certificate(domain):
    removed = []
    for ext in (".crt", ".key"):
        path = os.path.join(config.SSL_DIR, f"{domain}{ext}")
        if config.SIMULATE:
            removed.append(path)
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)
        except OSError:
            pass
    detail = ("[simulated] " if config.SIMULATE else "") + (
        "removed " + ", ".join(removed) if removed else "no cert files found")
    return StepResult("Remove SSL certificate", True, detail, simulated=config.SIMULATE)


# --------------------------------------------------------------------------
# STEP B — Nginx stream config generation (upstream load balancing)
# --------------------------------------------------------------------------
def upstream_name(domain, port=None):
    """Unique upstream identifier for a mapping. A domain may be mapped on
    several ports (each its own conf file), so the name MUST include the port —
    otherwise two same-domain mappings declare the same `upstream {}` and nginx
    fails with "duplicate upstream". `port=None` keeps the legacy domain-only
    name for any caller that doesn't have a port."""
    key = f"{domain}:{port}" if port not in (None, "") else domain
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"upstream_{h}"


# Load-balancing methods of the nginx *stream* upstream module. Note: stream
# has no `ip_hash` (that's http — it uses `hash` instead); `least_time` is
# NGINX Plus only. Method-specific options (hash key/consistent, random two)
# are carried separately on the mapping.
LB_METHODS = ("round_robin", "least_conn", "hash", "random")
DEFAULT_LB_METHOD = "round_robin"


def lb_directive(mapping):
    """Build the load-balancing directive line for the upstream block."""
    method = mapping.get("lb_method") or DEFAULT_LB_METHOD
    if method == "least_conn":
        return "least_conn;"
    if method == "hash":
        key = (mapping.get("hash_key") or "$remote_addr").strip()
        return f"hash {key}" + (" consistent" if mapping.get("hash_consistent") else "") + ";"
    if method == "random":
        return "random two least_conn;" if mapping.get("random_two") else "random;"
    return None  # round-robin (default, no directive)


def _normalize_backends(mapping):
    """Return backends as a list of dicts: {server, weight?, max_fails?, ...}."""
    raw = mapping.get("backends") or (
        [mapping["backend"]] if mapping.get("backend") else [])
    out = []
    for b in raw:
        out.append({"server": b} if isinstance(b, str) else dict(b))
    return out


def _server_line(b):
    """Render one `server …;` line with optional per-server parameters."""
    parts = ["server", b["server"]]
    if b.get("weight"):
        parts.append(f"weight={b['weight']}")
    if b.get("max_fails") not in (None, ""):
        parts.append(f"max_fails={b['max_fails']}")
    if b.get("fail_timeout"):
        parts.append(f"fail_timeout={b['fail_timeout']}")
    if b.get("max_conns"):
        parts.append(f"max_conns={b['max_conns']}")
    if b.get("backup"):
        parts.append("backup")
    if b.get("down"):
        parts.append("down")
    return "    " + " ".join(parts) + ";"


def _effective_priority(b):
    """A backend's failover priority tier (1 = highest). Defaults to 1."""
    try:
        return int(b.get("priority") or 1)
    except (TypeError, ValueError):
        return 1


def failover_priorities(mapping):
    """Sorted list of distinct priority tiers present in the pool (e.g. [1,2,3])."""
    return sorted({_effective_priority(b) for b in _normalize_backends(mapping)}) or [1]


def failover_active_priority(mapping):
    """The tier currently serving. `active_priority` if valid, else the highest."""
    prios = failover_priorities(mapping)
    ap = mapping.get("active_priority")
    return ap if ap in prios else prios[0]


def _apply_failover(mapping, backends):
    """For a failover mapping, mark every backend NOT in the active tier `down`
    so nginx routes only to the active tier (others are hot standby)."""
    if not mapping.get("failover"):
        return backends
    active = failover_active_priority(mapping)
    return [b if _effective_priority(b) == active else {**b, "down": True}
            for b in backends]


def render_conf(mapping):
    backends = _apply_failover(mapping, _normalize_backends(mapping))
    name = upstream_name(mapping["domain"], mapping.get("listen_port") or config.LISTEN_PORT)
    port = mapping.get("listen_port") or config.LISTEN_PORT
    bind_ip = mapping.get("bind_ip") or "<provisioned-ip>"
    proxy_timeout = mapping.get("proxy_timeout") or config.PROXY_TIMEOUT
    proxy_connect_timeout = mapping.get("proxy_connect_timeout") or config.PROXY_CONNECT_TIMEOUT

    # Rate limiting (optional): per-IP connection cap + per-connection bandwidth.
    rate_limit = bool(mapping.get("rate_limit"))
    limit_conn = mapping.get("limit_conn") if rate_limit else None
    down_rate = mapping.get("proxy_download_rate") if rate_limit else None
    up_rate = mapping.get("proxy_upload_rate") if rate_limit else None
    conn_zone = f"{name}_conn"

    lines = [
        "# Automatically generated Stream Proxy Cluster",
        f"# Managed by Splitter — domain: {mapping['domain']}",
        f"# Generated: {_now()}",
        "# Do not edit by hand; changes are overwritten on the next Apply.",
    ]
    # limit_conn_zone is a stream{}-level directive; this file is included
    # directly inside stream{}, so declare the per-mapping zone here.
    if limit_conn:
        lines.append(
            f"limit_conn_zone $binary_remote_addr zone={conn_zone}:10m;")
        lines.append("")
    lines.append(f"upstream {name} {{")
    directive = lb_directive(mapping)
    if directive:
        lines.append(f"    {directive}")
    for b in backends:
        lines.append(_server_line(b))
    lines += ["}", ""]

    # UDP is a datagram transport: nginx can't terminate TLS or read a TLS SNI
    # on it (those are TCP/stream-TLS concepts), so a UDP listener is always a
    # plain passthrough.
    is_udp = (mapping.get("transport") or "tcp").lower() == "udp"
    terminate = bool(mapping.get("has_cert")) and not is_udp
    proxy_ssl = mapping.get("proxy_ssl", True) and not is_udp  # re-encrypt to backend
    # SNI guard: only serve this exact hostname; drop any other SNI. Reads the
    # TLS ClientHello with ssl_preread, so it requires passthrough (no TLS
    # termination on this listener) and TCP.
    sni_guard = bool(mapping.get("sni_guard")) and not terminate and not is_udp
    sni_var = "sni_" + name

    if sni_guard:
        lines += [
            f"map $ssl_preread_server_name ${sni_var} {{",
            f"    {mapping['domain']}  {name};   # allowed host -> upstream",
            '    default   "";                  # anything else -> connection dropped',
            "}",
            "",
        ]

    lines.append("server {")
    acl_include = _acl_include_line(mapping)
    if acl_include:
        lines.append(acl_include)
    if limit_conn:
        lines.append(f"    limit_conn {conn_zone} {limit_conn};   # max connections per client IP")
    if terminate:
        # nginx TERMINATES TLS using the managed certificate (which may be shared
        # from another mapping via "use existing").
        cert_domain = mapping.get("cert_domain") or mapping["domain"]
        cert_path = os.path.join(config.SSL_DIR, f"{cert_domain}.crt")
        key_path = os.path.join(config.SSL_DIR, f"{cert_domain}.key")
        if cert_domain != mapping["domain"]:
            lines.append(f"    # certificate shared from {cert_domain}")
        lines.append(f"    listen {bind_ip}:{port} ssl;")
        lines.append(f"    ssl_certificate     {cert_path};")
        lines.append(f"    ssl_certificate_key {key_path};")
        lines.append(f"    proxy_pass {name};")
        if proxy_ssl:
            # backend also speaks TLS — open a new encrypted connection to it.
            lines.append("    proxy_ssl on;")
    else:
        # Plain Layer-4 passthrough: forward the raw TCP/TLS stream (or UDP
        # datagrams) untouched; the backend terminates TLS.
        lines.append(f"    listen {bind_ip}:{port}{' udp' if is_udp else ''};")
        if sni_guard:
            lines.append("    ssl_preread on;")
            lines.append(f"    proxy_pass ${sni_var};")
        else:
            lines.append(f"    proxy_pass {name};")
    lines += [
        f"    proxy_timeout {proxy_timeout};",
        f"    proxy_connect_timeout {proxy_connect_timeout};",
    ]
    if down_rate:
        lines.append(f"    proxy_download_rate {down_rate};   # bytes/sec per connection")
    if up_rate:
        lines.append(f"    proxy_upload_rate {up_rate};       # bytes/sec per connection")
    lines += ["}", ""]
    return "\n".join(lines)


def _mapping_port(mapping):
    return mapping.get("listen_port") or config.LISTEN_PORT


def conf_path_for(domain, port):
    # A domain can map on several ports, so the listen port is part of the file
    # name (one server block per file).
    return os.path.join(config.STREAM_CONF_DIR, f"{domain}.{port}.conf")


def _legacy_conf_path(domain):
    """Pre-port-scoping file name — kept only so migrate_conf_files() can rename."""
    return os.path.join(config.STREAM_CONF_DIR, f"{domain}.conf")


def migrate_conf_files(mappings):
    """One-time on-disk migration: rename legacy `<domain>.conf` files to the
    port-scoped `<domain>.<port>.conf` scheme. Safe because a legacy file only
    exists while a domain had exactly one mapping, so its port is unambiguous.
    Best-effort and a no-op in simulation mode."""
    if config.SIMULATE:
        return
    for m in mappings:
        domain, port = m["domain"], _mapping_port(m)
        for legacy, target in (
            (_legacy_conf_path(domain), conf_path_for(domain, port)),
            (_legacy_http_conf_path(domain), http_conf_path_for(domain, port)),
        ):
            try:
                if os.path.exists(legacy) and not os.path.exists(target):
                    os.replace(legacy, target)
                    m["conf_path"] = target
            except OSError:
                pass


def write_conf(mapping):
    path = conf_path_for(mapping["domain"], _mapping_port(mapping))
    content = render_conf(mapping)
    if config.SIMULATE:
        return path, StepResult(
            "Generate Nginx stream config", True,
            f"[simulated] would write {path}\n\n{content}", simulated=True)
    try:
        os.makedirs(config.STREAM_CONF_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o644)
    except OSError as exc:
        return path, StepResult("Generate Nginx stream config", False, str(exc))
    return path, StepResult("Generate Nginx stream config", True, f"wrote {path}")


def remove_conf(domain, port):
    path = conf_path_for(domain, port)
    if config.SIMULATE:
        return StepResult("Remove Nginx stream config", True,
                          f"[simulated] would remove {path}", simulated=True)
    try:
        if os.path.exists(path):
            os.remove(path)
            detail = f"removed {path}"
        else:
            detail = f"{path} not present"
    except OSError as exc:
        return StepResult("Remove Nginx stream config", False, str(exc))
    return StepResult("Remove Nginx stream config", True, detail)


# --------------------------------------------------------------------------
# L7 HTTP reverse proxy + WAF (ModSecurity/CRS) — "binding" a mapping's app
# --------------------------------------------------------------------------
# A WAF can only inspect HTTP, so a mapping bound to the WAF is rendered as an
# http{} server block (terminating TLS so ModSecurity can read the request)
# instead of the L4 stream{} block. The file lives in conf.d (already included
# by http{}), NOT stream.d — so a mapping is served by exactly one of them.
def waf_eligible(mapping):
    """(eligible, reason). Eligible when the app speaks HTTP the WAF can read:
    HTTPS (we terminate TLS) or plain HTTP. Non-HTTP and TLS passthrough can't."""
    if (mapping.get("transport") or "tcp").lower() == "udp":
        return False, "UDP — Layer-4 only; a WAF can't inspect it"
    proto = (mapping.get("protocol") or "").lower()
    if mapping.get("has_cert"):
        return True, ""                       # HTTPS: terminate TLS, then inspect
    if proto == "http":
        return True, ""                       # plain HTTP: inspect directly
    if proto == "https" or mapping.get("sni_guard"):
        return False, "TLS passthrough — enable TLS termination to inspect"
    return False, "Not HTTP — set protocol to HTTP, or enable TLS termination (HTTPS)"


def http_conf_path_for(domain, port):
    return os.path.join(config.WAF_APP_CONF_DIR, f"splitter-app-{domain}.{port}.conf")


def _legacy_http_conf_path(domain):
    return os.path.join(config.WAF_APP_CONF_DIR, f"splitter-app-{domain}.conf")


def render_http_conf(mapping):
    """Render the L7 HTTP reverse-proxy + ModSecurity server block."""
    backends = _apply_failover(mapping, _normalize_backends(mapping))
    name = upstream_name(mapping["domain"], mapping.get("listen_port") or config.LISTEN_PORT) + "_http"
    port = mapping.get("listen_port") or config.LISTEN_PORT
    bind_ip = mapping.get("bind_ip") or "<provisioned-ip>"
    cert_domain = mapping.get("cert_domain") or mapping["domain"]
    cert_path = os.path.join(config.SSL_DIR, f"{cert_domain}.crt")
    key_path = os.path.join(config.SSL_DIR, f"{cert_domain}.key")
    scheme = "https" if mapping.get("proxy_ssl") else "http"
    terminate = bool(mapping.get("has_cert"))   # HTTPS front vs plain HTTP front

    lines = [
        "# Automatically generated HTTP reverse proxy + WAF (ModSecurity/CRS)",
        f"# Managed by Splitter — domain: {mapping['domain']}",
        f"# Generated: {_now()}",
        "# Do not edit by hand; changes are overwritten on the next Apply.",
        f"upstream {name} {{",
    ]
    directive = lb_directive(mapping)
    if directive:
        lines.append(f"    {directive}")
    for b in backends:
        lines.append(_server_line(b))
    lines += ["}", ""]

    lines.append("server {")
    if terminate:
        lines.append(f"    listen {bind_ip}:{port} ssl;")
        lines.append(f"    server_name {mapping['domain']};")
        if cert_domain != mapping["domain"]:
            lines.append(f"    # certificate shared from {cert_domain}")
        lines.append(f"    ssl_certificate     {cert_path};")
        lines.append(f"    ssl_certificate_key {key_path};")
        lines.append("    ssl_protocols       TLSv1.2 TLSv1.3;")
    else:
        # Plain HTTP app — inspect directly, no TLS to terminate.
        lines.append(f"    listen {bind_ip}:{port};")
        lines.append(f"    server_name {mapping['domain']};")
    lines.append("")
    lines.append("    modsecurity on;")
    lines.append(f"    modsecurity_rules_file {config.WAF_MODSEC_DIR}/main.conf;")
    acl_include = _acl_include_line(mapping)   # allow/deny is valid in http too
    if acl_include:
        lines.append(acl_include)
    lines.append("    location / {")
    lines.append(f"        proxy_pass {scheme}://{name};")
    lines.append("        proxy_set_header Host              $host;")
    lines.append("        proxy_set_header X-Real-IP         $remote_addr;")
    lines.append("        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;")
    lines.append("        proxy_set_header X-Forwarded-Proto $scheme;")
    lines.append("    }")
    lines += ["}", ""]
    return "\n".join(lines)


def write_http_conf(mapping):
    path = http_conf_path_for(mapping["domain"], _mapping_port(mapping))
    content = render_http_conf(mapping)
    if config.SIMULATE:
        return path, StepResult(
            "Generate Nginx HTTP+WAF config", True,
            f"[simulated] would write {path}\n\n{content}", simulated=True)
    try:
        os.makedirs(config.WAF_APP_CONF_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o644)
    except OSError as exc:
        return path, StepResult("Generate Nginx HTTP+WAF config", False, str(exc))
    return path, StepResult("Generate Nginx HTTP+WAF config", True, f"wrote {path}")


def remove_http_conf(domain, port):
    path = http_conf_path_for(domain, port)
    if config.SIMULATE:
        return StepResult("Remove Nginx HTTP+WAF config", True,
                          f"[simulated] would remove {path}", simulated=True)
    try:
        if os.path.exists(path):
            os.remove(path)
            detail = f"removed {path}"
        else:
            detail = f"{path} not present"
    except OSError as exc:
        return StepResult("Remove Nginx HTTP+WAF config", False, str(exc))
    return StepResult("Remove Nginx HTTP+WAF config", True, detail)


def bind_waf(mapping):
    """Switch an enabled mapping from L4 stream to L7 HTTP+WAF. (ok, steps).
    Rolls back to the L4 stream config if nginx -t fails."""
    port = _mapping_port(mapping)
    steps = [remove_conf(mapping["domain"], port)]   # drop the L4 stream block
    _p, res = write_http_conf(mapping)
    steps.append(res)
    steps.append(test_config())
    if not steps[-1]["ok"]:
        # Roll back so nginx stays valid and the app keeps serving on L4.
        steps.append(remove_http_conf(mapping["domain"], port))
        _p2, r2 = write_conf(mapping)
        steps.append(r2)
        steps.append(test_config())
        steps.append(reload_nginx())
        return False, steps
    steps.append(reload_nginx())
    return True, steps


def unbind_waf(mapping):
    """Switch a mapping back from L7 HTTP+WAF to its L4 stream proxy. (ok, steps).
    Rolls back to the L7 config if the L4 config fails to validate."""
    port = _mapping_port(mapping)
    steps = [remove_http_conf(mapping["domain"], port)]  # drop the L7 block
    _p, res = write_conf(mapping)
    steps.append(res)
    steps.append(test_config())
    if not steps[-1]["ok"]:
        # Restore the L7 block so nginx stays valid and the app keeps serving.
        steps.append(remove_conf(mapping["domain"], port))
        _p2, r2 = write_http_conf(mapping)
        steps.append(r2)
        steps.append(test_config())
        steps.append(reload_nginx())
        return False, steps
    steps.append(reload_nginx())
    return True, steps


# --------------------------------------------------------------------------
# Access lists — per-list allow/deny snippets included by a mapping's server {}
# --------------------------------------------------------------------------
# Loopback appended when a list has include_private set, so the host can always
# reach its own mappings. (The RFC-1918 ranges the original allow.sh added are
# intentionally NOT included — add them explicitly to a list if you need them.)
PRIVATE_NETS = [
    "127.0.0.1/32",
]

# Special snippet that always mirrors the current global-default list (or is
# empty/allow-all when no default is set). Mappings set to "use global default"
# include this, so changing the default needs no mapping re-apply.
DEFAULT_ACL_NAME = "_default"


def acl_conf_path_for(name):
    return os.path.join(config.ACL_CONF_DIR, f"{name}.conf")


def render_acl(rec):
    """Render an access list to its nginx allow/deny snippet."""
    entries = rec.get("entries") or []
    label = rec.get("label") or rec.get("name")
    lines = [
        f"# Access list '{rec.get('name')}' — {label}",
        "# Managed by Splitter; do not edit by hand.",
        f"# Generated: {_now()}  ({len(entries)} network(s))",
    ]
    if rec.get("source_url"):
        lines.append(f"# Source: {rec['source_url']}"
                     + (f"  (refreshed {rec['last_refresh']})"
                        if rec.get("last_refresh") else ""))
    for cidr in entries:
        lines.append(f"allow {cidr};")
    if rec.get("include_private", True):
        lines.append("")
        lines.append("# Loopback")
        for cidr in PRIVATE_NETS:
            lines.append(f"allow {cidr};")
    lines += ["", "deny all;", ""]
    return "\n".join(lines)


def _write_acl_file(path, content, label):
    if config.SIMULATE:
        return StepResult(label, True,
                          f"[simulated] would write {path}\n\n{content}", simulated=True)
    try:
        os.makedirs(config.ACL_CONF_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o644)
    except OSError as exc:
        return StepResult(label, False, str(exc))
    return StepResult(label, True, f"wrote {path}")


def write_acl(rec):
    """Write a list's <name>.conf snippet. Returns (path, StepResult)."""
    path = acl_conf_path_for(rec["name"])
    return path, _write_acl_file(path, render_acl(rec), "Generate access-list config")


def write_default_acl(rec):
    """Write _default.conf — mirror of `rec` (the global default list), or an
    empty allow-all snippet when `rec` is None (no default set)."""
    path = acl_conf_path_for(DEFAULT_ACL_NAME)
    if rec:
        content = render_acl(rec)
    else:
        content = ("# No global default access list set — allow all.\n"
                   "# Managed by Splitter; do not edit by hand.\n")
    return path, _write_acl_file(path, content, "Update default access-list")


def remove_acl(name):
    path = acl_conf_path_for(name)
    if config.SIMULATE:
        return StepResult("Remove access-list config", True,
                          f"[simulated] would remove {path}", simulated=True)
    try:
        if os.path.exists(path):
            os.remove(path)
            detail = f"removed {path}"
        else:
            detail = f"{path} not present"
    except OSError as exc:
        return StepResult("Remove access-list config", False, str(exc))
    return StepResult("Remove access-list config", True, detail)


def _acl_include_line(mapping):
    """The `include …;` line for a mapping's selected access list, or "".

    access_list field: "" => open (no include); "__default__" => the special
    _default snippet; "<name>" => that list's snippet.
    """
    sel = (mapping.get("access_list") or "").strip()
    if not sel:
        return ""
    name = DEFAULT_ACL_NAME if sel == "__default__" else sel
    return f"    include {acl_conf_path_for(name)};"


def ensure_nonlocal_bind(steps):
    """
    Allow nginx to bind an address that isn't fully up yet. Without this, a
    reload/restart fired right after `ip addr add` can fail with
    'bind() ... Cannot assign requested address' even though a later manual
    restart works once the address settles. Best-effort — never fatal.
    """
    if config.SIMULATE or not config.IP_NONLOCAL_BIND:
        return
    steps.append(run_cmd(
        "Enable net.ipv4.ip_nonlocal_bind",
        _privileged(["sysctl", "-w", "net.ipv4.ip_nonlocal_bind=1"]),
        allow_fail=True))


def test_config():
    return run_cmd("Validate Nginx config (nginx -t)",
                   _privileged([config.NGINX_BIN, "-t"]))


def reload_nginx():
    if config.RELOAD_CMD:
        argv = shlex.split(config.RELOAD_CMD)
    else:
        argv = _privileged([config.NGINX_BIN, "-s", "reload"])
    return run_cmd("Reload Nginx", argv)


def _nginx_running():
    if config.SIMULATE:
        return False
    try:
        return subprocess.run(["pgrep", "-x", "nginx"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def restart_nginx():
    """
    Restart nginx so it re-binds every listen socket (incl. freshly-added IPs).

    If RESTART_CMD is set, use it. Otherwise drive the nginx binary directly:
    `nginx -s stop`, wait for the old master to exit and release its sockets,
    then start a fresh `nginx`. This is more reliable than a reload (and than a
    systemctl restart that may not pick up a just-added address).
    """
    if config.RESTART_CMD:
        return run_cmd("Restart Nginx", shlex.split(config.RESTART_CMD))
    if config.SIMULATE:
        return run_cmd("Restart Nginx (stop+start)", _privileged([config.NGINX_BIN]))

    # Stop the running master and wait until it has fully exited.
    if _nginx_running():
        run_cmd("Stop Nginx", _privileged([config.NGINX_BIN, "-s", "stop"]),
                allow_fail=True)
        deadline = time.monotonic() + 5.0
        while _nginx_running() and time.monotonic() < deadline:
            time.sleep(0.3)
    return run_cmd("Start Nginx", _privileged([config.NGINX_BIN]))


def is_listening(ip, port, udp=False):
    """True if something is bound to ip:port (via `ss`). udp=True checks UDP."""
    if config.SIMULATE:
        return True
    target = f"{ip}:{port}"
    # Listing sockets is unprivileged — don't wrap in sudo, or a missing sudoers
    # rule would make this falsely report "not bound". UDP sockets are UNCONN,
    # so list them with `-uan` rather than `-ltn`.
    flags = "-uanH" if udp else "-ltnH"
    try:
        proc = subprocess.run(["ss", flags],
                              capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    for line in proc.stdout.splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[3] == target:
            return True
    return False


# --------------------------------------------------------------------------
# Live diagnostics (per-mapping "Diagnose" panel)
# --------------------------------------------------------------------------
def count_connections(ip, port):
    """Count ESTABLISHED TCP connections whose local end is ip:port.

    Best-effort and unprivileged (like is_listening). Returns an int, or None if
    `ss` could not be run."""
    if config.SIMULATE:
        return 0
    if not ip:
        return 0
    target = f"{ip}:{port}"
    try:
        # `state established` drops the State column, so the local address shifts
        # position — match the target as a whole token rather than by index.
        proc = subprocess.run(["ss", "-tnH", "state", "established"],
                              capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return sum(1 for line in proc.stdout.splitlines() if target in line.split())


def connection_counts():
    """Map local-address ("ip:port") -> count of ESTABLISHED connections.

    One `ss` call covers every mapping at once (far cheaper than probing each
    bind IP separately for the live-traffic column). Keying by the full local
    address supports mappings that listen on different ports. Returns a dict, or
    None if `ss` could not be run."""
    if config.SIMULATE:
        return {}
    counts = {}
    try:
        proc = subprocess.run(["ss", "-tnH", "state", "established"],
                              capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    for line in proc.stdout.splitlines():
        # Columns: Recv-Q Send-Q <Local> <Peer>. The local end is the 3rd token.
        cols = line.split()
        if len(cols) >= 3:
            local = cols[2]
            counts[local] = counts.get(local, 0) + 1
    return counts


def tail_error_log(patterns, lines=400, max_out=200):
    """Tail the global nginx error log, keeping only lines matching a pattern.

    `patterns` is a list of substrings (a mapping's listen IP and its upstream
    `host:port`s); a line is kept if it contains any of them. The log is
    root-owned, so it is read via sudo. Returns a dict the API hands straight to
    the UI: {ok, path, lines[], error?}."""
    path = config.NGINX_ERROR_LOG
    if config.SIMULATE:
        return {"ok": True, "path": path,
                "lines": [f"[simulated] would tail {path} (filtered to this mapping)"]}
    try:
        proc = subprocess.run(_privileged(["tail", "-n", str(lines), path]),
                              capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return {"ok": False, "path": path, "lines": [], "error": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "path": path, "lines": [],
                "error": (proc.stderr or "").strip() or f"tail exited {proc.returncode}"}
    pats = [p for p in patterns if p]
    kept = [ln for ln in proc.stdout.splitlines() if not pats or any(p in ln for p in pats)]
    return {"ok": True, "path": path, "lines": kept[-max_out:]}


def _wait_listening(ip, port, timeout, udp=False):
    """Poll `ss` until ip:port is bound or `timeout` seconds elapse."""
    if config.SIMULATE:
        return True
    deadline = time.monotonic() + timeout
    while True:
        if is_listening(ip, port, udp=udp):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def ensure_listening(mapping, steps):
    """
    A graceful `reload` sometimes fails to bind a freshly-added IP (nginx logs
    `bind() ... Cannot assign requested address` and keeps the old sockets). If
    the new listener isn't up after reload, fall back to a full restart.

    `systemctl restart` returns once the unit is *active*, but the worker can
    take a moment longer to actually open the socket on the new address — so we
    poll for the listener over a window rather than checking exactly once.
    """
    bind_ip = mapping.get("bind_ip")
    if not bind_ip:
        return True
    port = mapping.get("listen_port") or config.LISTEN_PORT
    udp = (mapping.get("transport") or "tcp").lower() == "udp"
    # Give the reloaded master a few seconds to open the socket before deciding
    # the reload didn't take.
    if _wait_listening(bind_ip, port, timeout=3.0, udp=udp):
        return True
    steps.append(StepResult(
        f"Listener {bind_ip}:{port} not bound after reload", True,
        "reload did not bind the new address — restarting nginx"))
    # A full restart must re-bind EVERY mapping's IP at once; make sure the
    # kernel allows binding not-yet-up addresses so one un-settled IP can't take
    # the whole of nginx (and all other listeners) down with it.
    ensure_nonlocal_bind(steps)
    res = restart_nginx()
    steps.append(res)
    if not res["ok"]:
        return False
    ok = _wait_listening(bind_ip, port, timeout=10.0, udp=udp)
    steps.append(StepResult(
        f"Verify {bind_ip}:{port} listening", ok,
        "" if ok else "still not bound 10s after restart"))
    return ok


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def apply_mapping(mapping, cert_bytes=None, key_bytes=None):
    steps = []

    # Persist certificate material (uploaded or self-signed) before nginx -t.
    if cert_bytes is not None and key_bytes is not None:
        _c, _k, res = save_certificate(mapping["domain"], cert_bytes, key_bytes)
        steps.append(res)
        if not res["ok"]:
            raise ProvisionError("Failed to save certificate", steps)
        mapping["has_cert"] = True

    # STEP A — provision IP (static alias or DHCP macvlan).
    if not provision_ip(mapping, steps):
        raise ProvisionError("Failed to provision IP / sub-interface", steps)

    # Let nginx bind the address even if it hasn't fully settled yet.
    ensure_nonlocal_bind(steps)

    # STEP B — generate the config. A WAF-bound mapping is an L7 HTTP reverse
    # proxy with ModSecurity (conf.d); everything else is an L4 stream proxy
    # (stream.d). Remove the other form first so an app is served by exactly one.
    port = _mapping_port(mapping)
    old_conf_path = mapping.get("conf_path")
    if mapping.get("waf_bound"):
        remove_conf(mapping["domain"], port)
        conf_path, res = write_http_conf(mapping)
    else:
        remove_http_conf(mapping["domain"], port)
        conf_path, res = write_conf(mapping)
    steps.append(res)
    mapping["conf_path"] = conf_path
    # Drop a stale config this same mapping previously wrote under a different
    # name (e.g. a migrated legacy file, or a toggle re-enable after upgrade).
    if old_conf_path and old_conf_path != conf_path and not config.SIMULATE:
        try:
            if os.path.exists(old_conf_path):
                os.remove(old_conf_path)
        except OSError:
            pass
    if not res["ok"]:
        raise ProvisionError("Failed to write Nginx config", steps)

    # Validate before reloading.
    res = test_config()
    steps.append(res)
    if not res["ok"]:
        steps.append(remove_conf(mapping["domain"], port))
        steps.append(remove_http_conf(mapping["domain"], port))
        raise ProvisionError("nginx -t failed; configuration rolled back", steps)

    res = reload_nginx()
    steps.append(res)
    if not res["ok"]:
        raise ProvisionError("Failed to reload Nginx", steps)

    # Reload can silently fail to bind a just-added IP — verify and, if needed,
    # restart so the new listener actually comes up.
    if not ensure_listening(mapping, steps):
        raise ProvisionError("New listener did not come up (even after restart)", steps)

    return steps


def deprovision_mapping(mapping, delete_vlan=False, remove_cert=False):
    """Tear down everything created for a mapping. Best-effort, never raises."""
    # Remove both possible config forms (L4 stream and L7 HTTP+WAF).
    port = _mapping_port(mapping)
    steps = [remove_conf(mapping["domain"], port), remove_http_conf(mapping["domain"], port)]

    # Only remove the cert if the caller says this mapping OWNS it and nothing
    # else still references it (shared certs are kept).
    if remove_cert:
        steps.append(remove_certificate(mapping["domain"]))

    steps.extend(deprovision_ip(mapping, delete_vlan=delete_vlan))

    steps.append(test_config())
    if steps[-1]["ok"]:
        steps.append(reload_nginx())
    return steps
