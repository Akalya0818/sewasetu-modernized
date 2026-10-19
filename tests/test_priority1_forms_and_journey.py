import io
from datetime import date
import pytest


def login_and_verify(client, mobile="9864012345"):
    """Helper to establish a verified mobile session."""
    with client.session_transaction() as sess:
        sess['verified_mobile'] = mobile
        sess['apply_mobile'] = mobile


def test_upload_ui_contract_and_size_limits(client):
    """
    Test 1.1: Verify upload contract vs baseline implementation.
    UI promises 'JPG or PDF, maximum size 5 MB' in upload.html.
    Defect: Baseline strictly rejects JPG files and any PDF > 100 KB with 'ERR_VAL_47'.
    """
    login_and_verify(client)

    # 1. Attempt uploading a valid JPG age proof (150 KB)
    jpg_data = io.BytesIO(b"\xff\xd8\xff\xe0" + b"\x00" * 150000)
    resp_jpg = client.post(
        '/upload',
        data={'document': (jpg_data, 'age_proof.jpg')},
        content_type='multipart/form-data',
        follow_redirects=True
    )
    # On baseline: fails with 'ERR_VAL_47' and remains on upload page
    assert b"ERR_VAL_47" not in resp_jpg.data, "Should not return cryptic error code ERR_VAL_47"
    assert resp_jpg.status_code == 200
    assert b"Declaration" in resp_jpg.data or resp_jpg.request.path == '/declaration', \
        "Valid JPG upload under 5MB should advance to declaration"

    # 2. Attempt uploading a valid 500 KB PDF document (well within the 5MB UI promise)
    pdf_data = io.BytesIO(b"%PDF-1.4 " + b"\x00" * 500000)
    resp_pdf = client.post(
        '/upload',
        data={'document': (pdf_data, 'voter_card.pdf')},
        content_type='multipart/form-data',
        follow_redirects=True
    )
    assert b"ERR_VAL_47" not in resp_pdf.data, "Should accept PDF up to 5MB per UI contract"
    assert b"Declaration" in resp_pdf.data or resp_pdf.request.path == '/declaration', \
        "Valid 500KB PDF should advance to declaration"


def test_widowed_applicant_not_blocked_on_husband_employer(client, test_db):
    """
    Test 1.2: Verify widowed applicants can complete submission without deceased spouse employer.
    Defect: Baseline handle_submission enforces:
        if marital == 'Widowed':
            if not husband_name or not husband_employer:
                flash('Something went wrong. Please try again.')
    This makes it impossible for widows (whose spouses are deceased) or male widowers to apply.
    """
    login_and_verify(client, mobile="9864099999")

    # Set up form data for an eligible elderly widow (age 65)
    with client.session_transaction() as sess:
        sess['form_data'] = {
            'applicant_name': 'Runu Saikia',
            'dob': '15/04/1958',
            'gender': 'Female',
            'marital_status': 'Widowed',
            'husband_name': 'Late Bhaben Saikia',
            'husband_employer': '',  # Deceased spouse has no current employer!
            'village': 'Milanpur',
            'block': 'Borgaon',
            'bank_account': '123456789012',
            'ifsc': 'SBIN0001234'
        }
        sess['doc_path'] = '/tmp/doc.pdf'

    # Submit final declaration
    resp = client.post('/declaration', follow_redirects=True)

    # On baseline: rejected with generic error and redirected back to form_step 1
    assert b"Something went wrong. Please try again." not in resp.data, \
        "Widowed applicant should not be blocked on deceased husband employer"
    assert b"Application Submitted" in resp.data or resp.status_code == 200, \
        "Eligible widowed applicant should receive confirmation"


def test_indian_date_format_precedence(client, test_db):
    """
    Test 1.3: Verify DD/MM/YYYY takes precedence over US MM/DD/YYYY format.
    Defect: Baseline checks ('%m/%d/%Y', '%d/%m/%Y').
    Entering '05/10/1950' (5th of October 1950, standard Indian date format)
    is erroneously parsed as May 10, 1950.
    Also, ISO format 'YYYY-MM-DD' should be supported cleanly.
    """
    login_and_verify(client, mobile="9864088888")

    # Day: 05, Month: 10 (October), Year: 1955 -> In India: 5th October 1955.
    with client.session_transaction() as sess:
        sess['form_data'] = {
            'applicant_name': 'Paban Borah',
            'dob': '05/10/1955',
            'gender': 'Male',
            'marital_status': 'Married',
            'husband_name': '',
            'husband_employer': '',
            'village': 'Namdang',
            'block': 'Sonari',
            'bank_account': '987654321098',
            'ifsc': 'SBIN0004321'
        }
        sess['doc_path'] = '/tmp/doc.pdf'

    resp = client.post('/declaration', follow_redirects=True)
    assert b"Application Submitted" in resp.data

    # Query the stored record in test DB
    cur = test_db.cursor()
    cur.execute("SELECT dob FROM applications WHERE mobile = %s", ("9864088888",))
    row = cur.fetchone()
    assert row is not None, "Application should be inserted into database"
    
    # Check stored date: Should be 1955-10-05 (October 5), NOT 1955-05-10 (May 10)
    stored_dob = str(row[0])
    assert stored_dob == '1955-10-05', f"Stored DOB should be 1955-10-05, got {stored_dob}"


def test_field_level_informative_validation_errors(client):
    """
    Test 1.4: Validation errors should be specific, helpful, and not opaque.
    Defect: Baseline emits 'Something went wrong. Please try again.' for all validation issues.
    """
    login_and_verify(client, mobile="9864077777")

    # Missing mandatory bank account
    with client.session_transaction() as sess:
        sess['form_data'] = {
            'applicant_name': 'Minati Das',
            'dob': '12/06/1954',
            'gender': 'Female',
            'marital_status': 'Unmarried',
            'village': 'Panidihing',
            'block': 'Namti',
            'bank_account': '',  # MISSING
            'ifsc': 'SBIN0001234'
        }
        sess['doc_path'] = '/tmp/doc.pdf'

    resp = client.post('/declaration', follow_redirects=True)
    assert b"Something went wrong. Please try again." not in resp.data, \
        "Should provide specific field guidance rather than generic failure message"
    assert b"bank account" in resp.data.lower() or b"bank" in resp.data.lower(), \
        "Should mention missing bank details"
