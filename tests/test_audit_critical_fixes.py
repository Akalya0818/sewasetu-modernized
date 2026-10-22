import os
import sys
import io
import re
import sqlite3
import threading
from datetime import datetime
import pytest

from app import app as flask_app, hash_password
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app', 'scripts')))
from init_db import init_database
from conftest import SCHEMA_SQL, SqliteConnectionWrapper


def test_init_database_idempotent_no_duplicates(tmp_path):
    """
    Verify that database initialization can run multiple times without
    creating duplicate applications or corrupting existing records.
    """
    db_file = str(tmp_path / "test_idempotent.db")
    raw_conn = sqlite3.connect(db_file)
    raw_conn.executescript(SCHEMA_SQL)
    wrapped = SqliteConnectionWrapper(raw_conn)

    # First initialization
    init_database(conn=wrapped)
    cur = wrapped.cursor()
    cur.execute("SELECT count(*), count(DISTINCT application_no) FROM applications")
    count1, distinct1 = cur.fetchone()

    # Second initialization
    init_database(conn=wrapped)
    cur.execute("SELECT count(*), count(DISTINCT application_no) FROM applications")
    count2, distinct2 = cur.fetchone()

    # Third initialization
    init_database(conn=wrapped)
    cur.execute("SELECT count(*), count(DISTINCT application_no) FROM applications")
    count3, distinct3 = cur.fetchone()

    assert count1 == count2 == count3, f"Repeated init altered count: {count1} -> {count2} -> {count3}"
    assert distinct1 == count1, f"Found duplicate application numbers: {distinct1} distinct of {count1}"
    assert distinct2 == count2
    assert distinct3 == count3
    raw_conn.close()


def test_concurrent_initialization_no_duplicates(tmp_path):
    """
    Verify that concurrent worker initialization does not produce duplicate records.
    """
    db_file = str(tmp_path / "test_concurrent.db")
    # Setup schema first
    init_conn = sqlite3.connect(db_file)
    init_conn.executescript(SCHEMA_SQL)
    init_conn.close()

    errors = []

    def run_init():
        try:
            conn = sqlite3.connect(db_file, timeout=20)
            wrapped = SqliteConnectionWrapper(conn)
            init_database(conn=wrapped)
            conn.close()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run_init) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread errors: {errors}"

    check_conn = sqlite3.connect(db_file)
    cur = check_conn.cursor()
    cur.execute("SELECT count(*), count(DISTINCT application_no) FROM applications")
    total_count, distinct_count = cur.fetchone()
    check_conn.close()

    assert total_count == distinct_count, f"Concurrent init duplicated applications: {distinct_count} distinct of {total_count}"


def test_database_with_existing_records_preserved(tmp_path):
    """
    Verify that when database already contains applications (e.g. production data),
    re-running initialization preserves existing records without injecting duplicates.
    """
    db_file = str(tmp_path / "test_existing.db")
    conn = sqlite3.connect(db_file)
    conn.executescript(SCHEMA_SQL)
    cur = conn.cursor()
    # Insert existing application
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, gender, marital_status,
                                  village, block, bank_account, ifsc, doc_path, status, submitted_at)
        VALUES ('SSP26999999', 'Existing Citizen', '9811122233', '1950-01-01', 'Female', 'Widowed',
                'Sonari Gaon', 'Sonari', '123456789012', 'SBIN0001234', 'uploads/test.pdf', 'APPROVED', '2026-01-01 10:00:00')
    """)
    conn.commit()

    wrapped = SqliteConnectionWrapper(conn)
    init_database(conn=wrapped)

    cur.execute("SELECT count(*) FROM applications WHERE application_no = 'SSP26999999'")
    assert cur.fetchone()[0] == 1, "Existing application was altered or removed"

    cur.execute("SELECT count(*) FROM applications")
    assert cur.fetchone()[0] == 1, "init_database must not inject synthetic seed into a non-empty database"
    conn.close()


def test_unknown_status_lookup_does_not_create_records(client, test_db):
    """
    FIX 2: Status lookup with unknown mobile or unknown application number
    must NOT create an application, portal_user, or OTP record.
    """
    cur = test_db.cursor()
    cur.execute("SELECT count(*) FROM applications")
    app_count_before = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM portal_users")
    user_count_before = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM otps")
    otp_count_before = cur.fetchone()[0]

    # 1. Lookup with unknown 10-digit mobile
    resp1 = client.post("/status", data={
        "mobile": "9998887776",
        "password": "15081955"
    }, follow_redirects=True)
    assert resp1.status_code == 200
    assert b"No registered application found" in resp1.data or b"Incorrect" in resp1.data

    # 2. Lookup with unknown application number
    resp2 = client.post("/status", data={
        "mobile": "APP-2026-99999",
        "password": "15081955"
    }, follow_redirects=True)
    assert resp2.status_code == 200
    assert b"No registered application found" in resp2.data or b"Incorrect" in resp2.data

    # Assert record counts remain strictly identical
    cur.execute("SELECT count(*) FROM applications")
    assert cur.fetchone()[0] == app_count_before, "Unknown lookup created an application record!"
    cur.execute("SELECT count(*) FROM portal_users")
    assert cur.fetchone()[0] == user_count_before, "Unknown lookup created a portal_user record!"
    cur.execute("SELECT count(*) FROM otps")
    assert cur.fetchone()[0] == otp_count_before, "Unknown lookup created an OTP record!"


def test_valid_status_tracking_succeeds(client, test_db):
    """
    Verify valid existing citizen credentials can track their application status normally.
    """
    cur = test_db.cursor()
    # Insert a valid citizen and portal_user
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, gender, marital_status,
                                  village, block, bank_account, ifsc, doc_path, status, submitted_at)
        VALUES ('APP-2026-00001', 'Ananya Sharma', '9876543210', '1955-08-15', 'Female', 'Widowed',
                'Sonari Gaon', 'Sonari', '123456789012', 'SBIN0001234', 'uploads/sample1.pdf', 'PENDING', '2026-10-01 10:00:00')
    """)
    cur.execute("""
        INSERT INTO portal_users (mobile, password_hash)
        VALUES ('9876543210', %s)
    """, (hash_password("15081955"),))
    test_db.commit()

    # Login with valid DOB password
    resp = client.post("/status", data={
        "mobile": "9876543210",
        "password": "15081955"
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Ananya Sharma" in resp.data
    assert b"APP-2026-00001" in resp.data
    assert b"PENDING" in resp.data


def test_otp_field_is_blank_on_initial_load_and_autofill_button_present(client):
    """
    FIX 3: Verify the OTP input field is blank upon page render,
    and the explicit Auto-Fill demonstration button is available.
    """
    # Establish apply session
    with client.session_transaction() as sess:
        sess['apply_mobile'] = "9876543210"
        sess['latest_otp'] = "654321"

    resp = client.get("/verify")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    # Verify input field does NOT prefill the OTP
    assert 'value=""' in html, "OTP input field must be blank upon initial load"
    assert 'value="654321"' not in html, "OTP input field must NOT be prefilled automatically"

    # Verify Auto-Fill button is present and contains demo code
    assert "btn-autofill-otp" in html or "Auto-Fill Code" in html
    assert "654321" in html, "Demonstration OTP should be displayed for the active session"


def test_otp_verification_flow_succeeds(client, test_db):
    """
    Verify that manual entry or autofill of the valid OTP allows advancing to step 1.
    """
    cur = test_db.cursor()
    cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES ('9876543210', '123456', %s)",
                (datetime.now(),))
    test_db.commit()

    with client.session_transaction() as sess:
        sess['apply_mobile'] = "9876543210"
        sess['latest_otp'] = "123456"

    # Post valid OTP
    resp = client.post("/verify", data={"otp": "123456"}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/form/1" in resp.headers.get("Location", "")

    # Post invalid OTP
    resp_bad = client.post("/verify", data={"otp": "000000"}, follow_redirects=True)
    assert b"Invalid OTP" in resp_bad.data


def test_new_application_submission_journey(client, test_db, tmp_path, monkeypatch):
    """
    Verify that legitimate application submission continues to work,
    generates acknowledgment PDF, and creates expected records.
    """
    upload_dir = str(tmp_path / "uploads_journey")
    os.makedirs(upload_dir, exist_ok=True)
    monkeypatch.setattr('app.UPLOAD_DIR', upload_dir)

    with client.session_transaction() as sess:
        sess['verified_mobile'] = "9876543111"
        sess['form_data'] = {
            'applicant_name': 'Dharani Gogoi',
            'dob': '15/06/1955',
            'gender': 'Female',
            'marital_status': 'Widowed',
            'husband_name': '',
            'husband_employer': '',
            'village': 'Namti Gaon',
            'block': 'Namti',
            'bank_account': '112233445566',
            'ifsc': 'SBIN0001234'
        }
        sess['doc_path'] = 'uploads/doc.pdf'

    resp = client.post("/declaration", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Application Submitted" in resp.data

    cur = test_db.cursor()
    cur.execute("SELECT id, application_no, applicant_name FROM applications WHERE mobile = '9876543111'")
    app_row = cur.fetchone()
    assert app_row is not None
    app_id, app_no, app_name = app_row
    assert app_name == "Dharani Gogoi"
    assert app_no.startswith("SSP")

    # Verify unauthenticated download is rejected per Priority 4 security policy
    assert client.get(f"/ack/{app_id}.pdf").status_code == 401

    # Login as citizen using registered credentials (mobile + DOB password)
    login_res = client.post("/status", data={"mobile": "9876543111", "password": "15061955"}, follow_redirects=True)
    assert login_res.status_code == 200

    # Verify acknowledgment PDF generation for authenticated owner
    ack_resp = client.get(f"/ack/{app_id}.pdf")
    assert ack_resp.status_code == 200
    assert ack_resp.headers.get("Content-Type") == "application/pdf"
    assert len(ack_resp.data) > 500


def test_admin_authentication_and_authorization(client):
    """
    Verify admin authentication and role authorization remain intact.
    """
    # Unauthenticated access to dashboard denied
    dash_unauth = client.get("/admin/dashboard", follow_redirects=False)
    assert dash_unauth.status_code == 302
    assert "/admin" in dash_unauth.headers.get("Location", "")

    # Admin login with valid assessor admin credentials
    login_resp = client.post("/admin", data={
        "username": "assessor_admin",
        "password": "FJFZU49EMDmeG9YDuse0hqdRKBWOqGs3l4BBS+cUZ08"
    }, follow_redirects=False)
    assert login_resp.status_code == 302
    assert "/admin/dashboard" in login_resp.headers.get("Location", "")
