"""
Splitter — web UI + REST API for managing a Layer-4 Nginx stream proxy
with dynamic interface detection, static/DHCP IP provisioning, upstream
load-balancing pools and automated SSL (upload or openssl self-signed).

Run:  sudo ./run.sh     (or install via ./setup.sh — see README)
"""
import datetime
import json
import logging
import os
import re
import shlex
import uuid
from functools import wraps

from flask import (
    Flask, Response, jsonify, redirect, render_template, request, session,
)

import access
import activity
import auth
import backup
import config
import docker_detect
import docker_reconcile
import failover
import firewall
import health
import metrics
import net_detect
import net_settings
import nginx_manager as nm
import storage
import waf

from validators import (
    ValidationError,
    clean_acl_name,
    clean_backend_pool,
    clean_cidr_list,
    clean_domain,
    clean_fw_action,
    clean_fw_direction,
    clean_fw_protocol,
    clean_fw_source,
    clean_hash_key,
    clean_interface,
    clean_ip,
    clean_mac,
    clean_port,
    clean_port_range,
    clean_prefix,
    clean_rate,
    clean_time,
    clean_transport,
    clean_uint,
    clean_vlan_id,
)


def _parse_lb(form):
    """
    Pull the backend pool + load-balancing config out of a form.
    Returns a dict ready to merge into a mapping. Raises ValidationError.
    """
    # Backend pool: prefer the rich JSON payload (per-server params); fall back
    # to simple repeated `backends` fields.
    raw = form.get("backends_json")
    if raw:
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            raise ValidationError("Malformed backend pool data.")
    else:
        items = form.getlist("backends") or [form.get("backend")]
    # Docker-discovered backends: an item may reference a container by name
    # ({"docker_container": "web", "docker_port": 80, ...}) instead of a static
    # address. Resolve it to the container's live IP:PORT now; the reconciler
    # (docker_reconcile) keeps it current as containers restart.
    for it in items:
        if isinstance(it, dict) and it.get("docker_container") and not it.get("server"):
            name = str(it["docker_container"]).strip()
            resolved = docker_detect.resolve(name, it.get("docker_port"))
            if not resolved:
                raise ValidationError(
                    f"Docker container {name!r} is not running or has no reachable "
                    f"IP/port — start it, or pick another container.")
            it["server"] = resolved
    backends = clean_backend_pool(items)

    method = (form.get("lb_method") or nm.DEFAULT_LB_METHOD).strip().lower()
    if method not in nm.LB_METHODS:
        raise ValidationError(f"Unknown load-balancing method: {method!r}")

    cfg = {
        "backends": backends,
        "lb_method": method,
        # Active-passive priority failover: only the lowest-priority tier with a
        # healthy backend serves; the failover scheduler promotes / fails back.
        "failover": _truthy(form.get("failover")),
        "hash_key": clean_hash_key(form.get("hash_key")),
        "hash_consistent": _truthy(form.get("hash_consistent")),
        "random_two": _truthy(form.get("random_two")),
        "proxy_timeout": clean_time(form.get("proxy_timeout"), "proxy_timeout")
                         or config.PROXY_TIMEOUT,
        "proxy_connect_timeout": clean_time(
            form.get("proxy_connect_timeout"), "proxy_connect_timeout")
                         or config.PROXY_CONNECT_TIMEOUT,
    }
    return cfg


def _parse_rate(form):
    """Pull the optional rate-limit config out of a form. Returns a dict ready to
    merge into a mapping. Raises ValidationError. All limits are ignored unless
    `rate_limit` is on."""
    enabled = _truthy(form.get("rate_limit"))
    return {
        "rate_limit": enabled,
        # max simultaneous connections per client IP (limit_conn).
        "limit_conn": clean_uint(form.get("limit_conn"), "max connections", lo=1)
                      if enabled else None,
        # per-connection bandwidth caps (bytes/sec, e.g. 1m / 512k).
        "proxy_download_rate": clean_rate(form.get("proxy_download_rate"),
                                          "download rate") if enabled else None,
        "proxy_upload_rate": clean_rate(form.get("proxy_upload_rate"),
                                        "upload rate") if enabled else None,
    }


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")

app = Flask(
    __name__,
    template_folder="../frontend/templates",
    static_folder="../frontend/static",
)
app.config["SECRET_KEY"] = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES * 4
# Make app.logger actually emit (the dev server logs to stderr -> journal).
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
app.logger.setLevel(logging.INFO)

# One-time on-disk migration: rename legacy `<domain>.conf` files to the new
# port-scoped `<domain>.<port>.conf` scheme so a domain can map on several ports.
try:
    nm.migrate_conf_files(storage.list_mappings())
except Exception as _exc:  # never let a migration hiccup stop the server
    app.logger.warning("conf-file migration skipped: %s", _exc)


@app.errorhandler(Exception)
def _log_unhandled(exc):
    # Let normal HTTP errors (404, 405, ...) pass through unchanged.
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    # Never let a bug return a blank 500 — log the traceback and report it.
    app.logger.exception("Unhandled error on %s %s", request.method, request.path)
    return _err(f"Server error: {exc}", code=500)
# Session cookie only (no permanent lifetime) → login expires on browser close.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


# --------------------------------------------------------------------------
# Authentication & authorization
# --------------------------------------------------------------------------
# Endpoints reachable without a session (login/setup flow + static assets).
_PUBLIC_ENDPOINTS = {
    "login_page", "auth_status", "do_login", "do_setup", "static",
}


@app.before_request
def _require_login():
    ep = request.endpoint
    if ep is None or ep in _PUBLIC_ENDPOINTS:
        return
    if not session.get("user"):
        if request.path.startswith("/api/"):
            return _err("Authentication required.", code=401)
        return redirect("/login")


def require_role(*roles):
    """Gate an endpoint behind a session and (optionally) specific role(s)."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = session.get("user")
            if not user:
                return _err("Authentication required.", code=401)
            if roles and user.get("role") not in roles:
                return _err("You do not have permission for this action.", code=403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(message, code=400, steps=None):
    payload = {"ok": False, "error": message}
    if steps is not None:
        payload["steps"] = steps
    return jsonify(payload), code


def _audit(action, target=None, detail=None, user=None, role=None):
    """Record an audit entry, defaulting actor to the current session user."""
    u = session.get("user") or {}
    activity.record(
        user=user or u.get("username"),
        role=role or u.get("role"),
        action=action, target=target, detail=detail,
        ip=request.remote_addr,
    )


def _macvlan_name(domain, idx):
    """
    Build a readable, unique, <=15-char macvlan device name, e.g. 'mv-site4'.
    Uniqueness is guaranteed by the index suffix.
    """
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower()) or "site"
    suffix = str(idx)
    budget = 15 - len("mv-") - len(suffix) - 1  # '-' between label and suffix
    label = label[:max(budget, 1)]
    return f"mv-{label}-{suffix}"


def _looks_like_pem(blob, _marker):
    try:
        text = blob.decode("ascii", "ignore")
    except Exception:
        return False
    return "-----BEGIN" in text and ("PRIVATE KEY" in text or "CERTIFICATE" in text)


# --------------------------------------------------------------------------
# Auth pages & endpoints
# --------------------------------------------------------------------------
@app.get("/login")
def login_page():
    if session.get("user"):
        return redirect("/")
    return render_template("auth.html")


@app.get("/api/auth/status")
def auth_status():
    """Public: lets the UI decide between the setup screen, login, or the app."""
    return jsonify({
        "ok": True,
        "needs_setup": not auth.any_users(),
        "authenticated": bool(session.get("user")),
        "user": session.get("user"),
    })


@app.post("/api/setup")
def do_setup():
    """One-time creation of the initial admin. Refused once any user exists."""
    if auth.any_users():
        return _err("Setup already completed.", code=409)
    form = request.form
    try:
        user = auth.create_user(
            form.get("username"), form.get("password"), role="admin")
    except auth.AuthError as exc:
        return _err(str(exc))
    session["user"] = user
    _audit("setup", target=user["username"], detail="initial admin account created",
           user=user["username"], role=user["role"])
    return jsonify({"ok": True, "user": user})


@app.post("/api/login")
def do_login():
    username = (request.form.get("username") or "").strip()
    user = auth.verify(username, request.form.get("password"))
    if not user:
        _audit("login.failed", target=username or "(blank)", user=username or "—", role="—")
        return _err("Invalid username or password.", code=401)
    session["user"] = user
    _audit("login", user=user["username"], role=user["role"])
    return jsonify({"ok": True, "user": user})


@app.post("/api/logout")
def do_logout():
    _audit("logout")   # session still present — record before clearing
    session.clear()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# User management (admin only)
# --------------------------------------------------------------------------
@app.get("/api/users")
@require_role("admin")
def list_users_api():
    return jsonify({"ok": True, "users": auth.list_users()})


@app.post("/api/users")
@require_role("admin")
def create_user_api():
    form = request.form
    try:
        user = auth.create_user(
            form.get("username"), form.get("password"),
            role=(form.get("role") or "creator").strip().lower())
    except auth.AuthError as exc:
        return _err(str(exc))
    _audit("user.create", target=user["username"], detail=f"role={user['role']}")
    return jsonify({"ok": True, "user": user})


@app.post("/api/users/<username>")
@require_role("admin")
def update_user_api(username):
    """Admin: change another user's role and/or reset their password."""
    if not auth.get_user(username):
        return _err("No such user.", code=404)
    form = request.form
    role = (form.get("role") or "").strip().lower()
    password = form.get("password") or ""
    changed = []
    try:
        if role:
            auth.set_role(username, role)
            changed.append(f"role={role}")
        if password:
            auth.set_password(username, password)
            changed.append("password reset")
    except auth.AuthError as exc:
        return _err(str(exc))
    if not changed:
        return _err("Nothing to update.")
    _audit("user.update", target=username, detail=", ".join(changed))
    return jsonify({"ok": True, "user": auth.get_user(username)})


@app.delete("/api/users/<username>")
@require_role("admin")
def delete_user_api(username):
    if username == session["user"]["username"]:
        return _err("You cannot delete your own account.")
    try:
        removed = auth.delete_user(username)
    except auth.AuthError as exc:
        return _err(str(exc))
    if not removed:
        return _err("No such user.", code=404)
    _audit("user.delete", target=username)
    return jsonify({"ok": True})


@app.post("/api/account/password")
@require_role("admin", "creator")
def change_own_password():
    """Any logged-in user may change their own password."""
    me = session["user"]["username"]
    if not auth.verify(me, request.form.get("current_password")):
        return _err("Current password is incorrect.", code=403)
    try:
        ok = auth.set_password(me, request.form.get("new_password"))
    except auth.AuthError as exc:
        return _err(str(exc))
    if ok:
        _audit("password.change", target=me, detail="own password")
    return jsonify({"ok": bool(ok)})


# --------------------------------------------------------------------------
# Pages & read endpoints
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def get_config():
    return jsonify({"ok": True, "config": config.as_dict()})


@app.get("/api/interfaces")
def get_interfaces():
    return jsonify({"ok": True, "interfaces": net_detect.list_interfaces()})


@app.get("/api/settings")
def get_settings():
    """Tool-wide settings (e.g. whether mappings create a sub-interface)."""
    return jsonify({"ok": True, "settings": storage.settings_all()})


@app.post("/api/settings")
@require_role("admin")
def update_settings():
    """Admin: change a tool-wide setting. Currently only subinterface_enabled."""
    form = request.form
    patch = {}
    steps = []
    if "subinterface_enabled" in form:
        patch["subinterface_enabled"] = _truthy(form.get("subinterface_enabled"))
    if "default_access_list" in form:
        sel = (form.get("default_access_list") or "").strip()
        if sel and not storage.access_get(sel):
            return _err(f"No such access list: {sel}")
        patch["default_access_list"] = sel
        # Keep the special _default.conf snippet in sync with the new default.
        _p, res = nm.write_default_acl(storage.access_get(sel) if sel else None)
        steps.append(res)
    if not patch:
        return _err("Nothing to update.")
    settings = storage.settings_update(patch)
    if "default_access_list" in patch:
        steps.append(nm.reload_nginx())
    _audit("settings.update", detail=", ".join(f"{k}={v}" for k, v in patch.items()))
    return jsonify({"ok": True, "settings": settings, "steps": steps})


# --------------------------------------------------------------------------
# Docker container discovery — power the "Docker" page and container backends.
# --------------------------------------------------------------------------
@app.get("/api/docker/status")
def docker_status():
    """Whether the Docker socket is reachable + a running-container count."""
    return jsonify({"ok": True, **docker_detect.status()})


@app.get("/api/docker/containers")
@require_role("admin", "creator")
def docker_containers():
    """Running containers (name, image, IPs, ports) for the Docker page picker."""
    if not docker_detect.available():
        return _err("Docker socket not available — mount /var/run/docker.sock "
                    "into the Splitter container to enable container discovery.",
                    code=503)
    try:
        return jsonify({"ok": True, "containers": docker_detect.list_containers()})
    except Exception as exc:                        # noqa: BLE001
        return _err(f"Could not query Docker: {exc}", code=502)


# --------------------------------------------------------------------------
# WAF (ModSecurity + OWASP CRS) — status + control (admin only)
# --------------------------------------------------------------------------
@app.get("/api/waf/status")
@require_role("admin")
def waf_status():
    return jsonify({"ok": True,
                    "status": waf.status(),
                    "events": waf.recent_events()})


@app.post("/api/waf/install")
@require_role("admin")
def waf_install():
    """(Re)install the WAF in the background (runs setup.sh --waf-only)."""
    res = waf.install()
    _audit("waf.install", detail="started" if res.get("running") else "noop")
    return jsonify({"ok": res.get("ok", True), "install": waf.install_state(),
                    "status": waf.status()})


def _backend_summary(mapping):
    """Short 'host:port (+N)' description of a mapping's backend pool."""
    backends = mapping.get("backends") or (
        [{"server": mapping["backend"]}] if mapping.get("backend") else [])
    if not backends:
        return "—"
    first = backends[0].get("server") if isinstance(backends[0], dict) else backends[0]
    extra = len(backends) - 1
    return f"{first}" + (f"  (+{extra})" if extra > 0 else "")


@app.get("/api/waf/apps")
@require_role("admin")
def waf_apps():
    """List every mapping with its WAF eligibility and bind state."""
    apps = []
    for m in storage.list_mappings():
        eligible, reason = nm.waf_eligible(m)
        apps.append({
            "domain": m["domain"],
            "backend": _backend_summary(m),
            "transport": (m.get("transport") or "tcp").lower(),
            "protocol": (m.get("protocol") or "").lower(),
            "listen_port": m.get("listen_port"),
            "has_cert": bool(m.get("has_cert")),
            "enabled": m.get("enabled", True),
            "eligible": eligible,
            "reason": reason,
            "bound": bool(m.get("waf_bound")),
        })
    return jsonify({"ok": True, "apps": apps, "waf_installed": waf.installed()})


@app.post("/api/waf/bind")
@require_role("admin")
def waf_bind():
    """Bind a mapping's app to the WAF (switch it to L7 HTTP reverse proxy)."""
    try:
        domain = clean_domain(request.form.get("domain") or "")
    except ValidationError as exc:
        return _err(str(exc))
    mapping = storage.get(domain)
    if not mapping:
        return _err("No such mapping.", code=404)
    if not waf.installed():
        return _err("Install the WAF first (WAF page → Install).")
    eligible, reason = nm.waf_eligible(mapping)
    if not eligible:
        return _err(f"Cannot bind {domain}: {reason}")
    if mapping.get("waf_bound"):
        return jsonify({"ok": True, "steps": [], "bound": True})

    mapping["waf_bound"] = True
    mapping["updated"] = _now()
    steps = []
    if mapping.get("enabled", True):
        ok, steps = nm.bind_waf(mapping)
        if not ok:
            mapping["waf_bound"] = False   # bind_waf already rolled back to L4
            return _err(f"Bind failed for {domain} — reverted to Layer-4.",
                        code=500, steps=steps)
    storage.upsert(mapping)
    _audit("waf.bind", target=domain)
    return jsonify({"ok": True, "steps": steps, "bound": True})


@app.post("/api/waf/unbind")
@require_role("admin")
def waf_unbind():
    """Unbind a mapping's app from the WAF (back to the L4 stream proxy)."""
    try:
        domain = clean_domain(request.form.get("domain") or "")
    except ValidationError as exc:
        return _err(str(exc))
    mapping = storage.get(domain)
    if not mapping:
        return _err("No such mapping.", code=404)
    if not mapping.get("waf_bound"):
        return jsonify({"ok": True, "steps": [], "bound": False})

    mapping["waf_bound"] = False
    mapping["updated"] = _now()
    steps = []
    if mapping.get("enabled", True):
        ok, steps = nm.unbind_waf(mapping)
        if not ok:
            return _err(f"Unbind failed for {domain} (see steps).",
                        code=500, steps=steps)
    storage.upsert(mapping)
    _audit("waf.unbind", target=domain)
    return jsonify({"ok": True, "steps": steps, "bound": False})


@app.post("/api/waf/mode")
@require_role("admin")
def waf_set_mode():
    """Switch the ModSecurity engine: Off / DetectionOnly / On."""
    mode = (request.form.get("mode") or "").strip()
    ok, steps = waf.set_mode(mode)
    _audit("waf.mode", detail=f"mode={mode} ok={ok}")
    return jsonify({"ok": ok, "steps": steps,
                    "status": waf.status()})


@app.post("/api/waf/settings")
@require_role("admin")
def waf_update_settings():
    """Change the WAF listen port and/or per-IP rate limits, then reload."""
    form = request.form
    patch = {}
    for key in ("port", "login_rate", "api_rate"):
        if key in form and form.get(key) != "":
            patch[key] = form.get(key)
    if not patch:
        return _err("Nothing to update.")
    ok, steps = waf.apply_settings(patch)
    _audit("waf.settings", detail=", ".join(f"{k}={v}" for k, v in patch.items()))
    return jsonify({"ok": ok, "steps": steps, "status": waf.status()})


# --------------------------------------------------------------------------
# Managed sub-interfaces — create / list / edit / delete (admin)
# A sub-interface owns a REAL macvlan device; mappings later select one to bind.
# --------------------------------------------------------------------------
def _alloc_subiface_name(label, taken=()):
    """Smallest free 'mv-<label>-<n>' device name not already registered."""
    idx = storage.allocate_subiface_index()
    while True:
        name = _macvlan_name(label or "sub", idx)
        if name not in taken and not storage.subiface_get(name):
            return name, idx
        idx += 1


def _build_subiface(form, existing=None):
    """Validate the sub-interface form into a provisioning record. Raises
    ValidationError. Does not touch the host."""
    allowed_ifaces = net_detect.interface_names()
    interface = clean_interface(form.get("interface") or config.NIC,
                                allowed=allowed_ifaces or None)
    vlan_id = clean_vlan_id(form.get("vlan_id"))
    if vlan_id:
        sel = net_detect.find(interface)
        if sel and sel.get("kind") == "vlan":
            raise ValidationError(
                f"{interface} is already a VLAN interface — clear the VLAN ID, "
                f"or pick a physical parent.")
    mac = clean_mac(form.get("mac"))                  # blank => random LAA MAC
    bind_prefix = clean_prefix(form.get("bind_prefix"), config.BIND_PREFIX)
    # Sub-interfaces are always statically addressed (DHCP creation removed).
    alloc_method = "static"
    bind_ip = clean_ip(form.get("bind_ip"), "bind IP address")

    if existing:
        name, idx = existing["name"], existing["subiface_index"]
    else:
        label = (form.get("label") or "").strip()
        name, idx = _alloc_subiface_name(label)

    return {
        "name": name,
        "interface": interface,
        "vlan_id": vlan_id,
        "mac": mac,
        "alloc_method": alloc_method,
        "bind_ip": bind_ip,
        "bind_prefix": bind_prefix,
        "subiface": name,                 # nginx_manager reads mapping['subiface']
        "subiface_index": idx,
        "subiface_kind": "macvlan",       # a real, self-owned device
        "created": (existing or {}).get("created", _now()),
        "updated": _now(),
    }


@app.get("/api/subinterfaces")
@require_role("admin", "creator")
def list_subinterfaces():
    subs = [{**s, "in_use": storage.subiface_in_use(s["name"])}
            for s in storage.subiface_list()]
    return jsonify({"ok": True, "subinterfaces": subs})


@app.post("/api/subinterfaces")
@require_role("admin")
def create_subinterface():
    try:
        rec = _build_subiface(request.form)
        if rec["bind_ip"]:
            other = storage.bind_ip_in_use(rec["bind_ip"])
            if other:
                return _err(f"Bind IP {rec['bind_ip']} is already used by {other}")
    except ValidationError as exc:
        return _err(str(exc))

    steps = []
    if not nm.provision_ip(rec, steps):
        return _err("Failed to provision sub-interface", code=500, steps=steps)
    nm.ensure_nonlocal_bind(steps)
    if rec.get("vlan_iface") and rec.get("vlan_created"):
        storage.vlan_owned_add(rec["vlan_iface"])
    storage.subiface_add(rec)
    _audit("subinterface.create", target=rec["name"],
           detail=f"{rec['interface']} · {rec.get('bind_ip')}")
    return jsonify({"ok": True, "subinterface": rec, "steps": steps})


@app.post("/api/subinterfaces/<name>")
@require_role("admin")
def update_subinterface(name):
    existing = storage.subiface_get(name)
    if not existing:
        return _err("No such sub-interface.", code=404)
    used_by = storage.subiface_in_use(name)
    if used_by:
        return _err(f"Sub-interface {name} is in use by {used_by} — detach that "
                    f"mapping first.", code=409)
    try:
        rec = _build_subiface(request.form, existing=existing)
        if rec["bind_ip"]:
            other = storage.bind_ip_in_use(rec["bind_ip"])
            if other:
                return _err(f"Bind IP {rec['bind_ip']} is already used by {other}")
    except ValidationError as exc:
        return _err(str(exc))

    steps = nm.deprovision_ip(existing, delete_vlan=False)
    if not nm.provision_ip(rec, steps):
        return _err("Failed to re-provision sub-interface", code=500, steps=steps)
    nm.ensure_nonlocal_bind(steps)
    if rec.get("vlan_iface") and rec.get("vlan_created"):
        storage.vlan_owned_add(rec["vlan_iface"])
    storage.subiface_add(rec)
    _audit("subinterface.update", target=name)
    return jsonify({"ok": True, "subinterface": rec, "steps": steps})


@app.delete("/api/subinterfaces/<name>")
@require_role("admin")
def delete_subinterface(name):
    rec = storage.subiface_get(name)
    if not rec:
        return _err("No such sub-interface.", code=404)
    used_by = storage.subiface_in_use(name)
    if used_by:
        return _err(f"Sub-interface {name} is in use by {used_by} — delete that "
                    f"mapping first.", code=409)
    vlan_iface = rec.get("vlan_iface")
    delete_vlan = bool(vlan_iface) and storage.vlan_owned_has(vlan_iface) and \
        not storage.vlan_iface_in_use(vlan_iface)
    steps = nm.deprovision_ip(rec, delete_vlan=delete_vlan)
    storage.subiface_remove(name)
    if delete_vlan:
        storage.vlan_owned_remove(vlan_iface)
    _audit("subinterface.delete", target=name)
    return jsonify({"ok": True, "steps": steps})


# --------------------------------------------------------------------------
# Host network settings — DNS + /etc/hosts (admin)
# --------------------------------------------------------------------------
@app.get("/api/network/dns")
@require_role("admin")
def get_dns():
    return jsonify({"ok": True, "servers": net_settings.read_dns(),
                    "path": config.RESOLV_CONF, "simulate": config.SIMULATE})


@app.post("/api/network/dns")
@require_role("admin")
def set_dns():
    servers = request.form.getlist("servers")
    step = net_settings.write_dns(servers)
    if not step["ok"]:
        return _err(step["detail"], steps=[step])
    _audit("network.dns", detail=", ".join(net_settings.read_dns()) or "(none)")
    return jsonify({"ok": True, "servers": net_settings.read_dns(), "steps": [step]})


@app.get("/api/network/hosts")
@require_role("admin")
def get_hosts():
    return jsonify({"ok": True, "text": net_settings.read_hosts(),
                    "path": config.HOSTS_FILE, "simulate": config.SIMULATE})


@app.post("/api/network/hosts")
@require_role("admin")
def set_hosts():
    step = net_settings.write_hosts(request.form.get("text") or "")
    if not step["ok"]:
        return _err(step["detail"], steps=[step])
    _audit("network.hosts", detail="updated /etc/hosts")
    return jsonify({"ok": True, "text": net_settings.read_hosts(), "steps": [step]})


# --------------------------------------------------------------------------
# Firewall — per-interface iptables rules, security-group style (admin)
# --------------------------------------------------------------------------
def _fw_allowed_interfaces():
    return {i["name"] for i in net_detect.list_interfaces(include_virtual=True)}


def _fw_public_iface(name):
    cfg = storage.fw_iface_get(name)
    rules = storage.fw_rule_list(name)
    return {**cfg, "rule_count": len(rules)}


@app.get("/api/firewall/overview")
@require_role("admin", "creator")
def firewall_overview():
    """Every host interface (incl. Splitter-managed sub-interfaces) with its
    firewall settings and rule count, plus the master switch."""
    ifaces = [_fw_public_iface(i["name"])
              for i in net_detect.list_interfaces(include_virtual=True)]
    return jsonify({
        "ok": True,
        "enabled": storage.settings_all().get("firewall_enabled", False),
        "interfaces": ifaces,
        "simulate": config.SIMULATE,
        "dashboard_port": config.PORT,
    })


@app.get("/api/firewall/whoami")
@require_role("admin", "creator")
def firewall_whoami():
    """The caller's own IP as a /32 (or /128) CIDR, for the rule form's "My IP"
    shortcut. Behind the WAF the peer address is loopback, so in that case fall
    back to the forwarding header nginx set."""
    ip = request.remote_addr or ""
    if ip in ("127.0.0.1", "::1"):
        fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        ip = fwd or ip
    try:
        ip = clean_ip(ip)
    except ValidationError:
        return _err("Could not determine your IP address.")
    return jsonify({"ok": True, "cidr": f"{ip}/128" if ":" in ip else f"{ip}/32"})


@app.post("/api/firewall/settings")
@require_role("admin")
def firewall_toggle():
    """Flip the global master switch. Turning it on applies every interface
    already marked enabled; turning it off tears every managed chain down
    (independent of each interface's own flag)."""
    enabled = _truthy(request.form.get("enabled"))
    storage.settings_update({"firewall_enabled": enabled})
    if enabled:
        steps = []
        for name, cfg in storage.fw_iface_all().items():
            if cfg.get("enabled"):
                steps += firewall.apply_interface(name)
    else:
        steps = firewall.disable_all()
    _audit("firewall.toggle", detail=f"enabled={enabled}")
    return jsonify({"ok": True, "enabled": enabled, "steps": steps})


@app.post("/api/firewall/interfaces/<name>")
@require_role("admin")
def firewall_update_interface(name):
    """Enable/disable enforcement on one interface and set its default
    (fallback) policy for traffic that matches no rule."""
    try:
        name = clean_interface(name, allowed=_fw_allowed_interfaces())
    except ValidationError as exc:
        return _err(str(exc))
    form = request.form
    patch = {}
    if "enabled" in form:
        patch["enabled"] = _truthy(form.get("enabled"))
    if "inbound_policy" in form:
        patch["inbound_policy"] = clean_fw_action(form.get("inbound_policy"))
        if patch["inbound_policy"] == "reject":
            return _err("Default policy must be accept or drop.")
    if "outbound_policy" in form:
        patch["outbound_policy"] = clean_fw_action(form.get("outbound_policy"))
        if patch["outbound_policy"] == "reject":
            return _err("Default policy must be accept or drop.")
    if not patch:
        return _err("Nothing to update.")

    cfg = storage.fw_iface_set(name, patch)
    steps = []
    if storage.settings_all().get("firewall_enabled"):
        steps = firewall.apply_interface(name) if cfg["enabled"] else firewall.teardown_interface(name)
    _audit("firewall.interface.update", target=name,
           detail=", ".join(f"{k}={v}" for k, v in patch.items()))
    return jsonify({"ok": True, "interface": _fw_public_iface(name), "steps": steps})


@app.get("/api/firewall/rules")
@require_role("admin", "creator")
def firewall_list_rules():
    interface = (request.args.get("interface") or "").strip() or None
    return jsonify({"ok": True, "rules": storage.fw_rule_list(interface)})


def _build_fw_rule(form, existing=None):
    interface = clean_interface(form.get("interface"), allowed=_fw_allowed_interfaces())
    return {
        **(existing or {}),
        "id": (existing or {}).get("id") or uuid.uuid4().hex[:12],
        "interface": interface,
        "direction": clean_fw_direction(form.get("direction")),
        "protocol": clean_fw_protocol(form.get("protocol")),
        "port_range": clean_port_range(form.get("port_range"), field="Port"),
        "source_cidr": clean_fw_source(form.get("source")),
        "action": clean_fw_action(form.get("action")),
        "priority": clean_uint(form.get("priority"), "Priority", lo=1, hi=65535) or 100,
        "description": (form.get("description") or "").strip()[:200],
        "enabled": _truthy(form.get("enabled")) if "enabled" in form else True,
        "created": (existing or {}).get("created", _now()),
        "updated": _now(),
    }


@app.post("/api/firewall/rules")
@require_role("admin")
def firewall_create_rule():
    try:
        rec = _build_fw_rule(request.form)
    except ValidationError as exc:
        return _err(str(exc))
    storage.fw_rule_add(rec)
    steps = []
    if storage.settings_all().get("firewall_enabled") and storage.fw_iface_get(rec["interface"])["enabled"]:
        steps = firewall.apply_interface(rec["interface"])
    _audit("firewall.rule.create", target=rec["interface"],
           detail=f"{rec['direction']} {rec['protocol']} {rec['port_range'] or 'any'} "
                  f"{rec['source_cidr']} -> {rec['action']}")
    return jsonify({"ok": True, "rule": rec, "steps": steps})


@app.post("/api/firewall/rules/<rule_id>")
@require_role("admin")
def firewall_update_rule(rule_id):
    existing = storage.fw_rule_get(rule_id)
    if not existing:
        return _err("No such firewall rule.", code=404)
    try:
        rec = _build_fw_rule(request.form, existing=existing)
    except ValidationError as exc:
        return _err(str(exc))
    storage.fw_rule_add(rec)
    steps = []
    if storage.settings_all().get("firewall_enabled") and storage.fw_iface_get(rec["interface"])["enabled"]:
        steps = firewall.apply_interface(rec["interface"])
        if rec["interface"] != existing["interface"] and storage.fw_iface_get(existing["interface"])["enabled"]:
            steps += firewall.apply_interface(existing["interface"])
    _audit("firewall.rule.update", target=rec["interface"], detail=rule_id)
    return jsonify({"ok": True, "rule": rec, "steps": steps})


@app.delete("/api/firewall/rules/<rule_id>")
@require_role("admin")
def firewall_delete_rule(rule_id):
    existing = storage.fw_rule_get(rule_id)
    if not existing:
        return _err("No such firewall rule.", code=404)
    storage.fw_rule_remove(rule_id)
    steps = []
    if storage.settings_all().get("firewall_enabled") and storage.fw_iface_get(existing["interface"])["enabled"]:
        steps = firewall.apply_interface(existing["interface"])
    _audit("firewall.rule.delete", target=existing["interface"], detail=rule_id)
    return jsonify({"ok": True, "steps": steps})


@app.post("/api/firewall/panic")
@require_role("admin")
def firewall_panic():
    """Emergency kill switch: tear down every managed chain and turn the
    master switch off, in one call."""
    steps = firewall.panic()
    _audit("firewall.panic", detail="all managed chains torn down")
    return jsonify({"ok": True, "steps": steps})


# --------------------------------------------------------------------------
# System — reboot the whole host (admin)
# --------------------------------------------------------------------------
@app.post("/api/system/reboot")
@require_role("admin")
def system_reboot():
    """Reboot the host OS. The command is scheduled a couple of seconds out so
    this HTTP response can flush before the box goes down."""
    argv = nm._privileged(shlex.split(config.REBOOT_CMD))
    printable = " ".join(shlex.quote(a) for a in argv)
    _audit("system.reboot", detail=printable)

    if config.SIMULATE:
        return jsonify({"ok": True, "simulate": True,
                        "message": f"[simulated] would run: {printable}"})

    try:
        # Detach with a short delay so the client gets a reply before the
        # network (and this process) dies with the reboot.
        import subprocess as _sp
        _sp.Popen(["sh", "-c", f"sleep 2; {printable}"],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                  start_new_session=True)
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"Failed to schedule reboot: {exc}")

    return jsonify({"ok": True, "message": "Reboot scheduled — the host is going down now."})


@app.get("/api/random-mac")
def get_random_mac():
    from validators import random_mac
    return jsonify({"ok": True, "mac": random_mac()})


@app.get("/api/mappings")
def list_mappings():
    return jsonify({"ok": True, "mappings": storage.list_mappings()})


@app.get("/api/health")
@require_role("admin", "creator")
def backend_health():
    """Probe every mapping's backend host:port and report a traffic-light status.

    Pass ?force=1 to bypass the short probe cache and re-check immediately.
    """
    return jsonify({"ok": True, **health.check_all(force=_truthy(request.args.get("force")))})


@app.get("/api/metrics")
@require_role("admin", "creator")
def host_metrics():
    """Current host resource usage: CPU, memory, storage and network."""
    return jsonify({"ok": True, "metrics": metrics.snapshot()})


@app.get("/api/interfaces/traffic")
@require_role("admin", "creator")
def interfaces_traffic():
    """Live upload/download per interface (physical at top, sub-interfaces
    nested) for the Interfaces page throughput tree."""
    return jsonify({"ok": True, **metrics.per_interface()})


@app.get("/api/traffic")
@require_role("admin", "creator")
def mapping_traffic():
    """Live per-mapping connection counts for the animated traffic column.

    A single `ss` call yields established-connection counts grouped by local IP;
    each mapping is matched on its bind IP. In simulation mode we synthesise a
    gently-moving value per mapping so the animation is visible off-host.
    """
    mappings = storage.list_mappings()
    if config.SIMULATE:
        import math
        import time
        now = time.time()
        traffic = {}
        for m in mappings:
            key = f"{m['domain']}:{m.get('listen_port') or config.LISTEN_PORT}"
            if (m.get("transport") or "tcp").lower() == "udp":
                traffic[key] = {"connections": None, "udp": True}
                continue
            seed = sum(ord(c) for c in m["domain"]) % 11
            n = max(0, round(5 + seed % 4 + 4 * math.sin(now / 4 + seed)))
            traffic[key] = {"connections": n}
        return jsonify({"ok": True, "traffic": traffic, "available": True})

    counts = nm.connection_counts()   # {"ip:port": count}
    traffic = {}
    for m in mappings:
        port = m.get("listen_port") or config.LISTEN_PORT
        key = f"{m['domain']}:{port}"
        if (m.get("transport") or "tcp").lower() == "udp":
            traffic[key] = {"connections": None, "udp": True}
            continue
        ip = m.get("bind_ip")
        n = (counts or {}).get(f"{ip}:{port}", 0) if ip else 0
        traffic[key] = {"connections": n}
    return jsonify({"ok": True, "traffic": traffic, "available": counts is not None})


@app.get("/api/certs")
def list_certs():
    """Certificates a mapping can reuse: the SSL-page registry plus any legacy
    certs still owned by an existing mapping (deduped by name)."""
    seen, out = set(), []
    for c in storage.cert_list():
        seen.add(c["name"])
        out.append({"domain": c["name"], "ssl_mode": c.get("source", "cert")})
    for c in storage.cert_owners():
        if c["domain"] not in seen:
            seen.add(c["domain"])
            out.append(c)
    return jsonify({"ok": True, "certs": out})


# --------------------------------------------------------------------------
# SSL Management — managed certificate registry (upload / self-signed / delete)
# --------------------------------------------------------------------------
@app.get("/api/ssl/certs")
@require_role("admin", "creator")
def ssl_list():
    certs = [{**c, "in_use": storage.cert_in_use(c["name"])} for c in storage.cert_list()]
    return jsonify({"ok": True, "certs": certs, "ssl_dir": config.SSL_DIR})


@app.post("/api/ssl/certs")
@require_role("admin", "creator")
def ssl_create():
    """Add a managed certificate: upload a cert+key, or generate a self-signed."""
    form = request.form
    mode = (form.get("mode") or "").strip().lower()
    try:
        name = clean_domain(form.get("name"))
    except ValidationError as exc:
        return _err(str(exc))
    if storage.cert_get(name):
        return _err(f"A certificate named {name} already exists.")

    steps = []
    if mode == "upload":
        cert_file = request.files.get("cert")
        key_file = request.files.get("key")
        if not (cert_file and cert_file.filename and key_file and key_file.filename):
            return _err("Upload both the certificate and the private key.")
        cert_bytes = cert_file.read()
        key_bytes = key_file.read()
        if len(cert_bytes) > config.MAX_UPLOAD_BYTES or len(key_bytes) > config.MAX_UPLOAD_BYTES:
            return _err("Uploaded file exceeds the size limit.")
        if not _looks_like_pem(cert_bytes, "CERTIFICATE"):
            return _err("Certificate does not look like a PEM/CRT file.")
        if not _looks_like_pem(key_bytes, "PRIVATE KEY"):
            return _err("Private key does not look like a PEM key file.")
        source = "upload"
    elif mode == "selfsigned":
        cert_bytes, key_bytes, step = nm.generate_self_signed(name)
        steps.append(step)
        if not step["ok"]:
            return _err(step["detail"], code=500, steps=steps)
        source = "selfsigned"
    else:
        return _err("Choose 'upload' or 'selfsigned'.")

    _c, _k, res = nm.save_certificate(name, cert_bytes, key_bytes)
    steps.append(res)
    if not res["ok"]:
        return _err(res["detail"], code=500, steps=steps)

    rec = {"name": name, "source": source, "created": _now(), **nm.cert_info(name)}
    storage.cert_add(rec)
    _audit("ssl.create", target=name, detail=source)
    return jsonify({"ok": True, "cert": {**rec, "in_use": False}, "steps": steps})


@app.delete("/api/ssl/certs/<name>")
@require_role("admin", "creator")
def ssl_delete(name):
    try:
        name = clean_domain(name)
    except ValidationError as exc:
        return _err(str(exc))
    if not storage.cert_get(name):
        return _err("No such certificate.", code=404)
    if storage.cert_in_use(name):
        return _err(f"Certificate {name} is in use by a mapping — change that "
                    "mapping's SSL to none/another cert first.")
    step = nm.remove_certificate(name)
    storage.cert_remove(name)
    _audit("ssl.delete", target=name)
    return jsonify({"ok": True, "steps": [step]})


# --------------------------------------------------------------------------
# Access lists — allow/deny CIDR lists a mapping can include (admin manages)
# --------------------------------------------------------------------------
def _acl_public(rec):
    """Trim a list record for the API (drop the full CIDR array from the index)."""
    return {
        "name": rec["name"],
        "label": rec.get("label") or rec["name"],
        "builtin": bool(rec.get("builtin")),
        "source_url": rec.get("source_url") or "",
        "include_private": bool(rec.get("include_private", True)),
        "refresh_hours": int(rec.get("refresh_hours") or 24),
        "count": rec.get("count", len(rec.get("entries") or [])),
        "last_refresh": rec.get("last_refresh"),
        "created": rec.get("created"),
        "updated": rec.get("updated"),
    }


@app.get("/api/access-lists")
def access_index():
    """Lists (for the Access Lists page and the mapping form dropdown)."""
    default = storage.settings_all().get("default_access_list", "")
    lists = [{**_acl_public(a), "in_use": bool(storage.access_in_use(a["name"]))}
             for a in storage.access_list()]
    return jsonify({"ok": True, "lists": lists, "default": default,
                    "acl_dir": config.ACL_CONF_DIR})


@app.get("/api/access-lists/<name>")
@require_role("admin")
def access_show(name):
    """Full record incl. entries — used to populate the edit form."""
    try:
        name = clean_acl_name(name)
    except ValidationError as exc:
        return _err(str(exc))
    rec = storage.access_get(name)
    if not rec:
        return _err("No such access list.", code=404)
    return jsonify({"ok": True, "list": {**rec, "in_use": bool(storage.access_in_use(name))}})


@app.post("/api/access-lists")
@require_role("admin")
def access_save():
    """Create or edit an access list, write its snippet and reload nginx."""
    form = request.form
    try:
        name = clean_acl_name(form.get("name"))
        entries = clean_cidr_list(form.get("entries"))
    except ValidationError as exc:
        return _err(str(exc))

    existing = storage.access_get(name)
    label = (form.get("label") or "").strip() or (existing or {}).get("label") or name
    source_url = (form.get("source_url") or "").strip()
    include_private = _truthy(form.get("include_private")) if "include_private" in form \
        else bool((existing or {}).get("include_private", True))
    try:
        refresh_hours = int(form.get("refresh_hours") or (existing or {}).get("refresh_hours") or 24)
    except ValueError:
        return _err("Refresh hours must be a whole number.")
    if refresh_hours < 1:
        refresh_hours = 24

    # A built-in list keeps its source/flag; only its label/private toggle and
    # (manually) entries can be touched here — refresh repopulates entries.
    builtin = bool((existing or {}).get("builtin"))
    if builtin:
        source_url = (existing or {}).get("source_url") or source_url

    rec = {
        **(existing or {}),
        "name": name,
        "label": label,
        "builtin": builtin,
        "entries": entries,
        "count": len(entries),
        "source_url": source_url,
        "include_private": include_private,
        "refresh_hours": refresh_hours,
        "created": (existing or {}).get("created", _now()),
        "updated": _now(),
    }
    rec.setdefault("last_refresh", None)
    storage.access_add(rec)
    steps = access.write_snippet(rec, reload=True)
    _audit("access.update" if existing else "access.create", target=name,
           detail=f"{len(entries)} network(s)" + (" · url" if source_url else ""))
    return jsonify({"ok": True, "list": _acl_public(rec), "steps": steps})


@app.post("/api/access-lists/<name>/refresh")
@require_role("admin")
def access_refresh(name):
    """Re-fetch an auto-refreshing list now."""
    try:
        name = clean_acl_name(name)
    except ValidationError as exc:
        return _err(str(exc))
    if not storage.access_get(name):
        return _err("No such access list.", code=404)
    ok, msg, steps = access.refresh(name)
    if not ok:
        return _err(msg, code=502, steps=steps)
    _audit("access.refresh", target=name, detail=msg)
    rec = storage.access_get(name)
    return jsonify({"ok": True, "message": msg, "list": _acl_public(rec), "steps": steps})


@app.delete("/api/access-lists/<name>")
@require_role("admin")
def access_delete(name):
    try:
        name = clean_acl_name(name)
    except ValidationError as exc:
        return _err(str(exc))
    rec = storage.access_get(name)
    if not rec:
        return _err("No such access list.", code=404)
    if rec.get("builtin"):
        return _err(f"{name} is a built-in list and can't be deleted.")
    holder = storage.access_in_use(name)
    if holder:
        return _err(f"Access list {name} is in use by {holder} — change that "
                    "mapping's access list first.")
    steps = [nm.remove_acl(name)]
    # If it was the global default, clear the default + refresh _default.conf.
    if storage.settings_all().get("default_access_list") == name:
        storage.settings_update({"default_access_list": ""})
        _p, res = nm.write_default_acl(None)
        steps.append(res)
    storage.access_remove(name)
    steps.append(nm.reload_nginx())
    _audit("access.delete", target=name)
    return jsonify({"ok": True, "steps": steps})


# --------------------------------------------------------------------------
# Create / update a mapping  (the "Save / Apply" button)
# --------------------------------------------------------------------------
@app.post("/api/mappings")
@require_role("admin", "creator")
def create_mapping():
    form = request.form
    allowed_ifaces = net_detect.interface_names()

    # Sub-interface creation is a global opt-in. When OFF (default), a mapping
    # binds nginx straight to the selected interface's existing primary IP — no
    # macvlan/VLAN/DHCP is provisioned (so the VLAN/MAC/static-IP form fields are
    # ignored). Several direct mappings may share one interface IP as long as
    # they listen on different ports.
    subiface_enabled = storage.settings_all().get("subinterface_enabled", False)

    # --- validate core fields ------------------------------------------------
    try:
        domain = clean_domain(form.get("domain"))
        # In managed mode the parent comes from the selected sub-interface, so the
        # form needn't send a (valid) interface; resolve it below from the record.
        interface = None if subiface_enabled else clean_interface(
            form.get("interface") or config.NIC,
            allowed=allowed_ifaces or None)
        lb = _parse_lb(form)               # backend pool + LB method/options/timeouts
        rate = _parse_rate(form)           # optional rate limiting
        alloc_method = (form.get("alloc_method") or "static").strip().lower()
        if alloc_method not in ("static", "dhcp"):
            raise ValidationError(f"Unknown allocation method: {alloc_method!r}")
        bind_prefix = clean_prefix(form.get("bind_prefix"), config.BIND_PREFIX)
        listen_port = clean_port(form.get("listen_port"), config.LISTEN_PORT)
        transport = clean_transport(form.get("transport"))
        vlan_id = clean_vlan_id(form.get("vlan_id"))
        mac = clean_mac(form.get("mac"))           # blank => random LAA MAC
        # The bind IP is never typed on the mapping form anymore — it comes from
        # the host interface (direct) or the selected managed sub-interface.
        bind_ip = None
    except ValidationError as exc:
        return _err(str(exc))

    # Identity is (domain, listen_port): a domain may map on several ports, each
    # its own mapping. When editing, the form carries the original identity so we
    # can allow the overwrite and detect a rename. Creating (or renaming onto) an
    # identity that already exists is rejected rather than silently clobbering it.
    orig_domain = (form.get("orig_domain") or "").strip() or None
    orig_port = None
    if form.get("orig_port"):
        try:
            orig_port = clean_port(form.get("orig_port"), config.LISTEN_PORT)
        except ValidationError:
            orig_port = None
    is_edit = bool(orig_domain and storage.get(orig_domain, orig_port))
    renamed = is_edit and (orig_domain, orig_port) != (domain, listen_port)
    if (not is_edit or renamed) and storage.exists(domain, listen_port):
        return _err(f"{domain}:{listen_port} already exists — edit that mapping, "
                    f"or use a different port for a new one.")

    # Resolve where this mapping binds — a mapping never creates a device now.
    # bind_kind drives provisioning: "direct" = an existing interface IP,
    # "managed" = an IP owned by a sub-interface from the Interfaces page.
    bind_subiface = None
    bind_kind = "direct"
    if not subiface_enabled:
        # Direct bind to the chosen physical interface's existing primary IP.
        sel = net_detect.find(interface)
        addrs = (sel or {}).get("addresses") or []
        if not addrs:
            return _err(
                f"{interface} has no IPv4 address to bind to — assign one to the "
                f"interface, or enable sub-interface creation on the Interfaces "
                f"page to provision a new IP.")
        bind_ip = addrs[0]["ip"]
        bind_prefix = str(addrs[0].get("prefix") or bind_prefix)
        alloc_method, vlan_id, mac = "static", None, None
    else:
        # The dropdown lists managed sub-interfaces AND physical interfaces. A
        # registry hit binds that sub-interface; otherwise it's a physical
        # interface and we bind its existing IP directly.
        selected = (form.get("subiface") or "").strip()
        rec = storage.subiface_get(selected) if selected else None
        if rec:
            if not rec.get("bind_ip"):
                return _err(f"Sub-interface {selected} has no IP yet — "
                            "re-provision it on the Interfaces page.")
            bind_kind, bind_subiface = "managed", selected
            interface = rec["interface"]
            bind_ip = rec["bind_ip"]
            bind_prefix = rec.get("bind_prefix") or bind_prefix
            vlan_id, mac = rec.get("vlan_id"), rec.get("mac")
            alloc_method = rec.get("alloc_method", "static")
        else:
            sel = net_detect.find(selected) if selected else None
            if not sel:
                return _err("Select a sub-interface or interface to bind to — "
                            "create a sub-interface on the Interfaces page first.")
            addrs = sel.get("addresses") or []
            if not addrs:
                return _err(f"{selected} has no IPv4 address to bind to — assign "
                            "one, or pick a sub-interface with an IP.")
            interface = selected
            bind_ip = addrs[0]["ip"]
            bind_prefix = str(addrs[0].get("prefix") or bind_prefix)
            alloc_method, vlan_id, mac = "static", None, None

    # A mapping binds an already-existing IP, so several mappings may share it as
    # long as their listen ports differ.
    if bind_ip:
        other = storage.bind_ip_in_use(
            bind_ip, exclude_domain=orig_domain or domain,
            exclude_port=orig_port if orig_domain else listen_port,
            listen_port=listen_port)
        if other:
            return _err(f"Bind {bind_ip}:{listen_port} is already used by {other}")

    existing = storage.get(orig_domain, orig_port) if is_edit else None
    has_cert = bool(existing and existing.get("has_cert"))

    # --- SSL: upload / self-signed / use-existing ---------------------------
    ssl_mode = (form.get("ssl_mode") or "none").strip().lower()
    cert_bytes = key_bytes = None
    selfsign_step = None
    # Preserve any existing cert (and its source) across updates that don't touch SSL.
    cert_domain = (existing or {}).get("cert_domain")

    if ssl_mode == "upload":
        cert_file = request.files.get("cert")
        key_file = request.files.get("key")
        if not (cert_file and cert_file.filename and key_file and key_file.filename):
            return _err("Upload both the certificate and the private key.")
        cert_bytes = cert_file.read()
        key_bytes = key_file.read()
        if len(cert_bytes) > config.MAX_UPLOAD_BYTES or len(key_bytes) > config.MAX_UPLOAD_BYTES:
            return _err("Uploaded file exceeds the size limit.")
        if not _looks_like_pem(cert_bytes, "CERTIFICATE"):
            return _err("Certificate does not look like a PEM/CRT file.")
        if not _looks_like_pem(key_bytes, "PRIVATE KEY"):
            return _err("Private key does not look like a PEM key file.")
        has_cert = True
        cert_domain = domain
    elif ssl_mode == "selfsigned":
        cert_bytes, key_bytes, selfsign_step = nm.generate_self_signed(domain)
        if not selfsign_step["ok"]:
            return _err(selfsign_step["detail"], code=500, steps=[selfsign_step])
        has_cert = True
        cert_domain = domain
    elif ssl_mode == "existing":
        try:
            sel = clean_domain(form.get("ssl_existing"))
        except ValidationError:
            return _err("Select an existing certificate.")
        if storage.cert_get(sel):
            # A certificate from the SSL-management registry.
            cert_domain = sel
            has_cert = True
        else:
            # Legacy: a cert still owned by another mapping.
            src = storage.get(sel)
            if not (src and src.get("has_cert")):
                return _err(f"No managed certificate found for {sel}.")
            cert_domain = src.get("cert_domain") or sel
        has_cert = True
    elif ssl_mode == "keep":
        # Edit without touching SSL: keep the existing cert exactly as-is.
        has_cert = bool(existing and existing.get("has_cert"))
        cert_domain = (existing or {}).get("cert_domain")
        ssl_mode = (existing or {}).get("ssl_mode", "none")

    # Re-encrypt to backend: preserve on a plain "keep" edit, else read the form.
    if (form.get("ssl_mode") or "none").strip().lower() == "keep":
        proxy_ssl = bool((existing or {}).get("proxy_ssl", True))
    else:
        proxy_ssl = _truthy(form.get("proxy_ssl"))

    # UDP is a datagram transport — nginx can't terminate TLS or read a TLS SNI
    # on it, so reject those combinations rather than silently dropping them.
    if transport == "udp":
        if has_cert:
            return _err("TLS termination isn't available for UDP — choose a TCP "
                        "protocol, or set SSL to none.")
        if _truthy(form.get("sni_guard")):
            return _err("Hostname (SNI) restriction needs TCP — it can't be used "
                        "with a UDP listener.")

    # SNI guard: only serve this exact hostname, drop any other. Uses ssl_preread,
    # so it is incompatible with terminating TLS on this listener.
    sni_guard = _truthy(form.get("sni_guard"))
    if sni_guard and has_cert:
        return _err("Hostname restriction (SNI) needs TLS passthrough — turn off "
                    "certificate termination for this mapping, or disable the "
                    "hostname restriction.")

    # Access list: "" => open, "__default__" => global default, "<name>" => list.
    access_list = (form.get("access_list") or "").strip()
    if access_list and access_list != "__default__" and not storage.access_get(access_list):
        return _err(f"No such access list: {access_list}")

    # --- assemble mapping & sub-interface naming ----------------------------
    if existing and isinstance(existing.get("subiface_index"), int):
        idx = existing["subiface_index"]
    else:
        idx = storage.allocate_subiface_index()

    # Binding was resolved above: a managed sub-interface (references it by name)
    # or a direct bind to an interface IP (no device owned).
    subiface = bind_subiface
    subiface_kind = bind_kind

    mapping = {
        "domain": domain,
        "interface": interface,              # selected parent (physical or vlan)
        "vlan_id": vlan_id,                  # 802.1Q tag, or None
        "mac": mac,                          # unique MAC of the macvlan device
        "alloc_method": alloc_method,
        "bind_ip": bind_ip,                  # filled by DHCP during apply
        "bind_prefix": bind_prefix,
        "listen_port": listen_port,          # per-mapping nginx listen port
        "transport": transport,              # tcp | udp
        "protocol": (form.get("protocol") or "").strip().lower() or None,
        "subiface": subiface,
        "subiface_index": idx,
        "subiface_kind": subiface_kind,
        "upstream_name": nm.upstream_name(domain),
        "has_cert": has_cert,
        "ssl_mode": ssl_mode,
        "cert_domain": cert_domain,
        "proxy_ssl": proxy_ssl,   # re-encrypt to backend
        "sni_guard": sni_guard,   # only serve this hostname (passthrough)
        "access_list": access_list,   # "" | "__default__" | "<name>" allow/deny list
        # Whether this app is bound to the WAF (L7 HTTP reverse proxy + ModSecurity)
        # instead of the default L4 stream proxy. Preserved across edits; toggled
        # from the WAF page's "Protected apps" list.
        "waf_bound": bool((existing or {}).get("waf_bound")),
        # Failover: which priority tier is currently active (managed by the
        # failover scheduler). Preserved across edits; None => defaults to the
        # highest-priority tier on next render.
        "active_priority": (existing or {}).get("active_priority"),
        "conf_path": nm.conf_path_for(domain, listen_port),
        # Saving a mapping always provisions it live, so an edit re-activates a
        # previously disabled mapping. Use the Disable toggle to take it offline.
        "enabled": True,
        "created": (existing or {}).get("created", _now()),
        "updated": _now(),
    }
    mapping.update(lb)   # backends, lb_method, hash_*, random_two, proxy_* timeouts
    mapping.update(rate)  # rate_limit, limit_conn, proxy_download_rate, proxy_upload_rate

    try:
        steps = nm.apply_mapping(mapping, cert_bytes, key_bytes)
    except nm.ProvisionError as exc:
        failed = [f"{s['name']}: {s.get('detail', '')}"
                  for s in (exc.steps or []) if not s.get("ok")]
        app.logger.error("apply_mapping FAILED for %s: %s | failing steps: %s",
                         mapping.get("domain"), exc, " || ".join(failed) or "(none)")
        return _err(str(exc), code=500, steps=exc.steps)

    if selfsign_step is not None:
        steps.insert(0, selfsign_step)

    # Record VLAN ownership so teardown only removes tool-created VLANs.
    if mapping.get("vlan_iface") and mapping.get("vlan_created"):
        storage.vlan_owned_add(mapping["vlan_iface"])

    storage.upsert(mapping)
    # A rename (domain and/or port changed on edit) leaves the old record and its
    # config behind — tear those down and reload so only the new identity remains.
    if renamed:
        steps.append(nm.remove_conf(orig_domain, orig_port))
        steps.append(nm.remove_http_conf(orig_domain, orig_port))
        storage.delete(orig_domain, orig_port)
        steps.append(nm.reload_nginx())
    nbackends = len(mapping.get("backends") or ([mapping["backend"]] if mapping.get("backend") else []))
    _audit("mapping.update" if existing else "mapping.create",
           target=f"{domain}:{listen_port}",
           detail=f"{mapping.get('bind_ip') or '(dhcp)'} · {nbackends} backend(s)"
                  + (f" · {mapping.get('ssl_mode')}" if mapping.get("has_cert") else ""))
    return jsonify({"ok": True, "mapping": mapping, "steps": steps})


# --------------------------------------------------------------------------
# Delete a mapping
# --------------------------------------------------------------------------
def _req_port():
    """Optional ?port= that disambiguates a domain mapped on several ports."""
    p = request.args.get("port")
    if p is None:
        return None
    try:
        return clean_port(p, config.LISTEN_PORT)
    except ValidationError:
        return None


@app.delete("/api/mappings/<domain>")
@require_role("admin")
def delete_mapping(domain):
    try:
        domain = clean_domain(domain)
    except ValidationError as exc:
        return _err(str(exc))
    port = _req_port()
    mapping = storage.get(domain, port)
    if not mapping:
        return _err("No such mapping.", code=404)
    port = mapping.get("listen_port") or config.LISTEN_PORT
    # Delete the VLAN interface only if the tool created it AND no other mapping
    # still uses it — never operator pre-existing VLANs.
    vlan_iface = mapping.get("vlan_iface")
    delete_vlan = bool(vlan_iface) and storage.vlan_owned_has(vlan_iface) and \
        not storage.vlan_iface_in_use(vlan_iface, exclude_domain=domain, exclude_port=port)

    # Remove the cert only if THIS mapping owns it (upload/self-signed) AND no
    # other mapping still reuses it — shared certs are kept.
    owns_cert = mapping.get("has_cert") and \
        (mapping.get("cert_domain") or domain) == domain and \
        mapping.get("ssl_mode") in ("upload", "selfsigned")
    remove_cert = bool(owns_cert) and config.REMOVE_CERTS_ON_DELETE and \
        not storage.cert_in_use(domain, exclude_domain=domain, exclude_port=port)

    steps = nm.deprovision_mapping(mapping, delete_vlan=delete_vlan, remove_cert=remove_cert)
    storage.delete(domain, port)
    if delete_vlan:
        storage.vlan_owned_remove(vlan_iface)
    _audit("mapping.delete", target=f"{domain}:{port}",
           detail="cert removed" if remove_cert else None)
    return jsonify({"ok": True, "steps": steps})


# --------------------------------------------------------------------------
# Enable / disable a mapping without deleting it
# --------------------------------------------------------------------------
@app.post("/api/mappings/<domain>/toggle")
@require_role("admin")
def toggle_mapping(domain):
    """Flip a mapping between active and inactive.

    Disable = remove the nginx stream config + reload, so it stops serving. The
    record, its bind IP / sub-interface and any certificate are kept intact, so
    Enable just re-applies the stored mapping (re-writes the config + reloads).
    """
    try:
        domain = clean_domain(domain)
    except ValidationError as exc:
        return _err(str(exc))
    mapping = storage.get(domain, _req_port())
    if not mapping:
        return _err("No such mapping.", code=404)
    port = mapping.get("listen_port") or config.LISTEN_PORT

    enabled = mapping.get("enabled", True)
    if enabled:
        # Disable: config-only teardown — keep the IP, sub-interface and cert.
        # Remove both possible forms (L4 stream and L7 HTTP+WAF).
        steps = [nm.remove_conf(domain, port), nm.remove_http_conf(domain, port), nm.test_config()]
        if steps[-1]["ok"]:
            steps.append(nm.reload_nginx())
        mapping["enabled"] = False
        mapping["updated"] = _now()
        storage.upsert(mapping)
        _audit("mapping.disable", target=f"{domain}:{port}")
        return jsonify({"ok": True, "enabled": False, "steps": steps})

    # Enable: re-provision (IP is idempotent), re-write the config and reload.
    try:
        steps = nm.apply_mapping(mapping)   # reuses cert files already on disk
    except nm.ProvisionError as exc:
        return _err(str(exc), code=500, steps=exc.steps)
    mapping["enabled"] = True
    mapping["updated"] = _now()
    storage.upsert(mapping)
    _audit("mapping.enable", target=f"{domain}:{port}")
    return jsonify({"ok": True, "enabled": True, "steps": steps})


# --------------------------------------------------------------------------
# Diagnose a single mapping (live status + filtered nginx error log)
# --------------------------------------------------------------------------
@app.get("/api/mappings/<domain>/diagnose")
@require_role("admin")
def diagnose_mapping(domain):
    """Live diagnostics for one mapping, polled by the UI's Diagnose panel:

    - is the listener (bind_ip:443) actually bound?
    - is each upstream backend reachable (TCP connect)?
    - how many connections are currently established to the listener?
    - recent nginx error-log lines filtered to this mapping.
    """
    try:
        domain = clean_domain(domain)
    except ValidationError as exc:
        return _err(str(exc))
    mapping = storage.get(domain, _req_port())
    if not mapping:
        return _err("No such mapping.", code=404)

    bind_ip = mapping.get("bind_ip")
    port = mapping.get("listen_port") or config.LISTEN_PORT
    udp = (mapping.get("transport") or "tcp").lower() == "udp"
    force = _truthy(request.args.get("force"))

    servers = health._mapping_servers(mapping)
    backends = [{**health._check(srv, force=force), "enabled": enabled}
                for srv, enabled in servers]

    # Filter the error log to lines naming this mapping's listen IP or upstreams.
    patterns = ([bind_ip] if bind_ip else []) + [srv for srv, _ in servers]

    return jsonify({
        "ok": True,
        "domain": domain,
        "simulate": config.SIMULATE,
        "listener": {"ip": bind_ip, "port": port, "transport": "udp" if udp else "tcp",
                     "bound": bool(bind_ip) and nm.is_listening(bind_ip, port, udp=udp)},
        # UDP is connectionless — there are no "established" connections to count.
        "connections": None if udp else nm.count_connections(bind_ip, port),
        "backends": backends,
        "logs": nm.tail_error_log(patterns),
    })


# --------------------------------------------------------------------------
# Activity / audit log (admin only)
# --------------------------------------------------------------------------
@app.get("/api/activity")
@require_role("admin")
def list_activity():
    """Recent audit events: who did what, when, from where."""
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, activity.MAX_EVENTS))
    return jsonify({"ok": True, "events": activity.list_events(limit=limit)})


# --------------------------------------------------------------------------
# Full-system backup & restore (admin) — see backup.py
# A backup zips every data store + SSL cert/key. Treat the file as a secret.
# --------------------------------------------------------------------------
@app.get("/api/backups")
@require_role("admin")
def backups_list():
    return jsonify({"ok": True, "backups": backup.list_backups(),
                    "schedule": backup.schedule_config(),
                    "dir": backup.BACKUPS_DIR})


@app.post("/api/backups")
@require_role("admin")
def backups_create():
    """Take a full-system snapshot now and store it on the server."""
    meta = backup.create(label=request.form.get("label"))
    _audit("backup.create", target=meta["name"],
           detail=f"{meta['counts'].get('mappings', 0)} mappings")
    return jsonify({"ok": True, "backup": meta})


@app.get("/api/backups/download")
@require_role("admin")
def backups_download_stored():
    """Download a stored backup by name, or a fresh one with ?now=1."""
    if _truthy(request.args.get("now")):
        raw = backup.snapshot_bytes()
        fname = f"splitter-backup-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
        _audit("backup.download", detail="fresh full backup")
        return Response(raw, mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    name = request.args.get("name") or ""
    raw = backup.path_bytes(name)
    if raw is None:
        return _err("No such backup.", code=404)
    _audit("backup.download", target=name)
    return Response(raw, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/backups/restore")
@require_role("admin")
def backups_restore():
    """Restore the system from a stored backup (by name) or an uploaded file."""
    upload = request.files.get("file")
    try:
        if upload and upload.filename:
            raw = upload.read()
            if len(raw) > app.config["MAX_CONTENT_LENGTH"]:
                return _err("Backup file exceeds the size limit.")
            result = backup.restore_bytes(raw)        # validates the archive
            backup.store_bytes(raw, label="imported")  # keep a copy in the list
            source = upload.filename
        else:
            name = request.form.get("name") or ""
            result = backup.restore_name(name)
            source = name
    except ValueError as exc:
        return _err(str(exc))
    r = result["restored"]
    _audit("backup.restore", target=source,
           detail=f"{len(r['data'])} data file(s), {len(r['ssl'])} cert file(s)")
    return jsonify({"ok": True, "restored": r, "manifest": result["manifest"]})


@app.delete("/api/backups/<name>")
@require_role("admin")
def backups_delete(name):
    if not backup.delete(name):
        return _err("No such backup.", code=404)
    _audit("backup.delete", target=name)
    return jsonify({"ok": True})


@app.post("/api/backups/schedule")
@require_role("admin")
def backups_schedule():
    """Configure automatic backups: enable, interval (hours), retention count."""
    form = request.form
    patch = {}
    if "enabled" in form:
        patch["backup_enabled"] = _truthy(form.get("enabled"))
    if form.get("interval_hours"):
        try:
            patch["backup_interval_hours"] = max(1, int(form["interval_hours"]))
        except (TypeError, ValueError):
            return _err("Interval must be a whole number of hours.")
    if form.get("retention"):
        try:
            patch["backup_retention"] = max(1, int(form["retention"]))
        except (TypeError, ValueError):
            return _err("Retention must be a whole number.")
    storage.settings_update(patch)
    _audit("backup.schedule", detail=", ".join(f"{k}={v}" for k, v in patch.items()) or "(none)")
    return jsonify({"ok": True, "schedule": backup.schedule_config()})


# --------------------------------------------------------------------------
# Backup — export / import the mappings store as JSON (legacy, mappings-only)
# --------------------------------------------------------------------------
@app.get("/api/backup")
def export_backup():
    """Download the whole mappings store as a self-describing JSON file."""
    payload = {
        "splitter_backup": True,
        "version": 1,
        "exported": _now(),
        "mappings": storage.export_all(),
    }
    body = json.dumps(payload, indent=2, sort_keys=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    _audit("mappings.export", detail=f"{len(payload['mappings'])} mapping(s)")
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="splitter-backup-{stamp}.json"'},
    )


@app.post("/api/import")
@require_role("admin")
def import_backup():
    """
    Restore mappings from an uploaded backup file. Accepts either the wrapped
    export format ({"mappings": {...}}) or a raw {domain: mapping} object.
    Existing mappings with the same domain are overwritten; the rest are kept.

    This restores the data store only — it does not re-provision IPs or rewrite
    Nginx configs. Use Edit -> Save/Apply on a mapping to re-apply it on the host.
    """
    upload = request.files.get("file")
    raw = upload.read() if (upload and upload.filename) else request.get_data()
    if not raw:
        return _err("No backup file provided.")
    if len(raw) > app.config["MAX_CONTENT_LENGTH"]:
        return _err("Backup file exceeds the size limit.")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return _err("File is not valid JSON.")

    if isinstance(data, dict) and isinstance(data.get("mappings"), dict):
        data = data["mappings"]
    if not isinstance(data, dict):
        return _err("Unrecognised backup format — expected a JSON object of mappings.")

    records = {}
    for key, m in data.items():
        if not isinstance(m, dict):
            return _err(f"Invalid mapping entry: {key!r}")
        try:
            dom = clean_domain(m.get("domain") or key)
        except ValidationError as exc:
            return _err(f"{key}: {exc}")
        m["domain"] = dom
        records[dom] = m

    if not records:
        return _err("Backup contains no mappings.")
    storage.import_merge(records)
    _audit("mappings.import", detail=f"{len(records)} mapping(s) merged")
    return jsonify({"ok": True, "imported": len(records)})


@app.post("/api/reapply")
@require_role("admin")
def reapply_all():
    """
    Re-provision every stored mapping onto the host: re-create IPs / sub-interfaces,
    rewrite the Nginx stream configs and reload. Used after an import or a redeploy
    to make Nginx actually serve the restored mappings. Best-effort per mapping —
    one failure does not stop the rest.
    """
    # Re-create managed sub-interfaces first so mappings that bind them find
    # their IP already on the host (handy after a reboot).
    sub_results = []
    for sub in storage.subiface_list():
        steps = []
        try:
            ok = nm.provision_ip(sub, steps)
            if ok:
                storage.subiface_add(sub)       # persist any DHCP-filled IP
            sub_results.append({"name": sub["name"], "ok": bool(ok), "steps": steps})
        except Exception as exc:
            sub_results.append({"name": sub["name"], "ok": False,
                                "error": str(exc), "steps": steps})

    results = []
    ok_count = 0
    for mapping in storage.list_mappings():
        # Disabled mappings stay offline — ensure no stale config is left behind.
        if not mapping.get("enabled", True):
            nm.remove_conf(mapping["domain"], mapping.get("listen_port") or config.LISTEN_PORT)
            results.append({"domain": mapping["domain"], "ok": True,
                            "skipped": "disabled", "steps": []})
            continue
        try:
            steps = nm.apply_mapping(mapping)   # uses cert files already on disk
            storage.upsert(mapping)             # persist any fields apply filled in
            results.append({"domain": mapping["domain"], "ok": True, "steps": steps})
            ok_count += 1
        except nm.ProvisionError as exc:
            results.append({"domain": mapping["domain"], "ok": False,
                            "error": str(exc), "steps": exc.steps})
        except Exception as exc:  # never let one bad record 500 the whole run
            results.append({"domain": mapping["domain"], "ok": False,
                            "error": str(exc), "steps": []})
    failed = sum(1 for r in results if not r["ok"])
    _audit("mappings.reapply", detail=f"{ok_count} applied, {failed} failed")
    return jsonify({"ok": True, "applied": ok_count,
                    "failed": failed, "results": results,
                    "subinterfaces": sub_results})


# --------------------------------------------------------------------------
# Preview the generated conf without applying
# --------------------------------------------------------------------------
@app.post("/api/preview")
def preview_conf():
    form = request.form
    try:
        domain = clean_domain(form.get("domain"))
        lb = _parse_lb(form)
        rate = _parse_rate(form)
    except ValidationError as exc:
        return _err(str(exc))
    bind_ip = (form.get("bind_ip") or "").strip() or None
    try:
        listen_port = clean_port(form.get("listen_port"), config.LISTEN_PORT)
    except ValidationError as exc:
        return _err(str(exc))
    ssl_mode = (form.get("ssl_mode") or "none").lower()
    has_cert = ssl_mode in ("upload", "selfsigned", "existing")
    cert_domain = domain
    if ssl_mode == "existing":
        try:
            cert_domain = clean_domain(form.get("ssl_existing"))
        except ValidationError:
            cert_domain, has_cert = domain, False
    mapping = {
        "domain": domain, "bind_ip": bind_ip, "has_cert": has_cert,
        "listen_port": listen_port,
        "transport": (form.get("transport") or "tcp").strip().lower(),
        "cert_domain": cert_domain, "proxy_ssl": _truthy(form.get("proxy_ssl")),
        "sni_guard": _truthy(form.get("sni_guard")),
        "access_list": (form.get("access_list") or "").strip(),
        "upstream_name": nm.upstream_name(domain),
    }
    mapping.update(lb)
    mapping.update(rate)
    conf = nm.render_conf(mapping)
    return jsonify({"ok": True, "conf": conf, "path": nm.conf_path_for(domain, listen_port)})


# ==========================================================================
# Network diagnostic tools
# ==========================================================================
import socket as _socket
import subprocess as _subprocess
import time as _time


def _valid_host(h: str) -> bool:
    return bool(h) and len(h) < 256 and bool(re.match(r'^[a-zA-Z0-9.\-_]+$', h))


def _run_cmd(cmd: list, timeout: int = 30) -> dict:
    try:
        r = _subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return {"ok": True, "output": out.strip() or "(no output)"}
    except _subprocess.TimeoutExpired:
        return {"ok": False, "output": f"Timed out after {timeout}s."}
    except FileNotFoundError:
        return {"ok": False, "output": f"Command not found: {cmd[0]}"}
    except Exception as exc:
        return {"ok": False, "output": str(exc)}


@app.post("/api/tools/ping")
@require_role("admin", "creator")
def tools_ping():
    host  = (request.form.get("host") or "").strip()
    count = max(1, min(int(request.form.get("count") or 4), 20))
    if not _valid_host(host):
        return _err("Invalid host")
    if config.SIMULATE:
        lines = [f"PING {host}: 56 data bytes"]
        for i in range(count):
            lines.append(f"64 bytes from {host}: icmp_seq={i} ttl=64 time={1.1 + i * 0.3:.1f} ms")
        lines += ["", f"--- {host} ping statistics ---",
                  f"{count} packets transmitted, {count} received, 0% packet loss, time {count * 1000}ms"]
        return jsonify({"ok": True, "output": "\n".join(lines)})
    result = _run_cmd(["ping", "-c", str(count), "-W", "3", host], timeout=count * 5 + 5)
    return jsonify(result)


@app.post("/api/tools/port")
@require_role("admin", "creator")
def tools_port():
    host = (request.form.get("host") or "").strip()
    port_s = (request.form.get("port") or "").strip()
    tout = max(1, min(int(request.form.get("timeout") or 5), 30))
    if not _valid_host(host):
        return _err("Invalid host")
    try:
        port = int(port_s)
        if not (1 <= port <= 65535):
            raise ValueError
    except (ValueError, TypeError):
        return _err("Invalid port (1–65535)")
    if config.SIMULATE:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        return jsonify({"ok": True, "output": f"[{ts}] Connecting to {host}:{port} ...\nPort {port} is OPEN  (simulated, 2 ms)"})
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    t0 = _time.time()
    try:
        with _socket.create_connection((host, port), timeout=tout):
            ms = (_time.time() - t0) * 1000
            return jsonify({"ok": True,  "output": f"[{ts}] {host}:{port}\nPort {port} is OPEN  ({ms:.0f} ms)"})
    except _socket.timeout:
        return jsonify({"ok": True,  "output": f"[{ts}] {host}:{port}\nPort {port} is CLOSED or filtered  (timeout after {tout}s)"})
    except ConnectionRefusedError:
        return jsonify({"ok": True,  "output": f"[{ts}] {host}:{port}\nPort {port} is CLOSED  (connection refused)"})
    except Exception as exc:
        return jsonify({"ok": False, "output": str(exc)})


@app.post("/api/tools/dns")
@require_role("admin", "creator")
def tools_dns():
    host   = (request.form.get("host") or "").strip()
    rtype  = (request.form.get("type") or "A").strip().upper()
    server = (request.form.get("server") or "").strip()
    if not _valid_host(host):
        return _err("Invalid host")
    if rtype not in {"A", "AAAA", "MX", "NS", "TXT", "CNAME", "PTR", "SOA"}:
        rtype = "A"
    if config.SIMULATE:
        return jsonify({"ok": True, "output": (
            f"; <<>> DiG (simulated) <<>> {rtype} {host}\n"
            f";; QUESTION SECTION:\n;{host}.\t\tIN\t{rtype}\n\n"
            f";; ANSWER SECTION:\n{host}.\t300\tIN\t{rtype}\t93.184.216.34\n\n"
            f";; Query time: 12 msec\n;; SERVER: 8.8.8.8#53"
        )})
    cmd = ["dig", "+nocomments", "+nocmd"]
    if server and _valid_host(server):
        cmd.append(f"@{server}")
    cmd += [rtype, host]
    result = _run_cmd(cmd, timeout=15)
    if not result["ok"]:
        cmd2 = ["nslookup", f"-type={rtype}", host]
        if server and _valid_host(server):
            cmd2.append(server)
        result = _run_cmd(cmd2, timeout=15)
    return jsonify(result)


@app.post("/api/tools/traceroute")
@require_role("admin", "creator")
def tools_traceroute():
    host    = (request.form.get("host") or "").strip()
    maxhops = max(1, min(int(request.form.get("maxhops") or 30), 64))
    if not _valid_host(host):
        return _err("Invalid host")
    if config.SIMULATE:
        lines = [f"traceroute to {host} ({host}), {maxhops} hops max, 60 byte packets"]
        for i in range(1, 6):
            lines.append(f" {i:2}  gw-{i}.isp.net (10.0.{i}.1)  {i * 2.5:.1f} ms  {i * 2.4:.1f} ms  {i * 2.6:.1f} ms")
        lines.append(f"  6  {host}  22.1 ms  21.9 ms  22.3 ms")
        return jsonify({"ok": True, "output": "\n".join(lines)})
    result = _run_cmd(["traceroute", "-m", str(maxhops), "-n", host], timeout=120)
    if not result["ok"]:
        result = _run_cmd(["tracert", "-h", str(maxhops), host], timeout=120)
    return jsonify(result)


@app.post("/api/tools/tcpdump")
@require_role("admin")
def tools_tcpdump():
    iface = (request.form.get("interface") or "any").strip()
    count = max(1, min(int(request.form.get("count") or 50), 500))
    filt  = (request.form.get("filter") or "").strip()
    if not re.match(r'^[a-zA-Z0-9.\-_:]+$', iface):
        return _err("Invalid interface name")
    if filt and not re.match(r'^[a-zA-Z0-9 .:\-_/()\[\]!&|]+$', filt):
        return _err("Invalid filter expression")
    if config.SIMULATE:
        import random
        lines = [
            f"tcpdump: verbose output suppressed, use -v for full decode",
            f"listening on {iface}, link-type EN10MB (Ethernet), capture size 262144 bytes",
        ]
        for _ in range(min(count, 12)):
            s = f"10.0.0.{random.randint(1,254)}.{random.randint(1024,65000)}"
            d = f"10.0.0.{random.randint(1,254)}.{random.randint(80,443)}"
            lines.append(f"{_time.strftime('%H:%M:%S')}.{random.randint(100000,999999)} IP {s} > {d}: Flags [S], seq {random.randint(1000000,9999999)}, win 65535, length 0")
        lines.append(f"\n{min(count, 12)} packets captured")
        return jsonify({"ok": True, "output": "\n".join(lines)})
    cmd = ["tcpdump", "-i", iface, "-c", str(count), "-l", "-n", "--immediate-mode"]
    if filt:
        cmd.append(filt)
    return jsonify(_run_cmd(cmd, timeout=60))


@app.post("/api/tools/whois")
@require_role("admin", "creator")
def tools_whois():
    query = (request.form.get("query") or "").strip()
    if not _valid_host(query):
        return _err("Invalid domain or IP")
    if config.SIMULATE:
        return jsonify({"ok": True, "output": (
            f"% WHOIS lookup for {query} (simulated)\n\n"
            f"Domain Name: {query.upper()}\n"
            f"Registrar: Example Registrar, Inc.\n"
            f"Registered On: 2020-01-15\n"
            f"Expires On:    2030-01-15\n"
            f"Name Server: ns1.example.com\n"
            f"Name Server: ns2.example.com\n"
            f"Status: clientTransferProhibited\n"
        )})
    result = _run_cmd(["whois", query], timeout=30)
    if not result["ok"]:
        try:
            with _socket.create_connection(("whois.iana.org", 43), timeout=10) as s:
                s.sendall((query + "\r\n").encode())
                data, chunk = b"", b"x"
                while chunk:
                    chunk = s.recv(4096)
                    data += chunk
            result = {"ok": True, "output": data.decode(errors="replace").strip()}
        except Exception as exc:
            result = {"ok": False, "output": str(exc)}
    return jsonify(result)


@app.post("/api/tools/routes")
@require_role("admin", "creator")
def tools_routes():
    if config.SIMULATE:
        return jsonify({"ok": True, "output": (
            "Routing tables\n\n"
            "Internet:\n"
            "Destination        Gateway            Flags        Netif Expire\n"
            "default            10.0.0.1           UGScg          en0\n"
            "10.0.0/24          link#4             UCS            en0\n"
            "10.0.0.1           aa:bb:cc:dd:ee:ff  UHLWIir        en0\n"
            "127.0.0.1          127.0.0.1          UH             lo0\n"
        )})
    result = _run_cmd(["netstat", "-nr"], timeout=15)
    if not result["ok"]:
        result = _run_cmd(["ip", "route"], timeout=15)
    return jsonify(result)


if __name__ == "__main__":
    os.makedirs(config.DATA_DIR, exist_ok=True)
    backup.start_scheduler()   # automatic backups when enabled in settings
    access.ensure_builtin()    # built-in tas-ix ("tasx") access list
    access.sync_all()          # materialise acl.d snippets (self-healing on boot)
    access.start_scheduler()   # background re-fetch of auto-refreshing lists
    failover.start_scheduler() # active-passive priority failover orchestration
    docker_reconcile.start_scheduler()  # keep docker-backed backends' IPs current
    firewall.sync_all()        # re-apply per-interface iptables rules (self-healing on boot)
    mode = "SIMULATION (no system commands executed)" if config.SIMULATE else "LIVE"
    print(f" * Splitter starting in {mode} mode")
    print(f" * stream.d : {config.STREAM_CONF_DIR}")
    print(f" * ssl dir  : {config.SSL_DIR}")
    print(f" * default NIC: {config.NIC}/{config.BIND_PREFIX}")
    app.run(host=config.HOST, port=config.PORT, debug=False)
