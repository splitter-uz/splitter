"""
Thread-safe JSON persistence for stream mappings.

A mapping is keyed by domain and looks like:
    {
        "domain":   "site4.example.com",
        "bind_ip":  "192.168.10.15",
        "backend":  "192.168.10.10:443",
        "has_cert": true,
        "conf_path": "/etc/nginx/stream.d/site4.example.com.conf",
        "created":  "2026-06-12T10:00:00Z"
    }
"""
import json
import os
import tempfile
import threading

import config

_lock = threading.RLock()


def _ensure_store():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    if not os.path.exists(config.DB_FILE):
        _write_all({})


def _key(domain, port):
    """Composite identity for a mapping: a domain may appear on several listen
    ports, each its own mapping. Domains can't contain ':' (validated), so it's
    an unambiguous delimiter."""
    return f"{domain}:{int(port)}"


def _canonical_key(m):
    return _key(m["domain"], m.get("listen_port") or config.LISTEN_PORT)


def _read_all():
    _ensure_store()
    with open(config.DB_FILE, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError:
            return {}
    # Normalize to composite (domain:port) keys, tolerating legacy stores that
    # were keyed by bare domain. Writes (upsert/delete) then persist the
    # canonical form, so the on-disk file migrates itself on the next change.
    norm = {}
    for k, m in data.items():
        if isinstance(m, dict) and "domain" in m:
            norm[_canonical_key(m)] = m
    return norm


def _write_all(data):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    # Atomic write: temp file + rename so a crash never corrupts the store.
    fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, config.DB_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _port(m):
    return int(m.get("listen_port") or config.LISTEN_PORT)


def list_mappings():
    with _lock:
        return sorted(_read_all().values(), key=lambda m: (m["domain"], _port(m)))


def get(domain, port=None):
    """Fetch one mapping. With a port it's an exact (domain, port) lookup;
    without, it returns the sole mapping for that domain (or the first, for
    callers that don't yet care about the port)."""
    with _lock:
        data = _read_all()
        if port is not None:
            return data.get(_key(domain, port))
        matches = [m for m in data.values() if m["domain"] == domain]
        return matches[0] if matches else None


def exists(domain, port):
    with _lock:
        return _key(domain, port) in _read_all()


def allocate_subiface_index():
    """
    Smallest non-negative integer not used by any existing mapping's
    `subiface_index`. Used to name alias labels (eth0:N) and macvlan devices
    (strmN) uniquely.
    """
    used = set()
    with _lock:
        for m in _read_all().values():
            idx = m.get("subiface_index")
            if isinstance(idx, int):
                used.add(idx)
    idx = 0
    while idx in used:
        idx += 1
    return idx


def _is_self(m, exclude_domain, exclude_port):
    """Whether m is the exact mapping being excluded (identity is domain+port)."""
    if exclude_domain is None:
        return False
    if m["domain"] != exclude_domain:
        return False
    return exclude_port is None or _port(m) == int(exclude_port)


def bind_ip_in_use(bind_ip, exclude_domain=None, exclude_port=None, listen_port=None):
    """Return the endpoint already using bind_ip, or None.

    When `listen_port` is given the clash is scoped to the exact (IP, port)
    endpoint, so several direct-bind mappings may share one interface IP as long
    as they listen on different ports. With no port it falls back to the legacy
    "IP is unique across mappings" rule used for macvlan sub-interfaces.
    """
    with _lock:
        for m in _read_all().values():
            if m.get("bind_ip") != bind_ip or _is_self(m, exclude_domain, exclude_port):
                continue
            if listen_port is not None and _port(m) != int(listen_port):
                continue
            return f"{m['domain']}:{_port(m)}"
    return None


def vlan_iface_in_use(vlan_iface, exclude_domain=None, exclude_port=None):
    """True if another mapping still relies on the given VLAN interface."""
    with _lock:
        for m in _read_all().values():
            if _is_self(m, exclude_domain, exclude_port):
                continue
            if m.get("vlan_iface") == vlan_iface:
                return True
    return False


def cert_owners():
    """
    Domains that OWN a certificate on disk (uploaded or self-signed) and can be
    reused by other mappings. Returns [{domain, ssl_mode}].
    """
    with _lock:
        return [
            {"domain": m["domain"], "ssl_mode": m["ssl_mode"]}
            for m in sorted(_read_all().values(), key=lambda x: x["domain"])
            if m.get("has_cert") and m.get("ssl_mode") in ("upload", "selfsigned")
        ]


def cert_in_use(cert_domain, exclude_domain=None, exclude_port=None):
    """True if any other mapping references cert_domain's certificate."""
    with _lock:
        for m in _read_all().values():
            if _is_self(m, exclude_domain, exclude_port):
                continue
            if m.get("has_cert") and (m.get("cert_domain") or m["domain"]) == cert_domain:
                return True
    return False


# --- VLAN ownership registry ----------------------------------------------
_VLAN_FILE = os.path.join(config.DATA_DIR, "owned_vlans.json")


def _read_vlans():
    try:
        with open(_VLAN_FILE, "r", encoding="utf-8") as fh:
            return set(json.load(fh))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _write_vlans(names):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(sorted(names), fh, indent=2)
    os.replace(tmp, _VLAN_FILE)


def vlan_owned_add(vlan_iface):
    with _lock:
        names = _read_vlans()
        names.add(vlan_iface)
        _write_vlans(names)


def vlan_owned_remove(vlan_iface):
    with _lock:
        names = _read_vlans()
        names.discard(vlan_iface)
        _write_vlans(names)


def vlan_owned_has(vlan_iface):
    with _lock:
        return vlan_iface in _read_vlans()


# --- Managed certificate registry -----------------------------------------
# Certificates managed on the SSL page exist independently of mappings. The
# cert/key files live in config.SSL_DIR/<name>.crt|.key; this registry records
# their metadata (source, subject, expiry) keyed by name.
_CERTS_FILE = os.path.join(config.DATA_DIR, "certs.json")


def _read_certs():
    try:
        with open(_CERTS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_certs(data):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, _CERTS_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def cert_list():
    with _lock:
        return sorted(_read_certs().values(), key=lambda c: c["name"])


def cert_get(name):
    with _lock:
        return _read_certs().get(name)


def cert_add(rec):
    with _lock:
        data = _read_certs()
        data[rec["name"]] = rec
        _write_certs(data)
        return rec


def cert_remove(name):
    with _lock:
        data = _read_certs()
        removed = data.pop(name, None)
        _write_certs(data)
        return removed


# --- Global settings ------------------------------------------------------
# Tool-wide preferences that aren't per-mapping, e.g. whether saving a mapping
# provisions a real macvlan sub-interface (the default behaviour) or binds nginx
# straight to the chosen interface's existing IP.
_SETTINGS_FILE = os.path.join(config.DATA_DIR, "settings.json")

_SETTINGS_DEFAULTS = {
    "subinterface_enabled": False,
    # Automatic full-system backups (see backup.py).
    "backup_enabled": False,
    "backup_interval_hours": 24,
    "backup_retention": 7,
    "backup_last_run": None,
    # Name of the access list applied to mappings set to "use global default"
    # ("" => no default; those mappings stay open). See access.py.
    "default_access_list": "",
    # WAF (ModSecurity) server-block settings, editable on the WAF page. Engine
    # mode is NOT here — it lives in the ModSecurity config on the host (see
    # waf.py). These only drive the rendered nginx server block.
    "waf_port": 8443,
    "waf_login_rate": "10r/m",
    "waf_api_rate": "30r/s",
    # Master kill switch for the per-interface iptables firewall (firewall.py).
    # Off by default so installing the feature never changes host behaviour
    # until an admin explicitly opts in.
    "firewall_enabled": False,
}


def _read_settings():
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def settings_all():
    """Current settings with defaults applied for any missing key."""
    with _lock:
        return {**_SETTINGS_DEFAULTS, **_read_settings()}


def settings_update(patch):
    """Merge `patch` into the stored settings and return the full new dict."""
    with _lock:
        data = {**_SETTINGS_DEFAULTS, **_read_settings(), **patch}
        os.makedirs(config.DATA_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, _SETTINGS_FILE)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return data


# --- Managed sub-interface registry ---------------------------------------
# Sub-interfaces are created/edited/deleted on the Interfaces page (when the
# global subinterface toggle is on) and exist independently of mappings. Each
# record is shaped like the fields nginx_manager.provision_ip/deprovision_ip
# read (interface / vlan_id / mac / subiface / subiface_index / alloc_method /
# bind_ip / bind_prefix) so those primitives can be reused directly. Keyed by
# device name.
_SUBIFACES_FILE = os.path.join(config.DATA_DIR, "subinterfaces.json")


def _read_subifaces():
    try:
        with open(_SUBIFACES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_subifaces(data):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, _SUBIFACES_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def subiface_list():
    with _lock:
        return sorted(_read_subifaces().values(), key=lambda s: s["name"])


def subiface_get(name):
    with _lock:
        return _read_subifaces().get(name)


def subiface_add(rec):
    with _lock:
        data = _read_subifaces()
        data[rec["name"]] = rec
        _write_subifaces(data)
        return rec


def subiface_remove(name):
    with _lock:
        data = _read_subifaces()
        removed = data.pop(name, None)
        _write_subifaces(data)
        return removed


def subiface_in_use(name, exclude_domain=None):
    """Return the domain of the first mapping bound to sub-interface `name`,
    or None. Used to block edit/delete of an in-use sub-interface."""
    with _lock:
        for m in _read_all().values():
            if m["domain"] == exclude_domain:
                continue
            if m.get("subiface") == name and m.get("subiface_kind") == "managed":
                return m["domain"]
    return None


# --- Access-list registry -------------------------------------------------
# Access lists are IP/CIDR allow lists rendered to nginx snippets (one
# <name>.conf each) that a mapping's stream server block includes. A list may be
# manual (user-entered CIDRs) or auto-refreshing (fetched from source_url, e.g.
# the built-in "tasx" tas-ix feed). See access.py / nginx_manager for rendering.
_ACCESS_FILE = os.path.join(config.DATA_DIR, "access_lists.json")


def _read_access():
    try:
        with open(_ACCESS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_access(data):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, _ACCESS_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def access_list():
    with _lock:
        return sorted(_read_access().values(), key=lambda a: a["name"])


def access_get(name):
    with _lock:
        return _read_access().get(name)


def access_add(rec):
    with _lock:
        data = _read_access()
        data[rec["name"]] = rec
        _write_access(data)
        return rec


def access_remove(name):
    with _lock:
        data = _read_access()
        removed = data.pop(name, None)
        _write_access(data)
        return removed


def access_in_use(name, exclude_domain=None):
    """Return the domain of the first mapping that selected access list `name`,
    or None. Used to block delete of an in-use list."""
    with _lock:
        for m in _read_all().values():
            if m["domain"] == exclude_domain:
                continue
            if m.get("access_list") == name:
                return m["domain"]
    return None


def export_all():
    """Whole store as a dict keyed by domain — used by the backup/export API."""
    with _lock:
        return _read_all()


def import_merge(records):
    """
    Merge `records` (dict keyed by domain) into the store, overwriting any
    existing mapping with the same domain and keeping the rest. Returns the
    merged store.
    """
    with _lock:
        data = _read_all()
        data.update(records)
        _write_all(data)
        return data


# --- Firewall registries ---------------------------------------------------
# Per-interface iptables rules (security-group style) managed on the Firewall
# page. Two stores: rules (keyed by generated id, many per interface) and
# per-interface settings (enabled flag + default in/outbound policy). Neither
# takes effect on the host unless settings_all()["firewall_enabled"] is also
# on — see firewall.py.
_FW_RULES_FILE = os.path.join(config.DATA_DIR, "firewall_rules.json")
_FW_IFACES_FILE = os.path.join(config.DATA_DIR, "firewall_interfaces.json")

FW_IFACE_DEFAULTS = {
    "enabled": False,
    "inbound_policy": "accept",   # fallback action when no rule matches
    "outbound_policy": "accept",
}


def _read_fw_rules():
    try:
        with open(_FW_RULES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_fw_rules(data):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, _FW_RULES_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def fw_rule_list(interface=None):
    with _lock:
        rules = list(_read_fw_rules().values())
    if interface:
        rules = [r for r in rules if r["interface"] == interface]
    return sorted(rules, key=lambda r: (r["interface"], r.get("priority", 100), r["id"]))


def fw_rule_get(rule_id):
    with _lock:
        return _read_fw_rules().get(rule_id)


def fw_rule_add(rec):
    with _lock:
        data = _read_fw_rules()
        data[rec["id"]] = rec
        _write_fw_rules(data)
        return rec


def fw_rule_remove(rule_id):
    with _lock:
        data = _read_fw_rules()
        removed = data.pop(rule_id, None)
        _write_fw_rules(data)
        return removed


def _read_fw_ifaces():
    try:
        with open(_FW_IFACES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_fw_ifaces(data):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, _FW_IFACES_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def fw_iface_all():
    """{interface: settings} for every interface that has been explicitly
    configured (defaults applied), keyed by interface name."""
    with _lock:
        data = _read_fw_ifaces()
    return {name: {**FW_IFACE_DEFAULTS, **cfg} for name, cfg in data.items()}


def fw_iface_get(name):
    with _lock:
        data = _read_fw_ifaces()
    return {**FW_IFACE_DEFAULTS, **data.get(name, {}), "interface": name}


def fw_iface_set(name, patch):
    with _lock:
        data = _read_fw_ifaces()
        cur = {**FW_IFACE_DEFAULTS, **data.get(name, {}), **patch}
        data[name] = cur
        _write_fw_ifaces(data)
        return {**cur, "interface": name}


def upsert(mapping):
    with _lock:
        data = _read_all()
        data[_canonical_key(mapping)] = mapping
        _write_all(data)
        return mapping


def delete(domain, port=None):
    with _lock:
        data = _read_all()
        if port is not None:
            removed = data.pop(_key(domain, port), None)
        else:
            key = next((k for k, m in data.items() if m["domain"] == domain), None)
            removed = data.pop(key, None) if key else None
        _write_all(data)
        return removed
