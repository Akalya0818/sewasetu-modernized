"""test_priority4_security_and_privacy.py - Regression tests for citizen data privacy,
access control, IDOR, privilege escalation, and SQL injection prevention.

Defects targeted:
1. /ack/<id>.pdf: Completely unauthenticated, allows anyone to harvest citizen PII.
2. /application/<id>: IDOR vulnerability, allows any logged-in citizen to view other citizens' bank details.
3. /admin/approve/<id> and /admin/reject/<id>: Checks session.get('logged_in') instead of session.get('admin'),
   allowing any authenticated citizen to approve/reject pension applications.
4. /status/reset: Raw string interpolation in SQL query ("SELECT ... WHERE mobile = '%s'" % mobile),
   vulnerable to SQL injection.

Safety notice:
All tests use synthetic records in an isolated in-memory test database.
No connection to seed.sql or real citizen databases.
"""

import os
import io
import pytest
from datetime import datetime, date

from app import app as flask_app, hash_password


# ---------------------------------------------------------------------------
# Test Helpers: Synthetic Citizens and Admin Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_applications(test_db):
    """Inserts two distinct synthetic applications for IDOR and access testing."""
    cur = test_db.cursor()
    # Citizen A
    cur.execute("""
        INSERT INTO applications (
            application_no, applicant_name, mobile, dob, village, block,
            bank_account, ifsc, submitted_at, status
        ) VALUES (
            'SSP_SEC_A', 'Citizen Alpha', '9800000401', '1960-01-01',
            'Sonari', 'Sonari', '111122223333', 'SBIN0001111',
            '2026-03-15 10:00:00', 'PENDING'
        )
    """)
    # Citizen B
    cur.execute("""
        INSERT INTO applications (
            application_no, applicant_name, mobile, dob, village, block,
            bank_account, ifsc, submitted_at, status
        ) VALUES (
            'SSP_SEC_B', 'Citizen Beta', '9800000402', '1958-05-12',
            'Namti', 'Namti', '444455556666', 'SBIN0002222',
            '2026-03-16 11:00:00', 'PENDING'
        )
    """)
    # Portal user accounts
    cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES ('9800000401', %s)",
                (hash_password('01011960'),))
    cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES ('9800000402', %s)",
                (hash_password('12051958'),))
    test_db.commit()

    cur.execute("SELECT id FROM applications WHERE application_no = 'SSP_SEC_A'")
    id_a = cur.fetchone()[0]
    cur.execute("SELECT id FROM applications WHERE application_no = 'SSP_SEC_B'")
    id_b = cur.fetchone()[0]

    return {"citizen_a": {"id": id_a, "mobile": "9800000401", "name": "Citizen Alpha"},
            "citizen_b": {"id": id_b, "mobile": "9800000402", "name": "Citizen Beta"}}


# ---------------------------------------------------------------------------
# Test A: Acknowledgment PDF Access Authorization — /ack/<id>.pdf
# ---------------------------------------------------------------------------

def test_a1_unauthenticated_access_to_ack_pdf_denied(client, sample_applications):
    """
    Test A.1: Anonymous / unauthenticated access to /ack/<id>.pdf must be denied.
    Defect on baseline: /ack/<id>.pdf is completely unauthenticated and returns 200 OK.
    Expected: Returns 401 Unauthorized, 403 Forbidden, or redirects to login.
    """
    app_id = sample_applications["citizen_a"]["id"]
    resp = client.get(f"/ack/{app_id}.pdf")

    # On baseline: resp.status_code is 200 (Vulnerability: public harvest)
    assert resp.status_code in (401, 403) or (resp.status_code == 302 and "/status" in resp.headers.get("Location", "")), (
        f"Unauthenticated request to /ack/{app_id}.pdf must not return 200 OK! "
        f"Observed status: {resp.status_code}"
    )


def test_a2_cross_citizen_access_to_ack_pdf_denied(client, sample_applications):
    """
    Test A.2: Citizen A logged in must NOT be able to access Citizen B's acknowledgment PDF.
    Defect on baseline: Anyone can download any citizen's PDF by ID.
    Expected: Returns 403 Forbidden.
    """
    id_b = sample_applications["citizen_b"]["id"]

    # Log in as Citizen A
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["portal_mobile"] = sample_applications["citizen_a"]["mobile"]

    # Attempt to access Citizen B's PDF
    resp = client.get(f"/ack/{id_b}.pdf")
    assert resp.status_code == 403, (
        f"Citizen A must receive 403 Forbidden when requesting Citizen B's acknowledgment PDF! "
        f"Observed status: {resp.status_code}"
    )


def test_a3_owner_access_to_own_ack_pdf_permitted(client, sample_applications):
    """
    Test A.3: An authenticated applicant must be permitted to access their OWN acknowledgment PDF.
    """
    id_a = sample_applications["citizen_a"]["id"]

    # Log in as Citizen A
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["portal_mobile"] = sample_applications["citizen_a"]["mobile"]

    resp = client.get(f"/ack/{id_a}.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")


def test_a4_admin_access_to_ack_pdf_permitted(client, sample_applications):
    """
    Test A.4: A verified administrator must be permitted to access acknowledgment PDFs.
    """
    id_a = sample_applications["citizen_a"]["id"]

    # Log in as Admin
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin"] = True

    resp = client.get(f"/ack/{id_a}.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"


def test_a5_nonexistent_ack_pdf_returns_404(client, sample_applications):
    """
    Test A.5: Requesting nonexistent application ID must return 404 Not Found.
    """
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin"] = True

    resp = client.get("/ack/999999.pdf")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test B: Application Dossier Access Authorization & IDOR — /application/<id>
# ---------------------------------------------------------------------------

def test_b1_cross_citizen_idor_access_denied(client, sample_applications):
    """
    Test B.1: Citizen A logged in must NOT view Citizen B's dossier at /application/<id_b>.
    Defect on baseline: /application/<id> checks only session.get('logged_in'), allowing IDOR.
    Expected: Returns 403 Forbidden.
    """
    id_b = sample_applications["citizen_b"]["id"]

    # Log in as Citizen A
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["portal_mobile"] = sample_applications["citizen_a"]["mobile"]

    resp = client.get(f"/application/{id_b}")

    # On baseline: resp.status_code is 200 and exposes Citizen B's banking details
    assert resp.status_code == 403, (
        f"Citizen A must receive 403 Forbidden when accessing Citizen B's application dossier! "
        f"Observed status: {resp.status_code}"
    )
    assert b"444455556666" not in resp.data, "Must not leak Citizen B's bank account number"


def test_b2_owner_access_to_own_dossier_permitted(client, sample_applications):
    """
    Test B.2: Citizen A logged in must be able to view their own application dossier.
    """
    id_a = sample_applications["citizen_a"]["id"]

    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["portal_mobile"] = sample_applications["citizen_a"]["mobile"]

    resp = client.get(f"/application/{id_a}")
    assert resp.status_code == 200
    assert b"Citizen Alpha" in resp.data
    assert b"111122223333" in resp.data


def test_b3_admin_access_to_dossier_permitted(client, sample_applications):
    """
    Test B.3: A verified administrator must be allowed to inspect dossiers.
    """
    id_a = sample_applications["citizen_a"]["id"]

    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin"] = True

    resp = client.get(f"/application/{id_a}")
    assert resp.status_code == 200


def test_b4_unauthenticated_access_to_dossier_redirects_to_login(client, sample_applications):
    """
    Test B.4: Unauthenticated access to application dossier must be redirected to login.
    """
    id_a = sample_applications["citizen_a"]["id"]
    resp = client.get(f"/application/{id_a}")
    assert resp.status_code == 302
    assert "/status" in resp.headers.get("Location", "")


def test_b5_dossier_http_methods(client, sample_applications):
    """
    Test B.5: Unsupported HTTP methods (POST, PUT, DELETE) on dossier route must be rejected.
    """
    id_a = sample_applications["citizen_a"]["id"]
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["portal_mobile"] = sample_applications["citizen_a"]["mobile"]

    resp = client.post(f"/application/{id_a}")
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Test C: Administrative Action Authorization — /admin/approve & /admin/reject
# ---------------------------------------------------------------------------

def test_c1_citizen_cannot_approve_applications(client, test_db, sample_applications):
    """
    Test C.1: Regular authenticated citizen must NOT be able to approve applications.
    Defect on baseline: /admin/approve/<id> checks session.get('logged_in') instead of
    session.get('admin'), allowing any citizen to approve records.
    Expected: Returns 403 Forbidden and status remains 'PENDING'.
    """
    id_b = sample_applications["citizen_b"]["id"]

    # Log in as Citizen A (regular user, not admin)
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["portal_mobile"] = sample_applications["citizen_a"]["mobile"]
        sess["admin"] = False

    resp = client.post(f"/admin/approve/{id_b}")

    # On baseline: resp.status_code is 302 redirect and status becomes APPROVED!
    assert resp.status_code == 403, (
        f"Citizen attempting admin action must receive 403 Forbidden! Observed: {resp.status_code}"
    )

    cur = test_db.cursor()
    cur.execute("SELECT status FROM applications WHERE id = %s", (id_b,))
    status = cur.fetchone()[0]
    assert status == "PENDING", f"Application status must remain PENDING, got {status}"


def test_c2_citizen_cannot_reject_applications(client, test_db, sample_applications):
    """
    Test C.2: Regular authenticated citizen must NOT be able to reject applications.
    Defect on baseline: /admin/reject/<id> checks session.get('logged_in') instead of session.get('admin').
    Expected: Returns 403 Forbidden and status remains 'PENDING'.
    """
    id_b = sample_applications["citizen_b"]["id"]

    # Log in as Citizen A
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["portal_mobile"] = sample_applications["citizen_a"]["mobile"]
        sess["admin"] = False

    resp = client.post(f"/admin/reject/{id_b}")

    assert resp.status_code == 403, (
        f"Citizen attempting admin action must receive 403 Forbidden! Observed: {resp.status_code}"
    )

    cur = test_db.cursor()
    cur.execute("SELECT status FROM applications WHERE id = %s", (id_b,))
    status = cur.fetchone()[0]
    assert status == "PENDING", f"Application status must remain PENDING, got {status}"


def test_c3_unauthenticated_user_cannot_approve_or_reject(client, sample_applications):
    """
    Test C.3: Unauthenticated request to /admin/approve or /admin/reject must be denied.
    """
    id_a = sample_applications["citizen_a"]["id"]
    resp1 = client.post(f"/admin/approve/{id_a}")
    assert resp1.status_code == 403

    resp2 = client.post(f"/admin/reject/{id_a}")
    assert resp2.status_code == 403


def test_c4_verified_admin_can_approve_application(client, test_db, sample_applications):
    """
    Test C.4: Verified administrator can approve applications with valid CSRF token and record audit trail.
    """
    id_a = sample_applications["citizen_a"]["id"]

    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin"] = True
        sess["csrf_token"] = "valid_test_admin_csrf_token"

    resp = client.post(f"/admin/approve/{id_a}", data={"csrf_token": "valid_test_admin_csrf_token"}, follow_redirects=False)
    assert resp.status_code == 302

    cur = test_db.cursor()
    cur.execute("SELECT status, decided_at, decided_by FROM applications WHERE id = %s", (id_a,))
    row = cur.fetchone()
    assert row[0] == "APPROVED"
    assert row[1] is not None
    assert row[2] == "admin"


def test_c5_verified_admin_can_reject_application(client, test_db, sample_applications):
    """
    Test C.5: Verified administrator can reject applications with valid CSRF token and record audit trail.
    """
    id_b = sample_applications["citizen_b"]["id"]

    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin"] = True
        sess["csrf_token"] = "valid_test_admin_csrf_token"

    resp = client.post(f"/admin/reject/{id_b}", data={"csrf_token": "valid_test_admin_csrf_token"}, follow_redirects=False)
    assert resp.status_code == 302

    cur = test_db.cursor()
    cur.execute("SELECT status, decided_at, decided_by FROM applications WHERE id = %s", (id_b,))
    row = cur.fetchone()
    assert row[0] == "REJECTED"
    assert row[1] is not None
    assert row[2] == "admin"


def test_c6_admin_approve_missing_csrf_token_denied(client, test_db, sample_applications):
    """
    Test C.6: Admin approve request missing CSRF token must be denied (403) and must NOT change status.
    """
    id_a = sample_applications["citizen_a"]["id"]

    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin"] = True
        sess["csrf_token"] = "valid_test_admin_csrf_token"

    # POST without csrf_token
    resp = client.post(f"/admin/approve/{id_a}", data={}, follow_redirects=False)
    assert resp.status_code == 403, f"Expected 403 Forbidden for missing CSRF token, got {resp.status_code}"

    cur = test_db.cursor()
    cur.execute("SELECT status, decided_at, decided_by FROM applications WHERE id = %s", (id_a,))
    row = cur.fetchone()
    assert row[0] == "PENDING", f"Application status must remain PENDING, got {row[0]}"
    assert row[1] is None, "decided_at must remain NULL"


def test_c7_admin_approve_invalid_csrf_token_denied(client, test_db, sample_applications):
    """
    Test C.7: Admin approve request with forged/invalid CSRF token must be denied (403).
    """
    id_a = sample_applications["citizen_a"]["id"]

    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin"] = True
        sess["csrf_token"] = "legitimate_session_token"

    # POST with attacker forged token
    resp = client.post(f"/admin/approve/{id_a}", data={"csrf_token": "forged_attacker_token"}, follow_redirects=False)
    assert resp.status_code == 403, f"Expected 403 Forbidden for invalid CSRF token, got {resp.status_code}"

    cur = test_db.cursor()
    cur.execute("SELECT status FROM applications WHERE id = %s", (id_a,))
    assert cur.fetchone()[0] == "PENDING"


def test_c8_admin_reject_missing_or_invalid_csrf_token_denied(client, test_db, sample_applications):
    """
    Test C.8: Admin reject request with missing or invalid CSRF token must be denied (403).
    """
    id_b = sample_applications["citizen_b"]["id"]

    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["admin"] = True
        sess["csrf_token"] = "legitimate_session_token"

    resp1 = client.post(f"/admin/reject/{id_b}", data={}, follow_redirects=False)
    assert resp1.status_code == 403

    resp2 = client.post(f"/admin/reject/{id_b}", data={"csrf_token": "wrong_token"}, follow_redirects=False)
    assert resp2.status_code == 403

    cur = test_db.cursor()
    cur.execute("SELECT status FROM applications WHERE id = %s", (id_b,))
    assert cur.fetchone()[0] == "PENDING"


# ---------------------------------------------------------------------------
# Test D: Password Reset SQL Injection — /status/reset
# ---------------------------------------------------------------------------

def test_d1_sql_injection_payload_does_not_manipulate_query(client, test_db, sample_applications):
    """
    Test D.1: Malicious SQL injection payloads in /status/reset must not be executed.
    Defect on baseline: cur.execute("SELECT mobile FROM portal_users WHERE mobile = '%s'" % mobile)
    uses raw string interpolation.
    """
    # SQL injection payload attempting tautology
    sqli_payload = "' OR '1'='1"

    cur = test_db.cursor()
    cur.execute("SELECT password_hash FROM portal_users WHERE mobile = '9800000401'")
    orig_hash = cur.fetchone()[0]

    resp = client.post("/status/reset", data={"mobile": sqli_payload}, follow_redirects=True)
    assert resp.status_code == 200

    # Ensure no accounts were arbitrarily reset due to tautology injection
    cur.execute("SELECT password_hash FROM portal_users WHERE mobile = '9800000401'")
    new_hash = cur.fetchone()[0]
    assert new_hash == orig_hash, "SQL injection must not modify password hash of arbitrary accounts"


def test_d2_valid_mobile_password_reset_functional(client, test_db, sample_applications):
    """
    Test D.2: Legitimate password reset resets the password to DOB as DDMMYYYY.
    """
    mobile = sample_applications["citizen_a"]["mobile"]

    resp = client.post("/status/reset", data={"mobile": mobile}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"If the mobile number exists" in resp.data

    cur = test_db.cursor()
    cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
    new_hash = cur.fetchone()[0]
    assert new_hash == hash_password("01011960"), "Password must be reset to DOB (01011960)"


def test_d3_unknown_mobile_does_not_reveal_account_existence(client):
    """
    Test D.3: Requesting password reset for an unregistered mobile number returns
    generic confirmation without leaking account existence or database errors.
    """
    resp = client.post("/status/reset", data={"mobile": "9999999999"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"If the mobile number exists, the password has been reset and sent by SMS." in resp.data


def test_d4_malformed_mobile_password_reset_handled_safely(client, test_db):
    """
    Test D.4: Malformed input (alphanumeric, symbols, empty string) to password reset
    is handled safely without leaking internal database errors or exceptions.
    """
    for malformed in ["not-a-number", "9800000401; DROP TABLE applications;", "", "   "]:
        resp = client.post("/status/reset", data={"mobile": malformed}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"If the mobile number exists, the password has been reset and sent by SMS." in resp.data


# ---------------------------------------------------------------------------
# Test E: Session & Privilege Escalation Resistance
# ---------------------------------------------------------------------------

def test_e1_form_parameters_cannot_grant_admin_session(client, sample_applications):
    """
    Test E.1: Submitting admin flags in login form data must not elevate citizen session to admin.
    """
    resp = client.post("/status", data={
        "mobile": "9800000401",
        "password": "password",
        "admin": "1",
        "is_admin": "true",
        "role": "admin"
    })
    with client.session_transaction() as sess:
        assert sess.get("admin") is not True, "Citizen session must not gain admin privileges via form parameters"


def test_e2_session_cookie_security_and_samesite_policy(client):
    """
    Test E.2: Verify session cookies are configured with HttpOnly=True,
    SameSite=Lax, and proper conditional Secure configuration.
    """
    assert flask_app.config.get("SESSION_COOKIE_HTTPONLY") is True, "Session cookie must be HttpOnly"
    assert flask_app.config.get("SESSION_COOKIE_SAMESITE") in ("Lax", "Strict"), "Session cookie SameSite must be set"
    # Verify local dev allows non-secure over HTTP (False by default in development)
    assert flask_app.config.get("SESSION_COOKIE_SECURE") is False, "Default local development must not force Secure"

    # Make request to generate session cookie
    resp = client.post("/status", data={"mobile": "9800000401", "password": "wrong"})
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
