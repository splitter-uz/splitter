"""
Host resource metrics for the Monitoring page (CPU / RAM / storage / network).

Reads from /proc on Linux (no external dependency — keeps the install to just
Flask + Werkzeug) and disk usage via shutil. On a non-Linux dev box, or when the
expected /proc files aren't there, it returns plausible synthesised values so the
UI can be exercised off the production host.

CPU and network are *rates*: they need two samples over time. The module keeps
the previous sample and computes the delta against the current call, so values
populate immediately for CPU (a short priming sample on the first call) and from
the second poll onward for network throughput.
"""
import math
import os
import platform
import shutil
import threading
import time

import config
import net_detect

_lock = threading.Lock()
_prev_cpu = None      # (idle, total) from the last /proc/stat read
_prev_net = None      # (monotonic_ts, rx_bytes, tx_bytes) from the last read
_prev_net_by_iface = {}  # iface -> (monotonic_ts, rx_bytes, tx_bytes)

_IS_LINUX = platform.system() == "Linux"


# --------------------------------------------------------------------------
# /proc readers (Linux)
# --------------------------------------------------------------------------
def _read_cpu_times():
    """(idle, total) jiffies from the aggregate 'cpu' line of /proc/stat."""
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("cpu "):
                parts = [int(x) for x in line.split()[1:]]
                idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
                return idle, sum(parts)
    return None


def _cpu():
    """Busy CPU percentage since the previous sample (0–100)."""
    global _prev_cpu
    cur = _read_cpu_times()
    if not cur:
        return None
    if _prev_cpu is None:
        # No baseline yet — take a brief priming sample so the first poll already
        # shows a real number instead of a blank.
        time.sleep(0.08)
        prev, cur = cur, _read_cpu_times()
    else:
        prev = _prev_cpu
    _prev_cpu = cur
    idle_d, total_d = cur[0] - prev[0], cur[1] - prev[1]
    if total_d <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1 - idle_d / total_d))), 1)


def _mem():
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, val = line.partition(":")
            info[key.strip()] = int(val.split()[0]) * 1024   # kB -> bytes
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(0, total - avail)
    return {"total": total, "used": used, "free": avail,
            "pct": round(100.0 * used / total, 1) if total else None}


def _net():
    """Aggregate rx/tx counters (excluding loopback) + per-second rates."""
    global _prev_net
    rx = tx = 0
    with open("/proc/net/dev") as fh:
        for line in fh.readlines()[2:]:           # skip the two header rows
            iface, _, data = line.partition(":")
            if iface.strip() == "lo":
                continue
            cols = data.split()
            if len(cols) >= 9:
                rx += int(cols[0])                # rx bytes
                tx += int(cols[8])                # tx bytes
    now = time.monotonic()
    rate_rx = rate_tx = None
    if _prev_net:
        dt = now - _prev_net[0]
        if dt > 0:
            rate_rx = max(0.0, (rx - _prev_net[1]) / dt)
            rate_tx = max(0.0, (tx - _prev_net[2]) / dt)
    _prev_net = (now, rx, tx)
    return {"rx_bytes": rx, "tx_bytes": tx, "rx_rate": rate_rx, "tx_rate": rate_tx}


def _read_net_dev():
    """{iface: (rx_bytes, tx_bytes)} for every non-loopback interface."""
    out = {}
    with open("/proc/net/dev") as fh:
        for line in fh.readlines()[2:]:           # skip the two header rows
            iface, _, data = line.partition(":")
            iface = iface.strip()
            if iface == "lo":
                continue
            cols = data.split()
            if len(cols) >= 9:
                out[iface] = (int(cols[0]), int(cols[8]))   # rx, tx bytes
    return out


def _iface_rates():
    """Per-interface byte counters + per-second rates since the previous poll."""
    global _prev_net_by_iface
    cur = _read_net_dev()
    now = time.monotonic()
    prev = _prev_net_by_iface
    rates = {}
    for iface, (rx, tx) in cur.items():
        rx_rate = tx_rate = None
        if iface in prev:
            pts, prx, ptx = prev[iface]
            dt = now - pts
            if dt > 0:
                rx_rate = max(0.0, (rx - prx) / dt)
                tx_rate = max(0.0, (tx - ptx) / dt)
        rates[iface] = {"rx_bytes": rx, "tx_bytes": tx,
                        "rx_rate": rx_rate, "tx_rate": tx_rate}
    _prev_net_by_iface = {k: (now, v[0], v[1]) for k, v in cur.items()}
    return rates


def _uptime():
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# Cross-platform helpers
# --------------------------------------------------------------------------
def _disk():
    path = config.MONITOR_DISK_PATH
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": u.total, "used": u.used, "free": u.free, "path": path,
            "pct": round(100.0 * u.used / u.total, 1) if u.total else None}


def _loadavg():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def _simulated():
    """Plausible, gently-moving values for a non-Linux dev box. Disk is real."""
    t = time.time()
    cores = os.cpu_count() or 4
    mem_total = 8 * 1024 ** 3
    mem_used = int(mem_total * (0.45 + 0.08 * math.sin(t / 11)))
    return {
        "cpu": {"pct": round(18 + 14 * (math.sin(t / 7) + 1), 1),
                "cores": cores, "load": _loadavg() or [0.4, 0.5, 0.6]},
        "memory": {"total": mem_total, "used": mem_used, "free": mem_total - mem_used,
                   "pct": round(100.0 * mem_used / mem_total, 1)},
        "disk": _disk() or {"total": 256 * 1024 ** 3, "used": 120 * 1024 ** 3,
                            "free": 136 * 1024 ** 3, "pct": 46.9, "path": "/"},
        "network": {"rx_bytes": 0, "tx_bytes": 0,
                    "rx_rate": 1_000_000 * (1.5 + math.sin(t / 5)),
                    "tx_rate": 400_000 * (1.2 + math.cos(t / 6))},
        "uptime": t % 1_000_000,
        "simulated": True,
    }


def snapshot():
    """One reading of host CPU / memory / disk / network. Never raises."""
    with _lock:
        if not _IS_LINUX:
            return _simulated()
        try:
            return {
                "cpu": {"pct": _cpu(), "cores": os.cpu_count() or 1, "load": _loadavg()},
                "memory": _mem(),
                "disk": _disk(),
                "network": _net(),
                "uptime": _uptime(),
                "simulated": False,
            }
        except (OSError, ValueError, IndexError):
            return _simulated()


# --------------------------------------------------------------------------
# Per-interface throughput tree (physical at top, sub-interfaces nested)
# --------------------------------------------------------------------------
_NODE_KEYS = ("name", "kind", "parent", "up", "addresses")


def _build_tree(metas, rates, simulated):
    """Nest sub-interfaces (vlan/macvlan) under their parent by the `parent`
    link; physical/root interfaces stay at the top."""
    nodes = {}
    for m in metas:
        r = rates.get(m["name"], {})
        nodes[m["name"]] = {
            **{k: m.get(k) for k in _NODE_KEYS},
            "rx_bytes": r.get("rx_bytes"), "tx_bytes": r.get("tx_bytes"),
            "rx_rate": r.get("rx_rate"), "tx_rate": r.get("tx_rate"),
            "children": [],
        }
    roots = []
    for node in nodes.values():
        parent = node.get("parent")
        if parent and parent in nodes and parent != node["name"]:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)
    for node in nodes.values():
        node["children"].sort(key=lambda n: n["name"])
    roots.sort(key=lambda n: n["name"])
    return {"interfaces": roots, "simulated": simulated}


def _simulated_iface_rates(metas):
    """Gently-moving fake rates per interface for a non-Linux dev box."""
    t = time.time()
    rates = {}
    for m in metas:
        seed = sum(ord(c) for c in m["name"]) % 9
        rates[m["name"]] = {
            "rx_bytes": None, "tx_bytes": None,
            "rx_rate": max(0.0, 600_000 * (1.0 + math.sin(t / 5 + seed))),
            "tx_rate": max(0.0, 250_000 * (1.0 + math.cos(t / 6 + seed))),
        }
    return rates


def per_interface():
    """Live upload/download per interface as a parent→children tree. Never raises."""
    with _lock:
        metas = net_detect.list_interfaces(include_virtual=True)
        if _IS_LINUX:
            try:
                return _build_tree(metas, _iface_rates(), simulated=False)
            except (OSError, ValueError, IndexError):
                pass
        return _build_tree(metas, _simulated_iface_rates(metas), simulated=True)
