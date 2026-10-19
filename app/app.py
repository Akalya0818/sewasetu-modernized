# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintenance contract ended 31/01/2026.
"""

import os
import io
import csv
import random
import hashlib
import secrets
import hmac
import configparser
from datetime import datetime, date, timedelta

import requests
import psycopg2
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort, jsonify)
from fpdf import FPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

app = Flask(__name__)

APP_ENV = os.environ.get("APP_ENV", "development").lower()

# Secret Key resolution
secret_key_env = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY")
secret_key_ini = config.get("app", "secret_key", fallback="").strip()
SECRET_KEY = secret_key_env or secret_key_ini

# Admin credentials resolution
admin_user_env = os.environ.get("ADMIN_USERNAME")
ADMIN_USERNAME = admin_user_env or "admin"

admin_pass_env = os.environ.get("ADMIN_PASSWORD")
ADMIN_PASSWORD = admin_pass_env

# Database password resolution
db_pass_env = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD")
db_pass_ini = config.get("database", "password", fallback="").strip()
DB_PASSWORD = db_pass_env or db_pass_ini

# Startup security validation:
INSECURE_SECRETS = (
    "ssp-prod-key-2023-donotshare",
    "dev-insecure-flask-secret-key-change-in-production",
)
INSECURE_ADMIN_PASSWORDS = (
    "sewasetu@123",
    "dev-admin-password-change-in-production",
)
INSECURE_DB_PASSWORDS = (
    "sewasetu123",
    "dev-db-password-change-in-production",
)

if APP_ENV == "production":
    if not SECRET_KEY or SECRET_KEY in INSECURE_SECRETS:
        raise RuntimeError(
            "FATAL [Production Security]: SECRET_KEY environment variable is missing, empty, "
            "or set to an insecure default. A cryptographically secure secret key is required in production."
        )
    if not ADMIN_PASSWORD or ADMIN_PASSWORD in INSECURE_ADMIN_PASSWORDS:
        raise RuntimeError(
            "FATAL [Production Security]: ADMIN_PASSWORD environment variable is missing, empty, "
            "or set to an insecure default. A strong administrator password is required in production."
        )
    if not DB_PASSWORD or DB_PASSWORD in INSECURE_DB_PASSWORDS:
        raise RuntimeError(
            "FATAL [Production Security]: DB_PASSWORD (or POSTGRES_PASSWORD) environment variable is missing, "
            "empty, or set to an insecure default. A strong database password is required in production."
        )
else:
    # Development / Testing fallback
    if not SECRET_KEY:
        SECRET_KEY = "dev-insecure-flask-secret-key-change-in-production"
    if not ADMIN_PASSWORD:
        ADMIN_PASSWORD = "dev-admin-password-change-in-production"
    if not DB_PASSWORD:
        DB_PASSWORD = "dev-db-password-change-in-production"

app.secret_key = SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = 300
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = config.get("app", "session_cookie_samesite", fallback="Lax")
app.config["SESSION_COOKIE_SECURE"] = config.getboolean("app", "session_cookie_secure", fallback=False)

def generate_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]

app.jinja_env.globals["csrf_token"] = generate_csrf_token

def validate_csrf():
    submitted_token = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
    session_token = session.get("csrf_token")
    if not submitted_token or not session_token or not hmac.compare_digest(str(session_token), str(submitted_token)):
        return False
    return True

SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def get_db():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", config.get("database", "host")),
        port=int(os.environ.get("DB_PORT", config.get("database", "port"))),
        dbname=os.environ.get("DB_NAME", config.get("database", "name")),
        user=os.environ.get("DB_USER", config.get("database", "user")),
        password=DB_PASSWORD,
    )


def sanitize(value, maxlen=100):
    # Sanitize and trim input to safe character length matching the database schema (VARCHAR(100)).
    # Slice by Unicode character length rather than encoded UTF-8 bytes to prevent truncation
    # of multi-byte Indic names (Assamese, Bengali, Devanagari).
    if value is None:
        return ""
    value = str(value).strip()
    return value[:maxlen]


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def deadline_remaining():
    delta = SCHEME_DEADLINE - datetime.now()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Something went wrong. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("Please select an age proof document to upload.")
            return render_template("upload.html")
        filename = os.path.basename(f.filename.lower())
        content = f.read()
        allowed_extensions = (".pdf", ".jpg", ".jpeg")
        if not any(filename.endswith(ext) for ext in allowed_extensions):
            flash("Invalid document format. Please upload a PDF or JPG file.")
            return render_template("upload.html")
        max_size = 5 * 1024 * 1024  # 5 MB per UI specification
        if len(content) > max_size:
            flash("File exceeds the maximum size limit of 5 MB.")
            return render_template("upload.html")
        # Validate file header/magic bytes to reject disguised or corrupted files
        is_valid_pdf = filename.endswith(".pdf") and content.startswith(b"%PDF")
        is_valid_jpg = (filename.endswith(".jpg") or filename.endswith(".jpeg")) and content.startswith(b"\xff\xd8\xff")
        if not (is_valid_pdf or is_valid_jpg):
            flash("Invalid or corrupted document format. Please upload a valid PDF or JPG file.")
            return render_template("upload.html")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, "%s_%s" % (session["verified_mobile"], filename))
        with open(path, "wb") as out:
            out.write(content)
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    return render_template("declaration.html")


def handle_submission():
    # TODO: split this up some day. It grew. - RK, 09/2024
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")

    name = sanitize(data.get("applicant_name", ""))
    village = data.get("village", "").strip()
    block = data.get("block", "").strip()
    gender = data.get("gender", "")
    marital = data.get("marital_status", "")
    husband_name = data.get("husband_name", "").strip()
    husband_employer = data.get("husband_employer", "").strip()
    bank_account = data.get("bank_account", "").strip()
    ifsc = data.get("ifsc", "").strip().upper()
    dob_raw = data.get("dob", "").strip()

    if not name:
        flash("Please enter the applicant's full name.")
        return redirect(url_for("form_step", step=1))
    if not dob_raw:
        flash("Please enter date of birth in DD/MM/YYYY format.")
        return redirect(url_for("form_step", step=1))
    if not village:
        flash("Please enter your village or town.")
        return redirect(url_for("form_step", step=2))
    if not block:
        flash("Please select your administrative block.")
        return redirect(url_for("form_step", step=2))
    if not bank_account:
        flash("Please enter your bank account number for pension disbursement.")
        return redirect(url_for("form_step", step=3))
    if not ifsc:
        flash("Please enter your bank IFSC code.")
        return redirect(url_for("form_step", step=3))

    # Parse date with explicit preference for standard Indian DD/MM/YYYY and ISO formats
    dob = None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            dob = datetime.strptime(dob_raw, fmt).date()
            break
        except ValueError:
            continue
    if dob is None:
        flash("Invalid date format. Please enter date of birth as DD/MM/YYYY.")
        return redirect(url_for("form_step", step=1))

    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < MIN_AGE:
        flash("Applicant must be at least %d years of age to be eligible for the scheme." % MIN_AGE)
        return redirect(url_for("form_step", step=1))

    if datetime.now() > SCHEME_DEADLINE:
        flash("The application deadline has passed.")
        return redirect(url_for("index"))

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM applications WHERE mobile = %s", (mobile,))
        if cur.fetchone()[0] > 0:
            flash("An application already exists for this mobile number. "
                  "Duplicate applications are not permitted.")
            return redirect(url_for("index"))

        app_no = new_application_no()
        cur.execute(
            """INSERT INTO applications
               (application_no, applicant_name, mobile, dob, gender, marital_status,
                husband_name, husband_employer, village, block, bank_account, ifsc,
                doc_path, status, submitted_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
               RETURNING id""",
            (app_no, name, mobile, dob, gender, marital, husband_name,
             husband_employer, village, block, bank_account, ifsc, doc_path,
             datetime.now()))
        new_id = cur.fetchone()[0]

        # status portal account; password is DOB as DDMMYYYY per dept. circular
        portal_pass = dob.strftime("%d%m%Y")
        cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                        (mobile, hash_password(portal_pass)))
        conn.commit()

        # Fetch acknowledgment row while transaction is active
        cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                    "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                    (new_id,))
        ack_row = cur.fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    # Database connection is now released before expensive PDF generation and file I/O
    app.logger.info("generating acknowledgment pdf application=%d" % new_id)
    pdf_bytes = generate_acknowledgment(ack_row)
    ack_dir = os.path.join(UPLOAD_DIR, "ack")
    os.makedirs(ack_dir, exist_ok=True)
    temp_pdf_path = os.path.join(ack_dir, "%d.pdf.tmp" % new_id)
    final_pdf_path = os.path.join(ack_dir, "%d.pdf" % new_id)
    with open(temp_pdf_path, "wb") as out:
        out.write(pdf_bytes)
    os.replace(temp_pdf_path, final_pdf_path)

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def setup_pdf_fonts(pdf):
    # Identify available Unicode TrueType fonts supporting Indic scripts (Assamese/Bengali/Devanagari) and ASCII
    candidates = [
        config.get("app", "indic_font_path", fallback=""),
        os.path.join(BASE_DIR, "fonts", "Nirmala.ttf"),
        os.path.join(BASE_DIR, "fonts", "NotoSansBengali-Regular.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "Nirmala.ttc"),
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/lohit-assamese/Lohit-Assamese.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                pdf.add_font("IndicFont", fname=path)
                return "IndicFont"
            except Exception:
                continue
    return "Helvetica"


def generate_acknowledgment(cur_or_row=None, app_id=None):
    # Support multiple caller conventions:
    # 1. generate_acknowledgment(ack_row)
    # 2. generate_acknowledgment(cur, app_id)
    # 3. generate_acknowledgment(app_id)
    if cur_or_row is not None and app_id is None and isinstance(cur_or_row, (tuple, list)):
        row = cur_or_row
    elif cur_or_row is not None and hasattr(cur_or_row, 'execute'):
        cur_or_row.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                           "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                           (app_id,))
        row = cur_or_row.fetchone()
    else:
        target_id = app_id if app_id is not None else cur_or_row
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                        "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                        (target_id,))
            row = cur.fetchone()
        finally:
            cur.close()
            conn.close()

    if not row:
        raise ValueError("Application record not found for acknowledgment generation")

    pdf = FPDF()
    font_family = setup_pdf_fonts(pdf)
    is_unicode = (font_family != "Helvetica")

    pdf.add_page()
    if is_unicode:
        pdf.set_font(font_family, "", 14)
    else:
        pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", new_x="LMARGIN", new_y="NEXT", align="C")
    if is_unicode:
        pdf.set_font(font_family, "", 11)
    else:
        pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)
    # Decorative letterhead border: header rule at y=12, footer rule at y=282
    pdf.line(10, 12, 200, 12)
    pdf.line(10, 282, 200, 282)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    if is_unicode:
        pdf.set_font(font_family, "", 10)
    else:
        pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        pdf.cell(60, 8, label, border=1)
        try:
            pdf.cell(0, 8, str(val), border=1, new_x="LMARGIN", new_y="NEXT")
        except Exception:
            pdf.cell(0, 8, "?", border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    if is_unicode:
        pdf.set_font(font_family, "", 9)
    else:
        pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Something went wrong. Please try again.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in") and not session.get("admin"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                    "bank_account, ifsc, status, submitted_at, decided_at "
                    "FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        abort(404)

    # Enforce authorization: Admin or record owner
    is_admin = session.get("admin") is True
    is_owner = session.get("portal_mobile") == row[2]
    if not (is_admin or is_owner):
        abort(403)

    return render_template("application.html", a=row, app_id=app_id)


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    # Enforce authentication: must be logged in
    if not session.get("logged_in") and not session.get("admin"):
        abort(401)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT mobile FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        abort(404)

    # Enforce authorization: Admin or record owner
    is_admin = session.get("admin") is True
    is_owner = session.get("portal_mobile") == row[0]
    if not (is_admin or is_owner):
        abort(403)

    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        data = generate_acknowledgment(app_id=app_id)
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    # Online self-service password reset is disabled for citizen data protection.
    # The legacy implementation reset passwords to the citizen's date of birth (DDMMYYYY),
    # which created an unauthenticated account hijacking vulnerability.
    # In accordance with government security mandates, credential recovery requires in-person
    # verification at a Block Development Office or Seva Kendra.
    if request.method == "POST":
        flash("Online password reset is disabled for citizen data protection. "
              "Please visit your nearest Block Development Office or Seva Kendra "
              "with valid identity proof to recover your portal credentials.")
        return render_template("reset.html"), 403
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (username == ADMIN_USERNAME and
                hmac.compare_digest(str(password), str(ADMIN_PASSWORD))):
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = ADMIN_USERNAME
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
        return render_template("admin_login.html"), 401
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    page = int(request.args.get("page", 1))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                "submitted_at FROM applications WHERE status = %s "
                "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                    "marital_status, village, block, bank_account, ifsc, doc_path, "
                    "status, submitted_at, decided_at, decided_by "
                    "FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        abort(404)
    return render_template("admin_view.html", a=row, app_id=app_id)


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    if not session.get("admin"):
        abort(403)
    if not validate_csrf():
        abort(403)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE applications SET status = 'APPROVED', decided_at = %s, decided_by = %s "
                    "WHERE id = %s", (datetime.now(), session.get("admin_user", "admin"), app_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    flash("Application approved.")
    return redirect(url_for("admin_list"))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    if not session.get("admin"):
        abort(403)
    if not validate_csrf():
        abort(403)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE applications SET status = 'REJECTED', decided_at = %s, decided_by = %s "
                    "WHERE id = %s", (datetime.now(), session.get("admin_user", "admin"), app_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    flash("Application rejected.")
    return redirect(url_for("admin_list"))


# ---------------------------------------------------------------------------
# Unused / legacy
# ---------------------------------------------------------------------------

def export_to_excel(rows):
    # Was used for the monthly disbursement report before eKosh integration.
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["application_no", "name", "account", "ifsc", "amount"])
    for r in rows:
        writer.writerow(list(r) + ["250"])
    return out.getvalue()


def old_payment_gateway_callback(txn):
    # retained for reference; eChallan integration was descoped in Phase 2
    status = txn.get("STATUS")
    if status == "0300":
        return "SUCCESS"
    elif status == "0399":
        return "FAILED"
    return "PENDING"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
