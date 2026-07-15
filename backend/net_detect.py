"""
Host network interface discovery.

Prefers `ip -j addr` (rich JSON: state, MAC, addresses) on Linux and falls back
to the stdlib `socket.if_nameindex()` so the dropdown is still populated on a dev
machine without iproute2.
"""
import json
import socket
import subprocess

import config

# Interfaces we never offer as a parent for a new bind IP.
_SKIP = ("lo",)


def _from_ip_json(include_virtual=False):
    proc = subprocess.run(
        ["ip", "-d", "-j", "addr", "show"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or "ip returned no data")

    data = json.loads(proc.stdout)
    interfaces = []
    for entry in data:
        name = entry.get("ifname")
        if not name or name.startswith(_SKIP):
            continue
        # Skip the macvlan sub-interfaces this tool itself creates so they are
        # never offered as a parent (they appear in the mappings table instead).
        # The monitoring tree (include_virtual=True) keeps them, nested under
        # their parent.
        if not include_virtual and (name.startswith(config.MACVLAN_PREFIX)
                                    or name.startswith("mv-")):
            continue

        linkinfo = entry.get("linkinfo") or {}
        kind = linkinfo.get("info_kind") or "physical"
        # macvlan devices generally aren't useful parents either.
        if kind == "macvlan" and not include_virtual:
            continue

        vlan_id = None
        if kind == "vlan":
            vlan_id = (linkinfo.get("info_data") or {}).get("id")

        addrs = []
        for a in entry.get("addr_info", []):
            if a.get("family") == "inet":
                addrs.append({"ip": a.get("local"), "prefix": a.get("prefixlen")})

        interfaces.append({
            "name": name,
            "state": entry.get("operstate", "UNKNOWN"),
            "mac": entry.get("address", ""),
            "kind": kind,                      # "physical" | "vlan" | ...
            "vlan_id": vlan_id,                # set when kind == "vlan"
            "parent": entry.get("link"),       # parent device for vlan/macvlan
            "addresses": addrs,
            "up": entry.get("operstate") == "UP"
                  or "UP" in entry.get("flags", []),
        })
    return interfaces


def _stdlib_addrs():
    """Best-effort {iface: [{ip, prefix}]} via `ifconfig` on a non-Linux dev box
    (where `ip -j addr` isn't available). Returns {} if ifconfig is missing."""
    try:
        proc = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    addrs, cur = {}, None
    for line in proc.stdout.splitlines():
        if line and not line[0].isspace():
            cur = line.split(":", 1)[0].split()[0]
            continue
        parts = line.split()
        if cur and len(parts) >= 2 and parts[0] == "inet":
            ip = parts[1]
            prefix = None
            # Linux: "inet 1.2.3.4 netmask 255.255.255.0"; macOS uses hex netmask.
            if "netmask" in parts:
                mask = parts[parts.index("netmask") + 1]
                try:
                    bits = int(mask, 16) if mask.startswith("0x") else None
                    if bits is not None:
                        prefix = bin(bits).count("1")
                    else:
                        prefix = sum(bin(int(o)).count("1") for o in mask.split("."))
                except (ValueError, IndexError):
                    prefix = None
            addrs.setdefault(cur, []).append({"ip": ip, "prefix": prefix})
    return addrs


def _from_stdlib():
    addr_map = _stdlib_addrs()
    interfaces = []
    for _idx, name in socket.if_nameindex():
        if name.startswith(_SKIP) or name.startswith(config.MACVLAN_PREFIX):
            continue
        addrs = addr_map.get(name, [])
        interfaces.append({
            "name": name,
            "state": "UP" if addrs else "UNKNOWN",
            "mac": "",
            "kind": "physical",
            "vlan_id": None,
            "parent": None,
            "addresses": addrs,
            "up": True,
        })
    return interfaces


def find(name):
    for i in list_interfaces():
        if i["name"] == name:
            return i
    return None


def list_interfaces(include_virtual=False):
    """Return a list of host interfaces (best-effort).

    By default only selectable *parent* interfaces are returned (physical/VLAN);
    pass include_virtual=True to also include the macvlan/VLAN sub-interfaces,
    each carrying a `parent` link, for the monitoring tree.
    """
    try:
        ifaces = _from_ip_json(include_virtual=include_virtual)
        if ifaces:
            return sorted(ifaces, key=lambda i: i["name"])
    except Exception:
        pass
    try:
        return sorted(_from_stdlib(), key=lambda i: i["name"])
    except Exception:
        return []


def interface_names():
    return [i["name"] for i in list_interfaces()]
