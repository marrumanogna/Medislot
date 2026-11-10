import hmac
import os
import re
import secrets
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, session, url_for,
)
from flask_session import Session

load_dotenv()

import api  # noqa: E402  (needs env loaded first)

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ["FLASK_SECRET_KEY"]
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
app.config["SESSION_FILE_DIR"] = os.path.join(os.path.dirname(__file__), "flask_session")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Set SESSION_COOKIE_SECURE=1 in .env once the site is served over HTTPS.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE") == "1"
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # profile photos are capped at 5 MB

Session(app)

ROLES = ("doctor", "patient")
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO_BYTES = 5 * 1024 * 1024

# Which API paths each portal may reach through /api/<path>.
PROXY_ALLOWED = {
    "DOCTOR": ("doctor/",),
    "PATIENT": ("patient/", "patients/", "doctors", "appointments", "slots", "waitlist-offers"),
}
SAFE_PATH = re.compile(r"^[A-Za-z0-9_\-/]+$")


# --------------------------------------------------------------------------- #
# Session, CSRF and role guards
# --------------------------------------------------------------------------- #
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def verify_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        if request.path.startswith(("/api/", "/register/")):
            return jsonify(message="Your session expired. Refresh the page and try again."), 400
        flash("Your session expired. Please try again.", "error")
        return redirect(url_for("login"))


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def current_role():
    if "access_token" in session and session.get("role") in ("DOCTOR", "PATIENT"):
        return session["role"].lower()
    return None


def portal_required(role):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            actual = current_role()
            if actual is None:
                return redirect(url_for("login", role=role, next=request.path))
            if actual != role:
                # A patient session may never see doctor pages, and vice versa.
                flash(f"That area is for {role}s. You're signed in as a {actual}.", "error")
                return redirect(url_for("landing"))
            return view(*args, **kwargs)
        return wrapper
    return decorator


# --------------------------------------------------------------------------- #
# Public pages
# --------------------------------------------------------------------------- #
@app.route("/")
def landing():
    return render_template("landing.html", role=current_role())


@app.route("/login", methods=["GET", "POST"])
def login():
    role = request.values.get("role", "patient").lower()
    role = role if role in ROLES else "patient"

    if request.method == "GET":
        if current_role():
            return redirect(url_for(f"{current_role()}_home"))
        return render_template(
            "login.html", role=role, email="",
            registered=request.args.get("registered") == "1",
            expired=request.args.get("expired") == "1",
        )

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    def fail(message, status=401):
        return render_template("login.html", role=role, email=email,
                               error=message), status

    if not email or not password:
        return fail("Enter your email and password.", 400)

    status, data = api.raw("POST", "auth/login", json={"email": email, "password": password})

    if status != 200 or "accessToken" not in data:
        if status == 502:
            return fail(data["message"], 502)
        return fail(data.get("message", "Invalid email or password."), status if status < 500 else 502)

    actual = str(data.get("role", "")).lower()
    if actual != role:
        # Valid credentials for the other role: do not keep the tokens we just minted.
        api.raw("POST", "auth/logout", token=data["accessToken"],
                json={"refreshToken": data.get("refreshToken")})
        return fail(f"These credentials belong to a {actual} account. "
                    f"Switch to {actual.title()} above to sign in.", 403)

    session.clear()
    api.store_tokens(session, data)
    session["user_id"] = data["userId"]
    session["role"] = data["role"]
    session["email"] = email.lower()
    csrf_token()

    dest = request.args.get("next", "")
    if not (dest.startswith(f"/{role}") and not dest.startswith("//")):
        dest = url_for(f"{role}_home")
    return redirect(dest)


@app.route("/register")
def register():
    if current_role():
        return redirect(url_for(f"{current_role()}_home"))
    role = request.args.get("role", "").lower()
    return render_template("register.html", role=role if role in ROLES else "")


@app.route("/register/<role>", methods=["POST"])
def register_submit(role):
    """Pass the registration JSON to the platform's register API unchanged."""
    if role not in ROLES:
        abort(404)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(message="Invalid request."), 400
    status, data = api.raw("POST", f"auth/register/{role}", json=payload)
    return jsonify(data), status


# Compatibility with the original routes.
@app.route("/doctor/login")
def doctor_login_legacy():
    return redirect(url_for("login", role="doctor"))


@app.route("/patient/login")
def patient_login_legacy():
    return redirect(url_for("login", role="patient"))


@app.route("/logout", methods=["POST"])
def logout():
    if current_role():
        api.call(session, "POST", "auth/logout",
                 json={"refreshToken": session.get("refresh_token")})
    session.clear()
    return redirect(url_for("landing"))


# --------------------------------------------------------------------------- #
# Doctor portal
# --------------------------------------------------------------------------- #
DOCTOR_PAGES = {
    "doctor_home": ("/doctor", "overview", "Overview"),
    "doctor_profile": ("/doctor/profile", "profile", "My profile"),
    "doctor_consultations": ("/doctor/consultations", "consultations", "Consultation types"),
    "doctor_availability": ("/doctor/availability", "availability", "Availability"),
    "doctor_blackouts": ("/doctor/blackouts", "blackouts", "Blackout dates"),
    "doctor_schedule": ("/doctor/schedule", "schedule", "Schedule"),
    "doctor_appointments": ("/doctor/appointments", "appointments", "Appointments"),
    "doctor_settings": ("/doctor/settings", "settings", "Settings"),
}


def _register_portal_pages(role, pages):
    for endpoint, (rule, page, title) in pages.items():
        def make(page=page, title=title):
            @portal_required(role)
            def view():
                return render_template(f"{role}/{page}.html", page=page, title=title,
                                       email=session.get("email", ""))
            return view
        app.add_url_rule(rule, endpoint, make())


PATIENT_PAGES = {
    "patient_home": ("/patient", "overview", "Overview"),
    "patient_doctors": ("/patient/doctors", "doctors", "Find a doctor"),
    "patient_appointments": ("/patient/appointments", "appointments", "My appointments"),
    "patient_profile": ("/patient/profile", "profile", "My profile"),
}

_register_portal_pages("doctor", DOCTOR_PAGES)
_register_portal_pages("patient", PATIENT_PAGES)


@app.route("/doctor/dashboard")
def doctor_dashboard_legacy():
    return redirect(url_for("doctor_home"))


# --------------------------------------------------------------------------- #
# Authenticated API proxy used by the dashboards' JavaScript
# --------------------------------------------------------------------------- #
@app.route("/api/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def api_proxy(path):
    role = session.get("role")
    if "access_token" not in session or role not in PROXY_ALLOWED:
        return jsonify(message="Please sign in again.", sessionExpired=True), 401

    path = path.strip("/")
    if not SAFE_PATH.match(path) or ".." in path or not path.startswith(PROXY_ALLOWED[role]):
        return jsonify(message="Not allowed."), 403

    kwargs = {"params": request.args.to_dict(flat=True) or None}
    if request.method in ("POST", "PUT", "PATCH"):
        body = request.get_json(silent=True)
        kwargs["json"] = body if body is not None else {}
    if request.headers.get("Idempotency-Key"):
        kwargs["headers"] = {"Idempotency-Key": request.headers["Idempotency-Key"]}

    status, data = api.call(session, request.method, path, **kwargs)

    if status == 401:
        session.clear()
        return jsonify(message="Your session has ended. Please sign in again.", sessionExpired=True), 401
    return jsonify(data), status
@app.route("/doctor-photo", methods=["GET"])
@portal_required("doctor")
def doctor_photo_view():
    status, data = api.call(session, "GET", "doctor/profile")
    app.logger.info(
    "PHOTO DEBUG: userId=%s photo_key=%s photo_url_present=%s",
    data.get("userId"),
    data.get("photo_key"),
    bool(data.get("photo_url"))
)

    if status != 200:
        return jsonify(
            message=data.get("message", "Could not load profile photo.")
        ), status

    photo_url = data.get("photo_url")

    if not photo_url:
        app.logger.warning(
            "Doctor photo unavailable: photo_key_present=%s, bucket_configured=%s",
            bool(data.get("photo_key")),
            bool(os.environ.get("DOCTOR_PHOTOS_BUCKET"))
        )
        return jsonify(message="No profile photo found."), 404

    return jsonify(photo_url=photo_url), 200
@app.route("/doctor-photo", methods=["POST"])
@portal_required("doctor")
def doctor_photo():
    """Upload a profile photo: presigned URL -> S3 -> save photo_key on the profile.

    Done server-side so the browser never needs S3 CORS access.
    """
    file = request.files.get("photo")
    if not file:
        return jsonify(message="Choose a photo to upload."), 400
    content_type = file.mimetype
    blob = file.read()
    if content_type not in PHOTO_TYPES:
        return jsonify(message="Photos must be JPEG, PNG or WebP."), 400
    if not 0 < len(blob) <= MAX_PHOTO_BYTES:
        return jsonify(message="Photos must be 5 MB or smaller."), 400

    status, data = api.call(session, "POST", "doctor/profile/photo-upload-url",
                            json={"content_type": content_type, "content_length": len(blob)})
    if status != 200:
        return jsonify(message=data.get("message", "Could not start the upload.")), status

    try:
        put = requests.put(data["upload_url"], data=blob,
                           headers=data.get("required_headers") or {"Content-Type": content_type},
                           timeout=30)
    except requests.RequestException:
        return jsonify(message="Could not reach photo storage. Try again."), 502
    if not put.ok:
        return jsonify(message="Photo storage rejected the upload."), 502

    status, data2 = api.call(session, "PATCH", "doctor/profile", json={"photo_key": data["photo_key"]})
    if status != 200:
        return jsonify(message=data2.get("message", "Photo uploaded but not saved.")), status
    return jsonify(message="Photo updated.", photo_key=data["photo_key"])


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify(message="Not found."), 404
    return render_template("error.html", code=404, headline="We can't find that page",
                           detail="The link may be old, or the page may have moved."), 404


@app.errorhandler(413)
def too_large(_):
    return jsonify(message="That file is too large. Photos must be 5 MB or smaller."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
