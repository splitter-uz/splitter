"""
Error-page middleware for the Splitter dashboard's own Flask app.

Maps an HTTP error status code to a page: a custom admin-uploaded Jinja2/
HTML template if one exists for that exact code or a range covering it
(e.g. "400-499"), else a built-in default page with SVG/CSS animation
(floating astronaut for 404, spinning gears for 500, a generic pulsing
icon otherwise) that matches the dashboard's own look.

This is deliberately built as Flask error handlers (see register() /
app.py), not a WSGI-level wrapper that inspects every response: a
successful (200-399) response never enters an errorhandler in the first
place, so "let it pass through untouched" falls out for free rather than
needing an explicit check.

A request whose path is under /api/, or that sends an explicit
Accept: application/json, always gets a plain JSON payload instead of
HTML — a script never has to scrape an error page to find out what
happened.

Custom templates are plain Jinja2 source rendered with the same context
as the built-in pages (status, title, message, path, method, timestamp,
request_id). Uploading one is admin-only (see /api/error-pages in
app.py) — the same trust boundary as the mapping form's raw "Advanced
config" nginx passthrough, so this doesn't introduce a new one.
"""
import datetime
import glob
import os

import jinja2
from flask import jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import config
from validators import ValidationError, clean_error_page_key

TITLES = {
    400: "Bad Request", 401: "Authentication Required", 403: "Access Denied",
    404: "Page Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    409: "Conflict", 410: "Gone", 413: "Payload Too Large",
    414: "URI Too Long", 415: "Unsupported Media Type",
    422: "Unprocessable Entity", 429: "Too Many Requests",
    500: "Server Error", 501: "Not Implemented", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
}

MESSAGES = {
    400: "The request couldn't be understood.",
    401: "Please sign in to continue.",
    403: "You don't have permission to view this page.",
    404: "The page you're looking for drifted off somewhere. Check the URL, or head back to the dashboard.",
    405: "That method isn't allowed on this URL.",
    408: "The request took too long and was dropped.",
    409: "That conflicts with the resource's current state.",
    410: "This resource is gone and isn't coming back.",
    413: "That request is too large.",
    414: "That URL is too long.",
    415: "That content type isn't supported here.",
    422: "The request couldn't be processed as sent.",
    429: "Slow down — you've hit a rate limit. Try again shortly.",
    500: "Something broke on our end. It's been logged — try again in a moment.",
    501: "This isn't implemented.",
    502: "The upstream service returned an invalid response.",
    503: "The service is temporarily unavailable. Try again in a moment.",
    504: "The upstream service took too long to respond.",
}

# Which built-in template renders which exact code — anything absent falls
# back to "errors/generic.html" (still styled per 4xx/5xx family).
_BUILTIN_TEMPLATES = {404: "errors/404.html", 500: "errors/500.html"}


def builtin_codes():
    """Exact status codes with a dedicated built-in template (others fall
    back to the generic default)."""
    return sorted(_BUILTIN_TEMPLATES.keys())


def title_for(code):
    return TITLES.get(code, f"HTTP {code}")


def message_for(code):
    if code in MESSAGES:
        return MESSAGES[code]
    return "A client error occurred." if code < 500 else "A server error occurred."


def request_id():
    """Stable per-request correlation id — reuses an inbound X-Request-Id
    (common when Splitter itself sits behind another proxy) so a trace stays
    consistent end to end, else mints one. Cached on flask.g for the life
    of the request; see app.py's before_request."""
    from flask import g
    if not getattr(g, "request_id", None):
        g.request_id = request.headers.get("X-Request-Id") or os.urandom(6).hex()
    return g.request_id


def _key_range(key):
    parts = key.split("-")
    lo = int(parts[0])
    hi = int(parts[1]) if len(parts) > 1 else lo
    return lo, hi


def list_custom():
    """[{key, size, mtime}] for every uploaded custom page."""
    out = []
    if not os.path.isdir(config.ERROR_PAGES_DIR):
        return out
    for path in sorted(glob.glob(os.path.join(config.ERROR_PAGES_DIR, "*.html"))):
        key = os.path.splitext(os.path.basename(path))[0]
        try:
            clean_error_page_key(key)
        except ValidationError:
            continue   # ignore anything that isn't one of ours
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({
            "key": key, "size": st.st_size,
            "mtime": datetime.datetime.fromtimestamp(
                st.st_mtime, datetime.timezone.utc).isoformat(timespec="seconds"),
        })
    return out


def _find_custom_path(code):
    """The most specific custom template covering `code`: an exact-code
    file first, else the narrowest range that contains it."""
    exact = os.path.join(config.ERROR_PAGES_DIR, f"{code}.html")
    if os.path.isfile(exact):
        return exact
    best_key, best_width = None, None
    for entry in list_custom():
        lo, hi = _key_range(entry["key"])
        if lo == hi:
            continue   # exact codes already handled above
        if lo <= code <= hi and (best_width is None or (hi - lo) < best_width):
            best_key, best_width = entry["key"], hi - lo
    return os.path.join(config.ERROR_PAGES_DIR, f"{best_key}.html") if best_key else None


def upload_custom(key, html_text):
    """Save `html_text` as the custom page for status code/range `key`.
    Raises ValidationError for a bad key."""
    key = clean_error_page_key(key)
    os.makedirs(config.ERROR_PAGES_DIR, exist_ok=True)
    path = os.path.join(config.ERROR_PAGES_DIR, f"{key}.html")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    os.replace(tmp, path)
    return key


def delete_custom(key):
    key = clean_error_page_key(key)
    path = os.path.join(config.ERROR_PAGES_DIR, f"{key}.html")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def preview_custom(key):
    """Render the custom page stored for the exact key `key` (no range
    matching — this previews what you just uploaded) with representative
    example context. None if there's no custom page for that key."""
    key = clean_error_page_key(key)
    path = os.path.join(config.ERROR_PAGES_DIR, f"{key}.html")
    if not os.path.isfile(path):
        return None
    code = _key_range(key)[0]
    ctx = {
        "status": code,
        "title": title_for(code),
        "message": message_for(code),
        "path": "/example/path",
        "method": "GET",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "request_id": "preview",
    }
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    return jinja2.Environment(autoescape=True).from_string(source).render(**ctx)


def _context(code, rid):
    return {
        "status": code,
        "title": title_for(code),
        "message": message_for(code),
        "path": request.path,
        "method": request.method,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "request_id": rid,
    }


def wants_json():
    """API paths, or an explicit Accept: application/json, get JSON — never
    an HTML error page a script would have to parse."""
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _render_html(code, ctx):
    custom_path = _find_custom_path(code)
    if custom_path:
        try:
            with open(custom_path, "r", encoding="utf-8") as fh:
                source = fh.read()
            return jinja2.Environment(autoescape=True).from_string(source).render(**ctx)
        except Exception:
            pass   # fall through to the built-in page below
    template = _BUILTIN_TEMPLATES.get(code, "errors/generic.html")
    return render_template(template, **ctx)


def respond(code):
    """The Flask response for status `code` — JSON for API/script callers,
    else the (custom or built-in default) HTML page."""
    rid = request_id()
    ctx = _context(code, rid)
    if wants_json():
        return jsonify({"ok": False, "error": ctx["message"], "status": code,
                        "path": ctx["path"], "request_id": rid,
                        "timestamp": ctx["timestamp"]}), code
    return _render_html(code, ctx), code


def handle_http_exception(exc):
    """Registered on werkzeug.exceptions.HTTPException — covers routing
    failures (404/405), payload-too-large, and any explicit abort()."""
    code = exc.code or 500
    return respond(code)


def handle_server_error(exc, logger=None):
    """For a genuinely unhandled non-HTTP exception. Logs the traceback
    (same as before this middleware existed) and reports it as a 500."""
    if logger:
        logger.exception("Unhandled error on %s %s", request.method, request.path)
    return respond(500)


def register(app):
    """Wire this middleware in as the app's error handling. Flask dispatches
    to the most specific registered handler class, so an HTTPException
    (routing 404/405, abort(), RequestEntityTooLarge, ...) always goes to
    handle_http_exception even though a bare Exception handler is also
    registered below."""
    app.register_error_handler(HTTPException, handle_http_exception)

    @app.errorhandler(Exception)
    def _catch_all(exc):
        return handle_server_error(exc, logger=app.logger)
