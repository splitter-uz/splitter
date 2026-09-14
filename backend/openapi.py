"""
OpenAPI 3 description of the REST API, generated from the live Flask route
table so it can never drift from what is actually registered: every /api/
rule contributes an operation with its methods, path parameters, the roles
its @require_role gate demands, and its handler's docstring. The parts a
route table can't tell you — request-body fields, query parameters, plain-
English summaries for handlers without docstrings — come from the curated
META table below (field names mirror what the handlers read from
request.form / request.args). Served at /api/openapi.json and rendered by
Swagger UI at /api/docs.
"""
import re

TITLE = "Splitter API"
VERSION = "0.2.0"

# (METHOD, rule) -> {summary, params: [(name, type, description, required)],
#                    query: [...], files: [...], tags: [...]}
# `params` are form fields (application/x-www-form-urlencoded, or multipart
# when `files` is present). Types: string | integer | boolean | array.
S, I, B, A = "string", "integer", "boolean", "array"
META = {
    # --- auth & accounts ---------------------------------------------------
    ("POST", "/api/setup"): {"summary": "Create the initial admin (first run only)",
        "params": [("username", S, "3–32 characters", True), ("password", S, "8+ characters", True)]},
    ("POST", "/api/login"): {"summary": "Log in (starts a cookie session)",
        "params": [("username", S, "", True), ("password", S, "", True)]},
    ("POST", "/api/logout"): {"summary": "End the session"},
    ("GET", "/api/auth/status"): {"summary": "Current session, if any"},
    ("GET", "/api/users"): {"summary": "List users"},
    ("POST", "/api/users"): {"summary": "Create a user",
        "params": [("username", S, "", True), ("password", S, "", True), ("role", S, "admin | creator | viewer (default creator)", False)]},
    ("POST", "/api/users/<username>"): {"summary": "Change a user's password and/or role",
        "params": [("password", S, "new password (optional)", False), ("role", S, "admin | creator | viewer (optional)", False)]},
    ("DELETE", "/api/users/<username>"): {"summary": "Delete a user (the last admin is protected)"},
    ("POST", "/api/account/password"): {"summary": "Change your own password",
        "params": [("current_password", S, "", True), ("new_password", S, "", True)]},
    # --- config / settings / network -----------------------------------------
    ("GET", "/api/config"): {"summary": "Effective configuration, mode and paths"},
    ("GET", "/api/interfaces"): {"summary": "Detected host interfaces (physical, VLAN, bridges)"},
    ("GET", "/api/interfaces/traffic"): {"summary": "Live per-interface throughput tree"},
    ("GET", "/api/settings"): {"summary": "Tool-wide settings"},
    ("POST", "/api/settings"): {"summary": "Change tool-wide settings (only the fields sent are changed)",
        "params": [("subinterface_enabled", B, "mappings create a sub-interface (on) or bind the interface IP (off)", False),
                   ("default_access_list", S, "access list applied to mappings set to 'use global default' ('' = none)", False),
                   ("log_keep_days", I, "default log retention in days (1–365)", False),
                   ("log_compress", B, "gzip rotated logs", False),
                   ("log_max_size", S, "also rotate when a log exceeds this size, e.g. 100M ('' = daily only)", False)]},
    ("GET", "/api/network/dns"): {"summary": "Host DNS servers (resolv.conf)"},
    ("POST", "/api/network/dns"): {"summary": "Write host DNS servers",
        "params": [("servers", A, "one or more nameserver IPs (repeat the field)", True)]},
    ("GET", "/api/network/hosts"): {"summary": "Host /etc/hosts contents"},
    ("POST", "/api/network/hosts"): {"summary": "Write /etc/hosts", "params": [("text", S, "full file contents", True)]},
    ("GET", "/api/random-mac"): {"summary": "Generate a random locally-administered MAC"},
    ("GET", "/api/subinterfaces"): {"summary": "Managed sub-interfaces"},
    ("POST", "/api/subinterfaces"): {"summary": "Create a managed macvlan/ipvlan sub-interface",
        "params": [("interface", S, "parent interface", True), ("label", S, "", False), ("kind", S, "macvlan | ipvlan", False),
                   ("vlan_id", I, "802.1Q tag (optional)", False), ("mac", S, "blank = random", False),
                   ("bind_ip", S, "static IP (blank = DHCP)", False), ("bind_prefix", I, "CIDR prefix, default 24", False)]},
    ("POST", "/api/subinterfaces/<name>"): {"summary": "Edit a sub-interface",
        "params": [("label", S, "", False), ("vlan_id", I, "", False), ("mac", S, "", False), ("bind_ip", S, "", False), ("bind_prefix", I, "", False)]},
    ("DELETE", "/api/subinterfaces/<name>"): {"summary": "Delete a sub-interface (refused while a mapping binds it)"},
    # --- mappings ----------------------------------------------------------
    ("GET", "/api/mappings"): {"summary": "List every mapping (Stream, Reverse Proxy, Docker-backed)"},
    ("POST", "/api/mappings"): {"summary": "Create or update a mapping and provision it live", "files": ["cert", "key"],
        "params": [("domain", S, "hostname clients connect to", True), ("listen_port", I, "default 443", False),
                   ("orig_domain", S, "when editing: the mapping's current domain", False), ("orig_port", I, "when editing: the mapping's current listen port", False),
                   ("transport", S, "tcp | udp", False), ("protocol", S, "preset name (https, http, mysql, dns, custom-tcp, …)", False),
                   ("interface", S, "parent interface to bind", True), ("subiface", S, "managed sub-interface name (when sub-interface creation is on)", False),
                   ("vlan_id", I, "", False), ("mac", S, "", False), ("alloc_method", S, "dhcp | static", False), ("bind_ip", S, "", False), ("bind_prefix", I, "", False),
                   ("backends", A, "host:port, repeat the field for a pool (or backends_json)", True),
                   ("backends_json", S, "JSON list of {server, weight, down, priority}", False),
                   ("lb_method", S, "round_robin | least_conn | hash | random", False), ("hash_key", S, "e.g. $remote_addr", False),
                   ("hash_consistent", B, "", False), ("random_two", B, "", False), ("failover", B, "active-passive priority tiers", False),
                   ("health_check", B, "HTTP health probe instead of TCP connect", False), ("health_path", S, "path or full URL", False),
                   ("health_scheme", S, "http | https", False), ("health_expect", I, "expected status (blank = 2xx/3xx)", False),
                   ("snippets", A, "snippet refs, repeat the field: ratelimit:<n>, timeouts:<n>, logformat:<n>, logrotate:<n>, errorpage:<key>, config:<n>", False),
                   ("locations_json", S, "JSON list of custom locations: [{path, config, backends:[host:port], methods:[POST,…], snippets:[config:<n>, ratelimit:<n>, timeouts:<n>]}]", False),
                   ("websocket_upgrade", B, "", False), ("http2", B, "", False), ("proxy_http11", B, "", False),
                   ("ssl_forced", B, "redirect :80 to this listener (Reverse Proxy)", False), ("hsts_enabled", B, "", False), ("hsts_subdomains", B, "", False),
                   ("advanced_config", S, "raw nginx for the L7 server block", False),
                   ("ssl_mode", S, "none | existing | upload | selfsigned", False), ("ssl_existing", S, "managed certificate name (ssl_mode=existing)", False),
                   ("proxy_ssl", B, "re-encrypt to the backend", False), ("sni_guard", B, "only accept this hostname's SNI (passthrough)", False),
                   ("access_list", S, "'' = allow all, __default__ = global default, or a list name", False)]},
    ("DELETE", "/api/mappings/<domain>"): {"summary": "Deprovision and delete a mapping", "query": [("port", I, "listen port, when the domain is mapped on several", False)]},
    ("POST", "/api/mappings/<domain>/toggle"): {"summary": "Disable (remove the nginx block) or re-enable a mapping", "query": [("port", I, "", False)]},
    ("GET", "/api/mappings/<domain>/diagnose"): {"summary": "Live status + filtered nginx error log for one mapping", "query": [("port", I, "", False), ("force", B, "bypass the probe cache", False)]},
    ("POST", "/api/preview"): {"summary": "Render a mapping's nginx config without applying it",
        "params": [("domain", S, "", True), ("listen_port", I, "", False), ("transport", S, "", False), ("bind_ip", S, "", False),
                   ("backends", A, "host:port (repeat)", True), ("snippets", A, "snippet refs (repeat)", False), ("locations_json", S, "", False),
                   ("l7", B, "1 = render the Reverse Proxy (HTTP) server block instead of the stream block", False),
                   ("ssl_mode", S, "", False), ("ssl_existing", S, "", False), ("proxy_ssl", B, "", False), ("sni_guard", B, "", False), ("access_list", S, "", False)]},
    ("POST", "/api/reapply"): {"summary": "Re-provision every stored mapping onto the host"},
    ("GET", "/api/health"): {"summary": "Cached per-backend up/down rollup for every mapping", "query": [("force", B, "re-probe now", False)]},
    ("GET", "/api/traffic"): {"summary": "Live connection counts per mapping"},
    ("GET", "/api/metrics"): {"summary": "Host CPU / memory / disk / network"},
    ("GET", "/api/nginx/status"): {"summary": "nginx stub_status counters (http layer) with per-second rates"},
    ("POST", "/api/nginx/status/provision"): {"summary": "(Re)write the loopback stub_status server block and reload nginx", "params": [("force", B, "rewrite even if unchanged", False)]},
    # --- docker / forward proxy ------------------------------------------------
    ("GET", "/api/docker/status"): {"summary": "Docker Engine reachability and Swarm role"},
    ("GET", "/api/docker/containers"): {"summary": "Running containers usable as backends"},
    ("GET", "/api/docker/services"): {"summary": "Swarm services usable as backends (manager only)"},
    ("GET", "/api/forward-proxies"): {"summary": "Outbound forward proxies"},
    ("POST", "/api/forward-proxies"): {"summary": "Create a forward proxy",
        "params": [("name", S, "", True), ("label", S, "", False), ("bind_ip", S, "", True), ("listen_port", I, "", True),
                   ("allow_all", B, "open relay (else allowed_domains)", False), ("allowed_domains", S, "domain patterns, one per line", False), ("access_list", S, "", False)]},
    ("POST", "/api/forward-proxies/<name>"): {"summary": "Edit a forward proxy", "params": [("label", S, "", False), ("bind_ip", S, "", False), ("listen_port", I, "", False), ("allow_all", B, "", False), ("allowed_domains", S, "", False), ("access_list", S, "", False)]},
    ("POST", "/api/forward-proxies/<name>/toggle"): {"summary": "Enable / disable a forward proxy"},
    ("DELETE", "/api/forward-proxies/<name>"): {"summary": "Delete a forward proxy"},
    # --- ssl / access lists ----------------------------------------------------
    ("GET", "/api/certs"): {"summary": "Certificates a mapping can reuse (dropdown source)"},
    ("GET", "/api/ssl/certs"): {"summary": "Managed certificates"},
    ("POST", "/api/ssl/certs"): {"summary": "Add a certificate: upload, self-sign or request from Let's Encrypt", "files": ["cert", "key"],
        "params": [("mode", S, "upload | selfsigned | letsencrypt", True), ("name", S, "certificate / primary domain", True),
                   ("extra_domains", S, "additional SANs, comma separated", False), ("email", S, "Let's Encrypt account email", False),
                   ("bind_ip", S, "IP for the HTTP-01 challenge listener", False), ("staging", B, "use the Let's Encrypt staging CA", False)]},
    ("POST", "/api/ssl/certs/<name>/renew"): {"summary": "Renew a Let's Encrypt certificate now"},
    ("DELETE", "/api/ssl/certs/<name>"): {"summary": "Delete a certificate (refused while a mapping uses it)"},
    ("GET", "/api/access-lists"): {"summary": "Access lists"},
    ("GET", "/api/access-lists/<name>"): {"summary": "One access list with its entries"},
    ("POST", "/api/access-lists"): {"summary": "Create or update an access list",
        "params": [("name", S, "", True), ("label", S, "", False), ("entries", S, "CIDRs, one per line", False),
                   ("source_url", S, "fetch entries from this URL", False), ("refresh_hours", I, "auto-refresh interval", False), ("include_private", B, "also allow RFC1918 ranges", False)]},
    ("POST", "/api/access-lists/<name>/refresh"): {"summary": "Re-fetch a sourced list now"},
    ("DELETE", "/api/access-lists/<name>"): {"summary": "Delete an access list (refused while in use)"},
    # --- snippets ------------------------------------------------------------
    ("GET", "/api/snippets"): {"summary": "Snippet catalogue for the mapping picker, by kind"},
    ("GET", "/api/snippets/<kind>"): {"summary": "Snippets of one generic kind (ratelimit | timeouts | logrotate)"},
    ("POST", "/api/snippets/<kind>"): {"summary": "Create / update a rate-limit, timeouts or log-rotation snippet",
        "params": [("name", S, "", True), ("description", S, "", False),
                   ("limit_conn", I, "ratelimit: max connections per client IP", False), ("proxy_download_rate", S, "ratelimit: e.g. 1m", False), ("proxy_upload_rate", S, "ratelimit: stream only, e.g. 512k", False),
                   ("proxy_timeout", S, "timeouts: e.g. 10m", False), ("proxy_connect_timeout", S, "timeouts: e.g. 5s", False),
                   ("keep_days", I, "logrotate: 1–365", False), ("compress", B, "logrotate: gzip", False), ("max_size", S, "logrotate: e.g. 100M", False)]},
    ("DELETE", "/api/snippets/<kind>/<name>"): {"summary": "Delete a snippet (409 while a mapping or location uses it)"},
    ("GET", "/api/log-formats"): {"summary": "Log-format snippets, presets and available variables"},
    ("POST", "/api/log-formats"): {"summary": "Create / update a log format (re-applies mappings using it)",
        "params": [("name", S, "", True), ("context", S, "stream | http", True), ("escape", S, "default | json | none", False), ("format", S, "log_format body; each line becomes one quoted string", True), ("description", S, "", False)]},
    ("DELETE", "/api/log-formats/<name>"): {"summary": "Delete a log format (409 while in use)"},
    ("GET", "/api/config-snippets"): {"summary": "Config snippets (raw nginx include files) and presets"},
    ("POST", "/api/config-snippets"): {"summary": "Create / update a config snippet (validated + reloaded if a live mapping includes it)",
        "params": [("name", S, "", True), ("scope", S, "server | location | any", False), ("content", S, "raw nginx directives", True), ("description", S, "", False)]},
    ("DELETE", "/api/config-snippets/<name>"): {"summary": "Delete a config snippet and its include file (409 while in use)"},
    ("GET", "/api/error-pages"): {"summary": "Uploaded error pages (with the mappings using each)"},
    ("POST", "/api/error-pages"): {"summary": "Upload a custom error page for a status code or range", "files": ["file"],
        "params": [("key", S, "e.g. 404 or 400-499", True), ("html", S, "page source (or upload `file`)", False)]},
    ("DELETE", "/api/error-pages/<key>"): {"summary": "Delete an error page (409 while a mapping selects it)"},
    ("GET", "/api/error-pages/<key>/preview"): {"summary": "Render the uploaded page with example context (HTML)"},
    # --- logs ----------------------------------------------------------------
    ("GET", "/api/logs"): {"summary": "Every mapping's log files, archives and rotation policy"},
    ("GET", "/api/logs/<domain>/<int:port>/<kind>"): {"summary": "Tail a mapping's access or error log",
        "query": [("lines", I, "10–5000, default 500", False), ("q", S, "substring filter", False)]},
    ("GET", "/api/logs/<domain>/<int:port>/<kind>/search"): {"summary": "Lines in a time window across the live log and rotated archives (.gz included)",
        "query": [("from", S, "ISO 8601, default one hour before `to`", False), ("to", S, "ISO 8601, default now", False), ("q", S, "substring filter", False), ("limit", I, "50–10000, default 2000", False)]},
    ("GET", "/api/logs/<domain>/<int:port>/<kind>/files"): {"summary": "The live log and its rotated archives, oldest first"},
    ("GET", "/api/logs/<domain>/<int:port>/<kind>/download"): {"summary": "Download the live log, or one archive with ?file=", "query": [("file", S, "archive name from /files", False)]},
    ("POST", "/api/logs/rotate"): {"summary": "Run logrotate now", "params": [("force", B, "rotate even if not due", False)]},
    ("GET", "/api/logs/rotation"): {"summary": "Rotation status, defaults and the generated logrotate config"},
    ("GET", "/api/activity"): {"summary": "Audit log (newest first)", "query": [("limit", I, "", False)]},
    # --- waf / firewall / backups / system ---------------------------------------
    ("GET", "/api/waf/status"): {"summary": "WAF install state, mode and settings"},
    ("POST", "/api/waf/install"): {"summary": "Install / repair ModSecurity + OWASP CRS"},
    ("GET", "/api/waf/apps"): {"summary": "Mappings and their WAF eligibility / bind state"},
    ("POST", "/api/waf/bind"): {"summary": "Bind a mapping behind the WAF (switch it to the L7 reverse proxy)", "params": [("domain", S, "", True), ("port", I, "", False)]},
    ("POST", "/api/waf/unbind"): {"summary": "Unbind a mapping (back to the L4 stream proxy)", "params": [("domain", S, "", True), ("port", I, "", False)]},
    ("POST", "/api/waf/mode"): {"summary": "Switch the ModSecurity engine mode", "params": [("mode", S, "Off | DetectionOnly | On", True)]},
    ("POST", "/api/waf/settings"): {"summary": "Change the WAF listener settings", "params": [("port", I, "WAF listener port (default 8443)", False), ("login_rate", S, "e.g. 10r/m", False), ("api_rate", S, "e.g. 30r/s", False)]},
    ("GET", "/api/firewall/overview"): {"summary": "Per-interface firewall state"},
    ("GET", "/api/firewall/whoami"): {"summary": "The caller's source IP as seen by the host (lockout protection)"},
    ("POST", "/api/firewall/settings"): {"summary": "Global firewall master switch", "params": [("enabled", B, "", True)]},
    ("POST", "/api/firewall/interfaces/<name>"): {"summary": "Per-interface enforce toggle and default policies",
        "params": [("enabled", B, "", False), ("inbound_policy", S, "accept | drop | reject", False), ("outbound_policy", S, "accept | drop | reject", False)]},
    ("GET", "/api/firewall/rules"): {"summary": "Firewall rules", "query": [("interface", S, "filter by interface", False)]},
    ("POST", "/api/firewall/rules"): {"summary": "Create a rule",
        "params": [("interface", S, "", True), ("direction", S, "in | out", True), ("protocol", S, "tcp | udp | icmp | all", True),
                   ("port_range", S, "e.g. 22 or 8000-9000", False), ("source", S, "CIDR", False), ("action", S, "accept | drop | reject", True),
                   ("priority", I, "lower runs first", False), ("description", S, "", False), ("enabled", B, "", False)]},
    ("POST", "/api/firewall/rules/<rule_id>"): {"summary": "Edit a rule"},
    ("DELETE", "/api/firewall/rules/<rule_id>"): {"summary": "Delete a rule"},
    ("POST", "/api/firewall/panic"): {"summary": "Tear down every managed chain immediately"},
    ("GET", "/api/backups"): {"summary": "Stored full-system snapshots and the schedule"},
    ("POST", "/api/backups"): {"summary": "Take a full-system snapshot now", "params": [("label", S, "", False)]},
    ("GET", "/api/backups/download"): {"summary": "Download a stored snapshot (?name=) or a fresh one (?now=1)", "query": [("name", S, "", False), ("now", B, "", False)]},
    ("POST", "/api/backups/restore"): {"summary": "Restore a stored snapshot (name) or an uploaded zip (file)", "files": ["file"], "params": [("name", S, "", False)]},
    ("DELETE", "/api/backups/<name>"): {"summary": "Delete a snapshot"},
    ("POST", "/api/backups/schedule"): {"summary": "Automatic snapshots", "params": [("enabled", B, "", True), ("interval_hours", I, "", False), ("retention", I, "snapshots to keep", False)]},
    ("GET", "/api/backup"): {"summary": "Export the mappings store as JSON"},
    ("POST", "/api/import"): {"summary": "Import a mappings JSON export (then Re-apply)", "files": ["file"]},
    ("POST", "/api/system/reboot"): {"summary": "Reboot the host (native install only)"},
    # --- tools ---------------------------------------------------------------
    ("POST", "/api/tools/ping"): {"summary": "ping", "params": [("host", S, "", True), ("count", I, "1–20, default 4", False)]},
    ("POST", "/api/tools/port"): {"summary": "TCP connect test", "params": [("host", S, "", True), ("port", I, "", True), ("timeout", I, "seconds, 1–30", False)]},
    ("POST", "/api/tools/portscan"): {"summary": "nmap port scan (pure-Python TCP connect fallback)",
        "params": [("target", S, "host, IP, CIDR or range (10.0.0.1-50)", True), ("ports", S, "top100 | top1000 | all | 22,80,8000-8100", False),
                   ("scan_type", S, "connect | syn | udp | ping", False), ("timing", I, "0–5", False), ("version", B, "-sV", False), ("no_ping", B, "-Pn", False)]},
    ("POST", "/api/tools/dns"): {"summary": "dig", "params": [("host", S, "", True), ("type", S, "A | AAAA | MX | NS | TXT | CNAME | PTR | SOA", False), ("server", S, "DNS server", False)]},
    ("POST", "/api/tools/traceroute"): {"summary": "traceroute", "params": [("host", S, "", True), ("maxhops", I, "1–64", False)]},
    ("POST", "/api/tools/tcpdump"): {"summary": "Packet capture (admin)", "params": [("interface", S, "default any", False), ("count", I, "1–500", False), ("filter", S, "BPF expression", False)]},
    ("POST", "/api/tools/whois"): {"summary": "whois", "params": [("query", S, "domain or IP", True)]},
    ("POST", "/api/tools/routes"): {"summary": "Host routing tables"},
    ("POST", "/api/tools/sslcheck"): {"summary": "Inspect a host's TLS certificate or a managed one", "params": [("mode", S, "external | own", False), ("host", S, "", False), ("port", I, "default 443", False), ("cert_name", S, "managed certificate (mode=own)", False)]},
}

_PUBLIC = {"/api/setup", "/api/login", "/api/auth/status"}
_TAG_ORDER = ["auth", "users", "account", "config", "settings", "network", "interfaces", "subinterfaces", "random-mac",
              "mappings", "preview", "reapply", "health", "traffic", "metrics", "nginx", "docker", "forward-proxies",
              "certs", "ssl", "access-lists", "snippets", "log-formats", "config-snippets", "error-pages",
              "logs", "activity", "waf", "firewall", "backups", "backup", "import", "system", "tools"]
_TAG_DESC = {
    "mappings": "Stream / Reverse Proxy mappings — create, provision, toggle, delete.",
    "snippets": "Reusable settings picked per mapping: rate limits, timeouts, log rotation (this catalogue), log formats, config snippets, error pages (their own tags).",
    "logs": "Per-mapping nginx logs: tail, time-range search across rotated archives, downloads, rotation.",
    "tools": "Network diagnostics run on the host (admin / creator).",
}


def _tag(rule):
    parts = rule.split("/")
    return parts[2] if len(parts) > 2 else "root"


def _path_params(rule_obj):
    out = []
    for arg in rule_obj.arguments:
        conv = rule_obj._converters.get(arg)
        typ = "integer" if conv and conv.__class__.__name__ == "IntegerConverter" else "string"
        desc = {"domain": "mapping domain", "port": "listen port", "kind": "access | error (logs) or ratelimit | timeouts | logrotate (snippets)",
                "name": "item name", "key": "error-page key, e.g. 404 or 400-499", "username": "", "rule_id": "firewall rule id"}.get(arg, "")
        out.append({"name": arg, "in": "path", "required": True, "schema": {"type": typ}, "description": desc})
    return out


def _schema(typ):
    if typ == "array":
        return {"type": "array", "items": {"type": "string"}}
    return {"type": typ}


def _roles_line(roles, public):
    if public:
        return "No session required."
    if not roles:
        return "Requires a session (any role)."
    return "Requires role: " + " or ".join(roles) + "."


def build(app):
    paths, tags = {}, set()
    for rule_obj in app.url_map.iter_rules():
        rule = rule_obj.rule
        if not rule.startswith("/api/") or rule in ("/api/openapi.json", "/api/docs"):
            continue
        fn = app.view_functions.get(rule_obj.endpoint)
        roles = tuple(getattr(fn, "_roles", ()) or ())
        doc = (fn.__doc__ or "").strip()
        for method in sorted(m for m in rule_obj.methods if m not in ("HEAD", "OPTIONS")):
            meta = META.get((method, rule), {})
            tag = _tag(rule)
            tags.add(tag)
            summary = meta.get("summary") or (doc.splitlines()[0].rstrip(".") if doc else rule_obj.endpoint.replace("_", " "))
            description = "\n\n".join(x for x in (doc, _roles_line(roles, rule in _PUBLIC)) if x)
            op = {"tags": [tag], "summary": summary[:120], "description": description,
                  "operationId": f"{method.lower()}_{rule_obj.endpoint}",
                  "parameters": _path_params(rule_obj) + [
                      {"name": n, "in": "query", "required": req, "schema": _schema(t), "description": d}
                      for n, t, d, req in meta.get("query", [])],
                  "responses": {
                      "200": {"description": "OK — `ok: true` plus the endpoint's payload (`steps[]` on provisioning calls).",
                              "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Ok"}}}},
                      "400": {"description": "Validation error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                      "401": {"description": "Not logged in", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                      "403": {"description": "Role not allowed", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                      "404": {"description": "No such item", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                  }}
            if method in ("POST", "PUT") and (meta.get("params") or meta.get("files")):
                props, required = {}, []
                for n, t, d, req in meta.get("params", []):
                    props[n] = {**_schema(t), "description": d}
                    if req:
                        required.append(n)
                for f in meta.get("files", []):
                    props[f] = {"type": "string", "format": "binary"}
                schema = {"type": "object", "properties": props}
                if required:
                    schema["required"] = required
                ctype = "multipart/form-data" if meta.get("files") else "application/x-www-form-urlencoded"
                op["requestBody"] = {"required": bool(required), "content": {ctype: {"schema": schema}}}
            if rule not in _PUBLIC:
                op["security"] = [{"cookieAuth": []}]
            if "409" in (meta.get("summary") or ""):
                op["responses"]["409"] = {"description": "In use — remove the references first", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
            paths.setdefault(rule_obj.rule.replace("<int:", "<").replace("<", "{").replace(">", "}"), {})[method.lower()] = op

    ordered = sorted(tags, key=lambda t: (_TAG_ORDER.index(t) if t in _TAG_ORDER else 99, t))
    return {
        "openapi": "3.0.3",
        "info": {"title": TITLE, "version": VERSION,
                 "description": ("Every dashboard action is a JSON endpoint under `/api/`. Log in with `POST /api/login` "
                                 "(form fields) and the session cookie authenticates every other call; roles are "
                                 "admin, creator and viewer (viewer is read-only and receives 403 on writes). "
                                 "Requests are form-encoded (or multipart when a file is involved); responses are JSON "
                                 "with an `ok` flag, and provisioning calls also return a `steps[]` trace of what ran.")},
        "servers": [{"url": "/"}],
        "tags": [{"name": t, **({"description": _TAG_DESC[t]} if t in _TAG_DESC else {})} for t in ordered],
        "paths": dict(sorted(paths.items())),
        "components": {
            "securitySchemes": {"cookieAuth": {"type": "apiKey", "in": "cookie", "name": "session",
                                               "description": "Flask session cookie set by POST /api/login."}},
            "schemas": {
                "Ok": {"type": "object", "properties": {"ok": {"type": "boolean", "example": True}}, "additionalProperties": True},
                "Error": {"type": "object", "properties": {"ok": {"type": "boolean", "example": False}, "error": {"type": "string"},
                                                            "steps": {"type": "array", "items": {"$ref": "#/components/schemas/Step"}}}},
                "Step": {"type": "object", "properties": {"name": {"type": "string"}, "ok": {"type": "boolean"}, "detail": {"type": "string"},
                                                           "command": {"type": "string", "nullable": True}, "simulated": {"type": "boolean"}}},
            },
        },
    }
