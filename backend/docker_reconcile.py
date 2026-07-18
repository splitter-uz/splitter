"""
Docker backend reconciler.

Mappings can reference a backend by Docker container name instead of a static
address (see docker_detect). A container's IP changes when it is recreated, so a
background loop re-resolves every docker-backed backend and, when an address has
changed, re-renders the mapping's nginx config and reloads — keeping the proxy
pointed at the live container without any user action. Same self-healing pattern
as failover.py / the access-list refresher.
"""
import threading
import time

import activity
import config
import docker_detect
import nginx_manager as nm
import storage

INTERVAL = 8          # seconds between reconciliation passes
_scheduler_started = False


def _reconcile_mapping(m):
    """Re-resolve this mapping's docker backends. Returns True if anything moved."""
    changed = False
    for b in m.get("backends") or []:
        cname = b.get("docker_container")
        if not cname:
            continue
        resolved = docker_detect.resolve(cname, b.get("docker_port"))
        if resolved and resolved != b.get("server"):
            b["server"] = resolved
            changed = True
        elif resolved is None and not b.get("down"):
            # Container gone/unreachable — park this backend so nginx stops
            # sending to a stale address until it comes back.
            b["down"] = True
            changed = True
        elif resolved is not None and b.get("down") and b.get("_docker_parked"):
            b.pop("down", None)
            changed = True
        # Track that WE parked it (so a user-disabled backend isn't revived).
        if resolved is None:
            b["_docker_parked"] = True
        else:
            b.pop("_docker_parked", None)
    return changed


def run_once():
    """One reconciliation pass over all enabled, docker-backed mappings."""
    for m in storage.list_mappings():
        if not m.get("enabled", True):
            continue
        if not any(b.get("docker_container") for b in (m.get("backends") or [])):
            continue
        fresh = storage.get(m["domain"], m.get("listen_port"))
        if not fresh:
            continue
        fresh = dict(fresh)
        if not _reconcile_mapping(fresh):
            continue
        try:
            if fresh.get("waf_bound"):
                nm.write_http_conf(fresh)
            else:
                nm.write_conf(fresh)
            if nm.test_config()["ok"]:
                nm.reload_nginx()
            storage.upsert(fresh)
            activity.record("system", "system", "docker.reconcile",
                            target=fresh["domain"],
                            detail="backend address(es) updated from Docker")
        except Exception:
            pass


def start_scheduler():
    """Launch the daemon thread that reconciles docker backends (idempotent)."""
    global _scheduler_started
    if _scheduler_started or config.SIMULATE:
        return
    _scheduler_started = True

    def _loop():
        while True:
            try:
                if docker_detect.available():
                    run_once()
            except Exception:
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=_loop, name="docker-reconciler", daemon=True).start()
