"""
Settings profiles (Snippets page → Profiles).

A profile is a named bundle of the optional per-mapping settings — rate limit,
proxy timeouts, log-format snippet, error pages and config-snippet includes.
A mapping stores only the profile *name*; `merge()` fills the profile's values
into the mapping at render time wherever the mapping itself leaves the field
empty, so a mapping with no profile (or an explicit value) behaves exactly as
before. Editing a profile re-applies every mapping that uses it (app.py).
"""
import os

import config
import storage

RATE_FIELDS = ("limit_conn", "proxy_download_rate", "proxy_upload_rate")
SCALAR_FIELDS = ("proxy_timeout", "proxy_connect_timeout", "log_format")


def snippet_include_line(snippet_name, profile_name):
    return (f"include {os.path.join(config.SNIPPET_DIR, snippet_name + '.inc')};"
            f"   # snippet: {snippet_name} (profile {profile_name})")


def merge(mapping):
    """Return a copy of `mapping` with its profile's values filled in where the
    mapping has none. Unknown / no profile => the mapping unchanged."""
    name = (mapping.get("profile") or "").strip()
    prof = storage.profile_get(name) if name else None
    if not prof:
        return mapping
    m = dict(mapping)
    if prof.get("rate_limit"):
        if not m.get("rate_limit"):
            m["rate_limit"] = True
            for k in RATE_FIELDS:
                m[k] = prof.get(k)
        else:
            for k in RATE_FIELDS:
                if not m.get(k):
                    m[k] = prof.get(k)
    # Mappings saved before profiles existed stored the built-in timeout
    # defaults explicitly; treat those as "not set" so a profile can fill them.
    defaults = {"proxy_timeout": config.PROXY_TIMEOUT,
                "proxy_connect_timeout": config.PROXY_CONNECT_TIMEOUT}
    for k in SCALAR_FIELDS:
        if (not m.get(k) or m.get(k) == defaults.get(k)) and prof.get(k):
            m[k] = prof[k]
    if not m.get("error_pages") and prof.get("error_pages"):
        m["error_pages"] = list(prof["error_pages"])
    adv = m.get("advanced_config") or ""
    extra = []
    for snip in prof.get("snippets") or []:
        if storage.cfgsnip_get(snip) and os.path.join(config.SNIPPET_DIR, snip + ".inc") not in adv:
            extra.append(snippet_include_line(snip, name))
    if extra:
        m["advanced_config"] = (adv.rstrip() + "\n\n" if adv.strip() else "") + "\n".join(extra)
    return m


def summary(prof):
    """Short human description of what a profile sets (list of strings)."""
    parts = []
    if prof.get("rate_limit"):
        bits = []
        if prof.get("limit_conn"):
            bits.append(f"{prof['limit_conn']} conn/IP")
        if prof.get("proxy_download_rate"):
            bits.append(f"down {prof['proxy_download_rate']}")
        if prof.get("proxy_upload_rate"):
            bits.append(f"up {prof['proxy_upload_rate']}")
        parts.append("rate limit " + (", ".join(bits) if bits else "on"))
    if prof.get("proxy_timeout") or prof.get("proxy_connect_timeout"):
        parts.append(f"timeouts {prof.get('proxy_timeout') or 'default'} / {prof.get('proxy_connect_timeout') or 'default'}")
    if prof.get("log_format"):
        parts.append(f"log format {prof['log_format']}")
    if prof.get("error_pages"):
        parts.append("error pages " + ", ".join(prof["error_pages"]))
    if prof.get("snippets"):
        parts.append("includes " + ", ".join(prof["snippets"]))
    return parts
