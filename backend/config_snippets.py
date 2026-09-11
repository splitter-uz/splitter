"""
Config snippets (Snippets page → Config snippets).

A snippet is a named block of raw nginx directives. It is stored in the app
store AND written to `<SNIPPET_DIR>/<name>.inc`; a mapping uses it by putting
`include <that path>;` into its Advanced config or a custom location (the
mapping form's "Insert snippet" picker writes that line). Because it is a real
nginx include, editing the snippet changes every mapping that includes it on
the next reload — this module reloads nginx itself (with nginx -t first and a
rollback of the file on failure) whenever a snippet in use changes.
"""
import os
import re
import tempfile

import config
import nginx_manager as nm
import storage

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
SCOPES = ("server", "location", "any")
MAX_CONTENT = 8000

PRESETS = [
    {"name": "security_headers", "scope": "server", "label": "Security headers",
     "description": "Common hardening headers on every response.",
     "content": "add_header X-Frame-Options SAMEORIGIN always;\n"
                "add_header X-Content-Type-Options nosniff always;\n"
                "add_header Referrer-Policy strict-origin-when-cross-origin always;\n"
                "add_header Permissions-Policy \"camera=(), microphone=(), geolocation=()\" always;"},
    {"name": "gzip", "scope": "server", "label": "gzip compression",
     "description": "Compress text responses for clients that accept it.",
     "content": "gzip on;\ngzip_vary on;\ngzip_min_length 1024;\ngzip_comp_level 5;\n"
                "gzip_types text/plain text/css application/json application/javascript "
                "text/xml application/xml image/svg+xml;"},
    {"name": "upload_100m", "scope": "server", "label": "Large uploads (100 MB)",
     "description": "Raise the request body limit and give slow uploads more time.",
     "content": "client_max_body_size 100m;\nclient_body_timeout 120s;\nproxy_request_buffering off;"},
    {"name": "no_buffering", "scope": "any", "label": "Streaming / SSE (no buffering)",
     "description": "Pass responses through immediately — server-sent events, chunked streams.",
     "content": "proxy_buffering off;\nproxy_cache off;\nproxy_read_timeout 3600s;\n"
                "proxy_set_header Connection \"\";\nchunked_transfer_encoding on;"},
    {"name": "static_cache", "scope": "location", "label": "Cache static assets",
     "description": "Long browser cache for images, fonts, CSS and JS under this location.",
     "content": "expires 30d;\nadd_header Cache-Control \"public, max-age=2592000, immutable\";\n"
                "access_log off;"},
    {"name": "cors_open", "scope": "location", "label": "CORS (allow any origin)",
     "description": "Answer preflights and allow cross-origin calls to this path.",
     "content": "add_header Access-Control-Allow-Origin * always;\n"
                "add_header Access-Control-Allow-Methods \"GET, POST, PUT, DELETE, OPTIONS\" always;\n"
                "add_header Access-Control-Allow-Headers \"Authorization, Content-Type\" always;\n"
                "if ($request_method = OPTIONS) { return 204; }"},
    {"name": "deny_dotfiles", "scope": "server", "label": "Block dot-files",
     "description": "Refuse /.git, /.env and any other hidden path.",
     "content": "location ~ /\\. {\n    deny all;\n    return 404;\n}"},
    {"name": "real_ip_cloudflare", "scope": "server", "label": "Real client IP behind Cloudflare",
     "description": "Trust CF-Connecting-IP from Cloudflare's ranges so logs and ACLs see the visitor.",
     "content": "real_ip_header CF-Connecting-IP;\n"
                "set_real_ip_from 173.245.48.0/20;\nset_real_ip_from 103.21.244.0/22;\n"
                "set_real_ip_from 103.22.200.0/22;\nset_real_ip_from 103.31.4.0/22;\n"
                "set_real_ip_from 141.101.64.0/18;\nset_real_ip_from 108.162.192.0/18;\n"
                "set_real_ip_from 190.93.240.0/20;\nset_real_ip_from 188.114.96.0/20;\n"
                "set_real_ip_from 197.234.240.0/22;\nset_real_ip_from 198.41.128.0/17;\n"
                "set_real_ip_from 162.158.0.0/15;\nset_real_ip_from 104.16.0.0/13;\n"
                "set_real_ip_from 104.24.0.0/14;\nset_real_ip_from 172.64.0.0/13;\n"
                "set_real_ip_from 131.0.72.0/22;"},
]


def path_for(name):
    return os.path.join(config.SNIPPET_DIR, f"{name}.inc")


def include_line(name):
    return f"include {path_for(name)};   # snippet: {name}"


def usage(name):
    """Mappings using this snippet — selected on the mapping or a location,
    or included by hand (matched on its file path)."""
    return storage.cfgsnip_usage(path_for(name), ref=f"config:{name}")


def render_file(rec):
    body = (rec.get("content") or "").rstrip() + "\n"
    return (f"# Managed by Splitter — config snippet '{rec['name']}' "
            f"({rec.get('scope', 'any')}). Edit it on the Snippets page.\n{body}")


def _read_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _write_file(path, text):
    os.makedirs(config.SNIPPET_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.SNIPPET_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def write_snippet(rec):
    """Write the snippet's include file. If a live mapping includes it, the
    change is validated with nginx -t and reloaded; on failure the previous
    file content is restored and (ok=False, steps) is returned."""
    path = path_for(rec["name"])
    text = render_file(rec)
    steps = []
    if config.SIMULATE:
        steps.append(nm.StepResult("Write snippet file", True,
                                   f"[simulated] would write {path}\n\n{text}", simulated=True))
        return True, steps
    previous = _read_file(path)
    try:
        _write_file(path, text)
        steps.append(nm.StepResult("Write snippet file", True, f"wrote {path}"))
    except OSError as exc:
        steps.append(nm.StepResult("Write snippet file", False, str(exc)))
        return False, steps

    live = [m for m in usage(rec["name"]) if m.get("enabled", True)]
    if not live:
        return True, steps   # nothing includes it yet — nothing to validate
    test = nm.test_config()
    steps.append(test)
    if not test["ok"]:
        try:
            if previous is None:
                os.remove(path)
            else:
                _write_file(path, previous)
        except OSError:
            pass
        steps.append(nm.StepResult("Roll back snippet file", True,
                                   "restored the previous content after failed nginx -t"))
        return False, steps
    reload = nm.reload_nginx()
    steps.append(reload)
    return bool(reload["ok"]), steps


def remove_snippet_file(name):
    path = path_for(name)
    if config.SIMULATE:
        return nm.StepResult("Remove snippet file", True, f"[simulated] would remove {path}", simulated=True)
    try:
        if os.path.exists(path):
            os.remove(path)
            return nm.StepResult("Remove snippet file", True, f"removed {path}")
        return nm.StepResult("Remove snippet file", True, f"{path} not present")
    except OSError as exc:
        return nm.StepResult("Remove snippet file", False, str(exc))


def sync_files():
    """Make sure every stored snippet has its include file (e.g. after a
    restore into a fresh conf.d). Never raises; returns names written."""
    written = []
    if config.SIMULATE:
        return written
    for rec in storage.cfgsnip_list():
        path = path_for(rec["name"])
        text = render_file(rec)
        if _read_file(path) != text:
            try:
                _write_file(path, text)
                written.append(rec["name"])
            except OSError:
                pass
    return written
