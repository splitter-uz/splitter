"""
Docker container discovery.

Splitter runs in the host network namespace, so it can't use Docker's embedded
DNS (service names). Instead we talk to the Docker Engine API over its unix
socket to DISCOVER running containers — their names, network IPs and ports — and
translate a chosen container into a concrete `IP:PORT` backend the host can
reach directly (the host has routes to the docker bridges, so a container's
`172.x` address is reachable from the host netns without publishing ports).

Stdlib only (http.client over an AF_UNIX socket) — no docker SDK dependency.
Read-only: we never create/stop containers, only list them.
"""
import http.client
import json
import os
import socket
import time

import config

# The daemon socket, mounted into the container (see docker-compose.yml).
DOCKER_SOCK = os.environ.get("SPLITTER_DOCKER_SOCK", "/var/run/docker.sock")

# Networks we never want to hand out as a backend IP (not reachable / not real
# service endpoints). Everything else (bridge, user-defined bridges) is fine.
_SKIP_NETWORKS = {"host", "none"}


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that dials a unix socket instead of TCP."""

    def __init__(self, sock_path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


def _api_get(path):
    """GET a Docker API path (unversioned — the daemon uses its default). Returns
    parsed JSON. Raises on transport/HTTP error."""
    conn = _UnixHTTPConnection(DOCKER_SOCK)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"docker API {path} -> HTTP {resp.status}: "
                               f"{body[:200].decode('utf-8', 'replace')}")
        return json.loads(body or b"[]")
    finally:
        conn.close()


def available():
    """True if the Docker socket is present and the daemon answers."""
    if config.SIMULATE:
        return False
    if not os.path.exists(DOCKER_SOCK):
        return False
    try:
        _api_get("/version")
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Swarm detection + services
# --------------------------------------------------------------------------
_swarm_cache = {"t": 0.0, "val": None}


def swarm_active():
    """True if this node is a Swarm MANAGER (can list services). Cached 30s —
    swarm state changes rarely and the reconciler calls resolve() often."""
    now = time.time()
    if _swarm_cache["val"] is not None and now - _swarm_cache["t"] < 30:
        return _swarm_cache["val"]
    val = False
    try:
        info = _api_get("/info")
        sw = info.get("Swarm") or {}
        val = sw.get("LocalNodeState") == "active" and bool(sw.get("ControlAvailable"))
    except Exception:
        val = False
    _swarm_cache.update(t=now, val=val)
    return val


def list_services():
    """Swarm services as UI-friendly dicts:
        {id, name, image, mode, replicas, ports:[{published,target,type}]}
    Only services with a PUBLISHED port are reachable from a host-networked
    Splitter (routing mesh on 127.0.0.1); the rest are marked unreachable."""
    raw = _api_get("/services")
    out = []
    for s in raw:
        spec = s.get("Spec") or {}
        name = spec.get("Name") or (s.get("ID") or "")[:12]
        image = (((spec.get("TaskTemplate") or {}).get("ContainerSpec") or {})
                 .get("Image") or "").split("@")[0]
        mode = spec.get("Mode") or {}
        if "Replicated" in mode:
            replicas = (mode.get("Replicated") or {}).get("Replicas")
        elif "Global" in mode:
            replicas = "global"
        else:
            replicas = None
        ports = []
        for p in ((s.get("Endpoint") or {}).get("Ports") or []):
            if p.get("PublishedPort"):
                ports.append({"published": p["PublishedPort"],
                              "target": p.get("TargetPort"),
                              "type": p.get("Protocol", "tcp")})
        out.append({
            "id": (s.get("ID") or "")[:12],
            "name": name,
            "image": image,
            "mode": "global" if replicas == "global" else "replicated",
            "replicas": replicas,
            "ports": ports,
            "reachable": bool(ports),   # needs a published port (routing mesh)
        })
    out.sort(key=lambda x: x["name"])
    return out


def _find_service(name):
    for s in list_services():
        if s["name"] == name or s["id"] == name or s["id"].startswith(name):
            return s
    return None


def _container_ips(net_settings):
    """[(network, ip)] for a container's attached networks, skipping host/none."""
    out = []
    for name, net in (net_settings.get("Networks") or {}).items():
        if name in _SKIP_NETWORKS:
            continue
        ip = (net or {}).get("IPAddress")
        if ip:
            out.append({"network": name, "ip": ip})
    return out


def list_containers():
    """Running containers as UI-friendly dicts:
        {id, name, image, state, ips:[{network,ip}], ports:[int...], published:[...]}
    """
    raw = _api_get("/containers/json")   # running only (no ?all=1)
    out = []
    for c in raw:
        names = c.get("Names") or []
        name = (names[0] if names else c.get("Id", "?")).lstrip("/")
        ips = _container_ips(c.get("NetworkSettings") or {})
        # Distinct exposed container-side ports (what you'd proxy to).
        priv = sorted({p["PrivatePort"] for p in (c.get("Ports") or [])
                       if p.get("PrivatePort")})
        published = [{"private": p.get("PrivatePort"), "public": p.get("PublicPort"),
                      "type": p.get("Type"), "host_ip": p.get("IP")}
                     for p in (c.get("Ports") or []) if p.get("PublicPort")]
        out.append({
            "id": (c.get("Id") or "")[:12],
            "name": name,
            "image": c.get("Image"),
            "state": c.get("State"),
            "status": c.get("Status"),
            "ips": ips,
            "ports": priv,
            "published": published,
        })
    out.sort(key=lambda x: x["name"])
    return out


def _pick_ip(container):
    """The address to reach this container from the host. Prefer the first
    bridge/user-network IP (reachable from the host netns)."""
    ips = container.get("ips") or []
    return ips[0]["ip"] if ips else None


def resolve(name, port=None):
    """Translate a container OR swarm-service name (+ optional port) into a
    concrete `IP:PORT` backend, or None if it can't be resolved right now.

    - Standalone container -> its bridge IP : container port.
    - Swarm service        -> 127.0.0.1 : published port (the routing mesh
      exposes the service on every node's loopback and load-balances replicas).
    """
    if not name:
        return None
    target = name.lstrip("/")
    # 1) standalone container by name/id
    for c in list_containers():
        if c["name"] == target or c["id"] == target or c["id"].startswith(target):
            ip = _pick_ip(c)
            if not ip:
                return None
            p = port or (c["ports"][0] if c["ports"] else None)
            return f"{ip}:{int(p)}" if p else None
    # 2) swarm service by name -> routing-mesh published port on loopback
    if swarm_active():
        s = _find_service(target)
        if s:
            pport = port or (s["ports"][0]["published"] if s["ports"] else None)
            return f"127.0.0.1:{int(pport)}" if pport else None
    return None


def status():
    """Summary for /api/docker/status and the Docker page header. Reports the
    mode so the UI lists containers (standalone) or services (swarm)."""
    if not available():
        return {"available": False, "swarm": False, "count": 0}
    try:
        if swarm_active():
            return {"available": True, "swarm": True, "count": len(list_services())}
        return {"available": True, "swarm": False, "count": len(list_containers())}
    except Exception as exc:                       # noqa: BLE001
        return {"available": False, "swarm": False, "count": 0, "error": str(exc)}
