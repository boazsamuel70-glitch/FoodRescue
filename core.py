"""
FoodResQ core layer
===================
Everything that is *not* a page lives here so app1.py stays readable:

  * configuration read from environment variables (.env for local dev)
  * database connection helper
  * security: headers, CSRF, rate limiting, role guard, safe redirects
  * server-side validation for names, e-mail, phone, pincode, OTP, passwords
  * the access queue that limits how many logged-in users can be active

Nothing in here contains a secret. Secrets come from the environment.
"""

import os
import re
import time
import secrets
import hmac
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta

import mysql.connector
from flask import (request, session, jsonify, redirect, url_for,
                   render_template, abort, flash)


# =========================================================
# 1. CONFIGURATION
# =========================================================

def _load_dotenv(path=".env"):
    """Tiny .env reader so local development needs no extra package."""
    folder = os.path.dirname(os.path.abspath(__file__))
    full = os.path.join(folder, path)
    if not os.path.exists(full):
        print(f"[FoodResQ] WARNING: no .env file found in {folder}")
        for name in os.listdir(folder):
            if name.lower().startswith(".env"):
                print(f"[FoodResQ]   found '{name}' - rename it to exactly '.env'")
        return
    with open(full, encoding="utf-8-sig") as fh:          # utf-8-sig: tolerate Notepad's BOM
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if value[:1] in ('"', "'") and value.count(value[0]) >= 2:
                value = value[1:value.index(value[0], 1)]      # quoted: keep everything inside
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()   # drop trailing comment
            os.environ[key.strip()] = value                    # .env wins over stray system values
    print(f"[FoodResQ] loaded settings from {full}")


_load_dotenv()


def env(name, default=None, cast=str):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


IS_PRODUCTION = env("FLASK_ENV", "development") == "production"

DB_CONFIG = {
    "host": env("DB_HOST", "localhost"),
    "port": env("DB_PORT", 3306, int),
    "user": env("DB_USER", "root"),
    "password": env("DB_PASSWORD", ""),
    "database": env("DB_NAME", "foodresq"),
    "connection_timeout": 10,
}
print("[FoodResQ] database: user=%r host=%r db=%r password=%s" % (
    DB_CONFIG["user"], DB_CONFIG["host"], DB_CONFIG["database"],
    "SET" if DB_CONFIG["password"] else "EMPTY  <-- put DB_PASSWORD in .env"))

if env("DB_SSL", "0") == "1":
    DB_CONFIG["ssl_disabled"] = False

# Access queue -------------------------------------------------------------
MAX_ACTIVE_USERS = env("MAX_ACTIVE_USERS", 50, int)     # logged-in at once
ACTIVE_IDLE_SECONDS = env("ACTIVE_IDLE_SECONDS", 600, int)   # free slot if idle
WAITING_STALE_SECONDS = env("WAITING_STALE_SECONDS", 30, int)  # left the queue
AVG_SESSION_MINUTES = env("AVG_SESSION_MINUTES", 8, int)  # for wait estimate


def get_secret_key():
    key = os.environ.get("SECRET_KEY")
    if key and len(key) >= 32:
        return key
    if IS_PRODUCTION:
        raise RuntimeError(
            "SECRET_KEY must be set (32+ random characters) in production.")
    # Development only: random per start, sessions reset on restart.
    return secrets.token_hex(32)


def get_db():
    """One place that knows how to reach MySQL."""
    return mysql.connector.connect(**DB_CONFIG)


# =========================================================
# 2. VALIDATION
# =========================================================

_NAME_RE = re.compile(r"^[A-Za-z\u00C0-\u024F][A-Za-z\u00C0-\u024F .'\-]{1,59}$")
_ORG_RE = re.compile(r"^[A-Za-z0-9\u00C0-\u024F][A-Za-z0-9\u00C0-\u024F .,&'()\-]{2,99}$")
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,24}$")
_PHONE_RE = re.compile(r"^[6-9]\d{9}$")          # Indian mobile numbers
_PIN_RE = re.compile(r"^[1-9]\d{5}$")            # Indian PIN codes
_OTP_RE = re.compile(r"^\d{6}$")
_REGNO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/\-. ]{2,39}$")


def only_digits(value):
    return re.sub(r"\D", "", value or "")


def valid_name(value):
    return bool(_NAME_RE.match(value or ""))


def valid_org_name(value):
    return bool(_ORG_RE.match(value or ""))


def valid_email(value):
    value = value or ""
    return len(value) <= 254 and bool(_EMAIL_RE.match(value))


def valid_phone(value):
    return bool(_PHONE_RE.match(value or ""))


def valid_pincode(value):
    return bool(_PIN_RE.match(value or ""))


def valid_otp(value):
    return bool(_OTP_RE.match(value or ""))


def valid_regno(value):
    return bool(_REGNO_RE.match(value or ""))


def valid_year(value):
    if not re.fullmatch(r"\d{4}", value or ""):
        return False
    return 1800 <= int(value) <= datetime.now().year


def password_problem(password):
    """Return a human message if the password is weak, else None."""
    password = password or ""
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if len(password) > 128:
        return "Password must be at most 128 characters long."
    if not re.search(r"[a-z]", password):
        return "Password must contain a lowercase letter."
    if not re.search(r"[A-Z]", password):
        return "Password must contain an uppercase letter."
    if not re.search(r"\d", password):
        return "Password must contain a number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must contain a special character."
    return None


def positive_int(value, maximum=100000):
    """Digits only; returns int or None."""
    if not re.fullmatch(r"\d{1,9}", (value or "").strip()):
        return None
    number = int(value)
    return number if 1 <= number <= maximum else None


# =========================================================
# 3. RATE LIMITING (per process, sliding window)
# =========================================================

_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def client_ip():
    # ProxyFix (set in app1.py) makes request.remote_addr the real client.
    return request.remote_addr or "unknown"


RATE_LIMIT_ENABLED = env("RATE_LIMIT", "1") != "0"     # "0" only for local load tests


def rate_limited(bucket, limit, window_seconds):
    """True if this client already used `limit` hits in the window."""
    if not RATE_LIMIT_ENABLED:
        return False
    key = (bucket, client_ip())
    now = time.time()
    with _hits_lock:
        q = _hits[key]
        while q and q[0] <= now - window_seconds:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        if len(_hits) > 20000:          # keep memory bounded
            for k in list(_hits)[:5000]:
                if not _hits[k]:
                    _hits.pop(k, None)
        return False


# Path -> (limit, window seconds). POST only.
RATE_RULES = {
    "/login_user": (10, 300),
    "/ngo_login_verify": (10, 300),
    "/admin_login_verify": (6, 300),
    "/register_user": (6, 600),
    "/register_ngo": (4, 600),
    "/send_reset_otp": (5, 600),
    "/verify_reset_otp": (10, 600),
    "/reset_password": (10, 600),
    "/access_heartbeat": (60, 60),
}


# =========================================================
# 4. CSRF
# =========================================================

def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def csrf_ok():
    sent = (request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token") or "")
    expected = session.get("_csrf", "")
    return bool(expected) and hmac.compare_digest(sent, expected)


# =========================================================
# 5. ROLE GUARD
# =========================================================

_PUBLIC_ADMIN = {"/admin_login", "/admin_login_verify"}

ADMIN_PREFIXES = ("/admin", "/assign_ngo", "/undo_assignment",
                  "/update_assignment_status", "/find_ngos/")
NGO_PREFIXES = ("/ngo_home", "/ngo_assigned_donations", "/nearby_donations",
                "/ngo_request_donation", "/ngo_donations", "/ngo_approve",
                "/ngo_reject", "/ngo/", "/ngo_start_delivery",
                "/ngo_analytics", "/ngo_record_distribution")
DONOR_PREFIXES = ("/user_dashboard", "/donate", "/submit_donation",
                  "/my_donations", "/my_donation_details",
                  "/donor_reject_pickup", "/report_issue", "/edit_profile",
                  "/update_profile", "/donor_")


def required_role(path):
    if path in _PUBLIC_ADMIN:
        return None
    if path.startswith(ADMIN_PREFIXES):
        return "admin"
    if path.startswith(NGO_PREFIXES):
        return "ngo"
    if path.startswith(DONOR_PREFIXES):
        return "donor"
    if path.startswith("/notifications"):
        return "any"
    return None


def role_allowed(role_needed):
    role = session.get("role")
    if role_needed == "any":
        return role in ("donor", "ngo", "admin")
    if role_needed == "admin":
        return role == "admin" and "admin_id" in session
    if role_needed == "ngo":
        return role == "ngo" and "ngo_id" in session
    if role_needed == "donor":
        return role == "donor" and "user_id" in session
    return True


LOGIN_FOR_ROLE = {"admin": "/admin_login", "ngo": "/ngo_login",
                  "donor": "/user_login", "any": "/user_login"}


# =========================================================
# 6. ACCESS QUEUE
# =========================================================
# A logged-in person needs an "active slot".  At most MAX_ACTIVE_USERS
# slots exist.  Everyone else waits, first come first served.  State
# lives in MySQL, so it is correct across several worker processes.
# Admins are never queued.

_queue_ready = False
_queue_lock = threading.Lock()

_CREATE_QUEUE = """
CREATE TABLE IF NOT EXISTS access_queue (
    token       CHAR(43)     NOT NULL PRIMARY KEY,
    role        VARCHAR(10)  NOT NULL,
    status      VARCHAR(10)  NOT NULL,
    created_at  DATETIME(3)  NOT NULL,
    last_seen   DATETIME(3)  NOT NULL,
    INDEX idx_status_created (status, created_at),
    INDEX idx_last_seen (last_seen)
) ENGINE=InnoDB
"""


def ensure_queue_table():
    global _queue_ready
    if _queue_ready:
        return
    with _queue_lock:
        if _queue_ready:
            return
        db = get_db()
        try:
            cur = db.cursor(buffered=True)
            cur.execute(_CREATE_QUEUE)
            db.commit()
            cur.close()
            _queue_ready = True
        finally:
            db.close()


class _QueueLock:
    """MySQL named lock so two workers never hand out the same slot."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        cur = self.db.cursor(buffered=True)
        cur.execute("SELECT GET_LOCK('foodresq_queue', 5)")
        got = cur.fetchone()
        cur.close()
        if not got or got[0] != 1:
            raise RuntimeError("queue busy")
        return self

    def __exit__(self, *exc):
        try:
            cur = self.db.cursor(buffered=True)
            cur.execute("SELECT RELEASE_LOCK('foodresq_queue')")
            cur.fetchall()
            cur.close()
        except Exception:
            pass


def _housekeeping(cur):
    """Drop abandoned rows and promote waiting people into free slots."""
    now = datetime.now()
    cur.execute("DELETE FROM access_queue WHERE status='active' AND last_seen < %s",
                (now - timedelta(seconds=ACTIVE_IDLE_SECONDS),))
    cur.execute("DELETE FROM access_queue WHERE status='waiting' AND last_seen < %s",
                (now - timedelta(seconds=WAITING_STALE_SECONDS),))
    cur.execute("SELECT COUNT(*) FROM access_queue WHERE status='active'")
    active = cur.fetchone()[0]
    free = MAX_ACTIVE_USERS - active
    if free > 0:
        cur.execute(
            "UPDATE access_queue SET status='active', last_seen=%s "
            "WHERE status='waiting' ORDER BY created_at ASC LIMIT %s",
            (now, int(free)))


def _position(cur, token):
    cur.execute("SELECT created_at FROM access_queue WHERE token=%s AND status='waiting'",
                (token,))
    row = cur.fetchone()
    if not row:
        return None
    cur.execute("SELECT COUNT(*) FROM access_queue WHERE status='waiting' AND created_at <= %s",
                (row[0],))
    return cur.fetchone()[0]


def acquire_access_slot():
    """Called right after a successful login.

    Returns {"allowed": bool, "waiting": bool, "position": int}.
    Fails open on database trouble: the login itself already succeeded
    and a broken queue should not lock every user out.
    """
    if session.get("role") == "admin":
        return {"allowed": True, "waiting": False}
    try:
        ensure_queue_table()
        token = session.get("q_token") or secrets.token_urlsafe(32)
        session["q_token"] = token
        role = (session.get("role") or "user")[:10]
        now = datetime.now()
        db = get_db()
        try:
            with _QueueLock(db):
                cur = db.cursor(buffered=True)
                _housekeeping(cur)
                cur.execute("SELECT status FROM access_queue WHERE token=%s", (token,))
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE access_queue SET last_seen=%s WHERE token=%s",
                                (now, token))
                else:
                    cur.execute("SELECT COUNT(*) FROM access_queue WHERE status='waiting'")
                    someone_waiting = cur.fetchone()[0] > 0
                    cur.execute("SELECT COUNT(*) FROM access_queue WHERE status='active'")
                    active = cur.fetchone()[0]
                    status = ("active" if active < MAX_ACTIVE_USERS and not someone_waiting
                              else "waiting")
                    cur.execute(
                        "INSERT INTO access_queue (token, role, status, created_at, last_seen) "
                        "VALUES (%s,%s,%s,%s,%s)", (token, role, status, now, now))
                cur.execute("SELECT status FROM access_queue WHERE token=%s", (token,))
                status = cur.fetchone()[0]
                position = _position(cur, token) if status == "waiting" else 0
                db.commit()
                cur.close()
        finally:
            db.close()
        if status == "active":
            return {"allowed": True, "waiting": False}
        return {"allowed": False, "waiting": True, "position": position}
    except Exception as exc:                     # fail open
        print("QUEUE ERROR (allowing access):", exc)
        return {"allowed": True, "waiting": False}


def queue_state():
    """Heartbeat / status for the current session."""
    if session.get("role") == "admin":
        return {"active": True}
    token = session.get("q_token")
    if not token:
        return {"active": False, "gone": True}
    try:
        ensure_queue_table()
        db = get_db()
        try:
            with _QueueLock(db):
                cur = db.cursor(buffered=True)
                cur.execute("UPDATE access_queue SET last_seen=%s WHERE token=%s",
                            (datetime.now(), token))
                _housekeeping(cur)
                cur.execute("SELECT status FROM access_queue WHERE token=%s", (token,))
                row = cur.fetchone()
                if not row:
                    db.commit()
                    return {"active": False, "gone": True}
                if row[0] == "active":
                    db.commit()
                    return {"active": True}
                position = _position(cur, token) or 1
                cur.execute("SELECT COUNT(*) FROM access_queue WHERE status='waiting'")
                total = cur.fetchone()[0]
                db.commit()
                cur.close()
        finally:
            db.close()
        wait = max(1, round(position * AVG_SESSION_MINUTES / max(MAX_ACTIVE_USERS, 1)))
        return {"active": False, "position": position, "waiting_total": total,
                "capacity": MAX_ACTIVE_USERS, "eta_minutes": wait}
    except Exception as exc:
        print("QUEUE HEARTBEAT ERROR:", exc)
        return {"active": True}          # fail open


def has_active_slot():
    """Cheap check used before serving protected pages."""
    if session.get("role") in (None, "admin") or not session.get("q_token"):
        return True
    now = time.time()
    if now - session.get("q_checked", 0) < 20:      # limit DB traffic
        return session.get("q_active", True)
    state = queue_state()
    session["q_checked"] = now
    session["q_active"] = bool(state.get("active"))
    if state.get("gone"):
        # Row expired while idle: try to get back in.
        result = acquire_access_slot()
        session["q_active"] = bool(result.get("allowed"))
    return session["q_active"]


def release_access_slot():
    token = session.get("q_token")
    if not token:
        return
    try:
        db = get_db()
        cur = db.cursor(buffered=True)
        cur.execute("DELETE FROM access_queue WHERE token=%s", (token,))
        db.commit()
        cur.close()
        db.close()
    except Exception as exc:
        print("QUEUE RELEASE ERROR:", exc)


def queue_totals():
    try:
        ensure_queue_table()
        db = get_db()
        cur = db.cursor(buffered=True)
        cur.execute("SELECT status, COUNT(*) FROM access_queue GROUP BY status")
        data = dict(cur.fetchall())
        cur.close()
        db.close()
        return {"active": int(data.get("active", 0)),
                "waiting": int(data.get("waiting", 0)),
                "capacity": MAX_ACTIVE_USERS}
    except Exception:
        return {"active": 0, "waiting": 0, "capacity": MAX_ACTIVE_USERS}


# Paths that must stay reachable while waiting in the queue.
QUEUE_EXEMPT = ("/static/", "/access_queue", "/access_heartbeat",
                "/access_redirect", "/queue_leave", "/logout", "/health")


# =========================================================
# 7. SECURITY HEADERS
# =========================================================

def apply_security_headers(response):
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy",
                 "camera=(), microphone=(), payment=(), geolocation=(self)")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    # The site uses inline <script>/<style> and Leaflet/OpenStreetMap, so
    # the policy allows those and blocks everything else (no foreign
    # scripts, no framing, no plugin content).
    h.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com data:; "
        "img-src 'self' data: blob: https://*.tile.openstreetmap.org https://tile.openstreetmap.org https://unpkg.com https://cdnjs.cloudflare.com; "
        "connect-src 'self' https://nominatim.openstreetmap.org; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    if IS_PRODUCTION:
        h.setdefault("Strict-Transport-Security",
                     "max-age=31536000; includeSubDomains")
    if request.path.startswith(("/admin", "/ngo", "/user", "/my_", "/donate",
                                "/edit_profile", "/notifications",
                                "/access_")):
        h["Cache-Control"] = "no-store"
    return response


def safe_next(target, fallback="/"):
    """Only allow same-site relative redirects."""
    if target and target.startswith("/") and not target.startswith("//") \
            and "\\" not in target:
        return target
    return fallback
