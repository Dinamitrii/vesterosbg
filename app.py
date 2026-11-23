import os
import sys

# Ensure the project root is on sys.path so app modules (aeo_mcp_server,
# responsive_images, etc.) can be imported regardless of the working
# directory used to launch the dev server or tests.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



import gzip
import sqlite3
import re
import unicodedata
import tempfile
from functools import wraps
from pathlib import Path
from uuid import uuid4

from urllib.parse import quote


from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import hashlib
from secrets import token_bytes

try:
    from PIL import Image, ImageOps, UnidentifiedImageError  # type: ignore
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - Pyodide/WASM worker ships without Pillow
    Image = ImageOps = UnidentifiedImageError = None  # type: ignore
    _PIL_AVAILABLE = False
from responsive_images import generate as generate_responsive_image, install as install_responsive_images
from aeo_mcp_server import McpView, MCP_PROTOCOL_VERSION

try:
    from flask import Flask, Response, abort, flash, redirect, render_template, request, send_from_directory, session, url_for
    _FLASK_AVAILABLE = True
except ImportError:  # Pyodide/WASM runtime ships without Flask.
    # Provide mock implementations so the app can define routes without crashing.
    # Full Flask functionality is not available, but the code can start and
    # serve requests with limited features.
    class _MockSession(dict):
        """Mock session that works like Flask's session dict."""
        pass

    class _MockFlaskNames:
        """Mock the Flask names that the code imports from flask."""

        @staticmethod
        def url_for(endpoint, **kwargs):
            """Generate a placeholder URL for the given endpoint."""
            # In real Flask this generates proper URLs; here we return a
            # minimal placeholder so routes don't crash.
            if kwargs:
                parts = [f"{k}={v!r}" for k, v in kwargs.items()]
                return f"/{endpoint}?{ '&'.join(parts) }"
            return f"/{endpoint}/"

        def __getattr__(self, name):
            # Delegate attribute access (e.g. session.get) to a mock dict.
            return {} if name in ("get", "pop", "setdefault", "update") else None

        def __getitem__(self, name):
            return {}

        def __setitem__(self, name, value):
            pass

        def __contains__(self, name):
            return False

    #: Mock session object shared across requests (good enough for dev).
    session = _MockSession()

    #: Mock flash that stores messages on the app instance; templates can
    #: iterate over get_flashed_messages() or check categories.
    _flashed_messages: list[tuple[str, str | None]] = []

    def flash(message: str, category: str | None = None) -> None:
        """Store a flash message (category optional)."""
        _flashed_messages.append((message, category))

    def _flush_flashed_messages() -> list[tuple[str, str | None]]:
        """Return and clear flashed messages; call once per request cycle."""
        messages = _flashed_messages
        _flashed_messages.clear()
        return messages

    def abort(code: int) -> None:
        """Raise a HTTP error for the given status code."""
        raise RuntimeError(f"HTTP {code}")

    #: Mock request object with minimal attributes used by the codebase.
    class _MockRequest:
        method: str = "GET"
        form: dict[str, str] = {}
        args: dict[str, str] = {}
        files: dict[str, any] = {}

    request = _MockRequest()

    #: Mock send_from_directory that attempts to serve files from the real
    #: static directory when running outside Pyodide, otherwise returns empty.
    import os as _os

    def send_from_directory(directory: str, filename: str) -> bytes:
        """Serve a file from *directory* / *filename*, or return b''."""
        try:
            path = _os.path.join(directory, filename)
            with open(path, "rb") as f:
                return f.read()
        except (FileNotFoundError, OSError):
            return b""

    #: Mock render_template that renders using the DummyEnv globals (installed
    #: by install_responsive_images) and returns a minimal HTML string.
    def render_template(template_name: str, **context: any) -> str:
        """Render *template_name* with *context* using the app's jinja2 env."""
        # Build a simple string representation; real Jinja2 rendering is
        # skipped in the Pyodide-less fallback, but we expose template
        # globals so the page still structures correctly.
        items: list[str] = []
        for key, value in context.items():
            if isinstance(value, str):
                items.append(f' {key}="{_escape_html(value)}"')
            else:
                items.append(f" {key}={value!r}")
        body = "".join(items)
        return f"<html><body>{template_name}{body}</body></html>"

    def _escape_html(text: str) -> str:
        """Minimal HTML escape for the mock renderer."""
        return text.replace("&", "&").replace("<", "<").replace(">", ">").replace('"', "&quot;")

    #: Expose a no-op redirect so callers don't crash; real Flask redirect
    #: returns a Response with Location set.
    def redirect(location: str) -> str:
        """Return a redirect placeholder string."""
        return f"Redirecting to {location}"

    # Set all the mock names the code imports from flask
    Flask = None  # type: ignore[assignment]
    Response = None  # type: ignore[assignment]
    abort = abort  # type: ignore[assignment]
    flash = flash  # type: ignore[assignment]
    redirect = redirect  # type: ignore[assignment]
    render_template = render_template  # type: ignore[assignment]
    request = request  # type: ignore[assignment]
    send_from_directory = send_from_directory  # type: ignore[assignment]
    session = session  # type: ignore[assignment]
    url_for = _MockFlaskNames()  # type: ignore[assignment]
    _FLASK_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent


class GzipCompression:
    """Compress text responses, including Flask development-server assets."""
    COMPRESSIBLE = ("text/", "application/json", "application/javascript", "application/xml")

    def __init__(self, application, minimum_size=500):
        self.application = application
        self.minimum_size = minimum_size

    def __call__(self, environ, start_response):
        captured = {}
        writes = []

        def capture(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers
            return writes.append

        iterable = self.application(environ, capture)
        status = captured["status"]
        headers = list(captured["headers"])
        header_map = {key.lower(): value for key, value in headers}
        content_type = header_map.get("content-type", "").lower()
        compressible = any(content_type.startswith(prefix) for prefix in self.COMPRESSIBLE) or "+xml" in content_type
        accepted = "gzip" in environ.get("HTTP_ACCEPT_ENCODING", "").lower()
        # Never compress the admin panel pages (login/dashboard) -- the WSGI/worker
        # layer re-gzips already-compressed bytes and the browser then renders
        # the response as binary.  Keep gzip for the API upload/download flows only,
        # never for the admin HTML pages.
        path = environ.get("PATH_INFO", "") or "/"
        is_api = path.startswith("/api")
        should_compress = (is_api and accepted and compressible
                           and status.split(" ", 1)[0] not in ("204", "304")
                           and "content-encoding" not in header_map
                           and "content-range" not in header_map
                           and environ.get("REQUEST_METHOD") != "HEAD")
        if not should_compress:
            start_response(status, headers)
            if not writes:
                return iterable

            def passthrough():
                yield from writes
                yield from iterable

            return passthrough()

        try:
            body = b"".join((*writes, *iterable))
        finally:
            close = getattr(iterable, "close", None)
            if close:
                close()

        if len(body) >= self.minimum_size:
            body = gzip.compress(body, compresslevel=6, mtime=0)
            headers = [(key, value) for key, value in headers if key.lower() != "content-length"]
            headers.append(("Content-Encoding", "gzip"))
            vary = header_map.get("vary", "")
            if "accept-encoding" not in vary.lower():
                headers.append(("Vary", f"{vary}, Accept-Encoding".strip(", ")))
            headers.append(("Content-Length", str(len(body))))

        start_response(status, headers)
        return [body]


def hash_password(password: str, pepper: str) -> str:
    """Hash a password with the AUTH_PEPPER using PBKDF2-SHA512.

    Adds the pepper value to the password before hashing:
        hash = PBKDF2-HMAC-SHA512(password + pepper, salt, iterations, 64)

    Args:
        password: Plain-text admin password from the login form.
        pepper: AUTH_PEPPER value from .env (never stored in the DB).

    Returns:
        String: <algorithm>$<iterations>$<salt_hex>$<hash_hex>
    """
    salt = token_bytes(16)
    iterations = 200_000
    combined = (password + pepper).encode("utf-8")
    hash_bytes = hashlib.pbkdf2_hmac("sha512", combined, salt, iterations)
    return f"pbkdf2_sha512${iterations}${salt.hex()}${hash_bytes.hex()}"


def load_env_file(path):
    """Load simple KEY=VALUE entries without overriding the shell environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_env_file(BASE_DIR / ".env")
if not os.environ.get("SECRET_KEY"):
    # Cloudflare Workers block entropy during Worker startup (startup values
    # repeat across instances), so a random key cannot be generated here.
    # Fall back to a deterministic dev key; set SECRET_KEY via env in prod.
    os.environ["SECRET_KEY"] = "vesterosbg-dev-secret-key"
def _resolve_db_path():
    """Pick a writable location for the SQLite database.

    The Cloudflare Python Worker runs on Pyodide, where the project directory
    (and its filesystem) is read-only at startup, so a SQLite file cannot be
    created next to app.py there.  Fall back to a writable temporary path in
    that case.  An explicit ``vesterosbg-db`` env var, when set, always wins.
    """
    configured = os.environ.get("vesterosbg-db")
    candidate = Path(configured) if configured else BASE_DIR / "vesterosbg-db.sqlite3"
    try:
        probe = candidate.with_name(".sqlite-probe")
        probe.write_bytes(b"")
        probe.unlink()
        return candidate
    except OSError:
        return Path("/tmp") / candidate.name


DB_PATH = _resolve_db_path()
CATEGORY_IMAGE_DIR = BASE_DIR / "static" / "img" / "brand" / "favicon"
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "AVIF", "JPG"}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_IMAGE_EDGE = 2400
if Flask is not None:
    app = Flask(__name__)
    # GzipCompression disabled: Cloudflare Workers / the browser already handle
    # compression; the middleware can double-compress and break rendering.
    # app.wsgi_app = GzipCompression(app.wsgi_app)
else:
    # Fallback Flask-less app object for the worker runtime.
    class _DummyApp:
        wsgi_app = None
        secret_key = None
        config = {}
        extensions = {}
        jinja_env = type("DummyEnv", (), {"globals": {}})()
        # Dummy route decorators that simply return the function unchanged.
        @staticmethod
        def get(path):
            def decorator(fn):
                return fn
            return decorator
        @staticmethod
        def post(path):
            def decorator(fn):
                return fn
            return decorator
        @staticmethod
        def route(path, **kwargs):
            def decorator(fn):
                return fn
            return decorator
        @staticmethod
        def before_request(fn):
            return fn
        def register_blueprint(self, blueprint):
            # No-op in Flask-less environment.
            return None
    app = _DummyApp()
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 1024 * 1024)

@app.get("/static/<path:filename>")
def static_file(filename):
    """Serve static files from the static/ directory."""
    return send_from_directory(BASE_DIR / "static", filename)
install_responsive_images(app)

# Helpers for self-referencing URLs in templates (this Jinja2 build has no
# trim_end/trim_start filters).
@app.template_filter('trim_end')
def filter_trim_end(value, char='/'):
    if not isinstance(value, str):
        return value
    return value.rstrip(char)

# WSGI adapter for the Cloudflare Worker. The worker shim depends on the
# Cloudflare Workers SDK (js, workers packages) which are not installed in
# the local dev venv, so skip the worker entrypoint in local development.
# The `Default` variable is used by miniflare when deploying to Cloudflare.
try:
    import workers.wsgi as _wsgi  # noqa: F401 (WSGI adapter for Cloudflare Worker)
    if _wsgi is not None and hasattr(_wsgi, "entrypoint"):
        Default = _wsgi.entrypoint(app)
    else:
        # Cannot instantiate entrypoint: worker shim missing (Cloudflare SDK
        # not available in local venv). Skip it; Default will be None.
        Default = None
except ImportError:
    # Not running under the Cloudflare Worker runtime (local dev).
    # workers.wsgi requires the Cloudflare Workers SDK (js, pyodide) which
    # is only available inside the Worker runtime, not a normal venv.
    _wsgi = None
    Default = None

# The WSGI adapter wraps the Flask app so it can serve the Worker's `fetch`
# entry point. miniflare loads this module and calls `fetch(request)`;
# without it the Worker rejects with "Handler does not export a fetch() function."
# The WSGI adapter wraps the Flask app. miniflare's Python Worker loads this
# module and instantiates the `Default` (a `WorkerEntrypoint` subclass) to serve
# the Worker's `fetch` entry point:
#   "There is no 'Default' class defined in the Python Worker's main module"
# with "Handler does not export a fetch() function."
# (Assigned above in the try/except block; local dev sets Default = None.)

def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def remove_uploaded_image(filename):
    path = BASE_DIR / "static" / filename
    remove_responsive_variants(path)
    path.unlink(missing_ok=True)


def init_db():
    with db() as conn:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS categories ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL UNIQUE, "
            "position INTEGER NOT NULL DEFAULT 0); "
            "CREATE TABLE IF NOT EXISTS services ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE, "
            "name TEXT NOT NULL, "
            "description TEXT NOT NULL DEFAULT '', "
            "price REAL, "
            "unit TEXT NOT NULL DEFAULT 'кв.м', "
            "position INTEGER NOT NULL DEFAULT 0); "
            "CREATE TABLE IF NOT EXISTS category_images ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE, "
            "service_id INTEGER REFERENCES services(id) ON DELETE CASCADE, "
            "filename TEXT NOT NULL UNIQUE, "
            "alt TEXT NOT NULL DEFAULT '', "
            "position INTEGER NOT NULL DEFAULT 0); "
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )
        # Normalize the schema AFTER table creation, BEFORE any read.
        image_columns = {row["name"] for row in conn.execute("PRAGMA table_info(category_images)")}
        if "module_id" not in image_columns and "service_id" in image_columns:
            conn.execute("ALTER TABLE category_images RENAME COLUMN service_id TO module_id")
        table_names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "modules" not in table_names and "services" in table_names:
            conn.execute("ALTER TABLE services RENAME TO modules")
        legacy_images = conn.execute(
            "SELECT id, category_id, alt FROM category_images "
            "WHERE module_id IS NULL "
            "AND EXISTS (SELECT 1 FROM modules WHERE modules.category_id=category_images.category_id) "
            "ORDER BY category_id, position, id"
        ).fetchall()
        for image in legacy_images:
            services = conn.execute(
                "SELECT id,name FROM modules WHERE category_id=? ORDER BY position,id",
                (image["category_id"],),
            ).fetchall()
            image_alt = image["alt"].casefold()
            target = next((service for service in services if service["name"].casefold() in image_alt), services[0])
            position = conn.execute(
                "SELECT COALESCE(MAX(position),-1)+1 FROM category_images WHERE module_id=?",
                (target["id"],),
            ).fetchone()[0]
            conn.execute("UPDATE category_images SET module_id=?,position=? WHERE id=?", (target["id"], position, image["id"]))
        if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            examples = [
                ("Изработка на уебсайтове", [
                    ("Уебсайт с включена SEO и AEO оптимизация", "Уникален дизайн, адаптивен за телефон, таблет и компютър. Готов до 5 дни.", 350.0, "сайт"),
                ]),
                ("SEO и AEO оптимизация", [
                    ("Оптимизация за търсачки и AI отговори", "Структура, скорост, мета описания и структурирани данни за Google и AI асистенти.", None, "включена"),
                ]),
                ("Поддръжка", [
                    ("Поддръжка на уебсайт", "Обновяване на съдържание, архивиране, мониторинг и бързи корекции.", None, "месец"),
                ]),
                ("Допълнителни модули", [
                    ("Онлайн магазин", "Каталог с продукти, кошница и поръчки.", 100.0, "модул"),
                    ("Блог", "Модул за публикации, статии и новини.", 100.0, "модул"),
                    ("Втори език", "Многоезичност на съдържанието.", 100.0, "модул"),
                    ("Резервации", "Онлайн график и резервационна форма.", 100.0, "модул"),
                    ("Контактна форма с известия", "Форма за запитвания с известия по имейл/WhatsApp.", 100.0, "модул"),
                    ("Галерия", "Секция с портфолио и оптимизирани снимки.", 100.0, "модул"),
                    ("Админ панел", "Панел за управление на съдържанието и заявките.", 100.0, "модул"),
                    ("Интеграция с външни услуги", "Свързване с външни API, карти, аналитика.", 100.0, "модул"),
                ]),
            ]
            for cat_pos, (category, items) in enumerate(examples):
                cur = conn.execute("INSERT INTO categories(name, position) VALUES (?, ?)", (category, cat_pos))
                for pos, (name, description, price, unit) in enumerate(items):
                    conn.execute("INSERT INTO modules(category_id,name,description,price,unit,position) VALUES (?,?,?,?,?,?)", (cur.lastrowid, name, description, price, unit, pos))
        for key, value in (("company_name", "Вестерос БГ"), ("legal_name", "Вестерос БГ ЕООД"), ("eik", "206244287"), ("address", "гр. София, ж.к. Дианабад, бл. 42, ет. 1, ап. 4, ПК 1172"), ("phone", os.environ.get("WHATSAPP_PHONE", "359876641667")), ("city", "София и областта")):
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, value))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS clients ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "name TEXT NOT NULL,"
            "phone TEXT NOT NULL,"
            "email TEXT NOT NULL DEFAULT '',"
            "message TEXT NOT NULL DEFAULT '',"
            "status TEXT NOT NULL DEFAULT 'nov',"
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )


_init_db_initialized = False


@app.before_request
def _initialize_db():
    global _init_db_initialized
    if not _init_db_initialized:
        _init_db_initialized = True
        init_db()



def public_site_url():
    configured_url = os.environ.get("SITE_URL", "").strip().rstrip("/")
    return configured_url or request.url_root.rstrip("/")


@app.get("/img/favicon/<path:filename>")
def favicon_asset(filename):
    allowed_files = {
        "favicon.ico", "favicon.svg", "favicon-16x16.png", "favicon-32x32.png",
        "apple-touch-icon.png", "web-app-manifest-192x192.png",
        "web-app-manifest-512x512.png", "site.webmanifest",
    }
    if filename not in allowed_files:
        abort(404)
    return send_from_directory(BASE_DIR / "static" / "img" / "favicon", filename)


@app.get("/favicon.ico")
def root_favicon():
    return send_from_directory(BASE_DIR / "static" / "img" / "favicon", "favicon.ico")

@app.get("/mcp/favicon.ico")
def mcp_favicon_ico():
    return send_from_directory(BASE_DIR / "static" / "img" / "favicon", "favicon.ico")


@app.get("/mcp/favicon.svg")
def mcp_favicon_svg():
    return send_from_directory(BASE_DIR / "static" / "img" / "favicon", "favicon.svg")


@app.get("/ai-catalog.json")
@app.get("/.well-known/ai-catalog.json")
def ai_catalog():
    return Response(
        render_template("ai-catalog.json", site_url=public_site_url()),
        mimetype="application/ai-catalog+json",
        headers={"Cache-Control": "max-age=3600"},
    )


def settings_dict(conn):
    return {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM settings")}


def whatsapp(settings):
    configured_phone = os.environ.get("WHATSAPP_PHONE") or settings.get("phone", "")
    number = "".join(ch for ch in configured_phone if ch.isdigit())
    if not number:
        return "https://wa.me/"
    return f"https://wa.me/{number}?text={quote('Здравейте! Уебсайтове от модули – дизайн, SEO и AEO, поддръжка.')}"


def map_links(settings):
    query = settings.get("address", "")
    return f"https://www.google.com/maps?q={quote(query)}&output=embed", f"https://www.google.com/maps/dir/?api=1&destination={quote(query)}"


@app.get("/")
def index():
    with db() as conn:
        settings = settings_dict(conn)
        categories = conn.execute("SELECT * FROM categories ORDER BY position,id").fetchall()
        data = []
        for category in categories:
            services = []
            for service in conn.execute("SELECT * FROM modules WHERE category_id=? ORDER BY position,id", (category["id"],)):
                service_data = dict(service)
                service_data["images"] = [dict(image) for image in conn.execute(
                    "SELECT * FROM category_images WHERE module_id=? ORDER BY position,id", (service["id"],)
                )]
                services.append(service_data)
            category_data = dict(category)
            category_data["images"] = [dict(image) for image in conn.execute(
                "SELECT ci.*, s.name AS service_name FROM category_images ci LEFT JOIN modules s ON s.id=ci.module_id WHERE ci.category_id=? ORDER BY s.position,s.id,ci.position,ci.id",
                [category["id"]],
            )]
            data.append({**category_data, "services": services})
    map_embed, directions = map_links(settings)
    return render_template("index.html", categories=data, settings=settings, wa=whatsapp(settings), map_embed=map_embed, directions=directions, canonical_url=f"{public_site_url()}/")


@app.get("/galeria/<int:service_id>")
def service_gallery(service_id):
    with db() as conn:
        settings = settings_dict(conn)
        row = conn.execute(
            "SELECT m.*, c.name AS category_name, c.id AS category_id "
            "FROM modules m JOIN categories c ON c.id=m.category_id "
            "WHERE m.id=?",
            (service_id,)).fetchone()
        if not row:
            abort(404)
        images = [dict(image) for image in conn.execute(
            "SELECT * FROM category_images WHERE module_id=? ORDER BY position,id", (service_id,),
        )]
    if not images:
        abort(404)
    service = dict(row)
    map_embed, directions = map_links(settings)
    return render_template(
        "gallery.html",
        service=service,
        images=images,
        settings=settings,
        wa=whatsapp(settings),
        map_embed=map_embed,
        directions=directions,
        canonical_url=f"{public_site_url()}{url_for('service_gallery', service_id=service_id)}",
    )


@app.get("/contacts")
def contact_page():
    with db() as conn:
        settings = settings_dict(conn)
    map_embed, directions = map_links(settings)
    return render_template(
        "contact.html",
        settings=settings,
        wa=whatsapp(settings),
        map_embed=map_embed,
        directions=directions,
        canonical_url=f"{public_site_url()}{url_for('contact_page')}",
    )


@app.post("/contact")
def contact_submit():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    message = request.form.get("message", "").strip()
    if not name or not phone or not message:
        flash("Името, телефона и съобщението са задължителни.")
        return redirect(url_for("contact_page"))
    with db() as conn:
        conn.execute(
            "INSERT INTO clients(name, phone, email, message, status) VALUES (?, ?, ?, ?, 'nov')",
            (name, phone, email, message),
        )
    flash("Благодарим, заявката е изпратена. Ще се свържем с вас.")
    return redirect(url_for("contact_page"))


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapped
@app.get("/admin/clients")
@admin_required
def admin_clients():
    with db() as conn:
        clients = conn.execute("SELECT * FROM clients ORDER BY id DESC").fetchall()
        settings = settings_dict(conn)
    return render_template("admin.html", clients=clients, settings=settings)


@app.get("/admin/client/<int:client_id>")
@admin_required
def admin_client_detail(client_id):
    with db() as conn:
        client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            abort(404)
        conn.execute("UPDATE clients SET status='pq' WHERE id=?", (client_id,))
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('last_client_view',?)", (client_id,))
    return render_template("admin_client.html", client=client)


@app.post("/admin/client/<int:client_id>/status")
@admin_required
def admin_client_status(client_id):
    status = request.form.get("status", "").strip()
    if status not in {"nov", "pq", "zatvorena"}:
        status = "pq"
    with db() as conn:
        conn.execute("UPDATE clients SET status=?", (status, client_id))
    flash("Статусът е обновен.")
    return redirect(url_for("admin_clients"))


def sitemap_image_url(filename, base_url):
    # Prefer the largest generated responsive WebP, falling back to original.
    source = (app.static_folder and Path(app.static_folder) / filename)
    if source is not None:
        manifest = source.parent / "_responsive" / (source.name + ".json")
        try:
            import json
            rows = json.loads(manifest.read_text(encoding="utf-8"))
            valid = [row for row in rows if isinstance(row, dict)
                     and isinstance(row.get("name"), str) and row["name"]
                     and "/" not in row["name"] and "\\" not in row["name"]
                     and isinstance(row.get("width"), int)
                     and (manifest.parent / row["name"]).is_file()]
            if valid:
                best = max(valid, key=lambda row: row["width"])
                directory = Path(filename).parent.as_posix()
                return f"{base_url}/static/{directory}/_responsive/{quote(best['name'])}"
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    return f"{base_url}{url_for('static', filename=filename)}"


@app.get("/galeria/<category_name>")
def category_gallery(category_name):
    with db() as conn:
        settings = settings_dict(conn)
        category = conn.execute("SELECT * FROM categories WHERE name=?", (category_name,)).fetchone()
        if not category:
            abort(404)
        images = [dict(image) for image in conn.execute(
            "SELECT ci.*, m.name AS service_name FROM category_images ci LEFT JOIN modules m ON m.id=ci.module_id WHERE ci.category_id=? ORDER BY m.position,m.id,ci.position,ci.id",
            [category["id"]],
        )]
    if len(images) < 4:
        abort(404)
    gallery = {"name": category["name"], "category_name": category["name"],
               "category_id": category["id"], "description": ""}
    return render_template(
        "gallery.html",
        service=gallery,
        images=images,
        settings=settings,
        wa=whatsapp(settings),
        map_embed="",
        directions="",
        canonical_url=f"{public_site_url()}{url_for('category_gallery', category_name=category_name)}",
    )


@app.get("/sitemap.xml")
@app.get("/static/sitemap.xml")
def sitemap():
    base_url = public_site_url().rstrip("/")
    sitemap_ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    image_ns = "http://www.google.com/schemas/sitemap-image/1.1"
    ET.register_namespace("", sitemap_ns)
    ET.register_namespace("image", image_ns)
    root = ET.Element(f"{{{sitemap_ns}}}urlset")

    def add_url(path, *, priority="0.7", images=()):
        entry = ET.SubElement(root, f"{{{sitemap_ns}}}url")
        ET.SubElement(entry, f"{{{sitemap_ns}}}loc").text = f"{base_url}{path}"
        ET.SubElement(entry, f"{{{sitemap_ns}}}changefreq").text = "weekly"
        ET.SubElement(entry, f"{{{sitemap_ns}}}priority").text = priority
        for filename, title in images:
            image = ET.SubElement(entry, f"{{{image_ns}}}image")
            ET.SubElement(image, f"{{{image_ns}}}loc").text = sitemap_image_url(filename, base_url)
            if title:
                ET.SubElement(image, f"{{{image_ns}}}title").text = title

    with db() as conn:
        images = conn.execute(
            "SELECT ci.filename, ci.alt, ci.module_id, ci.category_id, c.name AS category_name, "
            "s.name AS service_name, COUNT(*) OVER (PARTITION BY ci.module_id) AS service_image_count "
            "FROM category_images ci JOIN categories c ON c.id=ci.category_id "
            "LEFT JOIN modules s ON s.id=ci.module_id "
            "ORDER BY c.position,c.id,s.position,s.id,ci.position,ci.id"
        ).fetchall()
    homepage_images = []
    gallery_images = {}
    category_gallery_images = {}
    for image in images:
        category_gallery_images.setdefault(image["category_id"], []).append(image)
        if image["service_id"] is not None and image["service_image_count"] >= 4:
            gallery_images.setdefault(image["service_id"], []).append(image)
        else:
            homepage_images.append(image)
    def sitemap_image(image):
        return (image["filename"], image["alt"] or image["service_name"] or image["category_name"])

    add_url(url_for("index"), priority="1.0", images=[sitemap_image(image) for image in homepage_images])
    add_url(url_for("contact_page"))
    for category_id, category_images in category_gallery_images.items():
        if len(category_images) >= 4:
            cat_name = category_images[0]["category_name"]
            add_url(url_for("category_gallery", category_name=cat_name),
                    images=[sitemap_image(image) for image in category_images])
    for service_id, service_images in gallery_images.items():
        add_url(url_for("service_gallery", service_id=service_id),
                images=[sitemap_image(image) for image in service_images])
    add_url(url_for("robots"))

    if hasattr(ET, "indent"):
        ET.indent(root, space="  ")
    sitemap_data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=app.static_folder,
                                         prefix=".sitemap-", delete=False) as output:
            temp_path = output.name
            output.write(sitemap_data)
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, Path(app.static_folder) / "sitemap.xml")
        temp_path = None
    except OSError:
        app.logger.exception("Неуспешно записване на static/sitemap.xml")
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                app.logger.exception("Неуспешно изтриване на временния sitemap файл")
    return Response(sitemap_data, content_type="application/xml; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.get("/llms.txt")
def llms_txt():
    return send_from_directory(BASE_DIR / "static", "llms.txt", mimetype="text/plain")


@app.get("/robots.txt")
def robots():
    sitemap_url = escape(f"{public_site_url()}{url_for('sitemap')}")
    body = f"User-agent: *\nAllow: /\nDisallow: {url_for('admin_login')}\nSitemap: {sitemap_url}\n"
    return Response(body, mimetype="text/plain")




@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if session.get("admin"):
        return redirect(url_for("admin_dashboard"))
    with db() as conn:
        company = settings_dict(conn).get("company_name", "Админ")
    if request.method == "POST":
        password = request.form.get("password", "")
        pepper = os.environ.get("AUTH_PEPPER", "")
        # Hash the submitted password with the pepper, then compare
        # with the stored ADMIN_PASSWORD value
        expected_hash = hash_password(password, pepper)
        if expected_hash == os.environ.get("ADMIN_PASSWORD", ""):
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Невалидна парола.")
    return render_template("login.html", company=company)


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/admin/dashboard")
@admin_required
def admin_dashboard():
    with db() as conn:
        settings = settings_dict(conn)
        categories = conn.execute("SELECT * FROM categories ORDER BY position,id").fetchall()
        data = []
        for category in categories:
            services = []
            for service in conn.execute("SELECT * FROM modules WHERE category_id=? ORDER BY position,id", (category["id"],)):
                service_data = dict(service)
                service_data["images"] = [dict(image) for image in conn.execute(
                    "SELECT * FROM category_images WHERE module_id=? ORDER BY position,id", (service["id"],)
                )]
                services.append(service_data)
            data.append({**dict(category), "services": services})
    return render_template("admin.html", settings=settings, categories=data)


@app.post("/admin/settings")
@admin_required
def save_settings():
    allowed = {"company_name", "legal_name", "eik", "address", "phone", "city"}
    values = {k: request.form.get(k, "").strip() for k in allowed}
    if not values["company_name"] or not values["phone"]:
        flash("Име и телефон са задължителни.")
        return redirect(url_for("admin_dashboard"))
    with db() as conn:
        conn.executemany("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())
    flash("Данните са запазени.")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------------
# MCP API endpoint (Model Context Protocol)
# ---------------------------------------------------------------------------
_mcp_view = McpView(app=app)
# Expose the project DB factory to the MCP tools (avoids a circular import).
app.extensions["mcp_db"] = db


@app.get("/admin/settings")
@admin_required
def admin_settings():
    return "Settings disabled", 405


@app.post("/admin/category")
@admin_required
def add_category():
    name = request.form.get("name", "").strip()
    if name:
        try:
            with db() as conn:
                conn.execute("INSERT INTO categories(name,position) VALUES (?, (SELECT COALESCE(MAX(position),0)+1 FROM categories))", (name,))
            flash("Категорията е добавена.")
        except sqlite3.IntegrityError:
            flash("Тази категория вече съществува.")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/category/<int:category_id>/position")
@admin_required
def set_category_position(category_id):
    try:
        position = max(0, int(request.form.get("position", "0")))
        with db() as conn:
            conn.execute("UPDATE categories SET position=? WHERE id=?", (position, category_id))
        flash("Подредбата на категорията е запазена.")
    except ValueError:
        flash("Въведи валиден номер за подредба.")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/category/<int:category_id>/rename")
@admin_required
def rename_category(category_id):
    try:
        with db() as conn:
            conn.execute("UPDATE categories SET name=? WHERE id=?", (request.form.get("name", "").strip(), category_id))
        flash("Категорията е обновена.")
    except sqlite3.IntegrityError:
        flash("Категория с това име вече съществува.")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/category/<int:category_id>/delete")
@admin_required
def delete_category(category_id):
    with db() as conn:
        filenames = [row[0] for row in conn.execute("SELECT filename FROM category_images WHERE category_id=?", (category_id,))]
    with db() as conn:
        conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    for filename in filenames:
        remove_uploaded_image(filename)
    flash("Категорията и услугите в нея са изтрити.")
    return redirect(url_for("admin_dashboard"))


def parse_price(value):
    value = value.strip().replace(",", ".")
    return float(value) if value else None


def save_optimized_image(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("Избери изображение за качване.")
    raw = file_storage.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("Изображението трябва да е до 12 MB.")
    if Image is None:
        raise ValueError("Оптимизацията на изображенията не е налична в тази среда (Workers/Pyodide); услугата е достъпна само в обычната, не- Cloudflare среда на изпълнение.")
    try:
        from io import BytesIO
        with Image.open(BytesIO(raw)) as source:
            if source.format not in ALLOWED_IMAGE_FORMATS:
                raise ValueError("Поддържат се JPEG, PNG и WebP изображения.")
            source.verify()
        with Image.open(BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            if image.width * image.height > 40_000_000:
                raise ValueError("Изображението е прекалено голямо.")
            image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            CATEGORY_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid4().hex}.webp"
            target = CATEGORY_IMAGE_DIR / filename
            image.save(target, "WEBP", quality=82, method=6)
            return f"uploads/categories/{filename}"
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Файлът не е валидно изображение.") from exc


def save_image_to_service(service_id):
    saved_path = None
    try:
        with db() as conn:
            service = conn.execute("SELECT id,category_id FROM modules WHERE id=?", (service_id,)).fetchone()
            if not service:
                abort(404)
        saved_path = save_optimized_image(request.files.get("image"))
        generate_responsive_image(BASE_DIR / "static" / saved_path)
        alt = request.form.get("alt", "").strip()[:250]
        with db() as conn:
            position = conn.execute("SELECT COALESCE(MAX(position),-1)+1 FROM category_images WHERE service_id=?", (service_id,)).fetchone()[0]
            conn.execute("INSERT INTO category_images(category_id,service_id,filename,alt,position) VALUES (?,?,?,?,?)", (service["category_id"], service_id, saved_path, alt, position))
        flash("Снимката е качена и оптимизирана до WebP.")
    except ValueError as exc:
        if saved_path:
            remove_uploaded_image(saved_path)
        flash(str(exc))
    except sqlite3.Error:
        if saved_path:
            remove_uploaded_image(saved_path)
        flash("Снимката не можа да бъде запазена.")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/service/<int:service_id>/images")
@admin_required
def upload_service_image(service_id):
    return save_image_to_service(service_id)


@app.post("/admin/category/<int:category_id>/images")
@admin_required
def upload_category_image(category_id):
    with db() as conn:
        service = conn.execute("SELECT id FROM modules WHERE category_id=? ORDER BY position,id LIMIT 1", (category_id,)).fetchone()
    if not service:
        flash("Добави услуга към категорията, преди да качиш снимка.")
        return redirect(url_for("admin_dashboard"))
    return save_image_to_service(service["id"])


@app.post("/admin/category-image/<int:image_id>/edit")
@admin_required
def edit_category_image(image_id):
    try:
        position = max(0, int(request.form.get("position", "0")))
    except ValueError:
        flash("Въведи валиден номер за подредба.")
        return redirect(url_for("admin_dashboard"))
    replacement = None
    old_filename = None
    with db() as conn:
        current = conn.execute("SELECT filename FROM category_images WHERE id=?", (image_id,)).fetchone()
    if not current:
        abort(404)
    old_filename = current["filename"]
    try:
        replacement_file = request.files.get("image")
        if replacement_file and replacement_file.filename:
            replacement = save_optimized_image(replacement_file)
            generate_responsive_image(BASE_DIR / "static" / replacement)
        with db() as conn:
            filename = replacement or old_filename
            conn.execute("UPDATE category_images SET alt=?, position=?, filename=? WHERE id=?", (request.form.get("alt", "").strip()[:250], position, filename, image_id))
    except (ValueError, sqlite3.Error) as exc:
        if replacement:
            remove_uploaded_image(replacement)
        flash(str(exc) if isinstance(exc, ValueError) else "Промяната не можа да бъде запазена.")
        return redirect(url_for("admin_dashboard"))
    if replacement and old_filename:
        remove_uploaded_image(old_filename)
    flash("Описанието и подредбата на снимката са запазени.")
    return redirect(url_for("admin_dashboard"))


@app.get("/admin/category-image/<int:image_id>/export")
@admin_required
def export_category_image(image_id):
    with db() as conn:
        image = conn.execute("SELECT filename FROM category_images WHERE id=?", (image_id,)).fetchone()
    if not image:
        abort(404)
    path = BASE_DIR / "static" / image["filename"]
    if not path.is_file():
        abort(404)
    return app.response_class(path.read_bytes(), mimetype="image/webp", headers={"Content-Disposition": f'attachment; filename="category-image-{image_id}.webp"'})


@app.post("/admin/category-image/<int:image_id>/delete")
@admin_required
def delete_category_image(image_id):
    with db() as conn:
        image = conn.execute("SELECT filename FROM category_images WHERE id=?", (image_id,)).fetchone()
        if image:
            conn.execute("DELETE FROM category_images WHERE id=?", (image_id,))
    if image:
        remove_uploaded_image(image["filename"])
        flash("Снимката е изтрита.")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/service")
@admin_required
def add_service():
    try:
        price = parse_price(request.form.get("price", ""))
        with db() as conn:
            conn.execute("INSERT INTO modules(category_id,name,description,price,unit) VALUES (?,?,?,?,?)", (int(request.form["category_id"]), request.form["name"].strip(), request.form.get("description", "").strip(), price, request.form.get("unit", "кв.м").strip() or "бр."))
        flash("Услугата е добавена.")
    except (ValueError, KeyError, sqlite3.IntegrityError):
        flash("Провери въведените данни за услугата.")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/service/<int:service_id>/edit")
@admin_required
def edit_service(service_id):
    try:
        with db() as conn:
            conn.execute("UPDATE modules SET name=?,description=?,price=?,unit=? WHERE id=?", (request.form["name"].strip(), request.form.get("description", "").strip(), parse_price(request.form.get("price", "")), request.form.get("unit", "").strip() or "бр.", service_id))
        flash("Услугата е обновена.")
    except (ValueError, KeyError):
        flash("Провери въведената цена.")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/service/<int:service_id>/delete")
@admin_required
def delete_service(service_id):
    with db() as conn:
        filenames = [row[0] for row in conn.execute("SELECT filename FROM category_images WHERE module_id=?", (service_id,))]
        conn.execute("DELETE FROM modules WHERE id=?", (service_id,))
    for filename in filenames:
        remove_uploaded_image(filename)
    flash("Услугата е изтрита.")
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=False)
