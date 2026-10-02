from flask import Flask, render_template, request, redirect, session ,url_for,flash
import mysql.connector
import random
import smtplib
import string
import os
import uuid
from werkzeug.utils import secure_filename
from flask import send_from_directory
from email.message import EmailMessage
from datetime import date
from datetime import datetime, date, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from flask import jsonify, abort, make_response
import secrets
import hmac
import time
import re
import core
from core import (get_db, env, IS_PRODUCTION, csrf_token, csrf_ok,
                  rate_limited, RATE_RULES, required_role, role_allowed,
                  LOGIN_FOR_ROLE, apply_security_headers, acquire_access_slot,
                  queue_state, has_active_slot, release_access_slot,
                  queue_totals, QUEUE_EXEMPT, valid_name, valid_org_name,
                  valid_email, valid_phone, valid_pincode, valid_otp,
                  valid_regno, valid_year, password_problem, positive_int,
                  only_digits)
app = Flask(__name__)

# Behind a hosting proxy (Render, Railway, PythonAnywhere...) the real
# client address and https scheme arrive in X-Forwarded-* headers.
if env("BEHIND_PROXY", "1") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.context_processor
def inject_unread_notifications():

    unread_notifications = 0

    # Anonymous visitors and admins have no badge: skip the DB entirely.
    if not (session.get("ngo_id") or session.get("user_id")):
        return {"unread_notifications": 0}

    try:

        db = get_db()

        cursor = db.cursor()

        if session.get("role") == "ngo" and session.get("ngo_id"):

            cursor.execute("""
                SELECT COUNT(*)
                FROM notifications
                WHERE ngo_id = %s
                AND is_read = 0
            """, (session["ngo_id"],))

        elif session.get("user_id"):

            cursor.execute("""
                SELECT COUNT(*)
                FROM notifications
                WHERE user_id = %s
                AND is_read = 0
            """, (session["user_id"],))

        result = cursor.fetchone()

        if result:
            unread_notifications = result[0]

        cursor.close()
        db.close()

    except Exception as e:

        print("NOTIFICATION BADGE ERROR:", e)

    return {
        "unread_notifications": unread_notifications
    }

app.secret_key = core.get_secret_key()

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,      # https-only cookie when live
    SESSION_COOKIE_NAME="foodresq_session",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,      # reject uploads over 12 MB
    JSON_SORT_KEYS=False,
)
app.jinja_env.globals["csrf_token"] = csrf_token


# =========================================================
# DONATION PROGRESS
#
# One source of truth for the donation journey. Status text
# is written in several different places, so the tracker
# matches case-insensitively and accepts the aliases each
# step is known by. Anything unknown simply leaves the
# tracker where it was instead of blanking it out.
# =========================================================

DONATION_STAGES = [
    {
        "label": "Donation Submitted",
        "text":  "Your donation request has been submitted.",
        "match": ["pending"],
    },
    {
        "label": "Admin Verified",
        "text":  "FoodResQ has reviewed and verified your donation.",
        "match": ["admin verified"],
    },
    {
        "label": "NGO Assigned",
        "text":  "A partner NGO has been assigned to collect your donation.",
        "match": ["assigned"],
    },
    {
        "label": "NGO Accepted",
        "text":  "The NGO confirmed it will collect this donation.",
        "match": ["ngo accepted", "accepted"],
    },
    {
        "label": "Donation Verified",
        "text":  "The NGO checked the food at your location.",
        "match": ["verified", "donation verified", "at donor"],
    },
    {
        "label": "Pickup Confirmed",
        "text":  "Pickup has been confirmed by both sides.",
        "match": ["awaiting donor confirmation", "awaiting pickup otp",
                  "pickup confirmed", "picked up"],
    },
    {
        "label": "On the Way",
        "text":  "The NGO is taking your donation to the people it will feed.",
        "match": ["on the way", "reached distribution", "distributed",
                  "proof submitted"],
    },
    {
        "label": "Completed",
        "text":  "Your donation has been distributed. Thank you.",
        "match": ["completed"],
    },
]


# Statuses that stop the journey rather than advance it.
DONATION_STOPPED = {
    "cancelled":           "This donation was cancelled.",
    "rejected":            "This donation was rejected.",
    "assignment rejected": "The assigned NGO declined this donation.",
    "pickup failed":       "The pickup could not be completed.",
}


def donation_progress(status):

    """Describe where a donation is in its journey.

    Returns a dict with:
        stages  - every step, each marked done / current / todo
        index   - zero-based index of the current step (-1 if unknown)
        stopped - explanation text if the donation stopped early
    """

    key = (status or "").strip().lower()

    stopped = DONATION_STOPPED.get(key)

    index = -1

    if not stopped:
        for position, stage in enumerate(DONATION_STAGES):
            if key in stage["match"]:
                index = position
                break

    # An unrecognised, non-stopping status still counts as submitted,
    # so the tracker never renders completely empty.
    if index == -1 and not stopped and key:
        index = 0

    stages = []

    for position, stage in enumerate(DONATION_STAGES):

        if stopped:
            state = "todo"
        elif position < index:
            state = "done"
        elif position == index:
            state = "current"
        else:
            state = "todo"

        stages.append({
            "number": position + 1,
            "label":  stage["label"],
            "text":   stage["text"],
            "state":  state,
        })

    return {
        "stages":  stages,
        "index":   index,
        "stopped": stopped,
        "percent": (
            0 if index < 0
            else round(index / (len(DONATION_STAGES) - 1) * 100)
        ),
    }


# Make it available inside every template.
app.jinja_env.globals["donation_progress"] = donation_progress


# =========================================================
# WEBSITE MESSAGE SYSTEM
#
# Messages are flashed, then shown by the site's own popup
# (static/website-alert.js) on top of the page the user is
# already looking at - no separate message screen, and no
# native browser alert().
# =========================================================

def notify(message, kind=None):

    """Queue a message for the popup on the next rendered page."""

    flash(message, kind or _guess_kind(message))


def notify_redirect(message, target, kind=None):

    """Flash a message, then send the user to `target`, where
    the popup appears over that page."""

    notify(message, kind)

    return redirect(target)


def _guess_kind(message):

    text = str(message).lower()

    if any(w in text for w in ("success", "approved", "sent", "rejected.")):
        return "success"

    if any(w in text for w in ("already", "too many", "locked",
                               "deactivated", "no longer")):
        return "warning"

    if any(w in text for w in ("invalid", "not found", "unable", "incorrect",
                               "expired", "failed", "do not match",
                               "must contain")):
        return "error"

    return "info"



def create_notification(user_id=None, ngo_id=None, donation_id=None, message=""):

    db = get_db()

    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO notifications
        (user_id, ngo_id, donation_id, message)
        VALUES (%s, %s, %s, %s)
    """, (
        user_id,
        ngo_id,
        donation_id,
        message
    ))

    db.commit()

    cursor.close()
    db.close()


def allowed_image(filename):

    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_EXTENSIONS
    )

UPLOAD_FOLDER = os.path.join(
    app.root_path,
    "static",
    "distribution_proofs"
)

ALLOWED_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "webp"
}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Verification papers are private: they live OUTSIDE /static so only a
# logged-in admin can download them through /admin/ngo_document/<file>.
NGO_DOCUMENT_FOLDER = os.path.join(app.root_path, "private", "ngo_documents")
_LEGACY_DOCS = os.path.join(app.root_path, "static", "uploads", "ngo_documents")
ALLOWED_DOC_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

os.makedirs(NGO_DOCUMENT_FOLDER, exist_ok=True)

# One-time move of papers that older versions stored in the public folder.
if os.path.isdir(_LEGACY_DOCS):
    import shutil
    for _f in os.listdir(_LEGACY_DOCS):
        try:
            shutil.move(os.path.join(_LEGACY_DOCS, _f),
                        os.path.join(NGO_DOCUMENT_FOLDER, _f))
        except Exception as _e:
            print("DOC MOVE FAILED:", _f, _e)

MAIL_EMAIL = env("MAIL_EMAIL", "")
MAIL_APP_PASSWORD = env("MAIL_APP_PASSWORD", "")

def send_otp_email(receiver_email, otp):

    msg = EmailMessage()

    msg["Subject"] = "FoodResQ Password Reset Verification Code"
    msg["From"] = MAIL_EMAIL
    msg["To"] = receiver_email

    msg.set_content(f"""
Hello,

We received a request to reset your FoodResQ password.

Your verification code is:

{otp}

This code is valid for 5 minutes.

If you did not request a password reset, please ignore this email.

Regards,
FoodResQ Team
""")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:

        smtp.login(MAIL_EMAIL, MAIL_APP_PASSWORD)

        smtp.send_message(msg)


# =====================================================
# FOODRESQ DONATION WORKFLOW
# =====================================================

DONATION_STAGES = [

    "Pending",

    "Admin Verified",

    "Assigned",

    "NGO Accepted",

    "At Donor",

    "Donation Verified",

    "Pickup Confirmed",

    "On The Way",

    "Reached Distribution",

    "Distributed",

    "Proof Submitted",

    "Completed"

]

def send_pickup_verification_email(
    receiver_email,
    donor_name,
    donation_id,
    food_name,
    pickup_code
):
    """
    Sends the donor their pickup verification code.
    """

    if not receiver_email:
        print("PICKUP CODE EMAIL ERROR: Donor email is empty.")
        return False

    if not MAIL_EMAIL or not MAIL_APP_PASSWORD:
        print(
            "PICKUP CODE EMAIL ERROR: "
            "MAIL_EMAIL or MAIL_APP_PASSWORD is not configured."
        )
        return False

    try:

        subject = "FoodResQ - Pickup Verification Code"

        body = f"""Dear {donor_name},

Your FoodResQ donation is ready for pickup.

Donation ID: #{donation_id}
Food: {food_name}

Your Pickup Verification Code is:

{pickup_code}

IMPORTANT:
Please give this code to the NGO representative when they arrive to collect your donation.

The NGO must enter this code on FoodResQ to confirm that the food has been picked up.

Please do not share this code with anyone other than the NGO representative collecting your donation.

Thank you for helping turn surplus into sustenance.

Regards,
FoodResQ Team
Turning Surplus into Sustenance
"""

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = MAIL_EMAIL
        msg["To"] = receiver_email
        msg.set_content(body)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(
                MAIL_EMAIL,
                MAIL_APP_PASSWORD
            )

            smtp.send_message(msg)

        print(
            "PICKUP CODE EMAIL SENT:",
            pickup_code,
            "->",
            receiver_email
        )

        return True

    except Exception as e:

        print(
            "PICKUP CODE EMAIL ERROR:",
            repr(e)
        )

        return False

def send_ngo_status_email(receiver_email, ngo_name, status):
    """
    Sends an email to the NGO when the admin approves or rejects
    the NGO registration application.
    """

    if not receiver_email:
        print("NGO STATUS EMAIL ERROR: NGO email address is empty.")
        return False

    if not MAIL_EMAIL or not MAIL_APP_PASSWORD:
        print("NGO STATUS EMAIL ERROR: MAIL_EMAIL or MAIL_APP_PASSWORD is not configured.")
        return False

    try:

        if status == "Approved":

            subject = "FoodResQ NGO Application Approved"

            body = f"""Dear {ngo_name},

We are pleased to inform you that your NGO application for FoodResQ has been APPROVED.

Your NGO account is now active.

You can now log in to FoodResQ and start receiving donation assignments.

Thank you for joining FoodResQ and helping us turn surplus into sustenance.

Regards,
FoodResQ Team
Turning Surplus into Sustenance
"""

        elif status == "Rejected":

            subject = "FoodResQ NGO Application Status"

            body = f"""Dear {ngo_name},

We regret to inform you that your NGO application for FoodResQ has been REJECTED.

Please contact the FoodResQ administrator if you require more information regarding your application.

Regards,
FoodResQ Team
Turning Surplus into Sustenance
"""

        else:

            print("NGO STATUS EMAIL ERROR: Invalid status:", status)
            return False


        msg = EmailMessage()

        msg["Subject"] = subject
        msg["From"] = MAIL_EMAIL
        msg["To"] = receiver_email

        msg.set_content(body)


        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:

            smtp.login(
                MAIL_EMAIL,
                MAIL_APP_PASSWORD
            )

            smtp.send_message(msg)


        print(
            f"NGO STATUS EMAIL SENT: {status} -> {receiver_email}"
        )

        return True


    except Exception as e:

        print(
            "NGO STATUS EMAIL ERROR:",
            repr(e)
        )

        return False

def generate_pickup_code():

    characters = string.ascii_uppercase + string.digits

    while True:

        part1 = ''.join(secrets.choice(characters) for _ in range(4))

        part2 = ''.join(secrets.choice(string.digits) for _ in range(2))

        code = f"FRQ-{part1}-{part2}"

        conn = None
        cursor = None

        try:

            conn = get_db()

            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT id
                FROM donations
                WHERE pickup_code = %s
                """,
                (code,)
            )

            existing = cursor.fetchone()

            if not existing:
                return code

        finally:

            if cursor:
                cursor.close()

            if conn:
                conn.close()


# =========================================================
# SECURITY GATE + ACCESS QUEUE ROUTES
# =========================================================

@app.before_request
def security_gate():

    path = request.path

    if path.startswith("/static/") or path == "/health":
        return None

    # 1. Throttle abuse-prone POST endpoints (brute force, spam, OTP guessing)
    if request.method == "POST" and path in RATE_RULES:

        limit, window = RATE_RULES[path]

        if rate_limited(path, limit, window):

            if path == "/access_heartbeat":
                return jsonify(error="Too many requests"), 429

            return make_response(render_template(
                "error.html", code=429, title="Slow down a little",
                message="Too many attempts from your connection. "
                        "Please wait a few minutes and try again."), 429)

    # 2. CSRF: every state-changing request must carry the session token
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not csrf_ok():

        if request.headers.get("X-CSRF-Token") is not None \
                or request.path == "/access_heartbeat":
            return jsonify(error="Session expired. Refresh the page."), 400

        return make_response(render_template(
            "error.html", code=400, title="Session expired",
            message="Your form session expired or was invalid. "
                    "Go back, refresh the page and try again."), 400)

    # 3. Role check: the right kind of account for the right area
    needed = required_role(path)

    if needed and not role_allowed(needed):

        # Do not reveal that an admin area exists (or where its login is).
        if needed == "admin":
            abort(404)

        if request.method == "POST" and request.is_json:
            return jsonify(error="Please log in"), 401

        return redirect(LOGIN_FOR_ROLE.get(needed, "/user_login"))

    # 4. Access queue: logged-in users without a free slot wait their turn
    if needed and session.get("role") in ("donor", "ngo") \
            and not path.startswith(QUEUE_EXEMPT) \
            and not has_active_slot():

        return redirect(url_for("access_queue"))

    return None


@app.after_request
def add_security_headers(response):
    return apply_security_headers(response)


def _role_home():
    role = session.get("role")
    if role == "ngo":
        return "/ngo_home"
    if role == "admin":
        return "/admin_dashboard"
    if role == "donor":
        return "/user_dashboard"
    return "/"


@app.route("/access_queue")
def access_queue():

    if session.get("role") not in ("donor", "ngo"):
        return redirect("/")

    state = queue_state()

    if state.get("gone"):
        acquire_access_slot()
        state = queue_state()

    if state.get("active"):
        session["q_active"] = True
        session["q_checked"] = time.time()
        return redirect(url_for("access_redirect"))

    return render_template(
        "access_queue.html",
        position=state.get("position", 1),
        waiting_total=state.get("waiting_total", 1),
        capacity=state.get("capacity", core.MAX_ACTIVE_USERS),
        eta_minutes=state.get("eta_minutes", 1),
        role=session.get("role"),
    )


@app.route("/access_heartbeat", methods=["POST"])
def access_heartbeat():

    if session.get("role") not in ("donor", "ngo"):
        return jsonify(active=False, gone=True), 401

    state = queue_state()

    if state.get("active"):
        session["q_active"] = True
        session["q_checked"] = time.time()

    response = jsonify(state)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/access_redirect")
def access_redirect():

    if session.get("role") in ("donor", "ngo") and not has_active_slot():
        return redirect(url_for("access_queue"))

    return redirect(_role_home())


@app.route("/queue_leave", methods=["POST"])
def queue_leave():

    release_access_slot()
    session.clear()
    return redirect("/")


@app.route("/admin/queue_status")
def admin_queue_status():
    return jsonify(queue_totals())


@app.route("/health")
def health():

    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        db.close()
        return jsonify(status="ok"), 200

    except Exception:
        return jsonify(status="database unavailable"), 503


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, title="Page not found",
        message="The page you are looking for does not exist or has moved."), 404


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, title="Not allowed",
        message="You do not have permission to open this page."), 403


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", code=413, title="File too large",
        message="The upload is larger than 12 MB. Please choose a smaller file."), 413


@app.errorhandler(500)
def server_error(_e):
    return render_template("error.html", code=500, title="Something went wrong",
        message="We hit a problem on our side. Please try again in a moment."), 500


@app.route("/")
def home():

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # Total donations
    cursor.execute("SELECT COUNT(*) AS total FROM donations")
    total_donations = cursor.fetchone()["total"]

    # Total NGOs
    cursor.execute("SELECT COUNT(*) AS total FROM ngos")
    total_ngos = cursor.fetchone()["total"]

    # Total donors
    cursor.execute("SELECT COUNT(*) AS total FROM users")
    total_donors = cursor.fetchone()["total"]

    # Meals Saved
    cursor.execute("SELECT SUM(quantity) AS meals FROM donations")
    result = cursor.fetchone()

    meals_saved = result["meals"] if result["meals"] else 0

    # Recent Donations
    cursor.execute("""
    SELECT
        donation_id,
        food_name,
        quantity,
        quantity_unit,
        city,
        status
    FROM donations
    ORDER BY donation_id DESC
    LIMIT 3
    """)

    recent_donations = cursor.fetchall()

    cursor.execute("""
    SELECT
        ngo_name,
        partner_id,
        service_area
    FROM ngos
    WHERE status='Active'
    ORDER BY ngo_id DESC
    LIMIT 4
    """)

    partner_ngos = cursor.fetchall()

    cursor.close()
    db.close()


    return render_template(
        "index.html",
        total_donations=total_donations,
        total_ngos=total_ngos,
        total_donors=total_donors,
        meals_saved=meals_saved,
        recent_donations=recent_donations,
        partner_ngos=partner_ngos
    )
    

@app.route("/about")
def about():

    return render_template("about.html")

@app.route("/findngos")
def findngos():

    db = get_db()

    cursor = db.cursor()

    cursor.execute("""
        SELECT id, ngo_name, email, phone, location
        FROM ngo
        ORDER BY ngo_name
    """)

    ngos = cursor.fetchall()

    db.close()

    return render_template("findngos.html", ngos=ngos)

@app.route("/user_registration")
def user_registration():

    return render_template("user_registration.html")


@app.route("/register_user", methods=["POST"])
def register_user():

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()
    password = request.form.get("password", "")

    name = re.sub(r"\s+", " ", name)

    # =========================================================
    # VALIDATION
    # =========================================================

    _problem = None

    if not valid_name(name):
        _problem = "Name must contain letters only (2-60 characters)."

    elif not valid_email(email):
        _problem = "Please enter a valid email address."

    elif not valid_phone(phone):
        _problem = (
            "Phone number must be exactly 10 digits "
            "and start with 6, 7, 8 or 9."
        )

    else:
        _problem = password_problem(password)

    if _problem:

        return render_template(
            "user_registration.html",
            error=_problem,
            name=name,
            email=email,
            phone=phone
        )

    db = None
    cursor = None

    try:

        db = get_db()
        cursor = db.cursor(dictionary=True)

        # =====================================================
        # CHECK EMAIL
        # =====================================================

        cursor.execute("""
            SELECT
                id,
                email,
                phone,
                deactivated_until
            FROM users
            WHERE email = %s
            LIMIT 1
        """, (email,))

        existing_email = cursor.fetchone()

        if existing_email:

            if existing_email["deactivated_until"]:

                if datetime.now() < existing_email["deactivated_until"]:

                    remaining = (
                        existing_email["deactivated_until"]
                        - datetime.now()
                    )

                    days = remaining.days

                    if days > 0:

                        error = (
                            "This account has been deactivated by "
                            "the administrator. You cannot register "
                            f"again for another {days} day(s)."
                        )

                    else:

                        error = (
                            "This account has been deactivated by "
                            "the administrator. Please try again later."
                        )

                    return render_template(
                        "user_registration.html",
                        error=error,
                        name=name,
                        email=email,
                        phone=phone
                    )

            else:

                return render_template(
                    "user_registration.html",
                    error="An account with this email already exists.",
                    name=name,
                    email=email,
                    phone=phone
                )

        # =====================================================
        # CHECK PHONE
        # =====================================================

        cursor.execute("""
            SELECT
                id,
                email,
                phone,
                deactivated_until
            FROM users
            WHERE phone = %s
            LIMIT 1
        """, (phone,))

        existing_phone = cursor.fetchone()

        if existing_phone:

            if existing_phone["deactivated_until"]:

                if datetime.now() < existing_phone["deactivated_until"]:

                    remaining = (
                        existing_phone["deactivated_until"]
                        - datetime.now()
                    )

                    days = remaining.days

                    if days > 0:

                        error = (
                            "This phone number belongs to a "
                            "deactivated account. You cannot "
                            "register again for another "
                            f"{days} day(s)."
                        )

                    else:

                        error = (
                            "This phone number belongs to a "
                            "deactivated account. Please try again later."
                        )

                    return render_template(
                        "user_registration.html",
                        error=error,
                        name=name,
                        email=email,
                        phone=phone
                    )

            else:

                return render_template(
                    "user_registration.html",
                    error="An account with this phone number already exists.",
                    name=name,
                    email=email,
                    phone=phone
                )

        # =====================================================
        # CREATE DONOR ACCOUNT
        # =====================================================

        hashed_password = generate_password_hash(password)

        cursor.execute("""
            INSERT INTO users
            (
                name,
                email,
                phone,
                password
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s
            )
        """, (
            name,
            email,
            phone,
            hashed_password
        ))

        db.commit()

        # =====================================================
        # GET NEW USER DATABASE ID
        # =====================================================

        user_id = cursor.lastrowid

        # =====================================================
        # GENERATE FOODRESQ DONOR ID
        #
        # Example:
        # Database ID 1 -> FRQ-DNR-0001
        # Database ID 25 -> FRQ-DNR-0025
        # =====================================================

        donor_code = f"FRQ-DNR-{user_id:04d}"

        cursor.execute("""
            UPDATE users
            SET donor_code = %s
            WHERE id = %s
        """, (
            donor_code,
            user_id
        ))

        db.commit()

        # =====================================================
        # LOGIN SESSION
        # =====================================================

        session.clear()

        session.permanent = True

        session["user_id"] = user_id
        session["user_name"] = name
        session["role"] = "donor"
        session["donor_code"] = donor_code
        session["user_email"] = email
        session["profile_photo"] = None

        # =====================================================
        # ACCESS QUEUE
        # =====================================================

        slot = acquire_access_slot()

        if slot.get("allowed"):

            return redirect(
                url_for("user_dashboard")
            )

        return redirect(
            url_for("access_queue")
        )

    # =========================================================
    # DATABASE ERROR
    # =========================================================

    except mysql.connector.Error as e:

        if db:
            db.rollback()

        print(
            "REGISTRATION DATABASE ERROR:",
            repr(e)
        )

        error_message = str(e).lower()

        if (
            "duplicate" in error_message
            or "1062" in error_message
            or "unique" in error_message
        ):

            return render_template(
                "user_registration.html",
                error=(
                    "An account with this email "
                    "or phone number already exists."
                ),
                name=name,
                email=email,
                phone=phone
            )

        return render_template(
            "user_registration.html",
            error=(
                "Unable to create your account. "
                "Please try again."
            ),
            name=name,
            email=email,
            phone=phone
        )

    # =========================================================
    # GENERAL ERROR
    # =========================================================

    except Exception as e:

        if db:
            db.rollback()

        import traceback

        print(
            "REGISTRATION ERROR:",
            repr(e)
        )

        traceback.print_exc()

        return render_template(
            "user_registration.html",
            error=(
                "Something went wrong. "
                "Please try again."
            ),
            name=name,
            email=email,
            phone=phone
        )

    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

@app.route("/user_dashboard")
def user_dashboard():

    if "user_id" not in session or session.get("role") != "donor":
        return redirect("/user_login")

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # -----------------------------
    # Recent donations
    # -----------------------------
    cursor.execute("""
        SELECT
            donation_id,
            food_name,
            quantity,
            quantity_unit,
            pickup_date,
            status
        FROM donations
        WHERE user_id = %s
        ORDER BY donation_id DESC
        LIMIT 5
    """, (session["user_id"],))

    donations = cursor.fetchall()

    # -----------------------------
    # Total donations
    # -----------------------------
    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM donations
        WHERE user_id = %s
    """, (session["user_id"],))

    donations_count = cursor.fetchone()["total"]

    # -----------------------------
    # Pending donations
    # -----------------------------
    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM donations
        WHERE user_id = %s
        AND status = 'Pending'
    """, (session["user_id"],))

    pending_count = cursor.fetchone()["total"]

    # -----------------------------
    # Completed donations
    # -----------------------------
    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM donations
        WHERE user_id = %s
        AND status = 'Completed'
    """, (session["user_id"],))

    completed_count = cursor.fetchone()["total"]

    # -----------------------------
    # Meals saved by this donor
    # -----------------------------
    cursor.execute("""
        SELECT COALESCE(SUM(quantity), 0) AS total
        FROM donations
        WHERE user_id = %s
        AND status = 'Completed'
    """, (session["user_id"],))

    meals_saved = cursor.fetchone()["total"]

    # -----------------------------
    # Donation Goal
    # -----------------------------
    donation_goal = 1000

    # Total completed meals across FoodResQ
    cursor.execute("""
        SELECT COALESCE(SUM(quantity), 0) AS total
        FROM donations
        WHERE status = 'Completed'
    """)

    meals_completed = cursor.fetchone()["total"]

    # Calculate percentage
    progress = (meals_completed / donation_goal) * 100

    if progress > 100:
        progress = 100

    # -----------------------------
    # Close database
    # -----------------------------
    cursor.close()
    db.close()

    # -----------------------------
    # RETURN PAGE
    # -----------------------------
    return render_template(
        "user_dashboard.html",
        donations=donations,
        donations_count=donations_count,
        pending_count=pending_count,
        completed_count=completed_count,
        meals_saved=meals_saved,
        donation_goal=donation_goal,
        meals_completed=meals_completed,
        progress=progress
    )
@app.route("/user_login")
def user_login():

    return render_template("user_login.html")

@app.route("/login_user", methods=["POST"])
def login_user():

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not valid_email(email) or not password or len(password) > 128:
        return notify_redirect("Invalid Email or Password", "/user_login")

    db = get_db()

    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM users
        WHERE email=%s
    """, (email,))

    user = cursor.fetchone()

    # -------------------------------------------------
    # NO ACCOUNT WITH THIS EMAIL
    # -------------------------------------------------

    if not user:

        cursor.close()
        db.close()

        return notify_redirect("Invalid Email or Password", "/user_login")

    # -------------------------------------------------
    # CHECK 30-DAY ADMIN DEACTIVATION
    # -------------------------------------------------

    if user["deactivated_until"]:

        if datetime.now() < user["deactivated_until"]:

            remaining = user["deactivated_until"] - datetime.now()

            days = remaining.days

            hours = remaining.seconds // 3600

            if days > 0:
                message = f"Your account has been deactivated by the administrator. Please try again after {days} day(s)."

            else:
                message = f"Your account has been deactivated by the administrator. Please try again after {hours} hour(s)."

            cursor.close()
            db.close()

            return notify_redirect(f"{message}", "/user_login")

        else:

            # -------------------------------------------------
            # DEACTIVATION PERIOD FINISHED
            # -------------------------------------------------

            cursor.execute("""
                UPDATE users
                SET deactivated_until=NULL
                WHERE id=%s
            """, (user["id"],))

            db.commit()

            user["deactivated_until"] = None

    # -------------------------------------------------
    # CHECK WHETHER ACCOUNT IS CURRENTLY LOCKED
    # -------------------------------------------------

    if user["locked_until"]:

        if datetime.now() < user["locked_until"]:

            remaining = user["locked_until"] - datetime.now()

            minutes = max(
                1,
                int(remaining.total_seconds() / 60)
            )

            cursor.close()
            db.close()

            return notify_redirect(f"Too many failed login attempts. Try again in {minutes} minutes.", "/user_login")

        else:

            # Lock period finished

            cursor.execute("""
                UPDATE users
                SET failed_attempts=0,
                    locked_until=NULL
                WHERE id=%s
            """, (user["id"],))

            db.commit()

            user["failed_attempts"] = 0
            user["locked_until"] = None

    # -------------------------------------------------
    # CHECK PASSWORD
    # -------------------------------------------------

    if check_password_hash(user["password"], password):

        # Successful login → reset attempts

        cursor.execute("""
            UPDATE users
            SET failed_attempts=0,
                locked_until=NULL
            WHERE id=%s
        """, (user["id"],))

        db.commit()

        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["user_email"] = user["email"]
        session["role"] = "donor"
        session["donor_code"] = user["donor_code"]
        session["profile_photo"] = user["profile_photo"]
        

        cursor.close()
        db.close()

        slot = acquire_access_slot()

        return redirect(
            "/user_dashboard" if slot.get("allowed") else "/access_queue")

    # -------------------------------------------------
    # WRONG PASSWORD
    # -------------------------------------------------

    else:

        attempts = user["failed_attempts"] + 1

        # -------------------------------------------------
        # 5 FAILED ATTEMPTS → 15 MINUTE LOCK
        # -------------------------------------------------

        if attempts >= 5:

            lock_time = datetime.now() + timedelta(minutes=15)

            cursor.execute("""
                UPDATE users
                SET failed_attempts=%s,
                    locked_until=%s
                WHERE id=%s
            """, (
                attempts,
                lock_time,
                user["id"]
            ))

            db.commit()

            cursor.close()
            db.close()

            return notify_redirect("Too many failed login attempts. Your account is locked for 15 minutes.", "/user_login")

        # -------------------------------------------------
        # LESS THAN 5 FAILED ATTEMPTS
        # -------------------------------------------------

        else:

            cursor.execute("""
                UPDATE users
                SET failed_attempts=%s
                WHERE id=%s
            """, (
                attempts,
                user["id"]
            ))

            db.commit()

            remaining = 5 - attempts

            cursor.close()
            db.close()

            return notify_redirect(f"Invalid Email or Password. {remaining} login attempts remaining.", "/user_login")
    
@app.route("/logout")
def logout():

    release_access_slot()

    session.clear()

    return redirect("/")




@app.route("/donate")
def donate_food():

    if "user_id" not in session:
        return redirect(url_for("user_login"))

    now = datetime.now()

    today = now.date()
    prepared_min_date = today - timedelta(days=3)
    prepared_max_date = today

    pickup_min_date = today
    pickup_max_date = today + timedelta(days=7)

    return render_template(
        "donate_food.html",

        # PREPARED DATE
        prepared_min_date=prepared_min_date.strftime("%Y-%m-%d"),
        prepared_max_date=prepared_max_date.strftime("%Y-%m-%d"),

        # PICKUP DATE
        pickup_min_date=pickup_min_date.strftime("%Y-%m-%d"),
        pickup_max_date=pickup_max_date.strftime("%Y-%m-%d")
    )

@app.route('/submit_donation', methods=['POST'])
def submit_donation():

    if 'user_id' not in session:
        return redirect(url_for('user_login'))

    user_id = session['user_id']

    # =========================================================
    # FOOD INFORMATION
    # =========================================================

    food_name = request.form.get('food_name', '').strip()
    food_type = request.form.get('food_type')
    food_description = request.form.get('food_description')


    # =========================================================
    # QUANTITY
    # =========================================================

    quantity = request.form.get('quantity')
    quantity_unit = request.form.get('quantity_unit')
    serving_size = request.form.get('serving_size')


    # =========================================================
    # FOOD SAFETY
    # =========================================================

    prepared_date = request.form.get('prepared_date')
    preparation_time = request.form.get('prepared_time')

    expiry_date = request.form.get('best_before_date')
    expiry_time = request.form.get('best_before_time')

    storage_condition = request.form.get('storage_condition')
    allergen_status = request.form.get('allergen_status')
    allergens = request.form.get('allergens')


    # =========================================================
    # LOCATION
    # =========================================================

    address = request.form.get('address')
    landmark = request.form.get('landmark')
    city = request.form.get('city')

    latitude = request.form.get('latitude')
    longitude = request.form.get('longitude')

    if latitude == '':
        latitude = None

    if longitude == '':
        longitude = None


    # =========================================================
    # PICKUP DETAILS
    # =========================================================

    pickup_date = request.form.get('pickup_date')
    pickup_time = request.form.get('pickup_time')

    contact = request.form.get('contact', '').strip()

    pickup_instructions = request.form.get(
        'pickup_instructions'
    )


    # =========================================================
    # ADDITIONAL INFORMATION
    # =========================================================

    packaging_type = request.form.get('packaging_type')
    details = request.form.get('details')


    # =========================================================
    # CURRENT DATE/TIME
    # =========================================================

    now = datetime.now()

    today = now.date()

    prepared_min_date = today - timedelta(days=3)
    prepared_max_date = today


    # =========================================================
    # BASIC VALIDATION
    # =========================================================

    donation_error = None


    if not food_name:

        donation_error = (
            "Please enter the food name."
        )


    elif positive_int(
        quantity or "",
        100000
    ) is None:

        donation_error = (
            "Quantity must be a whole number "
            "of 1 or more (digits only)."
        )


    elif not valid_phone(contact):

        donation_error = (
            "Contact number must be exactly "
            "10 digits and start with 6, 7, 8 or 9."
        )


    # =========================================================
    # PREPARED DATE/TIME VALIDATION
    # =========================================================

    prepared_datetime = None

    if not donation_error:

        if not prepared_date:

            donation_error = (
                "Please select the prepared date."
            )

        elif not preparation_time:

            donation_error = (
                "Please select the prepared time."
            )

        else:

            try:

                prepared_datetime = datetime.strptime(
                    prepared_date + " " + preparation_time,
                    "%Y-%m-%d %H:%M"
                )


                # -------------------------------------------------
                # PREPARED DATE CANNOT BE MORE THAN 3 DAYS OLD
                # -------------------------------------------------

                if prepared_datetime.date() < prepared_min_date:

                    donation_error = (
                        "Prepared date cannot be more than "
                        "3 days before today."
                    )


                # -------------------------------------------------
                # PREPARED DATE CANNOT BE IN FUTURE
                # -------------------------------------------------

                elif prepared_datetime.date() > prepared_max_date:

                    donation_error = (
                        "Prepared date cannot be in the future."
                    )


                # -------------------------------------------------
                # PREPARED TIME CANNOT BE IN FUTURE
                # IF PREPARED DATE IS TODAY
                # -------------------------------------------------

                elif prepared_datetime > now:

                    donation_error = (
                        "Prepared time cannot be in the future."
                    )


            except ValueError:

                donation_error = (
                    "Please enter a valid prepared "
                    "date and time."
                )


    # =========================================================
    # BEST-BEFORE DATE/TIME
    # =========================================================

    best_before_datetime = None

    if not donation_error:

        if not expiry_date:

            donation_error = (
                "Please select the best-before date."
            )

        elif not expiry_time:

            donation_error = (
                "Please select the best-before time."
            )

        else:

            try:

                best_before_datetime = datetime.strptime(
                    expiry_date + " " + expiry_time,
                    "%Y-%m-%d %H:%M"
                )


                # -------------------------------------------------
                # BEST BEFORE CANNOT BE BEFORE PREPARATION
                # -------------------------------------------------

                if (
                    prepared_datetime is not None
                    and best_before_datetime <= prepared_datetime
                ):

                    donation_error = (
                        "Best-before date and time must be "
                        "after the prepared date and time."
                    )


                # -------------------------------------------------
                # BEST BEFORE CANNOT ALREADY BE EXPIRED
                # -------------------------------------------------

                elif best_before_datetime <= now:

                    donation_error = (
                        "Best-before date and time must "
                        "be in the future."
                    )


            except ValueError:

                donation_error = (
                    "Please enter a valid best-before "
                    "date and time."
                )


    # =========================================================
    # PICKUP DATE/TIME
    # =========================================================

    pickup_datetime = None

    if not donation_error:

        if not pickup_date:

            donation_error = (
                "Please select the pickup date."
            )

        elif not pickup_time:

            donation_error = (
                "Please select the pickup time."
            )

        else:

            try:

                pickup_datetime = datetime.strptime(
                    pickup_date + " " + pickup_time,
                    "%Y-%m-%d %H:%M"
                )


                # -------------------------------------------------
                # PICKUP MUST BE AT LEAST 30 MINUTES FROM NOW
                # -------------------------------------------------

                minimum_pickup_datetime = (
                    now + timedelta(minutes=30)
                )

                if pickup_datetime < minimum_pickup_datetime:

                    donation_error = (
                        "Pickup must be at least "
                        "30 minutes from now."
                    )


                # -------------------------------------------------
                # PICKUP CANNOT BE MORE THAN 7 DAYS AHEAD
                # -------------------------------------------------

                elif pickup_datetime.date() > (
                    today + timedelta(days=7)
                ):

                    donation_error = (
                        "Pickup date cannot be more "
                        "than 7 days from today."
                    )


                # -------------------------------------------------
                # PICKUP TIME 8 AM - 8 PM
                # -------------------------------------------------

                elif not (
                    8 <= pickup_datetime.hour <= 20
                ):

                    donation_error = (
                        "Pickup time must be between "
                        "8:00 AM and 8:00 PM."
                    )


                # -------------------------------------------------
                # BEST BEFORE MUST BE BEFORE/EQUAL TO PICKUP
                # -------------------------------------------------

                elif (
                    best_before_datetime is not None
                    and best_before_datetime > pickup_datetime
                ):

                    donation_error = (
                        "Best-before date and time cannot "
                        "be after the pickup date and time."
                    )


            except ValueError:

                donation_error = (
                    "Please enter a valid pickup "
                    "date and time."
                )


    # =========================================================
    # LOCATION VALIDATION
    # =========================================================

    if not donation_error:

        try:

            if latitude is not None:

                lat_value = float(latitude)
                lon_value = float(longitude)

                if not (
                    -90 <= lat_value <= 90
                    and
                    -180 <= lon_value <= 180
                ):

                    raise ValueError

        except (
            TypeError,
            ValueError
        ):

            donation_error = (
                "The selected map location is not valid. "
                "Please pick it again."
            )


    # =========================================================
    # STOP IF VALIDATION FAILED
    # =========================================================

    if donation_error:

        return notify_redirect(
            donation_error,
            "/donate",
            kind="error"
        )


    # =========================================================
    # DATABASE
    # =========================================================

    db = None
    cursor = None

    try:

        db = get_db()

        cursor = db.cursor()


        query = """
            INSERT INTO donations
            (
                user_id,
                food_name,
                food_type,
                food_description,
                quantity,
                quantity_unit,
                serving_size,
                address,
                landmark,
                city,
                latitude,
                longitude,
                prepared_date,
                preparation_time,
                expiry_date,
                expiry_time,
                storage_condition,
                allergen_status,
                allergens,
                pickup_date,
                pickup_time,
                contact,
                pickup_instructions,
                packaging_type,
                details,
                status
            )
            VALUES
            (
                %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s
            )
        """


        values = (

            user_id,

            food_name,
            food_type,
            food_description,

            quantity,
            quantity_unit,
            serving_size,

            address,
            landmark,
            city,

            latitude,
            longitude,

            prepared_date,
            preparation_time,

            expiry_date,
            expiry_time,

            storage_condition,

            allergen_status,
            allergens,

            pickup_date,
            pickup_time,

            contact,
            pickup_instructions,

            packaging_type,
            details,

            "Pending"
        )


        cursor.execute(
            query,
            values
        )

        db.commit()


        donation_id = cursor.lastrowid

        print(
            "DONATION CREATED:",
            donation_id
        )


        return redirect(
            url_for("my_donations")
        )


    except Exception as e:

        if db:
            db.rollback()

        print(
            "DONATION ERROR:",
            e
        )

        return (
            "Error submitting donation: "
            + str(e)
        ), 500


    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

def get_stage_status(current_status, stage):
    try:
        current_index = DONATION_STAGES.index(current_status)
        stage_index = DONATION_STAGES.index(stage)

        if stage_index < current_index:
            return "completed"

        elif stage_index == current_index:
            return "current"

        else:
            return "pending"

    except ValueError:
        return "pending"

@app.route("/my_donations")
def my_donations():

    user_id = session["user_id"]

    db = get_db()

    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            d.*,
            n.ngo_name,
            n.partner_id,
            n.phone AS ngo_phone,
            n.address AS ngo_address,
            n.service_area
        FROM donations d
        LEFT JOIN ngos n
            ON d.ngo_id = n.ngo_id
        WHERE d.user_id = %s
        ORDER BY d.donation_id DESC
    """, (user_id,))

    donations = cursor.fetchall()

    cursor.close()
    db.close()

    for donation in donations:

        donation["timeline"] = []

        for stage in DONATION_STAGES:

            donation["timeline"].append({
                "name": stage,
                "status": get_stage_status(
                    donation["status"],
                    stage
                )
            })

    return render_template(
        "my_donations.html",
        donations=donations
    )

@app.route("/ngo_registration")
def ngo_registration():

    return render_template(
        "ngo_registration.html"
    )


@app.route("/register_ngo", methods=["POST"])
def register_ngo():

    # =========================================================
    # GET FORM DATA
    # =========================================================

    ngo_name = request.form.get("ngo_name", "").strip()
    organization_type = request.form.get("organization_type", "").strip()
    registration_number = request.form.get("registration_number", "").strip()
    darpan_id = request.form.get("darpan_id", "").strip()
    year_established = request.form.get("year_established", "").strip()

    person_name = request.form.get("person_name", "").strip()
    designation = request.form.get("designation", "").strip()

    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()

    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    address = request.form.get("address", "").strip()
    pincode = request.form.get("pincode", "").strip()
    service_area = request.form.get("service_area", "").strip()

    latitude = request.form.get("latitude", "").strip()
    longitude = request.form.get("longitude", "").strip()

    agreement = request.form.get("agreement")


    # =========================================================
    # BASIC VALIDATION
    # =========================================================

    if not ngo_name:
        return render_template(
            "ngo_registration.html",
            error="Please enter the NGO / organization name."
        )

    if not organization_type:
        return render_template(
            "ngo_registration.html",
            error="Please select the organization type."
        )

    if not registration_number:
        return render_template(
            "ngo_registration.html",
            error="Please enter the registration number."
        )

    if not person_name:
        return render_template(
            "ngo_registration.html",
            error="Please enter the authorized person's name."
        )

    if not designation:
        return render_template(
            "ngo_registration.html",
            error="Please enter the designation."
        )

    if not email:
        return render_template(
            "ngo_registration.html",
            error="Please enter the email address."
        )

    if not phone or len(phone) != 10 or not phone.isdigit():
        return render_template(
            "ngo_registration.html",
            error="Please enter a valid 10-digit phone number."
        )

    if password != confirm_password:
        return render_template(
            "ngo_registration.html",
            error="Passwords do not match."
        )

    if len(password) < 6:
        return render_template(
            "ngo_registration.html",
            error="Password must contain at least 6 characters."
        )

    if not address:
        return render_template(
            "ngo_registration.html",
            error="Please enter the complete NGO address."
        )

    if not pincode or len(pincode) != 6 or not pincode.isdigit():
        return render_template(
            "ngo_registration.html",
            error="Please enter a valid 6-digit PIN code."
        )

    if not service_area:
        return render_template(
            "ngo_registration.html",
            error="Please enter the NGO service area."
        )

    if not latitude or not longitude:
        return render_template(
            "ngo_registration.html",
            error="Please select and confirm the NGO service location."
        )

    if not agreement:
        return render_template(
            "ngo_registration.html",
            error="Please accept the declaration before registering."
        )

    # ---- strict format checks (server side, cannot be bypassed) ----
    email = email.lower()
    _keep = dict(ngo_name=ngo_name, email=email, phone=phone,
                 address=address, service_area=service_area)
    _bad = None
    if not valid_org_name(ngo_name):
        _bad = "NGO name may only use letters, numbers and . , & ' ( ) - (3-100 characters)."
    elif not valid_regno(registration_number):
        _bad = "Registration number may only use letters, numbers, / - and . (3-40 characters)."
    elif darpan_id and not re.fullmatch(r"[A-Za-z]{2}/\d{4}/\d{7}|[A-Za-z0-9/\-]{5,30}", darpan_id):
        _bad = "Please enter a valid NGO Darpan ID."
    elif year_established and not valid_year(year_established):
        _bad = "Year established must be a 4-digit year, not in the future."
    elif not valid_name(person_name):
        _bad = "Authorized person name must contain letters only."
    elif not valid_name(designation):
        _bad = "Designation must contain letters only."
    elif not valid_email(email):
        _bad = "Please enter a valid email address."
    elif not valid_phone(phone):
        _bad = "Phone number must be exactly 10 digits and start with 6, 7, 8 or 9."
    elif not valid_pincode(pincode):
        _bad = "PIN code must be exactly 6 digits and cannot start with 0."
    elif password_problem(password):
        _bad = password_problem(password)
    else:
        try:
            _lat, _lng = float(latitude), float(longitude)
            if not (-90 <= _lat <= 90 and -180 <= _lng <= 180):
                raise ValueError
        except ValueError:
            _bad = "The selected map location is not valid. Please pick it again."
    if _bad:
        return render_template("ngo_registration.html", error=_bad, **_keep)


    # =========================================================
    # FILES
    # =========================================================

    registration_document = request.files.get(
        "registration_document"
    )

    authorized_person_document = request.files.get(
        "authorized_person_document"
    )


    if not registration_document or registration_document.filename == "":
        return render_template(
            "ngo_registration.html",
            error="Please upload the registration document."
        )


    if not authorized_person_document or authorized_person_document.filename == "":
        return render_template(
            "ngo_registration.html",
            error="Please upload the authorized person's document."
        )


    # =========================================================
    # PASSWORD HASH
    # =========================================================

    hashed_password = generate_password_hash(password)


    # =========================================================
    # DATABASE CONNECTION
    # =========================================================

    db = get_db()

    cursor = db.cursor(dictionary=True)


    try:

        # =====================================================
        # CHECK DUPLICATE EMAIL
        # =====================================================

        cursor.execute("""
            SELECT ngo_id, ngo_name
            FROM ngos
            WHERE email = %s
        """, (email,))

        existing_email = cursor.fetchone()

        if existing_email:

            return render_template(
                "ngo_registration.html",
                error="An NGO account with this email already exists.",
                ngo_name=ngo_name,
                email=email,
                phone=phone,
                address=address,
                service_area=service_area
            )


        # =====================================================
        # CHECK DUPLICATE PHONE
        # =====================================================

        cursor.execute("""
            SELECT ngo_id
            FROM ngos
            WHERE phone = %s
        """, (phone,))

        existing_phone = cursor.fetchone()

        if existing_phone:

            return render_template(
                "ngo_registration.html",
                error="An NGO account with this phone number already exists.",
                ngo_name=ngo_name,
                email=email,
                phone=phone,
                address=address,
                service_area=service_area
            )


        # =====================================================
        # CHECK DUPLICATE REGISTRATION NUMBER
        # =====================================================

        cursor.execute("""
            SELECT ngo_id
            FROM ngos
            WHERE registration_number = %s
        """, (registration_number,))

        existing_registration = cursor.fetchone()

        if existing_registration:

            return render_template(
                "ngo_registration.html",
                error="An NGO with this registration number already exists.",
                ngo_name=ngo_name,
                email=email,
                phone=phone,
                address=address,
                service_area=service_area
            )


        # =====================================================
        # SAVE FILES
        # =====================================================

        import os
        from werkzeug.utils import secure_filename

        upload_folder = NGO_DOCUMENT_FOLDER

        os.makedirs(
            upload_folder,
            exist_ok=True
        )


        for _f in (registration_document, authorized_person_document):
            _ext = _f.filename.rsplit(".", 1)[-1].lower() if "." in _f.filename else ""
            if _ext not in ALLOWED_DOC_EXTENSIONS:
                return render_template(
                    "ngo_registration.html",
                    error="Documents must be PDF, PNG or JPG files.",
                    ngo_name=ngo_name, email=email, phone=phone,
                    address=address, service_area=service_area)

        registration_filename = secure_filename(
            registration_document.filename
        )

        authorized_filename = secure_filename(
            authorized_person_document.filename
        )


        # Make filenames unique
        import uuid

        registration_filename = (
            str(uuid.uuid4()) + "_" +
            registration_filename
        )

        authorized_filename = (
            str(uuid.uuid4()) + "_" +
            authorized_filename
        )


        registration_document.save(
            os.path.join(
                upload_folder,
                registration_filename
            )
        )


        authorized_person_document.save(
            os.path.join(
                upload_folder,
                authorized_filename
            )
        )


        # =====================================================
        # YEAR ESTABLISHED
        # =====================================================

        if year_established:
            year_value = int(year_established)
        else:
            year_value = None


        # =====================================================
        # NGO PARTNER ID
        # =====================================================

        partner_id = (
            "FRQ-NGO-" +
            str(uuid.uuid4())[:8].upper()
        )


        # =====================================================
        # INSERT NGO
        # =====================================================

        cursor.execute("""
            INSERT INTO ngos
            (
                ngo_name,
                organization_type,
                registration_number,
                darpan_id,
                year_established,
                person_name,
                designation,
                email,
                phone,
                password,
                address,
                pincode,
                service_area,
                latitude,
                longitude,
                registration_document,
                authorized_person_document,
                partner_id,
                status,
                verification_status
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                'Pending',
                'Pending'
            )
        """, (
            ngo_name,
            organization_type,
            registration_number,
            darpan_id if darpan_id else None,
            year_value,
            person_name,
            designation,
            email,
            phone,
            hashed_password,
            address,
            pincode,
            service_area,
            latitude,
            longitude,
            registration_filename,
            authorized_filename,
            partner_id
        ))


        # =====================================================
        # COMMIT
        # =====================================================

        db.commit()


        print(
            "NGO REGISTERED SUCCESSFULLY:",
            ngo_name,
            email
        )


        # =====================================================
        # REDIRECT TO LOGIN
        # =====================================================

        return redirect("/ngo_login")


    except mysql.connector.Error as e:

        db.rollback()

        print(
            "NGO REGISTRATION MYSQL ERROR:",
            e
        )


        if e.errno == 1062:

            return render_template(
                "ngo_registration.html",
                error="An NGO account with these details already exists.",
                ngo_name=ngo_name,
                email=email,
                phone=phone,
                address=address,
                service_area=service_area
            )


        return render_template(
            "ngo_registration.html",
            error="Unable to complete registration. Please check the server error.",
            ngo_name=ngo_name,
            email=email,
            phone=phone,
            address=address,
            service_area=service_area
        )


    except Exception as e:

        db.rollback()

        print(
            "NGO REGISTRATION ERROR:",
            e
        )


        return render_template(
            "ngo_registration.html",
            error="Something went wrong. Please try again."
        )


    finally:

        cursor.close()
        db.close()

@app.route("/ngo_registration_success")
def ngo_registration_success():

    return render_template(
        "ngo_success_registration.html"
    )

    

# ============================================================
# ADMIN - VIEW NGO APPLICATIONS
# ============================================================

@app.route("/admin/ngo_applications")
def ngo_applications():

    if "admin" not in session:
        return redirect("/admin_login")

    db = get_db()

    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            ngo_id,
            partner_id,
            ngo_name,
            organization_type,
            registration_number,
            darpan_id,
            person_name,
            designation,
            email,
            phone,
            year_established,
            latitude,
            longitude,
            address,
            pincode,
            service_area,
            registration_document,
            authorized_person_document,
            status,
            verification_status,
            registered_on
        FROM ngos
        WHERE verification_status = 'Pending'
        ORDER BY ngo_id DESC
    """)

    ngos = cursor.fetchall()

    cursor.close()
    db.close()

    return render_template(
        "admin_ngo_applications.html",
        ngos=ngos
    )


# ============================================================
# ADMIN - APPROVE NGO APPLICATION
# ============================================================

@app.route("/admin/approve_ngo/<int:ngo_id>", methods=["POST"])
def approve_ngo(ngo_id):

    if "admin" not in session:
        return redirect("/admin_login")

    db = None
    cursor = None

    try:

        db = get_db()

        cursor = db.cursor(dictionary=True)


        # ============================================================
        # CHECK NGO EXISTS
        # ============================================================

        cursor.execute("""
            SELECT
                ngo_id,
                ngo_name,
                email,
                verification_status
            FROM ngos
            WHERE ngo_id=%s
            LIMIT 1
        """, (ngo_id,))

        ngo = cursor.fetchone()


        if not ngo:

            return notify_redirect(
                "NGO application not found.",
                "/admin/ngo_applications"
            )


        # ============================================================
        # MAKE SURE APPLICATION IS STILL PENDING
        # ============================================================

        if ngo["verification_status"] != "Pending":

            return notify_redirect(
                "This application has already been processed.",
                "/admin/ngo_applications"
            )


        # ============================================================
        # APPROVE NGO
        # ============================================================

        cursor.execute("""
            UPDATE ngos
            SET
                verification_status='Approved',
                status='Active',
                verified_on=NOW()
            WHERE ngo_id=%s
        """, (ngo_id,))


        # ============================================================
        # CREATE NGO APPROVAL NOTIFICATION
        # ============================================================

        cursor.execute("""
            INSERT INTO notifications
            (
                user_id,
                ngo_id,
                donation_id,
                message
            )
            VALUES
            (
                NULL,
                %s,
                NULL,
                %s
            )
        """, (
            ngo_id,
            "🎉 Your NGO application has been approved! You can now log in to FoodResQ and start receiving donation assignments."
        ))


        # ============================================================
        # COMMIT DATABASE CHANGES
        # ============================================================

        db.commit()


        # ============================================================
        # SEND APPROVAL EMAIL
        # ============================================================

        email_sent = send_ngo_status_email(
            ngo["email"],
            ngo["ngo_name"],
            "Approved"
        )


        if not email_sent:

            print(
                f"WARNING: NGO {ngo_id} was approved, "
                f"but approval email could not be sent."
            )


        # ============================================================
        # SUCCESS
        # ============================================================

        return notify_redirect(
            "NGO application approved successfully.",
            "/admin/ngo_applications",
            kind="success"
        )


    except Exception as e:

        if db:
            db.rollback()

        print(
            "APPROVE NGO ERROR:",
            repr(e)
        )

        return notify_redirect(
            "Unable to approve NGO application.",
            "/admin/ngo_applications"
        )


    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

@app.route("/admin/reject_ngo/<int:ngo_id>", methods=["POST"])
def reject_ngo(ngo_id):

    if "admin" not in session:
        return redirect("/admin_login")

    db = None
    cursor = None

    try:

        db = get_db()

        cursor = db.cursor(dictionary=True)


        # ============================================================
        # CHECK NGO EXISTS
        # ============================================================

        cursor.execute("""
            SELECT
                ngo_id,
                ngo_name,
                email,
                verification_status
            FROM ngos
            WHERE ngo_id=%s
            LIMIT 1
        """, (ngo_id,))

        ngo = cursor.fetchone()


        if not ngo:

            return notify_redirect(
                "NGO application not found.",
                "/admin/ngo_applications"
            )


        # ============================================================
        # MAKE SURE APPLICATION IS STILL PENDING
        # ============================================================

        if ngo["verification_status"] != "Pending":

            return notify_redirect(
                "This application has already been processed.",
                "/admin/ngo_applications"
            )


        # ============================================================
        # REJECT NGO
        # ============================================================

        cursor.execute("""
            UPDATE ngos
            SET
                verification_status='Rejected',
                status='Rejected'
            WHERE ngo_id=%s
        """, (ngo_id,))


        # ============================================================
        # CREATE NGO REJECTION NOTIFICATION
        # ============================================================

        cursor.execute("""
            INSERT INTO notifications
            (
                user_id,
                ngo_id,
                donation_id,
                message
            )
            VALUES
            (
                NULL,
                %s,
                NULL,
                %s
            )
        """, (
            ngo_id,
            "❌ Your NGO application has been rejected. Please contact the administrator for more information."
        ))


        # ============================================================
        # COMMIT DATABASE CHANGES
        # ============================================================

        db.commit()


        # ============================================================
        # SEND REJECTION EMAIL
        # ============================================================

        email_sent = send_ngo_status_email(
            ngo["email"],
            ngo["ngo_name"],
            "Rejected"
        )


        if not email_sent:

            print(
                f"WARNING: NGO {ngo_id} was rejected, "
                f"but rejection email could not be sent."
            )


        # ============================================================
        # SUCCESS
        # ============================================================

        return notify_redirect(
            "NGO application rejected.",
            "/admin/ngo_applications",
            kind="success"
        )


    except Exception as e:

        if db:
            db.rollback()

        print(
            "REJECT NGO ERROR:",
            repr(e)
        )

        return notify_redirect(
            "Unable to reject NGO application.",
            "/admin/ngo_applications"
        )


    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

@app.route("/ngo_login")
def ngo_login():

    return render_template("ngo_login.html")

@app.route("/ngo_login_verify", methods=["POST"])
def ngo_login_verify():

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not valid_email(email) or not password or len(password) > 128:
        return render_template(
            "ngo_login.html",
            error="Invalid Email or Password.",
            error_type="error",
            email=email
        )

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # ==========================================
    # FIND NGO
    # ==========================================

    cursor.execute("""
        SELECT *
        FROM ngos
        WHERE email = %s
    """, (email,))

    ngo = cursor.fetchone()


    # ==========================================
    # NGO NOT FOUND
    # ==========================================

    if not ngo:

        cursor.close()
        db.close()

        return render_template(
            "ngo_login.html",
            error="Invalid Email or Password.",
            error_type="error",
            email=email
        )


    # ==========================================
    # CHECK ACCOUNT LOCK
    # ==========================================

    if ngo.get("locked_until"):

        if datetime.now() < ngo["locked_until"]:

            remaining = (
                ngo["locked_until"] - datetime.now()
            )

            minutes = max(
                1,
                int(remaining.total_seconds() / 60)
            )

            cursor.close()
            db.close()

            return render_template(
                "ngo_login.html",
                error=(
                    "Too many failed login attempts. "
                    f"Please try again in {minutes} minutes."
                ),
                error_type="warning",
                email=email
            )


        else:

            # ======================================
            # LOCK PERIOD FINISHED
            # ======================================

            cursor.execute("""
                UPDATE ngos
                SET failed_attempts = 0,
                    locked_until = NULL
                WHERE ngo_id = %s
            """, (ngo["ngo_id"],))

            db.commit()

            ngo["failed_attempts"] = 0
            ngo["locked_until"] = None


    # ==========================================
    # CHECK PASSWORD
    # ==========================================

    if not check_password_hash(
        ngo["password"],
        password
    ):

        # ======================================
        # WRONG PASSWORD
        # ======================================

        attempts = (
            ngo.get("failed_attempts") or 0
        ) + 1


        # ======================================
        # 5 FAILED ATTEMPTS
        # ======================================

        if attempts >= 5:

            lock_time = (
                datetime.now()
                + timedelta(minutes=15)
            )

            cursor.execute("""
                UPDATE ngos
                SET failed_attempts = %s,
                    locked_until = %s
                WHERE ngo_id = %s
            """, (
                attempts,
                lock_time,
                ngo["ngo_id"]
            ))

            db.commit()

            cursor.close()
            db.close()

            return render_template(
                "ngo_login.html",
                error=(
                    "Too many failed login attempts. "
                    "Your account has been locked for 15 minutes."
                ),
                error_type="warning",
                email=email
            )


        # ======================================
        # LESS THAN 5 FAILED ATTEMPTS
        # ======================================

        else:

            cursor.execute("""
                UPDATE ngos
                SET failed_attempts = %s
                WHERE ngo_id = %s
            """, (
                attempts,
                ngo["ngo_id"]
            ))

            db.commit()

            remaining = 5 - attempts

            cursor.close()
            db.close()

            return render_template(
                "ngo_login.html",
                error=(
                    "Invalid Email or Password. "
                    f"{remaining} login attempts remaining."
                ),
                error_type="error",
                email=email
            )


    # ==========================================
    # PASSWORD CORRECT
    # ==========================================

    verification_status = ngo.get(
        "verification_status"
    )


    # ==========================================
    # NGO APPLICATION PENDING
    # ==========================================

    if verification_status == "Pending":

        cursor.close()
        db.close()

        return render_template(
            "ngo_login.html",
            error=(
                "Your NGO application is still pending "
                "verification. Please wait for administrator approval."
            ),
            error_type="warning",
            email=email
        )


    # ==========================================
    # NGO APPLICATION REJECTED
    # ==========================================

    if verification_status == "Rejected":

        cursor.close()
        db.close()

        rejection_reason = ngo.get(
            "rejection_reason"
        )

        if rejection_reason:

            message = (
                "Your NGO application has been rejected. "
                f"Reason: {rejection_reason}"
            )

        else:

            message = (
                "Your NGO application has been rejected. "
                "Please contact FoodResQ administration."
            )


        return render_template(
            "ngo_login.html",
            error=message,
            error_type="error",
            email=email
        )


    # ==========================================
    # NOT APPROVED
    # ==========================================

    if verification_status != "Approved":

        cursor.close()
        db.close()

        return render_template(
            "ngo_login.html",
            error=(
                "Your NGO account has not been approved yet. "
                "Please wait for administrator verification."
            ),
            error_type="warning",
            email=email
        )


    # ==========================================
    # APPROVED NGO
    # ==========================================

    cursor.execute("""
        UPDATE ngos
        SET failed_attempts = 0,
            locked_until = NULL
        WHERE ngo_id = %s
    """, (ngo["ngo_id"],))

    db.commit()


    # ==========================================
    # CREATE NGO SESSION
    # ==========================================

    session.clear()
    session.permanent = True
    session["ngo_id"] = ngo["ngo_id"]
    session["ngo_name"] = ngo["ngo_name"]
    session["role"] = "ngo"


    cursor.close()
    db.close()


    # ==========================================
    # NGO HOME
    # ==========================================

    return redirect("/ngo_home")

@app.route("/forgot_password")
def forgot_password():

    return render_template("forgot_password.html")

@app.route("/send_reset_otp", methods=["POST"])
def send_reset_otp():

    email = request.form.get("email", "").strip().lower()

    if not valid_email(email):
        return notify_redirect("Please enter a valid email address.", "/forgot_password")

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # --------------------------------
    # SEARCH DONOR
    # --------------------------------

    cursor.execute("""
        SELECT id, email
        FROM users
        WHERE email=%s
    """, (email,))

    donor = cursor.fetchone()


    # --------------------------------
    # SEARCH NGO
    # --------------------------------

    cursor.execute("""
        SELECT ngo_id, email
        FROM ngos
        WHERE email=%s
    """, (email,))

    ngo = cursor.fetchone()


    # --------------------------------
    # SEARCH ADMIN
    # --------------------------------

    cursor.execute("""
        SELECT id, email
        FROM admin
        WHERE email=%s
    """, (email,))

    admin = cursor.fetchone()


    # --------------------------------
    # ACCOUNT NOT FOUND
    # --------------------------------

    if not donor and not ngo and not admin:

        cursor.close()
        db.close()

        return notify_redirect("If an account exists with this email, a verification code has been sent.", "/forgot_password", kind="info")


    # --------------------------------
    # DETERMINE ACCOUNT
    # --------------------------------

    matches = sum([
        donor is not None,
        ngo is not None,
        admin is not None
    ])

    # Same email should not belong to multiple accounts
    if matches > 1:

        cursor.close()
        db.close()

        return notify_redirect("This email is associated with multiple accounts. Please contact FoodResQ support.", "/forgot_password")


    if donor:

        account_type = "donor"
        account_id = donor["id"]


    elif ngo:

        account_type = "ngo"
        account_id = ngo["ngo_id"]


    else:

        account_type = "admin"
        account_id = admin["id"]


    # --------------------------------
    # GENERATE OTP
    # --------------------------------

    otp = str(secrets.randbelow(900000) + 100000)

    expiry = datetime.now() + timedelta(minutes=5)


    # --------------------------------
    # SAVE OTP
    # --------------------------------

    if account_type == "donor":

        cursor.execute("""
            UPDATE users
            SET reset_otp=%s,
                reset_otp_expiry=%s,
                reset_otp_attempts=0
            WHERE id=%s
        """, (otp, expiry, account_id))


    elif account_type == "ngo":

        cursor.execute("""
            UPDATE ngos
            SET reset_otp=%s,
                reset_otp_expiry=%s,
                reset_otp_attempts=0
            WHERE ngo_id=%s
        """, (otp, expiry, account_id))


    else:

        cursor.execute("""
            UPDATE admin
            SET reset_otp=%s,
                reset_otp_expiry=%s,
                reset_otp_attempts=0
            WHERE id=%s
        """, (otp, expiry, account_id))


    db.commit()

    cursor.close()
    db.close()


    # --------------------------------
    # STORE RESET TARGET IN SESSION
    # --------------------------------

    session["reset_account_type"] = account_type
    session["reset_account_id"] = account_id


    # --------------------------------
    # SEND EMAIL
    # --------------------------------

    try:

        send_otp_email(email, otp)

    except Exception as e:

        print("EMAIL ERROR:", e)

        session.pop("reset_account_type", None)
        session.pop("reset_account_id", None)

        return notify_redirect("Unable to send verification email. Please try again later.", "/forgot_password")


    return render_template(
        "verify_reset_otp.html"
    )


@app.route("/verify_reset_otp", methods=["POST"])
def verify_reset_otp():

    entered_otp = request.form.get("otp", "").strip()

    if not valid_otp(entered_otp):
        return notify_redirect("The code must be exactly 6 digits.", "/forgot_password") \
            if not session.get("reset_account_id") else \
            render_template("verify_reset_otp.html", error="The code must be exactly 6 digits.")

    account_type = session.get("reset_account_type")
    account_id = session.get("reset_account_id")


    # No reset session
    if not account_type or not account_id:

        return notify_redirect("Invalid password reset request.", "/forgot_password")


    db = get_db()

    cursor = db.cursor(dictionary=True)


    # --------------------------------
    # GET ACCOUNT
    # --------------------------------

    if account_type == "donor":

        cursor.execute("""
            SELECT id, reset_otp, reset_otp_expiry,
                   reset_otp_attempts
            FROM users
            WHERE id=%s
        """, (account_id,))


    elif account_type == "ngo":

        cursor.execute("""
            SELECT ngo_id, reset_otp, reset_otp_expiry,
                   reset_otp_attempts
            FROM ngos
            WHERE ngo_id=%s
        """, (account_id,))


    elif account_type == "admin":

        cursor.execute("""
            SELECT id, reset_otp, reset_otp_expiry,
                   reset_otp_attempts
            FROM admin
            WHERE id=%s
        """, (account_id,))


    else:

        cursor.close()
        db.close()

        return redirect("/forgot_password")


    account = cursor.fetchone()


    if not account:

        cursor.close()
        db.close()

        return redirect("/forgot_password")


    # --------------------------------
    # MAXIMUM 5 OTP ATTEMPTS
    # --------------------------------

    attempts = account["reset_otp_attempts"] or 0

    if attempts >= 5:

        cursor.close()
        db.close()

        session.pop("reset_account_type", None)
        session.pop("reset_account_id", None)

        return notify_redirect("Too many incorrect verification attempts. Please request a new code.", "/forgot_password")


    # --------------------------------
    # CHECK EXPIRY
    # --------------------------------

    if (
        not account["reset_otp_expiry"]
        or datetime.now() > account["reset_otp_expiry"]
    ):

        cursor.close()
        db.close()

        session.pop("reset_account_type", None)
        session.pop("reset_account_id", None)

        return notify_redirect("Your verification code has expired. Please request a new code.", "/forgot_password")


    # --------------------------------
    # CHECK OTP
    # --------------------------------

    if not hmac.compare_digest(entered_otp, str(account["reset_otp"] or "")):

        attempts += 1


        if account_type == "donor":

            cursor.execute("""
                UPDATE users
                SET reset_otp_attempts=%s
                WHERE id=%s
            """, (attempts, account_id))


        elif account_type == "ngo":

            cursor.execute("""
                UPDATE ngos
                SET reset_otp_attempts=%s
                WHERE ngo_id=%s
            """, (attempts, account_id))


        else:

            cursor.execute("""
                UPDATE admin
                SET reset_otp_attempts=%s
                WHERE id=%s
            """, (attempts, account_id))


        db.commit()

        cursor.close()
        db.close()


        remaining = 5 - attempts


        if remaining <= 0:

            session.pop("reset_account_type", None)
            session.pop("reset_account_id", None)

            return notify_redirect("Too many incorrect verification attempts. Please request a new code.", "/forgot_password")


        notify(f"Incorrect verification code. {remaining} attempts remaining.")
        return render_template("verify_reset_otp.html")


    # --------------------------------
    # OTP CORRECT
    # --------------------------------

    cursor.close()
    db.close()


    # Give permission to reset password
    session["password_reset_verified"] = True


    return redirect("/reset_password")

@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():

    # --------------------------------
    # MUST HAVE VERIFIED OTP
    # --------------------------------

    if not session.get("password_reset_verified"):

        return redirect("/forgot_password")


    account_type = session.get("reset_account_type")
    account_id = session.get("reset_account_id")


    if not account_type or not account_id:

        return redirect("/forgot_password")


    # --------------------------------
    # SHOW PAGE
    # --------------------------------

    if request.method == "GET":

        return render_template("reset_password.html")


    # --------------------------------
    # GET NEW PASSWORD
    # --------------------------------

    password = request.form["password"]
    confirm_password = request.form["confirm_password"]


    if password != confirm_password:

        notify("Passwords do not match.")
        return render_template("reset_password.html")


    # Strong password requirement (same rule as registration)
    _weak = password_problem(password)

    if _weak:

        notify(_weak, "error")
        return render_template("reset_password.html")


    # --------------------------------
    # HASH PASSWORD
    # --------------------------------

    hashed_password = generate_password_hash(password)


    db = get_db()

    cursor = db.cursor()


    # --------------------------------
    # UPDATE DONOR
    # --------------------------------

    if account_type == "donor":

        cursor.execute("""
            UPDATE users
            SET password=%s,
                failed_attempts=0,
                locked_until=NULL,
                reset_otp=NULL,
                reset_otp_expiry=NULL,
                reset_otp_attempts=0
            WHERE id=%s
        """, (hashed_password, account_id))


    # --------------------------------
    # UPDATE NGO
    # --------------------------------

    elif account_type == "ngo":

        cursor.execute("""
            UPDATE ngos
            SET password=%s,
                failed_attempts=0,
                locked_until=NULL,
                reset_otp=NULL,
                reset_otp_expiry=NULL,
                reset_otp_attempts=0
            WHERE ngo_id=%s
        """, (hashed_password, account_id))


    # --------------------------------
    # UPDATE ADMIN
    # --------------------------------

    elif account_type == "admin":

        cursor.execute("""
            UPDATE admin
            SET password=%s,
                failed_attempts=0,
                locked_until=NULL,
                reset_otp=NULL,
                reset_otp_expiry=NULL,
                reset_otp_attempts=0
            WHERE id=%s
        """, (hashed_password, account_id))


    else:

        cursor.close()
        db.close()

        return redirect("/forgot_password")


    db.commit()

    cursor.close()
    db.close()


    # --------------------------------
    # CLEAR RESET SESSION
    # --------------------------------

    session.pop("password_reset_verified", None)
    session.pop("reset_account_type", None)
    session.pop("reset_account_id", None)


    return notify_redirect("Password reset successfully. You can now login.", "/")

@app.route("/ngo_home")
def ngo_home():

    # -----------------------------------------
    # CHECK NGO LOGIN
    # -----------------------------------------

    if "ngo_id" not in session:
        return redirect(url_for("ngo_login"))

    ngo_id = session["ngo_id"]

    # -----------------------------------------
    # DATABASE
    # -----------------------------------------

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # -----------------------------------------
    # TOTAL DONATIONS
    # -----------------------------------------

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM donations
        WHERE ngo_id=%s
    """, (ngo_id,))

    total_donations = cursor.fetchone()["total"]


    # -----------------------------------------
    # ACTIVE DONATIONS
    # -----------------------------------------

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM donations
        WHERE ngo_id=%s
        AND status NOT IN ('Completed', 'Rejected')
    """, (ngo_id,))

    active_donations = cursor.fetchone()["total"]


    # -----------------------------------------
    # COMPLETED DONATIONS
    # -----------------------------------------

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM donations
        WHERE ngo_id=%s
        AND status='Completed'
    """, (ngo_id,))

    completed_donations = cursor.fetchone()["total"]


    # -----------------------------------------
    # TOTAL FOOD RESCUED
    # -----------------------------------------

    cursor.execute("""
        SELECT COALESCE(SUM(quantity), 0) AS total
        FROM donations
        WHERE ngo_id=%s
        AND status='Completed'
    """, (ngo_id,))

    total_food = cursor.fetchone()["total"]


    # -----------------------------------------
    # COMPLETION RATE
    # -----------------------------------------

    if total_donations > 0:

        completion_rate = round(
            (completed_donations / total_donations) * 100
        )

    else:

        completion_rate = 0


    # -----------------------------------------
    # RECENT ACTIVITY
    # -----------------------------------------

    cursor.execute("""
        SELECT
            donation_id,
            food_name,
            quantity,
            quantity_unit,
            status,
            pickup_date
        FROM donations
        WHERE ngo_id=%s
        ORDER BY donation_id DESC
        LIMIT 5
    """, (ngo_id,))

    recent_activity = cursor.fetchall()


    # -----------------------------------------
    # CURRENT ASSIGNMENTS
    # -----------------------------------------

    cursor.execute("""
        SELECT
            donation_id,
            food_name,
            quantity,
            quantity_unit,
            pickup_date,
            status
        FROM donations
        WHERE ngo_id=%s
        AND status NOT IN ('Completed', 'Rejected')
        ORDER BY donation_id DESC
        LIMIT 10
    """, (ngo_id,))

    current_assignments = cursor.fetchall()


    # -----------------------------------------
    # UNREAD NOTIFICATIONS
    # -----------------------------------------

    cursor.execute("""
        SELECT COUNT(*) AS unread_notifications
        FROM notifications
        WHERE ngo_id=%s
        AND is_read=0
    """, (ngo_id,))

    unread_notifications = cursor.fetchone()["unread_notifications"]


    # -----------------------------------------
    # CLOSE DATABASE
    # -----------------------------------------

    cursor.close()
    db.close()


    # -----------------------------------------
    # SEND TO HTML
    # -----------------------------------------

    return render_template(
        "ngo_home.html",

        total_donations=total_donations,

        active_donations=active_donations,

        completed_donations=completed_donations,

        total_food=total_food,

        completion_rate=completion_rate,

        recent_activity=recent_activity,

        current_assignments=current_assignments,

        unread_notifications=unread_notifications
    )


@app.route("/FRSq_adm_log")
def admin_login():

    return render_template("admin_login.html")


@app.route("/admin_login_verify", methods=["POST"])
def admin_login_verify():

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not valid_email(email) or not password or len(password) > 128:
        return notify_redirect("Invalid Email or Password", "/admin_login")

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # Find admin using email
    cursor.execute("""
        SELECT *
        FROM admin
        WHERE email=%s
    """, (email,))

    admin = cursor.fetchone()

    # No admin with this email
    if not admin:

        cursor.close()
        db.close()

        return notify_redirect("Invalid Email or Password", "/admin_login")

    # Check whether account is currently locked
    if admin["locked_until"]:

        if datetime.now() < admin["locked_until"]:

            remaining = admin["locked_until"] - datetime.now()
            minutes = max(1, int(remaining.total_seconds() / 60))

            cursor.close()
            db.close()

            return notify_redirect(f"Too many failed login attempts. Try again in {minutes} minutes.", "/admin_login")

        else:

            # Lock period finished
            cursor.execute("""
                UPDATE admin
                SET failed_attempts=0,
                    locked_until=NULL
                WHERE id=%s
            """, (admin["id"],))

            db.commit()

            admin["failed_attempts"] = 0
            admin["locked_until"] = None

    # Check hashed password
    if check_password_hash(admin["password"], password):

        # Successful login → reset attempts
        cursor.execute("""
            UPDATE admin
            SET failed_attempts=0,
                locked_until=NULL
            WHERE id=%s
        """, (admin["id"],))

        db.commit()

        session.clear()
        session.permanent = True
        session["admin"] = "FoodResQ"
        session["admin_id"] = admin["id"]
        session["role"] = "admin"

        cursor.close()
        db.close()

        return redirect("/admin_dashboard")

    else:

        # Wrong password
        attempts = admin["failed_attempts"] + 1

        if attempts >= 5:

            lock_time = datetime.now() + timedelta(minutes=15)

            cursor.execute("""
                UPDATE admin
                SET failed_attempts=%s,
                    locked_until=%s
                WHERE id=%s
            """, (attempts, lock_time, admin["id"]))

            db.commit()

            cursor.close()
            db.close()

            return notify_redirect("Too many failed login attempts. Your account is locked for 15 minutes.", "/admin_login")

        else:

            cursor.execute("""
                UPDATE admin
                SET failed_attempts=%s
                WHERE id=%s
            """, (attempts, admin["id"]))

            db.commit()

            remaining = 5 - attempts

            cursor.close()
            db.close()

            return notify_redirect(f"Invalid Email or Password. {remaining} login attempts remaining.", "/admin_login")

@app.route("/admin_dashboard")
def admin_dashboard():

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))

    db = None
    cursor = None

    try:

        db = get_db()

        cursor = db.cursor(
            dictionary=True,
            buffered=True
        )

        # ============================================================
        # BASIC COUNTS
        # ============================================================

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM users
        """)

        total_donors = cursor.fetchone()["total"]


        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM ngos
        """)

        total_ngos = cursor.fetchone()["total"]


        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM donations
        """)

        total_donations = cursor.fetchone()["total"]


        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM donations
            WHERE status='Pending'
        """)

        pending = cursor.fetchone()["total"]


        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM donations
            WHERE status='Completed'
        """)

        completed = cursor.fetchone()["total"]


        # ============================================================
        # ADMIN NAVBAR NOTIFICATION COUNTS
        # ============================================================

        # ------------------------------------------------------------
        # NGO APPLICATIONS
        # New NGO applications waiting for admin approval
        # ------------------------------------------------------------

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM ngos
            WHERE verification_status='Pending'
        """)

        admin_ngo_notifications = cursor.fetchone()["total"]


        # ------------------------------------------------------------
        # NEW DONATIONS
        # Newly submitted donations waiting for admin review
        # ------------------------------------------------------------

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM donations
            WHERE status='Pending'
        """)

        admin_donation_notifications = cursor.fetchone()["total"]


        # ------------------------------------------------------------
        # ASSIGNMENTS
        # Admin-verified donations waiting to be assigned to an NGO
        # ------------------------------------------------------------

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM donations
            WHERE status='Admin Verified'
              AND ngo_id IS NULL
        """)

        admin_assignment_notifications = cursor.fetchone()["total"]


        # ------------------------------------------------------------
        # REPORTS
        # Donor + NGO reports that are still pending
        # ------------------------------------------------------------

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM donation_reports
            WHERE status='Pending'
        """)

        admin_report_notifications = cursor.fetchone()["total"]


        # ============================================================
        # EXISTING DASHBOARD COUNTS
        # ============================================================

        pending_ngos = admin_ngo_notifications

        pending_donations = admin_assignment_notifications


        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM users
            WHERE id >= (
                SELECT COALESCE(MAX(id), 0) - 10
                FROM users
            )
        """)

        new_users = cursor.fetchone()["total"]


        # ============================================================
        # RECENT DONATIONS
        # ============================================================

        cursor.execute("""
            SELECT *
            FROM donations
            ORDER BY donation_id DESC
            LIMIT 5
        """)

        recent_donations = cursor.fetchall()


        # ============================================================
        # RECENT NGOS
        # ============================================================

        cursor.execute("""
            SELECT *
            FROM ngos
            ORDER BY ngo_id DESC
            LIMIT 5
        """)

        recent_ngos = cursor.fetchall()


        # ============================================================
        # RECENT USERS
        # ============================================================

        cursor.execute("""
            SELECT *
            FROM users
            ORDER BY id DESC
            LIMIT 5
        """)

        recent_users = cursor.fetchall()


        # ============================================================
        # TOP PERFORMING NGOS
        # ============================================================

        cursor.execute("""
            SELECT
                ngos.ngo_name,
                SUM(donations.quantity) AS meals_saved

            FROM donations

            JOIN ngos
                ON donations.ngo_id = ngos.ngo_id

            GROUP BY ngos.ngo_name

            ORDER BY meals_saved DESC

            LIMIT 5
        """)

        top_ngos = cursor.fetchall()


        # ============================================================
        # CLOSE DATABASE
        # ============================================================

        cursor.close()
        cursor = None

        db.close()
        db = None


        # ============================================================
        # RENDER DASHBOARD
        # ============================================================

        return render_template(
            "admin_dashboard.html",

            total_donors=total_donors,
            total_ngos=total_ngos,
            total_donations=total_donations,

            pending=pending,
            completed=completed,

            meals_saved=0,

            recent_donations=recent_donations,
            recent_ngos=recent_ngos,
            recent_users=recent_users,

            top_ngos=top_ngos,

            pending_ngos=pending_ngos,
            pending_donations=pending_donations,
            new_users=new_users,

            # ========================================================
            # NAVBAR BADGES
            # ========================================================

            admin_ngo_notifications=admin_ngo_notifications,
            admin_donation_notifications=admin_donation_notifications,
            admin_assignment_notifications=admin_assignment_notifications,
            admin_report_notifications=admin_report_notifications
        )


    except Exception as e:

        if cursor:
            cursor.close()

        if db:
            db.close()

        import traceback

        print(
            "ADMIN DASHBOARD ERROR:",
            repr(e)
        )

        traceback.print_exc()

        return "Unable to load admin dashboard.", 500

# ============================================================
# ADMIN - DONOR LIST
# ============================================================
@app.route("/admin_donors")
def admin_donors():
    if "admin_id" not in session or session.get("role") != "admin":
        return redirect(url_for("admin_login"))

    db = None
    cursor = None
    try:
        search = request.args.get("search", "").strip()
        db = get_db()
        cursor = db.cursor(dictionary=True, buffered=True)

        query = """
            SELECT
                u.id,
                u.donor_code,
                u.name,
                u.email,
                u.phone,
                u.profile_photo,
                u.status,
                COUNT(d.donation_id) AS donations_count
            FROM users u
            LEFT JOIN donations d ON d.user_id = u.id
        """
        params = []

        if search:
            query += """
                WHERE u.name LIKE %s
                   OR u.email LIKE %s
                   OR u.donor_code LIKE %s
            """
            like = f"%{search}%"
            params.extend([like, like, like])

        query += """
            GROUP BY u.id, u.donor_code, u.name, u.email, u.phone,
                     u.profile_photo, u.status
            ORDER BY u.id DESC
        """

        cursor.execute(query, tuple(params))
        donors = cursor.fetchall()

        return render_template("admin_donors.html", donors=donors, search=search)
    except Exception as e:
        import traceback
        print("ADMIN DONORS ERROR:", repr(e))
        traceback.print_exc()
        return "Unable to load donors.", 500
    finally:
        if cursor:
            cursor.close()
        if db:
            db.close()

@app.route("/admin_donor/<int:id>")
def admin_donor_profile(id):

    db = None
    cursor = None

    try:

        db = get_db()
        cursor = db.cursor(dictionary=True, buffered=True)

        # =====================================================
        # DONOR PROFILE
        # =====================================================

        cursor.execute("""
            SELECT
                id,
                donor_code,
                name,
                email,
                phone,
                profile_photo,
                status
            FROM users
            WHERE id = %s
            LIMIT 1
        """, (id,))

        donor = cursor.fetchone()

        if not donor:
            return "Donor not found.", 404

        # =====================================================
        # DONOR IMPACT
        # =====================================================

        cursor.execute("""
            SELECT
                COUNT(donation_id) AS total_donations,
                COALESCE(SUM(quantity), 0) AS meals_saved
            FROM donations
            WHERE user_id = %s
        """, (id,))

        impact = cursor.fetchone()

        # =====================================================
        # DONATION HISTORY
        # =====================================================

        cursor.execute("""
            SELECT
                donation_id,
                food_name,
                quantity,
                quantity_unit,
                pickup_date,
                status
            FROM donations
            WHERE user_id = %s
            ORDER BY donation_id DESC
        """, (id,))

        donations = cursor.fetchall()

        # =====================================================
        # DONOR INITIALS
        # =====================================================

        name = (donor.get("name") or "Donor").strip()
        parts = name.split()
        initials = "".join(part[0] for part in parts[:2]).upper()
        donor["initials"] = initials or "D"

        return render_template(
            "admin_donor_profile.html",
            donor=donor,
            impact=impact,
            donations=donations
        )

    except mysql.connector.Error as e:

        print("ADMIN DONOR PROFILE DATABASE ERROR:", e)
        return "Unable to load donor profile.", 500

    except Exception as e:

        import traceback
        print("ADMIN DONOR PROFILE ERROR:", repr(e))
        traceback.print_exc()
        return "Unable to load donor profile.", 500

    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()


@app.route(
    "/admin_deactivate_donor/<int:id>",
    methods=["POST"]
)
def admin_deactivate_donor(id):

    if "admin_id" not in session:
        return redirect("/admin_login")

    db = None
    cursor = None

    try:

        db = get_db()
        cursor = db.cursor(dictionary=True)


        # ----------------------------------------------------
        # CHECK DONOR EXISTS
        # ----------------------------------------------------

        cursor.execute("""
            SELECT
                id,
                name,
                status

            FROM users

            WHERE id = %s

            LIMIT 1
        """, (id,))

        donor = cursor.fetchone()


        if not donor:

            return "Donor not found.", 404


        # ----------------------------------------------------
        # DEACTIVATE ACCOUNT
        # ----------------------------------------------------

        cursor.execute("""
            UPDATE users

            SET status = 'Inactive'

            WHERE id = %s
        """, (id,))


        db.commit()


        return redirect(
            url_for(
                "admin_donor_profile",
                id=id
            )
        )


    except Exception as e:

        if db:
            db.rollback()

        import traceback

        print(
            "ADMIN DEACTIVATE DONOR ERROR:",
            repr(e)
        )

        traceback.print_exc()

        return (
            f"Unable to deactivate donor: {e}",
            500
        )


    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

@app.route("/admin_ngos")
def admin_ngos():

    db = get_db()

    cursor = db.cursor(dictionary=True, buffered=True)


    search = request.args.get("search", "")


    if search:

        cursor.execute("""
            SELECT
                ngo_id,
                partner_id,
                ngo_name,
                email,
                phone,
                address,
                service_area,
                status

            FROM ngos

            WHERE ngo_name LIKE %s
            OR service_area LIKE %s

        """,
        (
            "%" + search + "%",
            "%" + search + "%"
        ))


    else:

        cursor.execute("""
            SELECT
                ngo_id,
                partner_id,
                ngo_name,
                email,
                phone,
                address,
                service_area,
                status

            FROM ngos

        """)



    ngos = cursor.fetchall()


    cursor.close()
    db.close()


    return render_template(
        "admin_ngos.html",
        ngos=ngos
    )

# =========================================================
# ADMIN - SINGLE NGO PROFILE
# =========================================================

@app.route("/admin_ngo/<int:ngo_id>")
def admin_ngo_profile(ngo_id):

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))

    db = get_db()

    cursor = db.cursor(dictionary=True, buffered=True)

    cursor.execute("""
        SELECT *
        FROM ngos
        WHERE ngo_id = %s
    """, (ngo_id,))

    ngo = cursor.fetchone()

    if not ngo:

        cursor.close()
        db.close()

        return notify_redirect("That NGO could not be found.", "/admin_ngos")


    # Work this NGO has handled
    cursor.execute("""
        SELECT
            d.donation_id,
            d.food_name,
            d.quantity,
            d.quantity_unit,
            d.status,
            d.pickup_date,
            u.name AS donor_name
        FROM donations d
        LEFT JOIN users u
            ON d.user_id = u.id
        WHERE d.ngo_id = %s
        ORDER BY d.donation_id DESC
        LIMIT 20
    """, (ngo_id,))

    donations = cursor.fetchall()

    cursor.execute("""
        SELECT
            COUNT(*)                    AS handled,
            SUM(status = 'Completed')   AS completed,
            SUM(quantity)               AS quantity
        FROM donations
        WHERE ngo_id = %s
    """, (ngo_id,))

    stats = cursor.fetchone() or {}

    cursor.close()
    db.close()

    return render_template(
        "admin_ngo_profile.html",
        ngo=ngo,
        donations=donations,
        stats=stats
    )


@app.route("/admin_set_ngo_status/<int:ngo_id>", methods=["POST"])
def admin_set_ngo_status(ngo_id):

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))

    new_status = (request.form.get("new_status") or "").strip()

    if new_status not in ("Active", "Inactive"):
        return notify_redirect(
            "That NGO status is not valid.",
            url_for('admin_ngo_profile', ngo_id=ngo_id)
        )

    db = get_db()

    cursor = db.cursor(dictionary=True, buffered=True)

    cursor.execute("""
        UPDATE ngos
        SET status = %s
        WHERE ngo_id = %s
    """, (new_status, ngo_id))

    db.commit()

    changed = cursor.rowcount

    cursor.close()
    db.close()

    if not changed:
        return notify_redirect("That NGO could not be found.", "/admin_ngos")

    return notify_redirect(
        f"NGO marked as {new_status}.",
        url_for('admin_ngo_profile', ngo_id=ngo_id),
        kind="success"
    )


@app.route("/admin_donations")
def admin_donations():

    db = get_db()

    cursor = db.cursor(dictionary=True, buffered=True)


    search = request.args.get("search", "")
    status = request.args.get("status", "")
    category = request.args.get("category", "")


    query = """
        SELECT
            d.donation_id,
            d.food_name,
            d.food_type,
            d.quantity,
            d.quantity_unit,
            d.status,
            d.pickup_date,
            u.name AS donor_name

        FROM donations d

        JOIN users u
        ON d.user_id = u.id

        WHERE 1=1
    """


    values = []


    if search:

        query += """
        AND (
            d.food_name LIKE %s
            OR u.name LIKE %s
        )
        """

        values.append("%"+search+"%")
        values.append("%"+search+"%")



    if status:

        query += """
        AND d.status=%s
        """

        values.append(status)



    if category:

        query += """
        AND d.food_type=%s
        """

        values.append(category)



    query += """
        ORDER BY d.donation_id DESC
    """



    cursor.execute(query, tuple(values))


    donations = cursor.fetchall()



    # Summary cards

    cursor.execute(
        "SELECT COUNT(*) total FROM donations"
    )

    total = cursor.fetchone()["total"]



    cursor.execute(
        "SELECT COUNT(*) total FROM donations WHERE status='Pending'"
    )

    pending = cursor.fetchone()["total"]



    cursor.execute(
        "SELECT COUNT(*) total FROM donations WHERE status='Completed'"
    )

    completed = cursor.fetchone()["total"]






    # Options for the filter dropdowns, taken from real data
    cursor.execute("""
        SELECT DISTINCT status
        FROM donations
        WHERE status IS NOT NULL AND status != ''
        ORDER BY status
    """)
    status_options = [r["status"] for r in cursor.fetchall()]

    cursor.execute("""
        SELECT DISTINCT food_type
        FROM donations
        WHERE food_type IS NOT NULL AND food_type != ''
        ORDER BY food_type
    """)
    category_options = [r["food_type"] for r in cursor.fetchall()]

    cursor.close()
    db.close()

    return render_template(
        "admin_donations.html",
        donations=donations,
        total=total,
        pending=pending,
        completed=completed,
        status_options=status_options,
        category_options=category_options,
        search=search,
        status=status,
        category=category
    )

@app.route("/admin_donation/<int:donation_id>")
def admin_donation(donation_id):

    db = get_db()

    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            d.*,

            u.name AS donor_name,
            u.email AS donor_email,
            u.phone AS donor_phone,

            n.ngo_name,
            n.partner_id,
            n.phone AS ngo_phone,
            n.address AS ngo_address,
            n.service_area

        FROM donations d

        JOIN users u
            ON d.user_id = u.id

        LEFT JOIN ngos n
            ON d.ngo_id = n.ngo_id

        WHERE d.donation_id = %s
    """, (donation_id,))

    donation = cursor.fetchone()

    cursor.close()
    db.close()

    if not donation:
        return notify_redirect("Donation not found.", "/admin_donations")

    return render_template(
        "admin_donation_details.html",
        donation=donation
    )


@app.route("/admin_update_donation_status/<int:donation_id>", methods=["POST"])
def admin_update_donation_status(donation_id):

    # -----------------------------------
    # ADMIN LOGIN CHECK
    # -----------------------------------

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))


    allowed = [
        'Pending',
        'Assigned',
        'NGO Accepted',
        'On the Way',
        'Picked Up',
        'Awaiting Donor Confirmation',
        'Completed'
    ]

    new_status = (request.form.get("new_status") or "").strip()

    if new_status not in allowed:
        return notify_redirect(
            "That status is not valid.",
            url_for('admin_donation', donation_id=donation_id)
        )


    db = get_db()

    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT donation_id, status, ngo_id
        FROM donations
        WHERE donation_id = %s
    """, (donation_id,))

    donation = cursor.fetchone()

    if not donation:

        cursor.close()
        db.close()

        return notify_redirect("Donation not found.", "/admin_donations")


    # A finished donation should not be reopened from here.
    if donation["status"] in ("Cancelled", "Completed"):

        cursor.close()
        db.close()

        return notify_redirect(
            f"This donation is already {donation['status'].lower()} "
            f"and can no longer be updated.",
            url_for('admin_donation', donation_id=donation_id)
        )


    # Anything past 'Pending' needs an NGO on the donation.
    if new_status != 'Pending' and not donation["ngo_id"]:

        cursor.close()
        db.close()

        return notify_redirect(
            "Assign an NGO to this donation before moving it past Pending.",
            url_for('admin_donation', donation_id=donation_id)
        )


    if donation["status"] == new_status:

        cursor.close()
        db.close()

        return notify_redirect(
            f"Status is already {new_status}.",
            url_for('admin_donation', donation_id=donation_id)
        )


    cursor.execute("""
        UPDATE donations
        SET status = %s
        WHERE donation_id = %s
    """, (new_status, donation_id))

    db.commit()

    cursor.close()
    db.close()

    return notify_redirect(
        f"Donation #{donation_id} updated to {new_status}.",
        url_for('admin_donation', donation_id=donation_id),
        kind="success"
    )

@app.route("/admin_assignments")
def admin_assignments():

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # ---------------------------------------------
    # GET TAB + SEARCH
    # ---------------------------------------------

    tab = request.args.get("tab", "pending")
    search = request.args.get("search", "").strip()


    # =================================================
    # PENDING / TO ASSIGN
    # =================================================

    pending_query = """
        SELECT

            d.donation_id,
            d.food_name,
            d.quantity,
            d.quantity_unit,
            d.city,
            d.status,

            u.name AS donor_name

        FROM donations d

        JOIN users u
            ON d.user_id = u.id

        WHERE d.ngo_id IS NULL

        AND d.status IN ('Pending', 'Admin Verified')
    """

    pending_values = []


    if search:

        pending_query += """
            AND (
                d.food_name LIKE %s
                OR u.name LIKE %s
                OR d.city LIKE %s
                OR CAST(d.donation_id AS CHAR) LIKE %s
            )
        """

        search_value = "%" + search + "%"

        pending_values = [
            search_value,
            search_value,
            search_value,
            search_value
        ]


    pending_query += """
        ORDER BY d.donation_id DESC
        LIMIT 50
    """


    cursor.execute(
        pending_query,
        tuple(pending_values)
    )

    pending = cursor.fetchall()



    # =================================================
    # ACTIVE ASSIGNMENTS
    # =================================================

    active_query = """
        SELECT

            d.donation_id,
            d.food_name,
            d.quantity,
            d.quantity_unit,
            d.city,
            d.status,

            u.name AS donor_name,

            n.ngo_name,
            n.partner_id

        FROM donations d

        JOIN users u
            ON d.user_id = u.id

        JOIN ngos n
            ON d.ngo_id = n.ngo_id

        WHERE d.status != 'Completed'
    """

    active_values = []


    if search:

        active_query += """
            AND (
                d.food_name LIKE %s
                OR u.name LIKE %s
                OR d.city LIKE %s
                OR n.ngo_name LIKE %s
                OR CAST(d.donation_id AS CHAR) LIKE %s
            )
        """

        search_value = "%" + search + "%"

        active_values = [
            search_value,
            search_value,
            search_value,
            search_value,
            search_value
        ]


    active_query += """
        ORDER BY d.donation_id DESC
        LIMIT 50
    """


    cursor.execute(
        active_query,
        tuple(active_values)
    )

    active = cursor.fetchall()



    # =================================================
    # COMPLETED ASSIGNMENTS
    # =================================================

    completed_query = """
        SELECT

            d.donation_id,
            d.food_name,
            d.quantity,
            d.quantity_unit,
            d.city,
            d.status,

            u.name AS donor_name,

            n.ngo_name,
            n.partner_id

        FROM donations d

        JOIN users u
            ON d.user_id = u.id

        JOIN ngos n
            ON d.ngo_id = n.ngo_id

        WHERE d.status = 'Completed'
    """

    completed_values = []


    if search:

        completed_query += """
            AND (
                d.food_name LIKE %s
                OR u.name LIKE %s
                OR d.city LIKE %s
                OR n.ngo_name LIKE %s
                OR CAST(d.donation_id AS CHAR) LIKE %s
            )
        """

        search_value = "%" + search + "%"

        completed_values = [
            search_value,
            search_value,
            search_value,
            search_value,
            search_value
        ]


    completed_query += """
        ORDER BY d.donation_id DESC
        LIMIT 50
    """


    cursor.execute(
        completed_query,
        tuple(completed_values)
    )

    completed = cursor.fetchall()



    # =================================================
    # CLOSE DATABASE
    # =================================================

    cursor.close()
    db.close()



    # =================================================
    # SEND DATA TO TEMPLATE
    # =================================================

    return render_template(
        "admin_assignments.html",

        tab=tab,

        search=search,

        pending=pending,

        active=active,

        completed=completed
    )

@app.route("/admin_ngo_details/<int:ngo_id>/<int:donation_id>")
def admin_ngo_details(ngo_id, donation_id):

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # --------------------------------
    # NGO DETAILS
    # --------------------------------

    cursor.execute("""
        SELECT
            ngo_id,
            ngo_name,
            partner_id,
            phone,
            email,
            address,
            service_area,
            status
        FROM ngos
        WHERE ngo_id = %s
    """, (ngo_id,))

    ngo = cursor.fetchone()

    # --------------------------------
    # DONATION DETAILS
    # --------------------------------

    cursor.execute("""
        SELECT
            d.*,
            u.name AS donor_name,
            u.email AS donor_email
        FROM donations d

        JOIN users u
            ON d.user_id = u.id

        WHERE d.donation_id = %s
    """, (donation_id,))

    donation = cursor.fetchone()

    cursor.close()
    db.close()

    if not ngo:
        return "NGO not found", 404

    if not donation:
        return "Donation not found", 404

    return render_template(
        "admin_ngo_details.html",
        ngo=ngo,
        donation=donation
    )

@app.route("/admin_verify_donation/<int:donation_id>", methods=["POST"])
def admin_verify_donation(donation_id):

    db = get_db()

    cursor = db.cursor()

    cursor.execute("""
        UPDATE donations
        SET status='Admin Verified'
        WHERE donation_id=%s
        AND status='Pending'
    """, (donation_id,))

    db.commit()

    cursor.close()
    db.close()

    return redirect(url_for("admin_assignments", tab="pending"))

@app.route("/assign_ngo", methods=["POST"])
def assign_ngo():

    db = None
    cursor = None

    try:

        # =====================================================
        # GET FORM DATA
        # =====================================================

        donation_id = request.form.get("donation_id")
        ngo_id = request.form.get("ngo_id")


        # =====================================================
        # VALIDATION
        # =====================================================

        if not donation_id or not ngo_id:
            return "Donation ID and NGO ID are required.", 400


        # =====================================================
        # DATABASE CONNECTION
        # =====================================================

        db = get_db()

        cursor = db.cursor()


        # =====================================================
        # CHECK NGO EXISTS AND IS ACTIVE
        # =====================================================

        cursor.execute(
            """
            SELECT ngo_id
            FROM ngos
            WHERE ngo_id = %s
            AND status = 'Active'
            """,
            (ngo_id,)
        )

        ngo = cursor.fetchone()

        if not ngo:

            return "Selected NGO is not active or does not exist.", 400


        # =====================================================
        # GENERATE UNIQUE FOODRESQ PICKUP CODE
        # =====================================================

        import random
        import string

        while True:

            characters = string.ascii_uppercase + string.digits

            part1 = ''.join(
                random.SystemRandom().choices(
                    characters,
                    k=4
                )
            )

            part2 = ''.join(
                random.SystemRandom().choices(
                    string.digits,
                    k=2
                )
            )

            pickup_code = f"FRQ-{part1}-{part2}"


            # Check whether this code already exists

            cursor.execute(
                """
                SELECT donation_id
                FROM donations
                WHERE pickup_code = %s
                """,
                (pickup_code,)
            )

            existing_code = cursor.fetchone()


            if not existing_code:
                break


        # =====================================================
        # ASSIGN NGO + CREATE PICKUP CODE
        # =====================================================

        cursor.execute(
            """
            UPDATE donations

            SET
                ngo_id = %s,
                status = 'Assigned',
                pickup_code = %s,
                pickup_code_verified = 0,
                pickup_code_created_at = NOW()

            WHERE donation_id = %s
            """,
            (
                ngo_id,
                pickup_code,
                donation_id
            )
        )


        # =====================================================
        # CHECK WHETHER DONATION WAS ACTUALLY UPDATED
        # =====================================================

        if cursor.rowcount == 0:

            db.rollback()

            return "Donation not found.", 404


        # =====================================================
        # CREATE NGO NOTIFICATION
        # =====================================================

        cursor.execute(
            """
            INSERT INTO notifications
            (
                user_id,
                ngo_id,
                donation_id,
                message
            )
            VALUES
            (
                NULL,
                %s,
                %s,
                %s
            )
            """,
            (
                ngo_id,
                donation_id,
                "📦 A new food donation has been assigned to your NGO. Please review the donation and respond."
            )
        )


        # =====================================================
        # SAVE EVERYTHING
        # =====================================================

        db.commit()


        # =====================================================
        # DEBUG INFORMATION
        # =====================================================

        print(
            "NGO ASSIGNED SUCCESSFULLY"
        )

        print(
            "Donation ID:",
            donation_id
        )

        print(
            "NGO ID:",
            ngo_id
        )

        print(
            "FoodResQ Pickup Code:",
            pickup_code
        )

        print(
            "NGO NOTIFICATION CREATED"
        )


        # =====================================================
        # RETURN TO ADMIN ASSIGNMENTS
        # =====================================================

        return redirect(
            url_for("admin_assignments")
        )


    # =========================================================
    # MYSQL ERROR
    # =========================================================

    except mysql.connector.Error as e:

        print(
            "ASSIGN NGO DATABASE ERROR:",
            e
        )

        if db:
            db.rollback()

        return (
            "Database error while assigning NGO: "
            + str(e),
            500
        )


    # =========================================================
    # OTHER ERROR
    # =========================================================

    except Exception as e:

        print(
            "ASSIGN NGO ERROR:",
            e
        )

        if db:
            db.rollback()

        return (
            "Error while assigning NGO: "
            + str(e),
            500
        )


    # =========================================================
    # CLOSE DATABASE
    # =========================================================

    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

@app.route(
    "/verify_pickup_code/<int:donation_id>",
    methods=["POST"]
)
def verify_pickup_code(donation_id):

    conn = None
    cursor = None

    try:

        # =====================================================
        # GET CODE ENTERED BY NGO
        # =====================================================

        entered_code = request.form.get(
            "pickup_code",
            ""
        ).strip().upper()


        if not entered_code:

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id,
                    error="Please enter the FoodResQ pickup code."
                )
            )


        # =====================================================
        # BASIC FORMAT CHECK
        # =====================================================

        if len(entered_code) != 11:

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id,
                    error="Invalid FoodResQ pickup code."
                )
            )


        if not entered_code.startswith("FRQ-"):

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id,
                    error="Invalid FoodResQ pickup code."
                )
            )


        # =====================================================
        # DATABASE CONNECTION
        # =====================================================

        conn = get_db()

        cursor = conn.cursor(dictionary=True)


        # =====================================================
        # GET DONATION
        # =====================================================

        cursor.execute(
            """
            SELECT
                id,
                ngo_id,
                status,
                pickup_code,
                pickup_code_verified,
                pickup_code_created_at
            FROM donations
            WHERE id = %s
            """,
            (donation_id,)
        )

        donation = cursor.fetchone()


        if not donation:

            return "Donation not found.", 404


        # =====================================================
        # CHECK STATUS
        # =====================================================

        if donation["status"] != "Accepted":

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id,
                    error="This donation is not currently awaiting pickup."
                )
            )


        # =====================================================
        # CHECK IF ALREADY VERIFIED
        # =====================================================

        if donation["pickup_code_verified"] == 1:

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id,
                    error="This pickup code has already been used."
                )
            )


        # =====================================================
        # CHECK CODE
        # =====================================================

        if donation["pickup_code"] != entered_code:

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id,
                    error="Incorrect FoodResQ pickup code."
                )
            )


        # =====================================================
        # CHECK CODE CREATION TIME
        # =====================================================

        created_at = donation[
            "pickup_code_created_at"
        ]


        if created_at is None:

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id,
                    error="This pickup code is invalid."
                )
            )


        # =====================================================
        # 30-MINUTE EXPIRY
        # =====================================================

        expiry_time = (
            created_at
            + timedelta(minutes=30)
        )


        if datetime.now() > expiry_time:

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id,
                    error="This FoodResQ pickup code has expired."
                )
            )


        # =====================================================
        # EVERYTHING IS CORRECT
        # =====================================================

        cursor.execute(
            """
            UPDATE donations
            SET
                pickup_code_verified = 1,
                status = 'Picked Up'
            WHERE id = %s
              AND status = 'Accepted'
              AND pickup_code_verified = 0
            """,
            (donation_id,)
        )


        conn.commit()


        # =====================================================
        # SUCCESS
        # =====================================================

        return redirect(
            url_for(
                "ngo_donations",
                donation_id=donation_id,
                success="Pickup verified successfully. Donation marked as Picked Up."
            )
        )


    except mysql.connector.Error as e:

        print(
            "PICKUP CODE DATABASE ERROR:",
            e
        )

        if conn:
            conn.rollback()

        return "Database error: " + str(e), 500


    except Exception as e:

        print(
            "PICKUP CODE ERROR:",
            e
        )

        if conn:
            conn.rollback()

        return "Pickup verification error: " + str(e), 500


    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()

@app.route("/update_assignment_status", methods=["POST"])
def update_assignment_status():

    donation_id = request.form["donation_id"]
    status = request.form["status"]

    db = get_db()

    cursor = db.cursor()

    cursor.execute("""

    UPDATE donations

    SET status=%s

    WHERE donation_id=%s

    """,
    (
        status,
        donation_id
    ))

    db.commit()

    cursor.close()
    db.close()

    return redirect("/admin_assignments")

@app.route("/find_ngos/<int:donation_id>")
def find_ngos(donation_id):

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # =====================================================
    # GET DONATION DETAILS
    # =====================================================

    cursor.execute("""
        SELECT
            donation_id,
            food_name,
            quantity,
            quantity_unit,
            city,
            latitude,
            longitude,
            pickup_date
        FROM donations
        WHERE donation_id = %s
    """, (donation_id,))

    donation = cursor.fetchone()

    if donation is None:

        cursor.close()
        db.close()

        return "Donation not found"

    latitude = donation["latitude"]
    longitude = donation["longitude"]

    # =====================================================
    # FIND NEAREST ACTIVE NGOS
    # =====================================================

    cursor.execute("""
        SELECT
            ngo_id,
            ngo_name,
            partner_id,
            phone,
            service_area,
            address,
            status,

            (
                6371 *
                ACOS(
                    LEAST(
                        1,
                        GREATEST(
                            -1,
                            COS(RADIANS(%s))
                            *
                            COS(RADIANS(latitude))
                            *
                            COS(
                                RADIANS(longitude)
                                -
                                RADIANS(%s)
                            )
                            +
                            SIN(RADIANS(%s))
                            *
                            SIN(RADIANS(latitude))
                        )
                    )
                )
            ) AS distance

        FROM ngos

        WHERE status = 'Active'

        HAVING distance <= 20

        ORDER BY distance

        LIMIT 5

    """, (
        latitude,
        longitude,
        latitude
    ))

    ngos = cursor.fetchall()

    cursor.close()
    db.close()

    return render_template(
        "find_ngos.html",
        donation=donation,
        ngos=ngos
    )

@app.route("/undo_assignment", methods=["POST"])
def undo_assignment():

    donation_id = request.form["donation_id"]

    db = get_db()

    cursor = db.cursor()

    cursor.execute("""
        UPDATE donations
        SET ngo_id = NULL
        WHERE donation_id = %s
    """, (donation_id,))

    db.commit()

    cursor.close()
    db.close()

    return redirect("/admin_assignments?tab=pending")

@app.route('/ngo_assigned_donations')
def ngo_assigned_donations():

    if 'ngo_id' not in session:
        return redirect(url_for('ngo_login'))

    ngo_id = session['ngo_id']

    connection = get_db()

    cursor = connection.cursor(dictionary=True)

    query = """
        SELECT
        d.donation_id,
        d.food_name,
        d.food_type,
        d.quantity,
        d.quantity_unit,
        d.address,
        d.city,
        d.pickup_date,
        d.pickup_time,
        d.status,
        d.ngo_id
        FROM donations d
        LEFT JOIN users u
            ON d.user_id = u.id
        WHERE d.ngo_id = %s
        ORDER BY d.donation_id DESC
    """

    cursor.execute(query, (ngo_id,))

    donations = cursor.fetchall()

    cursor.close()
    connection.close()

    return render_template(
        'ngo_assigned_donations.html',
        donations=donations
    )

@app.route("/nearby_donations")
def nearby_donations():

    if "ngo_id" not in session:
        return redirect("/ngo_login")

    ngo_id = session["ngo_id"]

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # Get logged-in NGO location
    cursor.execute("""
        SELECT
            ngo_id,
            ngo_name,
            latitude,
            longitude
        FROM ngos
        WHERE ngo_id=%s
    """, (ngo_id,))

    ngo = cursor.fetchone()

    if not ngo:
        cursor.close()
        db.close()
        return "NGO not found"

    # Get available donations
    cursor.execute("""
        SELECT
            d.donation_id,
            d.food_name,
            d.food_type,
            d.quantity,
            d.quantity_unit,
            d.address,
            d.city,
            d.pickup_date,
            d.pickup_time,
            d.details,
            d.latitude,
            d.longitude,
            d.status,
            u.name AS donor_name
        FROM donations d
        JOIN users u
            ON d.user_id = u.id
        WHERE d.status = 'Pending'
          AND d.ngo_id IS NULL
          AND d.latitude IS NOT NULL
          AND d.longitude IS NOT NULL
        ORDER BY d.donation_id DESC
    """)

    donations = cursor.fetchall()

    cursor.close()
    db.close()

    # Calculate distance in Python
    import math

    nearby = []

    for donation in donations:

        lat1 = float(ngo["latitude"])
        lon1 = float(ngo["longitude"])

        lat2 = float(donation["latitude"])
        lon2 = float(donation["longitude"])

        R = 6371

        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)

        a = (
            math.sin(dlat / 2) ** 2
            +
            math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(dlon / 2) ** 2
        )

        c = 2 * math.atan2(
            math.sqrt(a),
            math.sqrt(1 - a)
        )

        distance = R * c

        if distance <= 20:

            donation["distance"] = distance

            nearby.append(donation)

    nearby.sort(
        key=lambda x: x["distance"]
    )

    return render_template(
        "ngo_nearby_donations.html",
        donations=nearby,
        ngo=ngo
    )

@app.route("/ngo_request_donation/<int:donation_id>", methods=["POST"])
def ngo_request_donation(donation_id):

    if "ngo_id" not in session:
        return redirect("/ngo_login")

    ngo_id = session["ngo_id"]

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # Make sure donation is still available
    cursor.execute("""
        SELECT
            donation_id,
            status,
            ngo_id
        FROM donations
        WHERE donation_id=%s
    """, (donation_id,))

    donation = cursor.fetchone()

    if not donation:

        cursor.close()
        db.close()

        return notify_redirect("Donation not found.", "/nearby_donations")

    # Someone already got the donation
    if donation["ngo_id"] is not None or donation["status"] != "Pending":

        cursor.close()
        db.close()

        return notify_redirect("This donation is no longer available.", "/nearby_donations")

    # Check whether this NGO already requested it
    cursor.execute("""
        SELECT request_id
        FROM ngo_requests
        WHERE donation_id=%s
        AND ngo_id=%s
        AND status='Pending'
    """, (donation_id, ngo_id))

    existing_request = cursor.fetchone()

    if existing_request:

        cursor.close()
        db.close()

        return notify_redirect("You have already requested this donation.", "/nearby_donations")

    # Create request
    cursor.execute("""
        INSERT INTO ngo_requests
        (
            donation_id,
            ngo_id,
            status
        )
        VALUES
        (%s,%s,'Pending')
    """, (
        donation_id,
        ngo_id
    ))

    db.commit()

    cursor.close()
    db.close()

    return notify_redirect("Pickup request sent to FoodResQ admin.", "/nearby_donations", kind="success")

@app.route("/admin_ngo_requests")
def admin_ngo_requests():

    if "admin" not in session:
        return redirect("/admin_login")

    db = get_db()

    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT

            r.request_id,
            r.requested_on,
            r.status AS request_status,

            d.donation_id,
            d.food_name,
            d.food_type,
            d.quantity,
            d.quantity_unit,
            d.city,
            d.address,
            d.landmark,
            d.pickup_date,
            d.pickup_time,
            d.status AS donation_status,

            u.name AS donor_name,

            n.ngo_id,
            n.ngo_name,
            n.partner_id,
            n.phone AS ngo_phone,
            n.address AS ngo_address

        FROM ngo_requests r

        JOIN donations d
        ON r.donation_id = d.donation_id

        JOIN ngos n
        ON r.ngo_id = n.ngo_id

        JOIN users u
        ON d.user_id = u.id

        ORDER BY r.request_id DESC

    """)

    requests = cursor.fetchall()

    cursor.close()
    db.close()

    return render_template(
        "admin_ngo_requests.html",
        requests=requests
    )

@app.route("/admin_approve_ngo_request/<int:request_id>", methods=["POST"])
def admin_approve_ngo_request(request_id):

    if "admin" not in session:
        return redirect("/admin_login")

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # Get request
    cursor.execute("""
        SELECT
            request_id,
            donation_id,
            ngo_id,
            status
        FROM ngo_requests
        WHERE request_id=%s
    """, (request_id,))

    ngo_request = cursor.fetchone()

    if not ngo_request:

        cursor.close()
        db.close()

        return redirect("/admin_ngo_requests")

    # Only pending requests can be approved
    if ngo_request["status"] != "Pending":

        cursor.close()
        db.close()

        return redirect("/admin_ngo_requests")

    # Check donation is still available
    cursor.execute("""
        SELECT
            donation_id,
            ngo_id,
            status
        FROM donations
        WHERE donation_id=%s
    """, (ngo_request["donation_id"],))

    donation = cursor.fetchone()

    if not donation:

        cursor.close()
        db.close()

        return redirect("/admin_ngo_requests")

    # Donation already assigned to someone
    if donation["ngo_id"] is not None:

        cursor.execute("""
            UPDATE ngo_requests
            SET status='Rejected'
            WHERE request_id=%s
        """, (request_id,))

        db.commit()

        cursor.close()
        db.close()

        return notify_redirect("This donation has already been assigned to another NGO.", "/admin_ngo_requests")

    # Assign NGO to donation
    cursor.execute("""
        UPDATE donations
        SET
            ngo_id=%s,
            status='Assigned'
        WHERE donation_id=%s
    """, (
        ngo_request["ngo_id"],
        ngo_request["donation_id"]
    ))

    # Approve selected request
    cursor.execute("""
        UPDATE ngo_requests
        SET status='Approved'
        WHERE request_id=%s
    """, (request_id,))

    # Reject all other pending requests
    # for this same donation
    cursor.execute("""
        UPDATE ngo_requests
        SET status='Rejected'
        WHERE donation_id=%s
        AND request_id<>%s
        AND status='Pending'
    """, (
        ngo_request["donation_id"],
        request_id
    ))

    db.commit()

    cursor.close()
    db.close()

    return redirect("/admin_ngo_requests")


@app.route("/admin_reject_ngo_request/<int:request_id>", methods=["POST"])
def admin_reject_ngo_request(request_id):

    if "admin" not in session:
        return redirect("/admin_login")

    db = get_db()

    cursor = db.cursor()

    cursor.execute("""
        UPDATE ngo_requests
        SET status='Rejected'
        WHERE request_id=%s
    """, (request_id,))

    db.commit()

    cursor.close()
    db.close()

    return redirect("/admin_ngo_requests")

@app.route("/ngo_donations/<int:donation_id>")
def ngo_donations(donation_id):

    # -----------------------------------------
    # NGO LOGIN CHECK
    # -----------------------------------------

    if 'ngo_id' not in session or session.get('role') != 'ngo':
        return redirect("/ngo_login")

    ngo_id = session["ngo_id"]

    try:

        # -----------------------------------------
        # DATABASE
        # -----------------------------------------

        db = get_db()

        cursor = db.cursor(dictionary=True)

        # -----------------------------------------
        # GET DONATION
        # -----------------------------------------

        cursor.execute("""
            SELECT
                donation_id,
                user_id,
                food_name,
                food_type,
                food_description,
                quantity,
                quantity_unit,
                serving_size,
                address,
                city,
                pickup_date,
                pickup_time,
                preparation_time,
                expiry_time,
                contact,
                details,
                status,
                ngo_id,
                donor_confirmation,
                donor_feedback,
                rating,
                latitude,
                longitude,
                verification_completed,
                verification_date,
                pickup_marked_at,
                pickup_otp,
                pickup_otp_expiry,
                pickup_confirmed_at,
                on_the_way_at,
                completed_at,
                last_reminder_at,
                reminder_count,
                pickup_code,
                pickup_code_verified,
                pickup_code_created_at

            FROM donations

            WHERE donation_id = %s
            AND ngo_id = %s

            LIMIT 1
        """, (donation_id, ngo_id))

        donation = cursor.fetchone()

        cursor.close()
        db.close()

        # -----------------------------------------
        # DONATION NOT FOUND
        # -----------------------------------------

        if not donation:
            return "Donation not found.", 404

        # -----------------------------------------
        # SEND TO HTML
        # -----------------------------------------

        return render_template(
            "ngo_donations.html",
            donation=donation
        )

    except Exception as e:

        print("======================================")
        print("NGO DONATION DETAILS ERROR:")
        print(e)
        print("======================================")

        return "Unable to load donation details.", 500

# =========================================================
# NGO ACCEPT DONATION
# =========================================================

@app.route("/ngo_approve/<int:donation_id>", methods=["POST"])
def ngo_approve(donation_id):

    if "ngo_id" not in session or session.get("role") != "ngo":
        return redirect(url_for("ngo_login"))

    ngo_id = session["ngo_id"]

    db = get_db()

    cursor = db.cursor(dictionary=True)

    # -------------------------------------------------
    # GET DONATION + DONOR
    # -------------------------------------------------

    cursor.execute("""
        SELECT
            donation_id,
            user_id,
            food_name
        FROM donations
        WHERE donation_id = %s
        AND ngo_id = %s
        AND status = 'Assigned'
    """, (donation_id, ngo_id))

    donation = cursor.fetchone()

    # Donation not found / already processed
    if not donation:
        cursor.close()
        db.close()

        return redirect(
            url_for("ngo_assigned_donations")
        )

    # -------------------------------------------------
    # UPDATE STATUS
    # -------------------------------------------------

    cursor.execute("""
        UPDATE donations
        SET status = 'NGO Accepted'
        WHERE donation_id = %s
        AND ngo_id = %s
        AND status = 'Assigned'
    """, (donation_id, ngo_id))

    # -------------------------------------------------
    # DONOR NOTIFICATION
    # -------------------------------------------------

    if donation["user_id"] is not None:

        create_notification(
            user_id=donation["user_id"],
            donation_id=donation["donation_id"],
            message=(
                "Your donation #"
                + str(donation["donation_id"])
                + " ("
                + str(donation["food_name"])
                + ") has been accepted by the assigned NGO. "
                "It will now proceed to the verification and pickup stage."
            )
        )

    db.commit()

    cursor.close()
    db.close()

    return redirect(
        url_for("ngo_assigned_donations")
    )

# =========================================================
# NGO REJECT DONATION
# =========================================================

@app.route("/ngo_reject/<int:donation_id>", methods=["POST"])
def ngo_reject(donation_id):

    if "ngo_id" not in session or session.get("role") != "ngo":
        return redirect(url_for("ngo_login"))

    ngo_id = session["ngo_id"]

    db = get_db()

    cursor = db.cursor()

    cursor.execute("""
        UPDATE donations
        SET
            status = 'Assignment Rejected',
            ngo_id = NULL
        WHERE donation_id = %s
        AND ngo_id = %s
        AND status = 'Assigned'
    """, (donation_id, ngo_id))

    db.commit()

    cursor.close()
    db.close()

    return redirect(
        url_for("ngo_assigned_donations")
    )

@app.route('/ngo/verify_donation/<int:donation_id>')
def ngo_verify_donation(donation_id):

    # =====================================================
    # NGO LOGIN CHECK
    # =====================================================

    if 'ngo_id' not in session or session.get('role') != 'ngo':
        return redirect(url_for('ngo_login'))

    ngo_id = session['ngo_id']


    conn = None
    cursor = None


    try:

        # =================================================
        # DATABASE CONNECTION
        # =================================================

        conn = get_db()

        cursor = conn.cursor(dictionary=True)


        # =================================================
        # GET DONATION
        # =================================================

        cursor.execute("""
            SELECT

                donation_id,
                food_name,
                food_type,
                quantity,
                quantity_unit,
                address,
                city,
                pickup_date,
                pickup_time,
                contact,
                details,
                status,
                ngo_id,
                verification_completed,
                verification_date,
                pickup_code

            FROM donations

            WHERE donation_id = %s
              AND ngo_id = %s

            LIMIT 1

        """, (
            donation_id,
            ngo_id
        ))


        donation = cursor.fetchone()


        # =================================================
        # DONATION NOT FOUND
        # =================================================

        if not donation:

            return "Donation not found.", 404


        # =================================================
        # ONLY NGO ACCEPTED CAN BE VERIFIED
        # =================================================

        if donation['status'] != 'NGO Accepted':

            flash(
                "This donation is not ready for verification.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_donations',
                    donation_id=donation_id
                )
            )


        # =================================================
        # SHOW VERIFICATION PAGE
        # =================================================

        return render_template(
            'ngo_verify_donation.html',
            donation=donation
        )


    except mysql.connector.Error as e:

        return f"Database error: {str(e)}", 500


    except Exception as e:

        return f"Error: {str(e)}", 500


    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()

@app.route(
    '/ngo/verify_donation/<int:donation_id>/submit',
    methods=['POST']
)
def submit_donation_verification(donation_id):

    # =====================================================
    # NGO LOGIN CHECK
    # =====================================================

    if 'ngo_id' not in session or session.get('role') != 'ngo':
        return redirect(url_for('ngo_login'))

    ngo_id = session['ngo_id']

    conn = None
    cursor = None

    try:

        # =================================================
        # GET VERIFICATION DATA
        # =================================================

        food_match = request.form.get('food_match')
        quantity_match = request.form.get('quantity_match')
        food_condition = request.form.get('food_condition')
        packing_checked = request.form.get('packing_checked')
        address_checked = request.form.get('address_checked')
        pickup_details = request.form.get('pickup_details')

        checks = [
            food_match,
            quantity_match,
            food_condition,
            packing_checked,
            address_checked,
            pickup_details
        ]

        # =================================================
        # ALL CHECKS REQUIRED
        # =================================================

        if not all(checks):

            flash(
                "Please complete all verification checks before verifying the donation.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_verify_donation',
                    donation_id=donation_id
                )
            )

        # =================================================
        # DATABASE
        # =================================================

        conn = get_db()

        cursor = conn.cursor(dictionary=True)

        # =================================================
        # GET DONATION
        # =================================================

        cursor.execute("""
            SELECT
                donation_id,
                user_id,
                food_name,
                status,
                pickup_code

            FROM donations

            WHERE donation_id = %s
              AND ngo_id = %s

            LIMIT 1
        """, (
            donation_id,
            ngo_id
        ))

        donation = cursor.fetchone()

        # =================================================
        # DONATION NOT FOUND
        # =================================================

        if not donation:

            flash(
                "Donation not found.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_assigned_donations'
                )
            )

        # =================================================
        # STATUS CHECK
        # =================================================

        if donation["status"] != "NGO Accepted":

            flash(
                "This donation is no longer available for verification.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_donations',
                    donation_id=donation_id
                )
            )

        # =================================================
        # GENERATE UNIQUE PICKUP CODE
        # =================================================

        pickup_code = None

        characters = string.ascii_uppercase + string.digits

        for attempt in range(20):

            part1 = ''.join(
                random.SystemRandom().choices(
                    characters,
                    k=4
                )
            )

            part2 = ''.join(
                random.SystemRandom().choices(
                    characters,
                    k=4
                )
            )

            generated_code = f"FRQ-{part1}-{part2}"

            cursor.execute("""
                SELECT donation_id
                FROM donations
                WHERE pickup_code = %s
                LIMIT 1
            """, (
                generated_code,
            ))

            existing_code = cursor.fetchone()

            if not existing_code:

                pickup_code = generated_code
                break

        # =================================================
        # CODE GENERATION FAILED
        # =================================================

        if not pickup_code:

            conn.rollback()

            flash(
                "Unable to generate a unique FoodResQ pickup code. Please try again.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_verify_donation',
                    donation_id=donation_id
                )
            )

        # =================================================
        # UPDATE DONATION
        # =================================================

        cursor.execute("""
            UPDATE donations

            SET
                status = 'Verified',
                verification_completed = 1,
                verification_date = NOW(),
                pickup_code = %s

            WHERE donation_id = %s
              AND ngo_id = %s
              AND status = 'NGO Accepted'
        """, (
            pickup_code,
            donation_id,
            ngo_id
        ))

        # =================================================
        # MAKE SURE UPDATE WORKED
        # =================================================

        if cursor.rowcount == 0:

            conn.rollback()

            flash(
                "This donation could not be verified. Its status may have changed.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_donations',
                    donation_id=donation_id
                )
            )

        # =================================================
        # DONOR NOTIFICATION
        # =================================================

        if donation["user_id"] is not None:

            create_notification(
                user_id=donation["user_id"],
                donation_id=donation["donation_id"],
                message=(
                    "Your donation #"
                    + str(donation["donation_id"])
                    + " ("
                    + str(donation["food_name"])
                    + ") has been verified by the assigned NGO. "
                    "A FoodResQ pickup code has been generated and "
                    "the donation is ready for pickup."
                )
            )

        # =================================================
        # SAVE
        # =================================================

        conn.commit()

        # =================================================
        # SUCCESS
        # =================================================

        flash(
            "Donation verified successfully. A unique FoodResQ pickup code has been generated.",
            "success"
        )

        return redirect(
            url_for(
                'ngo_donations',
                donation_id=donation_id
            )
        )

    except mysql.connector.Error as e:

        if conn:
            conn.rollback()

        print("NGO VERIFICATION DATABASE ERROR:", e)

        return f"Database error: {str(e)}", 500

    except Exception as e:

        if conn:
            conn.rollback()

        print("NGO VERIFICATION ERROR:", e)

        return f"Error: {str(e)}", 500

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()
# ============================================================
# NGO REPORT ISSUE - OPEN PAGE
# ============================================================

@app.route('/ngo/report_issue/<int:donation_id>', methods=['GET'])
def ngo_report_issue(donation_id):

    # --------------------------------------------------------
    # NGO LOGIN CHECK
    # --------------------------------------------------------

    if 'ngo_id' not in session or session.get('role') != 'ngo':
        return redirect(url_for('ngo_login'))

    ngo_id = session['ngo_id']


    # --------------------------------------------------------
    # DATABASE CONNECTION
    # --------------------------------------------------------

    conn = get_db()

    cursor = conn.cursor(dictionary=True)


    # --------------------------------------------------------
    # GET DONATION
    # --------------------------------------------------------

    cursor.execute("""
        SELECT
            donation_id,
            user_id,
            food_name,
            food_type,
            quantity,
            quantity_unit,
            address,
            city,
            pickup_date,
            pickup_time,
            contact,
            details,
            status,
            ngo_id
        FROM donations
        WHERE donation_id = %s
        AND ngo_id = %s
    """, (donation_id, ngo_id))

    donation = cursor.fetchone()


    cursor.close()
    conn.close()


    # --------------------------------------------------------
    # CHECK DONATION
    # --------------------------------------------------------

    if not donation:

        return (
            "Donation not found or not assigned to your NGO.",
            404
        )


    # --------------------------------------------------------
    # OPEN REPORT PAGE
    # --------------------------------------------------------

    return render_template(
        'ngo_report_issue.html',
        donation=donation
    )

    
# ============================================================
# NGO REPORT ISSUE - SUBMIT
# ============================================================

@app.route(
    '/ngo/report_issue/<int:donation_id>/submit',
    methods=['POST']
)
def ngo_submit_report_issue(donation_id):

    # --------------------------------------------------------
    # NGO LOGIN CHECK
    # --------------------------------------------------------

    if 'ngo_id' not in session or session.get('role') != 'ngo':
        return redirect(url_for('ngo_login'))

    ngo_id = session['ngo_id']


    # --------------------------------------------------------
    # GET FORM DATA
    # --------------------------------------------------------

    reason = request.form.get('issue_type')

    description = request.form.get('issue_description')


    # --------------------------------------------------------
    # VALIDATE FORM
    # --------------------------------------------------------

    if not reason or not description:

        flash(
            "Please select an issue and describe the problem.",
            "error"
        )

        return redirect(
            url_for(
                'ngo_report_issue',
                donation_id=donation_id
            )
        )


    # --------------------------------------------------------
    # DATABASE CONNECTION
    # --------------------------------------------------------

    conn = get_db()

    cursor = conn.cursor(dictionary=True)


    # --------------------------------------------------------
    # CHECK DONATION BELONGS TO NGO
    # --------------------------------------------------------

    cursor.execute("""
        SELECT
            donation_id,
            user_id
        FROM donations
        WHERE donation_id = %s
        AND ngo_id = %s
    """, (donation_id, ngo_id))

    donation = cursor.fetchone()


    if not donation:

        cursor.close()
        conn.close()

        return (
            "Donation not found or not assigned to your NGO.",
            404
        )


    # --------------------------------------------------------
    # INSERT REPORT
    # --------------------------------------------------------

    cursor.execute("""
        INSERT INTO donation_reports
        (
            donation_id,
            user_id,
            reason,
            description,
            reported_on,
            status,
            reporter_type
        )
        VALUES
        (
            %s,
            %s,
            %s,
            %s,
            NOW(),
            'Pending',
            'NGO'
        )
    """, (
        donation['donation_id'],
        donation['user_id'],
        reason,
        description
    ))


    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    conn.commit()


    cursor.close()
    conn.close()


    # --------------------------------------------------------
    # SUCCESS MESSAGE
    # --------------------------------------------------------

    flash(
        "Your report has been sent to Admin successfully.",
        "success"
    )


    # --------------------------------------------------------
    # RETURN TO NGO ASSIGNED DONATIONS
    # --------------------------------------------------------

    return redirect(
        url_for('ngo_assigned_donations')
    )

@app.route(
    "/ngo_mark_picked_up/<int:donation_id>",
    methods=["POST"]
)
def ngo_mark_picked_up(donation_id):

    # =====================================================
    # NGO LOGIN CHECK
    # =====================================================

    if 'ngo_id' not in session or session.get('role') != 'ngo':
        return redirect(url_for('ngo_login'))

    ngo_id = session['ngo_id']

    conn = None
    cursor = None

    try:

        # =================================================
        # GET CODE ENTERED BY NGO
        # =================================================

        entered_code = request.form.get(
            "pickup_code",
            ""
        ).strip().upper()

        if not entered_code:

            flash(
                "Please enter the FoodResQ pickup code.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_donations',
                    donation_id=donation_id
                )
            )

        # =================================================
        # DATABASE CONNECTION
        # =================================================

        conn = get_db()

        cursor = conn.cursor(dictionary=True)

        # =================================================
        # GET DONATION
        # =================================================

        cursor.execute("""
            SELECT
                donation_id,
                ngo_id,
                status,
                pickup_code,
                pickup_code_verified
            FROM donations
            WHERE donation_id = %s
              AND ngo_id = %s
            LIMIT 1
        """, (
            donation_id,
            ngo_id
        ))

        donation = cursor.fetchone()

        # =================================================
        # DONATION NOT FOUND
        # =================================================

        if not donation:

            flash(
                "Donation not found or it is not assigned to your NGO.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_assigned_donations'
                )
            )

        # =================================================
        # ONLY VERIFIED DONATIONS CAN BE PICKED UP
        # =================================================

        if donation["status"] != "Verified":

            flash(
                "This donation is not ready for pickup verification.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_donations',
                    donation_id=donation_id
                )
            )

        # =================================================
        # CHECK PICKUP CODE EXISTS
        # =================================================

        if not donation["pickup_code"]:

            flash(
                "No FoodResQ pickup code has been generated for this donation.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_donations',
                    donation_id=donation_id
                )
            )

        # =================================================
        # COMPARE PICKUP CODE
        # =================================================

        stored_code = str(
            donation["pickup_code"]
        ).strip().upper()

        if entered_code != stored_code:

            flash(
                "Incorrect FoodResQ pickup code. Pickup has NOT been confirmed.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_donations',
                    donation_id=donation_id
                )
            )

        # =================================================
        # CORRECT CODE
        #
        # PICKUP IS NOW CONFIRMED
        # =================================================

        cursor.execute("""
            UPDATE donations
            SET
                status = 'Picked Up',
                pickup_code_verified = 1,
                pickup_marked_at = NOW(),
                pickup_confirmed_at = NOW()
            WHERE donation_id = %s
              AND ngo_id = %s
              AND status = 'Verified'
        """, (
            donation_id,
            ngo_id
        ))

        # =================================================
        # CHECK UPDATE
        # =================================================

        if cursor.rowcount == 0:

            conn.rollback()

            flash(
                "Pickup could not be processed. Please try again.",
                "error"
            )

            return redirect(
                url_for(
                    'ngo_donations',
                    donation_id=donation_id
                )
            )

        # =================================================
        # SAVE CHANGES
        # =================================================

        conn.commit()

        # =================================================
        # SUCCESS
        # =================================================

        flash(
            "Pickup code verified successfully. Donation marked as Picked Up.",
            "success"
        )

        return redirect(
            url_for(
                'ngo_donations',
                donation_id=donation_id
            )
        )

    # =====================================================
    # DATABASE ERROR
    # =====================================================

    except mysql.connector.Error as e:

        if conn:
            conn.rollback()

        return f"Database error: {str(e)}", 500

    # =====================================================
    # GENERAL ERROR
    # =====================================================

    except Exception as e:

        if conn:
            conn.rollback()

        return f"Error: {str(e)}", 500

    # =====================================================
    # CLEANUP
    # =====================================================

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()

@app.route("/ngo_start_delivery/<int:donation_id>", methods=["POST"])
def ngo_start_delivery(donation_id):

    # =====================================================
    # NGO LOGIN CHECK
    # =====================================================

    if 'ngo_id' not in session or session.get('role') != 'ngo':
        return redirect(url_for("ngo_login"))

    ngo_id = session["ngo_id"]

    db = None
    cursor = None

    try:

        # =================================================
        # DATABASE
        # =================================================

        db = get_db()

        cursor = db.cursor(dictionary=True)

        # =================================================
        # GET DONATION
        # =================================================

        cursor.execute("""
            SELECT
                donation_id,
                user_id,
                food_name,
                status
            FROM donations
            WHERE donation_id = %s
              AND ngo_id = %s
            LIMIT 1
        """, (
            donation_id,
            ngo_id
        ))

        donation = cursor.fetchone()

        # =================================================
        # DONATION NOT FOUND
        # =================================================

        if not donation:

            flash(
                "Donation not found.",
                "error"
            )

            return redirect(
                url_for("ngo_assigned_donations")
            )

        # =================================================
        # ONLY PICKED UP CAN START DELIVERY
        # =================================================

        if donation["status"] != "Picked Up":

            flash(
                "This donation is not ready to start delivery.",
                "error"
            )

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id
                )
            )

        # =================================================
        # CHANGE STATUS
        # =================================================

        cursor.execute("""
            UPDATE donations

            SET
                status = 'On the Way',
                on_the_way_at = NOW()

            WHERE donation_id = %s
              AND ngo_id = %s
              AND status = 'Picked Up'
        """, (
            donation_id,
            ngo_id
        ))

        # =================================================
        # CHECK UPDATE
        # =================================================

        if cursor.rowcount == 0:

            db.rollback()

            flash(
                "Unable to start delivery. Please try again.",
                "error"
            )

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id
                )
            )

        # =================================================
        # DONOR NOTIFICATION
        # =================================================

        if donation["user_id"] is not None:

            create_notification(
                user_id=donation["user_id"],
                donation_id=donation["donation_id"],
                message=(
                    "Your donation #"
                    + str(donation["donation_id"])
                    + " ("
                    + str(donation["food_name"])
                    + ") is now on the way 🚚. "
                    "The rescued food is being transported for distribution."
                )
            )

        # =================================================
        # SAVE
        # =================================================

        db.commit()

        # =================================================
        # SUCCESS
        # =================================================

        flash(
            "Delivery started successfully.",
            "success"
        )

        return redirect(
            url_for(
                "ngo_donations",
                donation_id=donation_id
            )
        )

    except mysql.connector.Error as e:

        if db:
            db.rollback()

        print("======================================")
        print("START DELIVERY DATABASE ERROR:")
        print(e)
        print("======================================")

        return f"Database error: {str(e)}", 500

    except Exception as e:

        if db:
            db.rollback()

        print("======================================")
        print("START DELIVERY ERROR:")
        print(e)
        print("======================================")

        return f"Error: {str(e)}", 500

    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

@app.route("/my_donation_details/<int:donation_id>")
def my_donation_details(donation_id):

    if "user_id" not in session or session.get("role") != "donor":
        return redirect("/user_login")

    user_id = session["user_id"]

    db = None
    cursor = None

    try:

        db = get_db()

        cursor = db.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                d.*,
                n.ngo_name,
                n.partner_id,
                n.phone AS ngo_phone,
                n.address AS ngo_address,
                n.service_area
            FROM donations d
            LEFT JOIN ngos n
                ON d.ngo_id = n.ngo_id
            WHERE d.donation_id = %s
              AND d.user_id = %s
            LIMIT 1
        """, (
            donation_id,
            user_id
        ))

        donation = cursor.fetchone()

        if donation is None:
            return "Donation not found.", 404

        return render_template(
            "my_donation_details.html",
            donation=donation
        )

    except Exception as e:

        import traceback

        print("========================================")
        print("MY DONATION DETAILS ERROR")
        print("========================================")
        traceback.print_exc()
        print("========================================")

        return f"Unable to load donation details: {e}", 500

    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

def donor_confirm_pickup(donation_id):

    # =====================================================
    # DONOR LOGIN CHECK
    # =====================================================

    if "user_id" not in session:
        return redirect(url_for("user_login"))

    user_id = session["user_id"]


    conn = None
    cursor = None


    try:

        # =================================================
        # DATABASE
        # =================================================

        conn = get_db()

        cursor = conn.cursor(dictionary=True)


        # =================================================
        # GET DONATION
        # =================================================

        cursor.execute("""
            SELECT

                donation_id,
                user_id,
                food_name,
                status,
                pickup_code

            FROM donations

            WHERE donation_id = %s

              AND user_id = %s

            LIMIT 1

        """, (
            donation_id,
            user_id
        ))


        donation = cursor.fetchone()


        # =================================================
        # NOT FOUND
        # =================================================

        if not donation:

            return "Donation not found.", 404


        # =================================================
        # ONLY AWAITING DONOR CONFIRMATION
        # =================================================

        if donation["status"] != "Awaiting Donor Confirmation":

            flash(
                "This donation is not waiting for pickup confirmation.",
                "error"
            )

            return redirect(
                url_for(
                    "my_donation_details",
                    donation_id=donation_id
                )
            )


        # =================================================
        # DONOR CONFIRMS
        # =================================================

        if request.method == "POST":

            cursor.execute("""
                UPDATE donations

                SET status = 'Picked Up'

                WHERE donation_id = %s

                  AND user_id = %s

                  AND status = 'Awaiting Donor Confirmation'

            """, (
                donation_id,
                user_id
            ))


            if cursor.rowcount == 0:

                conn.rollback()

                flash(
                    "Pickup confirmation could not be completed.",
                    "error"
                )

                return redirect(
                    url_for(
                        "my_donation_details",
                        donation_id=donation_id
                    )
                )


            conn.commit()


            flash(
                "Pickup confirmed successfully. The donation has been handed over to the NGO.",
                "success"
            )


            return redirect(
                url_for(
                    "my_donation_details",
                    donation_id=donation_id
                )
            )


        # =================================================
        # GET CONFIRMATION PAGE
        # =================================================

        return render_template(
            "donor_confirm_pickup.html",
            donation=donation
        )


    except mysql.connector.Error as e:

        if conn:
            conn.rollback()

        return f"Database error: {str(e)}", 500


    except Exception as e:

        if conn:
            conn.rollback()

        return f"Error: {str(e)}", 500


    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()

@app.route("/donor_reject_pickup/<int:donation_id>", methods=["POST"])
def donor_reject_pickup(donation_id):

    if session.get("role") != "donor" or "user_id" not in session:
        return redirect("/user_login")

    db = get_db()

    cursor = db.cursor()

    cursor.execute("""
        UPDATE donations
        SET
            donor_confirmation='No',
            status='Pickup Failed'
        WHERE donation_id=%s
          AND user_id=%s
    """, (donation_id, session["user_id"]))

    db.commit()

    cursor.close()
    db.close()

    return redirect("/my_donations")

@app.route('/report_issue/<int:donation_id>')
def report_issue(donation_id):

    # -----------------------------------
    # DONOR LOGIN CHECK
    # -----------------------------------

    if 'user_id' not in session:
        return redirect(url_for('user_login'))

    user_id = session['user_id']


    # -----------------------------------
    # DATABASE CONNECTION
    # -----------------------------------

    conn = get_db()

    cursor = conn.cursor(dictionary=True)


    # -----------------------------------
    # GET DONATION
    # -----------------------------------

    cursor.execute("""
        SELECT
            donation_id,
            user_id,
            food_name,
            food_type,
            quantity,
            quantity_unit,
            address,
            city,
            pickup_date,
            pickup_time,
            contact,
            details,
            status,
            ngo_id

        FROM donations

        WHERE donation_id = %s
        AND user_id = %s
    """, (donation_id, user_id))

    donation = cursor.fetchone()


    cursor.close()
    conn.close()


    # -----------------------------------
    # DONATION NOT FOUND
    # -----------------------------------

    if not donation:
        return "Donation not found", 404


    # -----------------------------------
    # OPEN REPORT PAGE
    # -----------------------------------

    return render_template(
        '_user_report_issue.html',
        donation=donation
    )

@app.route(
    '/report_issue/<int:donation_id>/submit',
    methods=['POST']
)
def submit_report_issue(donation_id):

    if 'user_id' not in session:
        return redirect(url_for('user_login'))

    user_id = session['user_id']

    reason = request.form.get('issue_type')
    description = request.form.get('issue_description')

    if not reason or not description:

        flash(
            "Please select an issue and describe the problem.",
            "error"
        )

        return redirect(
            url_for(
                'report_issue',
                donation_id=donation_id
            )
        )

    conn = get_db()

    cursor = conn.cursor(dictionary=True)

    # Make sure this donation belongs to this donor
    cursor.execute("""
        SELECT donation_id
        FROM donations
        WHERE donation_id = %s
        AND user_id = %s
    """, (donation_id, user_id))

    donation = cursor.fetchone()

    if not donation:

        cursor.close()
        conn.close()

        return "Donation not found", 404

    # Save donor report
    cursor.execute("""
        INSERT INTO donation_reports
        (
            donation_id,
            user_id,
            reason,
            description,
            reported_on,
            status,
            reporter_type
        )
        VALUES
        (
            %s,
            %s,
            %s,
            %s,
            NOW(),
            'Pending',
            'Donor'
        )
    """, (
        donation_id,
        user_id,
        reason,
        description
    ))

    conn.commit()

    cursor.close()
    conn.close()

    flash(
        "Your report has been sent to Admin successfully.",
        "success"
    )

    return redirect(
        url_for('my_donations')
    )


@app.route("/admin/ngo_document/<filename>")
def admin_ngo_document(filename):

    if session.get("role") != "admin" or "admin_id" not in session:
        return redirect("/admin_login")

    return send_from_directory(
        NGO_DOCUMENT_FOLDER,
        secure_filename(filename),
        as_attachment=False
    )


# ============================================================
# DONOR PROFILE
# ============================================================

@app.route("/profile")
def profile():

    if "user_id" not in session or session.get("role") != "donor":
        return redirect("/user_login")

    db = get_db()
    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            id,
            donor_code,
            name,
            email,
            phone,
            profile_photo
        FROM users
        WHERE id = %s
    """, (session["user_id"],))

    user = cursor.fetchone()

    cursor.close()
    db.close()

    if not user:
        session.clear()
        return redirect("/user_login")

    # --------------------------------------------------------
    # GENERATE INITIALS
    # --------------------------------------------------------

    initials = ""

    if user["name"]:

        parts = user["name"].strip().split()

        if len(parts) >= 2:

            initials = (
                parts[0][0] +
                parts[-1][0]
            ).upper()

        elif len(parts) == 1:

            initials = parts[0][0].upper()

    return render_template(
        "profile.html",
        user=user,
        initials=initials
    )


# ============================================================
# KEEP OLD EDIT PROFILE URL WORKING
# ============================================================

@app.route("/edit_profile")
def edit_profile():

    return redirect(url_for("profile"))


# ============================================================
# UPDATE DONOR PROFILE DETAILS
# ============================================================
@app.route("/update_profile", methods=["POST"])
def update_profile():

    if "user_id" not in session or session.get("role") != "donor":
        return redirect("/")

    db = None
    cursor = None

    try:

        # ==========================================
        # GET FORM DATA
        # ==========================================

        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()


        # ==========================================
        # VALIDATE NAME
        # ==========================================

        if not name:
            return "Name is required.", 400


        # ==========================================
        # DATABASE
        # ==========================================

        db = get_db()
        cursor = db.cursor()


        # ==========================================
        # UPDATE PROFILE
        # ==========================================

        cursor.execute("""
            UPDATE users
            SET
                name = %s,
                phone = %s
            WHERE id = %s
        """, (
            name,
            phone,
            session["user_id"]
        ))


        # ==========================================
        # SAVE
        # ==========================================

        db.commit()


        # ==========================================
        # UPDATE SESSION
        # ==========================================

        session["user_name"] = name


        # ==========================================
        # RETURN PROFILE
        # ==========================================

        return redirect(url_for("profile"))


    except mysql.connector.Error as e:

        if db:
            db.rollback()

        print("UPDATE PROFILE DATABASE ERROR:", e)

        return "Unable to update profile.", 500


    except Exception as e:

        if db:
            db.rollback()

        print("UPDATE PROFILE ERROR:", e)

        return "Unable to update profile.", 500


    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()# UPLOAD / CHANGE PROFILE PHOTO
# ============================================================

@app.route("/upload_profile_photo", methods=["POST"])
def upload_profile_photo():

    if "user_id" not in session or session.get("role") != "donor":
        return redirect("/")

    if "profile_photo" not in request.files:
        return redirect(url_for("profile"))

    file = request.files["profile_photo"]

    if file.filename == "":
        return redirect(url_for("profile"))

    allowed_extensions = {"jpg", "jpeg", "png", "webp"}

    original_name = file.filename.lower()

    if "." not in original_name:
        return redirect(url_for("profile"))

    extension = original_name.rsplit(".", 1)[1]

    if extension not in allowed_extensions:
        return redirect(url_for("profile"))

    db = None
    cursor = None

    try:

        upload_folder = os.path.join(
            app.root_path,
            "static",
            "uploads",
            "profile_photos"
        )

        os.makedirs(upload_folder, exist_ok=True)

        user_id = session["user_id"]

        filename = f"donor_{user_id}.{extension}"

        file_path = os.path.join(
            upload_folder,
            filename
        )

        file.save(file_path)

        db = get_db()
        cursor = db.cursor()

        cursor.execute("""
            UPDATE users
            SET profile_photo = %s
            WHERE id = %s
        """, (
            filename,
            user_id
        ))

        db.commit()

        return redirect(url_for("profile"))

    except Exception as e:

        if db:
            db.rollback()

        print("PROFILE PHOTO UPLOAD ERROR:", e)

        return "Unable to upload profile photo.", 500

    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

@app.route('/admin/reports')
def admin_reports():

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))


    conn = get_db()

    cursor = conn.cursor(dictionary=True)


    # -----------------------------
    # DONOR REPORT COUNT
    # -----------------------------

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM donation_reports
        WHERE reporter_type = 'Donor'
        AND status = 'Pending'
    """)

    donor_count = cursor.fetchone()['total']


    # -----------------------------
    # NGO REPORT COUNT
    # -----------------------------

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM donation_reports
        WHERE reporter_type = 'NGO'
        AND status = 'Pending'
    """)

    ngo_count = cursor.fetchone()['total']


    cursor.close()
    conn.close()


    return render_template(
        'admin_reports.html',
        donor_count=donor_count,
        ngo_count=ngo_count
    )


@app.route('/admin/reports/donors')
def admin_donor_reports():

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))

    conn = get_db()

    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            report_id,
            donation_id,
            user_id,
            reason,
            description,
            reported_on,
            status,
            reporter_type
        FROM donation_reports
        WHERE reporter_type = 'Donor'
        AND status = 'Pending'
        ORDER BY reported_on DESC
    """)

    reports = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        'admin_donor_reports.html',
        reports=reports
    )

@app.route('/admin/reports/ngos')
def admin_ngo_reports():

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))

    conn = get_db()

    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            report_id,
            donation_id,
            user_id,
            reason,
            description,
            reported_on,
            status,
            reporter_type
        FROM donation_reports
        WHERE reporter_type = 'NGO'
        AND status = 'Pending'
        ORDER BY reported_on DESC
    """)

    reports = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        'admin_ngo_reports.html',
        reports=reports
    )

@app.route('/admin/report/<int:report_id>')
def admin_view_report(report_id):

    # -----------------------------------
    # ADMIN LOGIN CHECK
    # -----------------------------------

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))


    # -----------------------------------
    # DATABASE CONNECTION
    # -----------------------------------

    conn = get_db()

    cursor = conn.cursor(dictionary=True)


    # -----------------------------------
    # GET REPORT
    # -----------------------------------

    cursor.execute("""
        SELECT
            report_id,
            donation_id,
            user_id,
            reason,
            description,
            reported_on,
            status,
            reporter_type
        FROM donation_reports
        WHERE report_id = %s
    """, (report_id,))

    report = cursor.fetchone()


    if not report:

        cursor.close()
        conn.close()

        flash(
            "Report not found.",
            "error"
        )

        return redirect(
            url_for('admin_reports')
        )


    # -----------------------------------
    # GET DONATION
    # -----------------------------------

    cursor.execute("""
        SELECT
            donation_id,
            user_id,
            food_name,
            food_type,
            quantity,
            quantity_unit,
            address,
            city,
            pickup_date,
            pickup_time,
            contact,
            details,
            status,
            ngo_id
        FROM donations
        WHERE donation_id = %s
    """, (report['donation_id'],))

    donation = cursor.fetchone()


    cursor.close()
    conn.close()


    # -----------------------------------
    # OPEN REPORT DETAILS
    # -----------------------------------

    return render_template(
        'admin_view_report.html',
        report=report,
        donation=donation
    )

@app.route('/admin/cancel_donation/<int:donation_id>', methods=['POST'])
def admin_cancel_donation(donation_id):

    # -----------------------------------
    # ADMIN LOGIN CHECK
    # -----------------------------------

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))

    conn = get_db()

    cursor = conn.cursor(dictionary=True)

    # -----------------------------------
    # CHECK DONATION
    # -----------------------------------

    cursor.execute("""
        SELECT donation_id, ngo_id, status
        FROM donations
        WHERE donation_id = %s
    """, (donation_id,))

    donation = cursor.fetchone()

    if not donation:

        cursor.close()
        conn.close()

        flash("Donation not found.", "error")

        return redirect(url_for('admin_reports'))


    # -----------------------------------
    # CANCEL DONATION
    # -----------------------------------

    cursor.execute("""
        UPDATE donations
        SET
            status = 'Cancelled',
            ngo_id = NULL
        WHERE donation_id = %s
    """, (donation_id,))

    # -----------------------------------
    # MARK RELATED REPORT AS RESOLVED
    # -----------------------------------

    cursor.execute("""
        UPDATE donation_reports
        SET status = 'Resolved'
        WHERE donation_id = %s
        AND status = 'Pending'
    """, (donation_id,))

    conn.commit()

    cursor.close()
    conn.close()


    flash(
        "Donation cancelled successfully. The related report has been resolved.",
        "success"
    )

    return redirect(url_for('admin_reports'))


# =========================================================
# ADMIN ANALYTICS
# =========================================================

@app.route("/admin_reject_donation/<int:donation_id>", methods=["POST"])
def admin_reject_donation(donation_id):

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))

    db = get_db()

    cursor = db.cursor(dictionary=True, buffered=True)

    cursor.execute("""
        SELECT status FROM donations WHERE donation_id = %s
    """, (donation_id,))

    donation = cursor.fetchone()

    if not donation:
        cursor.close()
        db.close()
        return notify_redirect("Donation not found.", "/admin_donations")

    if donation["status"] in ("Completed", "Cancelled", "Rejected"):
        cursor.close()
        db.close()
        return notify_redirect(
            f"This donation is already {donation['status'].lower()}.",
            url_for('admin_donation', donation_id=donation_id)
        )

    cursor.execute("""
        UPDATE donations
        SET status = 'Rejected', ngo_id = NULL
        WHERE donation_id = %s
    """, (donation_id,))

    db.commit()

    cursor.close()
    db.close()

    return notify_redirect(
        f"Donation #{donation_id} was rejected.",
        url_for('admin_donation', donation_id=donation_id),
        kind="success"
    )


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.route("/need_food", methods=["GET", "POST"])
def need_food():
    if request.method == "POST":
        requester_name = request.form.get("requester_name", "").strip()
        phone = request.form.get("phone", "").strip()
        location = request.form.get("location", "").strip()
        people_count = request.form.get("people_count", "").strip()
        request_details = request.form.get("request_details", "").strip()
        if not requester_name or not phone or not location or not people_count:
            return notify_redirect("Please fill all required fields.", "/need_food")

        if not valid_name(requester_name):
            return notify_redirect("Name must contain letters only.", "/need_food")

        if not valid_phone(phone):
            return notify_redirect("Phone number must be exactly 10 digits and start with 6, 7, 8 or 9.", "/need_food")

        if positive_int(people_count, 100000) is None:
            return notify_redirect("Number of people must be a whole number of 1 or more.", "/need_food")

        location = location[:255]
        request_details = request_details[:1000]
        try:
            people_count = int(people_count)
            db = get_db()
            cursor = db.cursor()
            cursor.execute("""INSERT INTO food_requests
                (requester_name, phone, location, people_count, request_details)
                VALUES (%s,%s,%s,%s,%s)""",
                (requester_name, phone, location, people_count, request_details))
            db.commit()
            cursor.close(); db.close()
            return notify_redirect("Food request submitted successfully.", "/need_food", kind="success")
        except Exception as e:
            print("FOOD REQUEST ERROR:", e)
            return notify_redirect("Unable to submit your request right now. Please try again.", "/need_food")
    return render_template("food_request.html")


@app.route("/ngo_dashboard")
def ngo_dashboard_alias():
    return redirect(url_for("ngo_home"))


@app.route("/request_orders")
def request_orders_alias():
    return redirect(url_for("ngo_assigned_donations"))


@app.route("/ngo_profile")
def ngo_profile_alias():
    return redirect(url_for("edit_profile"))


@app.route("/admin_login")
def admin_login_alias():
    return redirect(url_for("admin_login"))


@app.route("/admin_donation")
def admin_donation_alias():
    return redirect(url_for("admin_donations"))


# =========================================================
# ANALYTICS MONTHS
#
# Analytics start in August 2026 (first month of records) and
# run into the future, so upcoming months are always selectable.
# =========================================================

ANALYTICS_START = (2026, 8)
ANALYTICS_FUTURE_MONTHS = 12          # months offered after the current one


def _add_months(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def analytics_month_choices():

    today = date.today()

    end = _add_months(today.year, today.month, ANALYTICS_FUTURE_MONTHS)

    choices = []
    y, m = ANALYTICS_START

    while (y, m) <= end:

        is_future = (y, m) > (today.year, today.month)
        is_current = (y, m) == (today.year, today.month)

        label = datetime(y, m, 1).strftime("%B %Y")

        choices.append({
            "value": f"{y:04d}-{m:02d}",
            "label": label + (" (this month)" if is_current
                              else " (upcoming)" if is_future else ""),
            "future": is_future,
            "current": is_current,
        })

        y, m = _add_months(y, m, 1)

    return choices


def parse_analytics_month(raw):
    """Return (YYYY-MM string, datetime) if the value is an allowed
    month, otherwise ('', None)."""

    raw = (raw or "").strip()

    try:
        parsed = datetime.strptime(raw, "%Y-%m")
    except ValueError:
        return "", None

    allowed = {c["value"] for c in analytics_month_choices()}

    if raw not in allowed:
        return "", None

    return raw, parsed


def analytics_trend(rows):
    """Turn the grouped rows into one entry for every month from
    Aug 2026 to six months ahead, so future months show as empty
    'upcoming' columns instead of vanishing."""

    today = date.today()
    by_month = {r["ym"]: r for r in rows}

    end = _add_months(today.year, today.month, 6)
    y, m = ANALYTICS_START
    out = []

    while (y, m) <= end or len(out) < 12:

        key = f"{y:04d}-{m:02d}"
        row = by_month.get(key)

        out.append({
            "ym": key,
            "label": datetime(y, m, 1).strftime("%b"),
            "year": y,
            "total": int(row["total"]) if row else 0,
            "completed": int(row["completed"] or 0) if row else 0,
            "future": (y, m) > (today.year, today.month),
            "current": (y, m) == (today.year, today.month),
        })

        y, m = _add_months(y, m, 1)

    return out


# =========================================================
# SAMPLING (admin): probability + non-probability methods
# =========================================================

import io
import csv
import sampling

SAMPLING_POPULATIONS = {
    "donations": {
        "label": "Donations",
        "columns": [("id", "ID"), ("label", "Food"), ("food_type", "Type"),
                    ("city", "City"), ("status", "Status")],
        "group_keys": [("status", "Status"), ("food_type", "Food type"),
                       ("city", "City")],
        "cluster_keys": [("city", "City"), ("ngo_id", "Assigned NGO")],
    },
    "donors": {
        "label": "Donors",
        "columns": [("id", "ID"), ("label", "Name"), ("email", "Email"),
                    ("status", "Status"), ("activity", "Activity"),
                    ("city", "Main city")],
        "group_keys": [("activity", "Activity level"), ("status", "Status"),
                       ("city", "Main city")],
        "cluster_keys": [("city", "Main city"), ("activity", "Activity level")],
    },
    "ngos": {
        "label": "NGOs",
        "columns": [("id", "ID"), ("label", "NGO"), ("status", "Status"),
                    ("service_area", "Service area")],
        "group_keys": [("status", "Status"), ("service_area", "Service area")],
        "cluster_keys": [("service_area", "Service area"),
                         ("status", "Status")],
    },
}


def _activity_level(count):
    count = int(count or 0)
    if count == 0:
        return "No donations"
    return "Occasional (1-2)" if count <= 2 else "Regular (3+)"


def load_sampling_population(kind):
    """Return the population as a list of plain dicts (newest first)."""

    db = get_db()
    cur = db.cursor(dictionary=True, buffered=True)
    people = []

    try:

        if kind == "donations":

            cur.execute("""
                SELECT donation_id, food_name, food_type, city, status,
                       ngo_id, user_id
                FROM donations
                ORDER BY donation_id DESC
            """)

            for r in cur.fetchall():
                links = set()
                if r["user_id"] is not None:
                    links.add(f"donor:{r['user_id']}")
                if r["ngo_id"] is not None:
                    links.add(f"ngo:{r['ngo_id']}")
                people.append({
                    "id": r["donation_id"], "label": r["food_name"],
                    "food_type": r["food_type"], "city": r["city"],
                    "status": r["status"],
                    "ngo_id": ("NGO #%s" % r["ngo_id"]) if r["ngo_id"] else "Not assigned",
                    "links": links})

        elif kind == "donors":

            cur.execute("""
                SELECT u.id, u.name, u.email,
                       COALESCE(u.status, 'Active') AS status,
                       (SELECT COUNT(*) FROM donations d
                         WHERE d.user_id = u.id) AS donation_count,
                       (SELECT d.city FROM donations d
                         WHERE d.user_id = u.id AND d.city IS NOT NULL
                         GROUP BY d.city ORDER BY COUNT(*) DESC LIMIT 1) AS city
                FROM users u
                ORDER BY u.id DESC
            """)
            rows = cur.fetchall()

            cur.execute("""
                SELECT DISTINCT user_id, ngo_id FROM donations
                WHERE user_id IS NOT NULL AND ngo_id IS NOT NULL
            """)
            links_by_user = {}
            for e in cur.fetchall():
                links_by_user.setdefault(e["user_id"], set()).add(f"ngo:{e['ngo_id']}")

            for r in rows:
                people.append({
                    "id": r["id"], "label": r["name"], "email": r["email"],
                    "status": r["status"],
                    "activity": _activity_level(r["donation_count"]),
                    "city": r["city"] or "No donations yet",
                    "links": links_by_user.get(r["id"], set())})

        elif kind == "ngos":

            cur.execute("""
                SELECT ngo_id, ngo_name, status, service_area
                FROM ngos
                ORDER BY ngo_id DESC
            """)
            rows = cur.fetchall()

            cur.execute("""
                SELECT DISTINCT user_id, ngo_id FROM donations
                WHERE user_id IS NOT NULL AND ngo_id IS NOT NULL
            """)
            links_by_ngo = {}
            for e in cur.fetchall():
                links_by_ngo.setdefault(e["ngo_id"], set()).add(f"donor:{e['user_id']}")

            for r in rows:
                people.append({
                    "id": r["ngo_id"], "label": r["ngo_name"],
                    "status": r["status"],
                    "service_area": (r["service_area"] or "Unspecified")[:60],
                    "links": links_by_ngo.get(r["ngo_id"], set())})

    finally:
        cur.close()
        db.close()

    return people


def _csv_safe(value):
    """Stop spreadsheet formula injection in exported files."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@app.route("/admin_sampling")
def admin_sampling():

    args = request.args

    kind = args.get("population", "donations")
    if kind not in SAMPLING_POPULATIONS:
        kind = "donations"
    cfg = SAMPLING_POPULATIONS[kind]

    method = args.get("method", "simple_random")
    if method not in sampling.METHODS:
        method = "simple_random"

    form = {
        "population": kind, "method": method,
        "n": args.get("n", "").strip(),
        "seed": args.get("seed", "").strip(),
        "group_key": args.get("group_key", "").strip(),
        "cluster_key": args.get("cluster_key", "").strip(),
        "clusters": args.get("clusters", "").strip(),
        "quotas": args.get("quotas", "").strip()[:300],
        "seeds": args.get("seeds", "").strip()[:100],
    }

    result = None
    error = None
    rows = []
    used_seed = None

    if args.get("run") == "1":

        try:

            valid_group = {k for k, _ in cfg["group_keys"]}
            valid_cluster = {k for k, _ in cfg["cluster_keys"]}

            if form["seed"] and not re.fullmatch(r"\d{1,9}", form["seed"]):
                raise sampling.SamplingError(
                    "Seed must be digits only (up to 9 digits), or left blank.")

            used_seed = int(form["seed"]) if form["seed"] else secrets.randbelow(10 ** 9)

            population = load_sampling_population(kind)

            def need_n():
                n = positive_int(form["n"], sampling.MAX_SAMPLE)
                if n is None:
                    raise sampling.SamplingError(
                        f"Sample size must be a whole number from 1 to {sampling.MAX_SAMPLE} (digits only).")
                return n

            if method == "simple_random":
                result = sampling.simple_random(population, need_n(), used_seed)

            elif method == "systematic":
                result = sampling.systematic(population, need_n(), used_seed)

            elif method == "stratified":
                if form["group_key"] not in valid_group:
                    raise sampling.SamplingError("Choose what to split the population by.")
                result = sampling.stratified(population, need_n(), form["group_key"], used_seed)

            elif method == "cluster":
                if form["cluster_key"] not in valid_cluster:
                    raise sampling.SamplingError("Choose what defines a cluster.")
                count = positive_int(form["clusters"], 1000)
                if count is None:
                    raise sampling.SamplingError(
                        "Number of clusters must be a whole number of 1 or more (digits only).")
                result = sampling.cluster(population, count, form["cluster_key"], used_seed)

            elif method == "convenience":
                result = sampling.convenience(population, need_n())

            elif method == "quota":
                if form["group_key"] not in valid_group:
                    raise sampling.SamplingError("Choose the trait the quotas apply to.")
                result = sampling.quota(population, form["group_key"], form["quotas"])

            elif method == "snowball":
                seeds = []
                if form["seeds"]:
                    if not re.fullmatch(r"\d{1,9}(\s*,\s*\d{1,9}){0,9}", form["seeds"]):
                        raise sampling.SamplingError(
                            "Starting IDs must be numbers separated by commas, for example 4, 12.")
                    seeds = [int(x) for x in form["seeds"].split(",")]
                result = sampling.snowball(population, need_n(), seeds)

            rows = result.sample if result else []

        except sampling.SamplingError as exc:
            error = str(exc)

        except Exception as exc:
            print("SAMPLING ERROR:", exc)
            error = "Could not read the records right now. Please try again."

    # ---------------- CSV download ----------------
    if result and args.get("download") == "1":

        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Method", result.label, "Seed", used_seed,
                         "Population", result.population_size,
                         "Sample", result.sample_size])
        header = [h for _, h in cfg["columns"]]
        if method == "snowball":
            header.append("Wave")
        writer.writerow(header)
        for r in rows:
            line = [_csv_safe(r.get(k)) for k, _ in cfg["columns"]]
            if method == "snowball":
                line.append(r.get("_wave", ""))
            writer.writerow(line)

        response = make_response(out.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="foodresq_{kind}_{method}_sample.csv"')
        return response

    return render_template(
        "admin_sampling.html",
        populations=SAMPLING_POPULATIONS,
        cfg=cfg, form=form, methods=sampling.METHODS,
        result=result, rows=rows[:500], truncated=len(rows) > 500,
        error=error, used_seed=used_seed,
        download_query=request.query_string.decode("utf-8", "ignore"),
        unread_notifications=0)


@app.route("/admin_analytics")
def admin_analytics():

    # -----------------------------------
    # ADMIN LOGIN CHECK
    # -----------------------------------

    if 'admin_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('admin_login'))

    # Optional month filter: YYYY-MM.
    selected_month, selected_month_date = parse_analytics_month(
        request.args.get('month', ''))

    if selected_month_date:
        next_month = (selected_month_date.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_start = selected_month_date.strftime('%Y-%m-01')
        month_end = next_month.strftime('%Y-%m-01')
        month_sql = " AND pickup_date >= %s AND pickup_date < %s "
        month_params = (month_start, month_end)
        selected_month_label = selected_month_date.strftime('%B %Y')
    else:
        month_sql = ''
        month_params = ()
        selected_month_label = 'All Time'

    month_choices = analytics_month_choices()

    db = get_db()

    cursor = db.cursor(dictionary=True, buffered=True)


    def one(sql, params=None, default=0):

        """Run a single-value query. Returns `default` if the
        table or column is missing, so one optional feature
        never breaks the whole page."""

        try:
            cursor.execute(sql, params)
            row = cursor.fetchone()

            if not row:
                return default

            value = list(row.values())[0]

            return default if value is None else value

        except Exception:
            return default


    def many(sql, params=None):

        try:
            cursor.execute(sql, params)
            return cursor.fetchall() or []

        except Exception:
            return []


    # -----------------------------------
    # HEADLINE NUMBERS
    # -----------------------------------

    total_donations = one("SELECT COUNT(*) v FROM donations WHERE 1=1 " + month_sql, month_params)
    total_donors    = one("SELECT COUNT(*) v FROM users")
    total_ngos      = one("SELECT COUNT(*) v FROM ngos")

    active_ngos = one(
        "SELECT COUNT(*) v FROM ngos WHERE status = 'Active'"
    )

    completed = one(
        "SELECT COUNT(*) v FROM donations WHERE status = 'Completed' " + month_sql, month_params
    )

    pending = one(
        "SELECT COUNT(*) v FROM donations WHERE status = 'Pending' " + month_sql, month_params
    )

    cancelled = one(
        "SELECT COUNT(*) v FROM donations WHERE status = 'Cancelled' " + month_sql, month_params
    )

    # Donations that are moving but not finished yet.
    in_progress = max(
        total_donations - completed - pending - cancelled,
        0
    )

    food_rescued = one(
        """
        SELECT SUM(quantity) v
        FROM donations
        WHERE status = 'Completed'
        """ + month_sql, month_params
    )

    if selected_month_date:
        people_served = one(
            """SELECT SUM(dr.people_served) v
               FROM distribution_records dr
               JOIN donations d ON d.donation_id = dr.donation_id
               WHERE d.pickup_date >= %s AND d.pickup_date < %s""", month_params
        )
    else:
        people_served = one("SELECT SUM(people_served) v FROM distribution_records")

    avg_rating = one(
        "SELECT ROUND(AVG(rating), 1) v FROM donations WHERE rating IS NOT NULL " + month_sql,
        month_params,
        default=None
    )

    open_reports = one(
        """
        SELECT COUNT(*) v
        FROM donation_reports
        WHERE status IS NULL OR status != 'Resolved'
        """
    )

    pending_applications = one(
        "SELECT COUNT(*) v FROM ngos WHERE status = 'Pending'"
    )

    completion_rate = (
        round((completed / total_donations) * 100)
        if total_donations else 0
    )


    # -----------------------------------
    # STATUS BREAKDOWN
    # -----------------------------------

    status_rows = many(
        """
        SELECT status, COUNT(*) AS total
        FROM donations
        WHERE 1=1
        """ + month_sql + """
        GROUP BY status
        ORDER BY total DESC
        """, month_params
    )


    # -----------------------------------
    # FOOD TYPE SPLIT
    # -----------------------------------

    food_rows = many(
        """
        SELECT
            COALESCE(NULLIF(food_type, ''), 'Unspecified') AS food_type,
            COUNT(*) AS total
        FROM donations
        WHERE 1=1
        """ + month_sql + """
        GROUP BY food_type
        ORDER BY total DESC
        LIMIT 6
        """, month_params
    )


    # -----------------------------------
    # MONTHLY TREND (LAST 6 MONTHS)
    # -----------------------------------

    monthly_rows = analytics_trend(many(
        """
        SELECT
            DATE_FORMAT(pickup_date, '%Y-%m') AS ym,
            COUNT(*)                          AS total,
            SUM(status = 'Completed')         AS completed
        FROM donations
        WHERE pickup_date IS NOT NULL
          AND pickup_date >= '2026-08-01'
        GROUP BY ym
        ORDER BY ym
        """
    ))


    # -----------------------------------
    # TOP NGOs
    # -----------------------------------

    top_ngos = many(
        """
        SELECT
            n.ngo_name,
            COUNT(d.donation_id)                AS handled,
            SUM(d.status = 'Completed')         AS completed
        FROM donations d
        JOIN ngos n
            ON d.ngo_id = n.ngo_id
        WHERE 1=1
        """ + month_sql.replace('pickup_date', 'd.pickup_date') + """
        GROUP BY n.ngo_id, n.ngo_name
        ORDER BY completed DESC, handled DESC
        LIMIT 5
        """, month_params
    )


    # -----------------------------------
    # TOP DONORS
    # -----------------------------------

    top_donors = many(
        """
        SELECT
            u.name,
            COUNT(d.donation_id) AS donations
        FROM donations d
        JOIN users u
            ON d.user_id = u.id
        WHERE 1=1
        """ + month_sql.replace('pickup_date', 'd.pickup_date') + """
        GROUP BY u.id, u.name
        ORDER BY donations DESC
        LIMIT 5
        """, month_params
    )


    # -----------------------------------
    # TOP CITIES
    # -----------------------------------

    top_cities = many(
        """
        SELECT
            COALESCE(NULLIF(city, ''), 'Unknown') AS city,
            COUNT(*) AS total
        FROM donations
        WHERE 1=1
        """ + month_sql + """
        GROUP BY city
        ORDER BY total DESC
        LIMIT 5
        """, month_params
    )


    cursor.close()
    db.close()


    return render_template(
        "admin_analytics.html",

        total_donations=total_donations,
        total_donors=total_donors,
        total_ngos=total_ngos,
        active_ngos=active_ngos,

        completed=completed,
        pending=pending,
        cancelled=cancelled,
        in_progress=in_progress,

        food_rescued=food_rescued,
        people_served=people_served,
        avg_rating=avg_rating,
        open_reports=open_reports,
        pending_applications=pending_applications,
        completion_rate=completion_rate,

        status_rows=status_rows,
        food_rows=food_rows,
        monthly_rows=monthly_rows,
        top_ngos=top_ngos,
        top_donors=top_donors,
        top_cities=top_cities,
        month_choices=month_choices,
        selected_month=selected_month,
        selected_month_label=selected_month_label,
    )


@app.route("/ngo_analytics")
def ngo_analytics():

    # =====================================================
    # NGO LOGIN CHECK
    # =====================================================

    if 'ngo_id' not in session or session.get('role') != 'ngo':
        return redirect("/ngo_login")

    ngo_id = session["ngo_id"]

    selected_month, selected_month_date = parse_analytics_month(
        request.args.get('month', ''))

    if selected_month_date:
        next_month = (selected_month_date.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_start = selected_month_date.strftime('%Y-%m-01')
        month_end = next_month.strftime('%Y-%m-01')
        ngo_month_params = (ngo_id, month_start, month_end)
        selected_month_label = selected_month_date.strftime('%B %Y')
    else:
        ngo_month_params = (ngo_id,)
        selected_month_label = 'All Time'

    month_choices = analytics_month_choices()

    try:

        # =================================================
        # DATABASE CONNECTION
        # =================================================

        db = get_db()

        cursor = db.cursor(dictionary=True)

        # =================================================
        # 1. BASIC STATISTICS
        # =================================================

        cursor.execute("""
            SELECT
                COUNT(*) AS total_donations,

                SUM(
                    CASE
                        WHEN LOWER(status) IN ('completed', 'complete')
                        THEN 1
                        ELSE 0
                    END
                ) AS completed_donations,

                SUM(
                    CASE
                        WHEN pickup_code_verified = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS verified_pickups,

                COALESCE(SUM(quantity), 0) AS total_quantity,

                COALESCE(AVG(rating), 0) AS average_rating

            FROM donations

            WHERE ngo_id = %s
        """, (ngo_id,))

        stats = cursor.fetchone()

        total_donations = stats["total_donations"] or 0
        completed_donations = stats["completed_donations"] or 0
        verified_pickups = stats["verified_pickups"] or 0
        total_quantity = stats["total_quantity"] or 0

        average_rating = round(
            float(stats["average_rating"] or 0),
            1
        )

        # =================================================
        # COMPLETION RATE
        # =================================================

        if total_donations > 0:

            completion_rate = round(
                (completed_donations / total_donations) * 100
            )

        else:

            completion_rate = 0

        # =================================================
        # VERIFICATION RATE
        # =================================================

        if total_donations > 0:

            verification_rate = round(
                (verified_pickups / total_donations) * 100
            )

        else:

            verification_rate = 0

        # =================================================
        # 2. MONTHLY DONATION DATA
        # =================================================
        #
        # IMPORTANT:
        # MIN(pickup_date) fixes MySQL
        # ONLY_FULL_GROUP_BY error.
        #
        # =================================================

        cursor.execute("""
            SELECT

                DATE_FORMAT(
                    MIN(pickup_date),
                    '%b %Y'
                ) AS month_name,

                COUNT(*) AS donations,

                SUM(
                    CASE
                        WHEN LOWER(status) IN ('completed', 'complete')
                        THEN 1
                        ELSE 0
                    END
                ) AS completed

            FROM donations

            WHERE ngo_id = %s
            AND pickup_date IS NOT NULL
            AND pickup_date >= '2026-08-01'

            GROUP BY
                YEAR(pickup_date),
                MONTH(pickup_date)

            ORDER BY
                YEAR(pickup_date),
                MONTH(pickup_date)

        """, (ngo_id,))

        monthly_data = cursor.fetchall()

        # When a month is selected, replace the main analytics datasets with
        # that month's data. The existing all-time monthly timeline remains available.
        if selected_month_date:
            cursor.execute("""
                SELECT COUNT(*) AS total_donations,
                       SUM(CASE WHEN LOWER(status) IN ('completed','complete') THEN 1 ELSE 0 END) AS completed_donations,
                       SUM(CASE WHEN pickup_code_verified = 1 THEN 1 ELSE 0 END) AS verified_pickups,
                       COALESCE(SUM(quantity),0) AS total_quantity,
                       COALESCE(AVG(rating),0) AS average_rating
                FROM donations
                WHERE ngo_id = %s AND pickup_date >= %s AND pickup_date < %s
            """, ngo_month_params)
            stats = cursor.fetchone()
            total_donations = stats['total_donations'] or 0
            completed_donations = stats['completed_donations'] or 0
            verified_pickups = stats['verified_pickups'] or 0
            total_quantity = stats['total_quantity'] or 0
            average_rating = round(float(stats['average_rating'] or 0), 1)
            completion_rate = round((completed_donations / total_donations) * 100) if total_donations else 0
            verification_rate = round((verified_pickups / total_donations) * 100) if total_donations else 0

            cursor.execute("""SELECT COALESCE(NULLIF(food_type,''),'Other') AS food_type, COUNT(*) AS count
                             FROM donations WHERE ngo_id=%s AND pickup_date >= %s AND pickup_date < %s
                             GROUP BY food_type ORDER BY count DESC""", ngo_month_params)
            food_categories = cursor.fetchall()

            cursor.execute("""SELECT COALESCE(NULLIF(city,''),'Unknown') AS city, COUNT(*) AS count
                             FROM donations WHERE ngo_id=%s AND pickup_date >= %s AND pickup_date < %s
                             GROUP BY city ORDER BY count DESC LIMIT 8""", ngo_month_params)
            locations = cursor.fetchall()

            cursor.execute("""SELECT COUNT(DISTINCT user_id) AS total_donors FROM donations
                             WHERE ngo_id=%s AND user_id IS NOT NULL AND pickup_date >= %s AND pickup_date < %s""", ngo_month_params)
            total_donors = cursor.fetchone()['total_donors'] or 0

            cursor.execute("""SELECT COUNT(*) AS donations,
                                    SUM(CASE WHEN LOWER(status) IN ('completed','complete') THEN 1 ELSE 0 END) AS completed,
                                    DATE_FORMAT(MIN(pickup_date),'%b %Y') AS month_name
                             FROM donations WHERE ngo_id=%s AND pickup_date >= %s AND pickup_date < %s""", ngo_month_params)
            selected_month_row = cursor.fetchone()
            monthly_data = [selected_month_row] if selected_month_row and selected_month_row['donations'] else []

        # =================================================
        # 3. FOOD TYPE DATA
        # =================================================

        cursor.execute("""
            SELECT

                COALESCE(
                    NULLIF(food_type, ''),
                    'Other'
                ) AS food_type,

                COUNT(*) AS count

            FROM donations

            WHERE ngo_id = %s

            GROUP BY food_type

            ORDER BY count DESC

        """, (ngo_id,))

        food_categories = cursor.fetchall()

        # =================================================
        # 4. LOCATION DATA
        # =================================================

        cursor.execute("""
            SELECT

                COALESCE(
                    NULLIF(city, ''),
                    'Unknown'
                ) AS city,

                COUNT(*) AS count

            FROM donations

            WHERE ngo_id = %s

            GROUP BY city

            ORDER BY count DESC

            LIMIT 8

        """, (ngo_id,))

        locations = cursor.fetchall()

        # =================================================
        # 5. DONOR DATA
        # =================================================

        cursor.execute("""
            SELECT

                COUNT(DISTINCT user_id) AS total_donors

            FROM donations

            WHERE ngo_id = %s
            AND user_id IS NOT NULL

        """, (ngo_id,))

        donor_result = cursor.fetchone()

        total_donors = donor_result["total_donors"] or 0

        # =================================================
        # 6. REPEAT DONORS
        # =================================================

        cursor.execute("""
            SELECT COUNT(*) AS repeat_donors

            FROM (

                SELECT
                    user_id

                FROM donations

                WHERE ngo_id = %s
                AND user_id IS NOT NULL

                GROUP BY user_id

                HAVING COUNT(*) > 1

            ) AS repeat_table

        """, (ngo_id,))

        repeat_result = cursor.fetchone()

        repeat_donors = repeat_result["repeat_donors"] or 0

        # =================================================
        # DONOR PERCENTAGES
        # =================================================

        if total_donors > 0:

            repeat_percentage = round(
                (repeat_donors / total_donors) * 100
            )

        else:

            repeat_percentage = 0

        new_percentage = 100 - repeat_percentage

        # =================================================
        # 7. PICKUP HOUR DATA
        # =================================================

        cursor.execute("""
            SELECT

                HOUR(pickup_time) AS pickup_hour,

                COUNT(*) AS count

            FROM donations

            WHERE ngo_id = %s
            AND pickup_time IS NOT NULL

            GROUP BY HOUR(pickup_time)

            ORDER BY HOUR(pickup_time)

        """, (ngo_id,))

        hourly_data = cursor.fetchall()

        # =================================================
        # 8. PEAK PICKUP HOUR
        # =================================================

        peak_hour = None

        if hourly_data:

            peak_record = max(
                hourly_data,
                key=lambda x: x["count"]
            )

            peak_hour = peak_record["pickup_hour"]

        # =================================================
        # 9. PICKUP PERFORMANCE
        # =================================================

        cursor.execute("""
            SELECT

                AVG(
                    CASE
                        WHEN pickup_marked_at IS NOT NULL
                        AND on_the_way_at IS NOT NULL
                        THEN TIMESTAMPDIFF(
                            MINUTE,
                            pickup_marked_at,
                            on_the_way_at
                        )
                    END
                ) AS avg_response_minutes,

                AVG(
                    CASE
                        WHEN pickup_marked_at IS NOT NULL
                        AND pickup_confirmed_at IS NOT NULL
                        THEN TIMESTAMPDIFF(
                            MINUTE,
                            pickup_marked_at,
                            pickup_confirmed_at
                        )
                    END
                ) AS avg_pickup_minutes,

                AVG(
                    CASE
                        WHEN pickup_confirmed_at IS NOT NULL
                        AND completed_at IS NOT NULL
                        THEN TIMESTAMPDIFF(
                            MINUTE,
                            pickup_confirmed_at,
                            completed_at
                        )
                    END
                ) AS avg_completion_minutes

            FROM donations

            WHERE ngo_id = %s

        """, (ngo_id,))

        timing_data = cursor.fetchone()

        avg_response_minutes = timing_data[
            "avg_response_minutes"
        ]

        avg_pickup_minutes = timing_data[
            "avg_pickup_minutes"
        ]

        avg_completion_minutes = timing_data[
            "avg_completion_minutes"
        ]

        if avg_response_minutes is not None:

            avg_response_minutes = round(
                float(avg_response_minutes)
            )

        if avg_pickup_minutes is not None:

            avg_pickup_minutes = round(
                float(avg_pickup_minutes)
            )

        if avg_completion_minutes is not None:

            avg_completion_minutes = round(
                float(avg_completion_minutes)
            )

        # =================================================
        # 10. DONOR CONFIRMATION
        # =================================================

        cursor.execute("""
            SELECT

                SUM(
                    CASE
                        WHEN LOWER(donor_confirmation) = 'confirmed'
                        THEN 1
                        ELSE 0
                    END
                ) AS confirmed,

                SUM(
                    CASE
                        WHEN LOWER(donor_confirmation) = 'rejected'
                        THEN 1
                        ELSE 0
                    END
                ) AS rejected

            FROM donations

            WHERE ngo_id = %s

        """, (ngo_id,))

        confirmation_data = cursor.fetchone()

        donor_confirmed = (
            confirmation_data["confirmed"] or 0
        )

        donor_rejected = (
            confirmation_data["rejected"] or 0
        )

        # =================================================
        # 11. PICKUP CODE DATA
        # =================================================

        cursor.execute("""
            SELECT

                COUNT(
                    CASE
                        WHEN pickup_code IS NOT NULL
                        AND pickup_code != ''
                        THEN 1
                    END
                ) AS codes_created,

                SUM(
                    CASE
                        WHEN pickup_code_verified = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS verified

            FROM donations

            WHERE ngo_id = %s

        """, (ngo_id,))

        code_data = cursor.fetchone()

        pickup_codes_created = (
            code_data["codes_created"] or 0
        )

        pickup_codes_verified = (
            code_data["verified"] or 0
        )

        # =================================================
        # 12. TOP FOOD
        # =================================================

        if food_categories:

            top_food = food_categories[0]["food_type"]

        else:

            top_food = None

        # =================================================
        # 13. TOP LOCATION
        # =================================================

        if locations:

            top_location = locations[0]["city"]

        else:

            top_location = None

        # =================================================
        # 14. MOST ACTIVE MONTH
        # =================================================

        if monthly_data:

            best_month = max(
                monthly_data,
                key=lambda x: x["donations"]
            )

            most_active_month = best_month["month_name"]

        else:

            most_active_month = None

        # =================================================
        # 15. APPLY SELECTED MONTH TO ALL NGO ANALYTICS
        # =================================================
        # The page supports an optional YYYY-MM filter.  The original
        # analytics queries are also kept for All Time; when a month is
        # selected, every donation-based metric is recalculated here so
        # later all-time queries cannot overwrite the selected-month data.
        if selected_month_date:
            month_where = "ngo_id = %s AND pickup_date >= %s AND pickup_date < %s"

            cursor.execute(f"""
                SELECT COUNT(*) AS total_donations,
                       SUM(CASE WHEN LOWER(status) IN ('completed','complete') THEN 1 ELSE 0 END) AS completed_donations,
                       SUM(CASE WHEN pickup_code_verified = 1 THEN 1 ELSE 0 END) AS verified_pickups,
                       COALESCE(SUM(quantity), 0) AS total_quantity,
                       COALESCE(AVG(rating), 0) AS average_rating
                FROM donations
                WHERE {month_where}
            """, ngo_month_params)
            stats = cursor.fetchone()
            total_donations = stats['total_donations'] or 0
            completed_donations = stats['completed_donations'] or 0
            verified_pickups = stats['verified_pickups'] or 0
            total_quantity = stats['total_quantity'] or 0
            average_rating = round(float(stats['average_rating'] or 0), 1)
            completion_rate = round((completed_donations / total_donations) * 100) if total_donations else 0
            verification_rate = round((verified_pickups / total_donations) * 100) if total_donations else 0

            cursor.execute(f"""
                SELECT COALESCE(NULLIF(food_type,''),'Other') AS food_type, COUNT(*) AS count
                FROM donations
                WHERE {month_where}
                GROUP BY food_type
                ORDER BY count DESC
            """, ngo_month_params)
            food_categories = cursor.fetchall()

            cursor.execute(f"""
                SELECT COALESCE(NULLIF(city,''),'Unknown') AS city, COUNT(*) AS count
                FROM donations
                WHERE {month_where}
                GROUP BY city
                ORDER BY count DESC
                LIMIT 8
            """, ngo_month_params)
            locations = cursor.fetchall()

            cursor.execute(f"""
                SELECT COUNT(DISTINCT user_id) AS total_donors
                FROM donations
                WHERE {month_where} AND user_id IS NOT NULL
            """, ngo_month_params)
            total_donors = cursor.fetchone()['total_donors'] or 0

            cursor.execute(f"""
                SELECT COUNT(*) AS repeat_donors
                FROM (
                    SELECT user_id
                    FROM donations
                    WHERE {month_where} AND user_id IS NOT NULL
                    GROUP BY user_id
                    HAVING COUNT(*) > 1
                ) AS repeat_table
            """, ngo_month_params)
            repeat_donors = cursor.fetchone()['repeat_donors'] or 0
            repeat_percentage = round((repeat_donors / total_donors) * 100) if total_donors else 0
            new_percentage = 100 - repeat_percentage

            cursor.execute(f"""
                SELECT HOUR(pickup_time) AS pickup_hour, COUNT(*) AS count
                FROM donations
                WHERE {month_where} AND pickup_time IS NOT NULL
                GROUP BY HOUR(pickup_time)
                ORDER BY HOUR(pickup_time)
            """, ngo_month_params)
            hourly_data = cursor.fetchall()
            peak_hour = max(hourly_data, key=lambda x: x['count'])['pickup_hour'] if hourly_data else None

            cursor.execute(f"""
                SELECT
                    AVG(CASE WHEN pickup_marked_at IS NOT NULL AND on_the_way_at IS NOT NULL
                        THEN TIMESTAMPDIFF(MINUTE, pickup_marked_at, on_the_way_at) END) AS avg_response_minutes,
                    AVG(CASE WHEN pickup_marked_at IS NOT NULL AND pickup_confirmed_at IS NOT NULL
                        THEN TIMESTAMPDIFF(MINUTE, pickup_marked_at, pickup_confirmed_at) END) AS avg_pickup_minutes,
                    AVG(CASE WHEN pickup_confirmed_at IS NOT NULL AND completed_at IS NOT NULL
                        THEN TIMESTAMPDIFF(MINUTE, pickup_confirmed_at, completed_at) END) AS avg_completion_minutes
                FROM donations
                WHERE {month_where}
            """, ngo_month_params)
            timing_data = cursor.fetchone()
            avg_response_minutes = round(float(timing_data['avg_response_minutes'])) if timing_data['avg_response_minutes'] is not None else None
            avg_pickup_minutes = round(float(timing_data['avg_pickup_minutes'])) if timing_data['avg_pickup_minutes'] is not None else None
            avg_completion_minutes = round(float(timing_data['avg_completion_minutes'])) if timing_data['avg_completion_minutes'] is not None else None

            cursor.execute(f"""
                SELECT
                    SUM(CASE WHEN LOWER(donor_confirmation) = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
                    SUM(CASE WHEN LOWER(donor_confirmation) = 'rejected' THEN 1 ELSE 0 END) AS rejected
                FROM donations
                WHERE {month_where}
            """, ngo_month_params)
            confirmation_data = cursor.fetchone()
            donor_confirmed = confirmation_data['confirmed'] or 0
            donor_rejected = confirmation_data['rejected'] or 0

            cursor.execute(f"""
                SELECT
                    COUNT(CASE WHEN pickup_code IS NOT NULL AND pickup_code != '' THEN 1 END) AS codes_created,
                    SUM(CASE WHEN pickup_code_verified = 1 THEN 1 ELSE 0 END) AS verified
                FROM donations
                WHERE {month_where}
            """, ngo_month_params)
            code_data = cursor.fetchone()
            pickup_codes_created = code_data['codes_created'] or 0
            pickup_codes_verified = code_data['verified'] or 0

            top_food = food_categories[0]['food_type'] if food_categories else None
            top_location = locations[0]['city'] if locations else None

            if total_donations:
                monthly_data = [{
                    'month_name': selected_month_date.strftime('%b %Y'),
                    'donations': total_donations,
                    'completed': completed_donations
                }]
                most_active_month = selected_month_date.strftime('%b %Y')
            else:
                monthly_data = []
                most_active_month = None

        # =================================================
        # 16. CLOSE DATABASE
        # =================================================

        cursor.close()
        db.close()

        # =================================================
        # 17. RENDER PAGE
        # =================================================

        return render_template(
            "ngo_analytics.html",

            total_donations=total_donations,

            completed_donations=completed_donations,

            verified_pickups=verified_pickups,

            total_quantity=total_quantity,

            average_rating=average_rating,

            completion_rate=completion_rate,

            verification_rate=verification_rate,

            monthly_data=monthly_data,

            food_categories=food_categories,

            locations=locations,

            total_donors=total_donors,

            repeat_donors=repeat_donors,

            repeat_percentage=repeat_percentage,

            new_percentage=new_percentage,

            hourly_data=hourly_data,

            peak_hour=peak_hour,

            avg_response_minutes=avg_response_minutes,

            avg_pickup_minutes=avg_pickup_minutes,

            avg_completion_minutes=avg_completion_minutes,

            donor_confirmed=donor_confirmed,

            donor_rejected=donor_rejected,

            pickup_codes_created=pickup_codes_created,

            pickup_codes_verified=pickup_codes_verified,

            top_food=top_food,

            top_location=top_location,

            most_active_month=most_active_month,
            month_choices=month_choices,
            selected_month=selected_month,
            selected_month_label=selected_month_label
        )

    except Exception as e:

        print("\n======================================")
        print("NGO ANALYTICS ERROR:")
        print(e)
        print("======================================\n")

        return f"""
        <div style="
            font-family: Arial;
            padding: 40px;
            color: #333;
        ">

            <h2>NGO Analytics Error</h2>

            <p>
                The analytics page could not be loaded.
            </p>

            <pre style="
                background: #f5f5f5;
                padding: 20px;
                border-radius: 8px;
                color: #b00020;
                white-space: pre-wrap;
            ">{e}</pre>

        </div>
        """, 500


@app.route("/ngo_record_distribution/<int:donation_id>", methods=["GET", "POST"])
def ngo_record_distribution(donation_id):

    # =====================================================
    # NGO LOGIN CHECK
    # =====================================================

    if "ngo_id" not in session or session.get("role") != "ngo":
        return redirect(url_for("ngo_login"))

    ngo_id = session["ngo_id"]

    db = None
    cursor = None

    try:

        # =====================================================
        # DATABASE CONNECTION
        # =====================================================

        db = get_db()
        cursor = db.cursor(dictionary=True)

        # =====================================================
        # GET DONATION
        # =====================================================

        cursor.execute("""
            SELECT
                donation_id,
                user_id,
                food_name,
                food_type,
                quantity,
                quantity_unit,
                address,
                city,
                pickup_date,
                pickup_time,
                status,
                ngo_id
            FROM donations
            WHERE donation_id = %s
              AND ngo_id = %s
            LIMIT 1
        """, (
            donation_id,
            ngo_id
        ))

        donation = cursor.fetchone()

        # =====================================================
        # DONATION NOT FOUND
        # =====================================================

        if not donation:

            return """
                <h2>Donation not found.</h2>
                <p>This donation is not assigned to your NGO.</p>

                <a href="/ngo_assigned_donations">
                    Back to Assigned Donations
                </a>
            """, 404

        # =====================================================
        # ALREADY COMPLETED
        # =====================================================

        if donation["status"] in ("Completed", "complete"):

            return render_template(
                "ngo_distribution.html",
                donation=donation,
                success=False,
                already_completed=True
            )

        # =====================================================
        # DISTRIBUTION ONLY AFTER ON THE WAY
        # =====================================================

        if donation["status"] != "On the Way":

            flash(
                "Distribution can only be recorded after the donation is On the Way.",
                "error"
            )

            return redirect(
                url_for(
                    "ngo_donations",
                    donation_id=donation_id
                )
            )

        # =====================================================
        # POST - RECORD DISTRIBUTION
        # =====================================================

        if request.method == "POST":

            recipient_type = request.form.get(
                "recipient_type",
                "People in need"
            ).strip()

            people_served_text = request.form.get(
                "people_served",
                "0"
            ).strip()

            quantity_distributed_text = request.form.get(
                "quantity_distributed",
                "0"
            ).strip()

            distribution_location = request.form.get(
                "distribution_location",
                ""
            ).strip()

            notes = request.form.get(
                "notes",
                ""
            ).strip()

            confirmation = request.form.get(
                "confirmation"
            )

            # =================================================
            # REQUIRED LOCATION
            # =================================================

            if not distribution_location:

                return render_template(
                    "ngo_distribution.html",
                    donation=donation,
                    success=False,
                    already_completed=False,
                    error="Please enter where the food was distributed."
                )

            # =================================================
            # REQUIRED CONFIRMATION
            # =================================================

            if not confirmation:

                return render_template(
                    "ngo_distribution.html",
                    donation=donation,
                    success=False,
                    already_completed=False,
                    error="Please confirm the distribution."
                )

            # =================================================
            # NUMBER VALIDATION
            # =================================================

            try:

                people_served = int(
                    people_served_text or 0
                )

                quantity_distributed = int(
                    quantity_distributed_text or 0
                )

            except ValueError:

                return render_template(
                    "ngo_distribution.html",
                    donation=donation,
                    success=False,
                    already_completed=False,
                    error="People served and quantity must be valid numbers."
                )

            # =================================================
            # PEOPLE CANNOT BE NEGATIVE
            # =================================================

            if people_served < 0:

                return render_template(
                    "ngo_distribution.html",
                    donation=donation,
                    success=False,
                    already_completed=False,
                    error="People served cannot be negative."
                )

            # =================================================
            # QUANTITY MUST BE POSITIVE
            # =================================================

            if quantity_distributed <= 0:

                return render_template(
                    "ngo_distribution.html",
                    donation=donation,
                    success=False,
                    already_completed=False,
                    error="Please enter the quantity distributed."
                )

            # =================================================
            # CHECK WHETHER DISTRIBUTION RECORD ALREADY EXISTS
            # =================================================

            cursor.execute("""
                SELECT distribution_id
                FROM distribution_records
                WHERE donation_id = %s
                LIMIT 1
            """, (
                donation_id,
            ))

            existing_record = cursor.fetchone()

            if existing_record:

                return render_template(
                    "ngo_distribution.html",
                    donation=donation,
                    success=False,
                    already_completed=True
                )

            # =================================================
            # INSERT DISTRIBUTION RECORD
            # =================================================

            cursor.execute("""
                INSERT INTO distribution_records (
                    donation_id,
                    ngo_id,
                    recipient_type,
                    people_served,
                    distribution_location,
                    quantity_distributed,
                    quantity_unit,
                    notes,
                    proof_method
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
            """, (
                donation_id,
                ngo_id,
                recipient_type or "People in need",
                people_served,
                distribution_location,
                quantity_distributed,
                donation["quantity_unit"],
                notes,
                "NGO Confirmation"
            ))

            # =================================================
            # MARK DONATION AS COMPLETED
            # =================================================

            cursor.execute("""
                UPDATE donations
                SET
                    status = 'Completed',
                    completed_at = NOW()
                WHERE donation_id = %s
                  AND ngo_id = %s
                  AND status = 'On the Way'
            """, (
                donation_id,
                ngo_id
            ))

            # =================================================
            # MAKE SURE DONATION WAS UPDATED
            # =================================================

            if cursor.rowcount == 0:

                db.rollback()

                return render_template(
                    "ngo_distribution.html",
                    donation=donation,
                    success=False,
                    already_completed=False,
                    error="The donation status changed before completion. Please try again."
                )

            # =================================================
            # DONOR COMPLETION NOTIFICATION
            # =================================================

            if donation["user_id"] is not None:

                cursor.execute("""
                    INSERT INTO notifications
                    (
                        user_id,
                        ngo_id,
                        donation_id,
                        message
                    )
                    VALUES
                    (
                        %s,
                        NULL,
                        %s,
                        %s
                    )
                """, (
                    donation["user_id"],
                    donation_id,
                    (
                        "🎉 Your donation #"
                        + str(donation_id)
                        + " ("
                        + str(donation["food_name"])
                        + ") has been completed successfully! "
                        "The rescued food has been distributed to people in need. "
                        "Thank you for helping turn surplus into sustenance."
                    )
                ))

                print(
                    "DONOR COMPLETION NOTIFICATION CREATED:",
                    donation["user_id"],
                    donation_id
                )

            # =================================================
            # SAVE DISTRIBUTION + COMPLETED STATUS
            # + DONOR NOTIFICATION TOGETHER
            # =================================================

            db.commit()

            # =================================================
            # UPDATE DONATION OBJECT FOR SUCCESS PAGE
            # =================================================

            donation["status"] = "Completed"

            return render_template(
                "ngo_distribution.html",
                donation=donation,
                success=True,
                already_completed=False
            )

        # =====================================================
        # GET DISTRIBUTION PAGE
        # =====================================================

        return render_template(
            "ngo_distribution.html",
            donation=donation,
            success=False,
            already_completed=False
        )

    # =========================================================
    # MYSQL ERROR
    # =========================================================

    except mysql.connector.Error as e:

        if db:
            db.rollback()

        print("====================================")
        print("DISTRIBUTION DATABASE ERROR:")
        print(e)
        print("====================================")

        return """
            <h2>Unable to record distribution.</h2>
            <p>Database error occurred.</p>

            <a href="/ngo_assigned_donations">
                Back to Assigned Donations
            </a>
        """, 500

    # =========================================================
    # GENERAL ERROR
    # =========================================================

    except Exception as e:

        if db:
            db.rollback()

        print("====================================")
        print("DISTRIBUTION ERROR:")
        print(e)
        print("====================================")

        return """
            <h2>Unable to record distribution.</h2>
            <p>Please check the Flask terminal for the exact error.</p>

            <a href="/ngo_assigned_donations">
                Back to Assigned Donations
            </a>
        """, 500

    # =========================================================
    # CLEANUP
    # =========================================================

    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

@app.route("/notifications")
def notifications():

    # ==========================================
    # LOGIN CHECK
    # ==========================================

    if 'ngo_id' not in session and 'user_id' not in session:
        return redirect("/")

    db = None
    cursor = None

    try:

        # ==========================================
        # DATABASE CONNECTION
        # ==========================================

        db = get_db()

        cursor = db.cursor(dictionary=True)

        # ==========================================
        # NGO NOTIFICATIONS
        # ==========================================

        if session.get("role") == "ngo":

            ngo_id = session["ngo_id"]

            # Get notifications
            cursor.execute("""
                SELECT
                    notification_id,
                    user_id,
                    ngo_id,
                    donation_id,
                    message,
                    is_read,
                    created_at
                FROM notifications
                WHERE ngo_id = %s
                ORDER BY created_at DESC
            """, (ngo_id,))

            notifications = cursor.fetchall()

            # Get unread count
            cursor.execute("""
                SELECT COUNT(*) AS unread_count
                FROM notifications
                WHERE ngo_id = %s
                AND is_read = 0
            """, (ngo_id,))

            unread_notifications = cursor.fetchone()["unread_count"]

            # Mark notifications as read
            cursor.execute("""
                UPDATE notifications
                SET is_read = 1
                WHERE ngo_id = %s
            """, (ngo_id,))

        # ==========================================
        # USER / DONOR NOTIFICATIONS
        # ==========================================

        else:

            user_id = session["user_id"]

            # Get notifications
            cursor.execute("""
                SELECT
                    notification_id,
                    user_id,
                    ngo_id,
                    donation_id,
                    message,
                    is_read,
                    created_at
                FROM notifications
                WHERE user_id = %s
                ORDER BY created_at DESC
            """, (user_id,))

            notifications = cursor.fetchall()

            # Get unread count
            cursor.execute("""
                SELECT COUNT(*) AS unread_count
                FROM notifications
                WHERE user_id = %s
                AND is_read = 0
            """, (user_id,))

            unread_notifications = cursor.fetchone()["unread_count"]

            # Mark notifications as read
            cursor.execute("""
                UPDATE notifications
                SET is_read = 1
                WHERE user_id = %s
            """, (user_id,))

        # ==========================================
        # SAVE
        # ==========================================

        db.commit()

        # ==========================================
        # SEND TO HTML
        # ==========================================

        return render_template(
            "notifications.html",
            notifications=notifications,
            unread_notifications=unread_notifications
        )

    except mysql.connector.Error as e:

        if db:
            db.rollback()

        print("NOTIFICATION DATABASE ERROR:", e)

        return "Unable to load notifications.", 500

    except Exception as e:

        if db:
            db.rollback()

        print("NOTIFICATION ERROR:", e)

        return "Unable to load notifications.", 500

    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()
    
# ==========================================
# CLEAR NOTIFICATIONS
# ==========================================

@app.route("/clear_notifications", methods=["POST"])
def clear_notifications():

    # ==========================================
    # SAME LOGIN CHECK AS NOTIFICATIONS ROUTE
    # ==========================================

    if 'ngo_id' not in session and 'user_id' not in session:
        return redirect("/")

    db = None
    cursor = None

    try:

        # ==========================================
        # DATABASE
        # ==========================================

        db = get_db()
        cursor = db.cursor()

        # ==========================================
        # NGO
        # ==========================================

        if session.get("role") == "ngo":

            cursor.execute("""
                DELETE FROM notifications
                WHERE ngo_id = %s
            """, (session["ngo_id"],))

        # ==========================================
        # USER / DONOR
        # ==========================================

        else:

            cursor.execute("""
                DELETE FROM notifications
                WHERE user_id = %s
            """, (session["user_id"],))

        # ==========================================
        # SAVE
        # ==========================================

        db.commit()

        return redirect(url_for("notifications"))

    except mysql.connector.Error as e:

        if db:
            db.rollback()

        print("CLEAR NOTIFICATIONS DATABASE ERROR:", e)

        return "Unable to clear notifications.", 500

    except Exception as e:

        if db:
            db.rollback()

        print("CLEAR NOTIFICATIONS ERROR:", e)

        return "Unable to clear notifications.", 500

    finally:

        if cursor:
            cursor.close()

        if db:
            db.close()

if __name__ == "__main__":
    app.run(
        host=env("HOST", "127.0.0.1"),
        port=env("PORT", 5000, int),
        debug=(not IS_PRODUCTION and env("FLASK_DEBUG", "0") == "1"),
    )