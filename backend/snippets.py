"""
Snippets — the one list a mapping (and each of its custom locations) picks
reusable settings from. Five kinds live on the Snippets page:

  ratelimit  — max connections per client IP + per-connection rates   (one per mapping)
  timeouts   — proxy_timeout / proxy_connect_timeout                    (one per mapping)
  logformat  — a log_format body (log_formats.py / storage.logfmt_*)    (one per mapping)
  errorpage  — an uploaded error page (error_pages.py)                  (many)
  config     — raw nginx directives as an include file (config_snippets) (many)

A mapping stores refs like "ratelimit:api" in `snippets`; a custom location
stores its own in `loc["snippets"]` (config / ratelimit / timeouts only).
`resolve()` turns the mapping-level refs into the fields the renderers already
understand; `location_lines()` renders a location's refs as directives. No
refs => the built-in defaults, exactly as before.
"""
import os

import config
import storage
from validators import ValidationError

KINDS = {
    "ratelimit": {"label": "Rate limit", "single": True},
    "timeouts":  {"label": "Timeouts", "single": True},
    "logformat": {"label": "Log format", "single": True},
    "errorpage": {"label": "Error page", "single": False},
    "config":    {"label": "Config snippet", "single": False},
    "logrotate": {"label": "Log rotation", "single": True},
}
LOCATION_KINDS = ("config", "ratelimit", "timeouts")
RATE_FIELDS = ("limit_conn", "proxy_download_rate", "proxy_upload_rate")
TIMEOUT_FIELDS = ("proxy_timeout", "proxy_connect_timeout")


def parse_ref(ref):
    kind, _, name = (ref or "").strip().partition(":")
    return kind, name.strip()


def exists(kind, name):
    if kind in ("ratelimit", "timeouts", "logrotate"):
        return storage.snip_get(kind, name) is not None
    if kind == "logformat":
        return storage.logfmt_get(name) is not None
    if kind == "config":
        return storage.cfgsnip_get(name) is not None
    if kind == "errorpage":
        import error_pages
        return any(e["key"] == name for e in error_pages.list_custom())
    return False


def validate_refs(raw_refs, allowed=None):
    """Clean a list of 'kind:name' refs from a form: blanks dropped, kinds
    checked, targets must exist, single-valued kinds at most once."""
    allowed = allowed or tuple(KINDS)
    out, seen_single = [], set()
    for raw in raw_refs or []:
        kind, name = parse_ref(raw)
        if not kind and not name:
            continue
        if kind not in allowed:
            raise ValidationError(f"Snippet kind {kind!r} is not allowed here.")
        if not name or not exists(kind, name):
            raise ValidationError(f"No such {KINDS[kind]['label'].lower()} snippet: {name!r}")
        if KINDS[kind]["single"]:
            if kind in seen_single:
                raise ValidationError(f"Pick only one {KINDS[kind]['label'].lower()} snippet.")
            seen_single.add(kind)
        ref = f"{kind}:{name}"
        if ref not in out:
            out.append(ref)
    return out


def include_line(name, note=""):
    return f"include {os.path.join(config.SNIPPET_DIR, name + '.inc')};   # snippet: {name}{note}"


def resolve(mapping):
    """Copy of `mapping` with its snippet refs applied as plain fields."""
    refs = mapping.get("snippets") or []
    if not refs:
        return mapping
    m = dict(mapping)
    for ref in refs:
        kind, name = parse_ref(ref)
        if kind == "ratelimit":
            rec = storage.snip_get(kind, name)
            if rec:
                m["rate_limit"] = True
                for k in RATE_FIELDS:
                    m[k] = rec.get(k)
        elif kind == "timeouts":
            rec = storage.snip_get(kind, name)
            if rec:
                for k in TIMEOUT_FIELDS:
                    m[k] = rec.get(k)
        elif kind == "logformat":
            m["log_format"] = name
        elif kind == "errorpage":
            pages = list(m.get("error_pages") or [])
            if name not in pages:
                pages.append(name)
            m["error_pages"] = pages
        elif kind == "config":
            adv = m.get("advanced_config") or ""
            if storage.cfgsnip_get(name) and os.path.join(config.SNIPPET_DIR, name + ".inc") not in adv:
                m["advanced_config"] = (adv.rstrip() + "\n" if adv.strip() else "") + include_line(name)
    return m


def location_uses_ratelimit(loc):
    return any(parse_ref(r)[0] == "ratelimit" for r in loc.get("snippets") or [])


def location_lines(loc, conn_zone):
    """Directives (unindented) for a custom location's snippet refs."""
    lines = []
    for ref in loc.get("snippets") or []:
        kind, name = parse_ref(ref)
        if kind == "config":
            if storage.cfgsnip_get(name):
                lines.append(include_line(name))
        elif kind == "ratelimit":
            rec = storage.snip_get(kind, name)
            if not rec:
                continue
            if rec.get("limit_conn"):
                lines.append(f"limit_conn {conn_zone} {rec['limit_conn']};   # snippet: {name}")
            if rec.get("proxy_download_rate"):
                lines.append(f"limit_rate {rec['proxy_download_rate']};   # snippet: {name}")
        elif kind == "timeouts":
            rec = storage.snip_get(kind, name)
            if not rec:
                continue
            if rec.get("proxy_connect_timeout"):
                lines.append(f"proxy_connect_timeout {rec['proxy_connect_timeout']};   # snippet: {name}")
            if rec.get("proxy_timeout"):
                lines.append(f"proxy_read_timeout {rec['proxy_timeout']};   # snippet: {name}")
                lines.append(f"proxy_send_timeout {rec['proxy_timeout']};")
    return lines


def describe(kind, rec):
    """One-line summary for pickers / lists."""
    if kind == "ratelimit":
        bits = []
        if rec.get("limit_conn"):
            bits.append(f"{rec['limit_conn']} conn/IP")
        if rec.get("proxy_download_rate"):
            bits.append(f"down {rec['proxy_download_rate']}")
        if rec.get("proxy_upload_rate"):
            bits.append(f"up {rec['proxy_upload_rate']}")
        return ", ".join(bits) or "no limits"
    if kind == "timeouts":
        return f"{rec.get('proxy_timeout') or 'default'} / {rec.get('proxy_connect_timeout') or 'default'}"
    if kind == "logrotate":
        bits = [f"keep {rec.get('keep_days')} days", "gzip" if rec.get("compress") else "no compression"]
        if rec.get("max_size"):
            bits.append(f"or over {rec['max_size']}")
        return ", ".join(bits)
    return rec.get("description") or ""
