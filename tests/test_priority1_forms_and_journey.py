import os
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


def test_upload_boundary_cases_and_cleanup(client, tmp_path, monkeypatch):
    """
    Test 1.2: Boundary verification for uploads:
    - Valid .jpeg extension
    - Unsupported extensions (.png, .txt, .exe)
    - Misleading extensions (e.g. text file disguised as .pdf)
    - Files exceeding 5 MB limit (5MB + 1 byte)
    - Confirm rejected uploads do not leave temporary or saved files in upload_dir.
    """
    upload_dir = str(tmp_path / "uploads_boundary")
    os.makedirs(upload_dir, exist_ok=True)
    monkeypatch.setattr('app.UPLOAD_DIR', upload_dir)
    login_and_verify(client, mobile="9864011111")

    # 1. Valid .jpeg file
    jpeg_data = io.BytesIO(b"\xff\xd8\xff\xe0" + b"\x00" * 50000)
    resp = client.post('/upload', data={'document': (jpeg_data, 'doc.jpeg')}, content_type='multipart/form-data', follow_redirects=True)
    assert b"Declaration" in resp.data
    saved_files = os.listdir(upload_dir)
    assert len(saved_files) == 1 and saved_files[0].endswith(".jpeg")

    # Reset directory
    for f in os.listdir(upload_dir):
        os.remove(os.path.join(upload_dir, f))

    # 2. Unsupported extension (.png)
    png_data = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000)
    resp = client.post('/upload', data={'document': (png_data, 'doc.png')}, content_type='multipart/form-data', follow_redirects=True)
    assert b"Invalid document format" in resp.data or b"PDF or JPG" in resp.data
    assert len(os.listdir(upload_dir)) == 0, "Rejected .png must not leave files in upload directory"

    # 3. Misleading extension: text file named document.pdf without PDF magic bytes
    fake_pdf = io.BytesIO(b"This is just plain text content masquerading as a pdf")
    resp = client.post('/upload', data={'document': (fake_pdf, 'fake.pdf')}, content_type='multipart/form-data', follow_redirects=True)
    assert b"Invalid" in resp.data or b"PDF or JPG" in resp.data
    assert len(os.listdir(upload_dir)) == 0, "Misleading extension file must be rejected and leave no files"

    # 4. File exceeding 5 MB (5MB + 1 byte = 5,242,881 bytes)
    oversized = io.BytesIO(b"%PDF-1.4 " + b"\x00" * (5 * 1024 * 1024 + 1))
    resp = client.post('/upload', data={'document': (oversized, 'large.pdf')}, content_type='multipart/form-data', follow_redirects=True)
    assert b"exceeds the maximum size limit of 5 MB" in resp.data
    assert len(os.listdir(upload_dir)) == 0, "Oversized file must be rejected and leave no files"


def test_widowed_applicant_not_blocked_on_husband_employer(client, test_db):
    """
    Test 1.3: Verify widowed applicants can complete submission without deceased spouse employer.
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
            'husband_employer': '',  # Deceased spouse has no current employer
            'village': 'Milanpur',
            'block': 'Borgaon',
            'bank_account': '123456789012',
            'ifsc': 'SBIN0001234'
        }
        sess['doc_path'] = '/tmp/doc.pdf'

    resp = client.post('/declaration', follow_redirects=True)
    assert b"Something went wrong. Please try again." not in resp.data
    assert b"Application Submitted" in resp.data or resp.status_code == 200


def test_widowed_applicant_unrelated_validation_preserved(client):
    """
    Test 1.4: Verify that widowed-applicant fix does NOT bypass unrelated required-field validations.
    """
    login_and_verify(client, mobile="9864098765")

    base_widow_data = {
        'applicant_name': 'Runu Saikia',
        'dob': '15/04/1958',
        'gender': 'Female',
        'marital_status': 'Widowed',
        'husband_name': 'Late Bhaben',
        'husband_employer': '',
        'village': 'Milanpur',
        'block': 'Borgaon',
        'bank_account': '123456789012',
        'ifsc': 'SBIN0001234'
    }

    # 1. Missing applicant name
    d = dict(base_widow_data, applicant_name='')
    with client.session_transaction() as sess:
        sess['form_data'] = d
        sess['doc_path'] = '/tmp/doc.pdf'
    resp = client.post('/declaration', follow_redirects=True)
    assert b"full name" in resp.data.lower()

    # 2. Missing village
    d = dict(base_widow_data, village='')
    with client.session_transaction() as sess:
        sess['form_data'] = d
    resp = client.post('/declaration', follow_redirects=True)
    assert b"village" in resp.data.lower()

    # 3. Missing bank account
    d = dict(base_widow_data, bank_account='')
    with client.session_transaction() as sess:
        sess['form_data'] = d
    resp = client.post('/declaration', follow_redirects=True)
    assert b"bank account" in resp.data.lower()

    # 4. Underage widowed applicant (e.g. age 45)
    d = dict(base_widow_data, dob='15/04/1980')
    with client.session_transaction() as sess:
        sess['form_data'] = d
    resp = client.post('/declaration', follow_redirects=True)
    assert b"at least 60 years" in resp.data.lower() or b"eligible" in resp.data.lower()


def test_indian_and_ambiguous_date_parsing(client, test_db):
    """
    Test 1.5: Verify DD/MM/YYYY takes precedence, handles ambiguous dates correctly, and accepts ISO.
    - Ambiguous date '04/05/1952': In Indian format, Day 04, Month 05 (May 4, 1952) -> MUST be 1952-05-04.
    - ISO format '1952-05-04' -> 1952-05-04.
    """
    login_and_verify(client, mobile="9864088888")

    # Ambiguous date: 04/05/1952
    with client.session_transaction() as sess:
        sess['form_data'] = {
            'applicant_name': 'Paban Borah',
            'dob': '04/05/1952',
            'gender': 'Male',
            'marital_status': 'Married',
            'village': 'Namdang',
            'block': 'Sonari',
            'bank_account': '987654321098',
            'ifsc': 'SBIN0004321'
        }
        sess['doc_path'] = '/tmp/doc.pdf'

    resp = client.post('/declaration', follow_redirects=True)
    assert b"Application Submitted" in resp.data

    cur = test_db.cursor()
    cur.execute("SELECT dob FROM applications WHERE mobile = %s", ("9864088888",))
    row = cur.fetchone()
    assert row is not None
    # 04/05 in DD/MM/YYYY must be May 4th (1952-05-04), NOT April 5th (1952-04-05)
    assert str(row[0]) == '1952-05-04', f"Ambiguous date 04/05/1952 must parse as 1952-05-04, got {row[0]}"


def test_privacy_in_validation_error_messages(client):
    """
    Test 1.6: Review that validation error messages do NOT echo back sensitive personal/banking data.
    """
    login_and_verify(client, mobile="9864077777")

    sensitive_account = "99887766554433"
    with client.session_transaction() as sess:
        sess['form_data'] = {
            'applicant_name': 'Minati Das',
            'dob': 'invalid-date',
            'gender': 'Female',
            'marital_status': 'Unmarried',
            'village': 'Panidihing',
            'block': 'Namti',
            'bank_account': sensitive_account,
            'ifsc': 'SBIN0001234'
        }
        sess['doc_path'] = '/tmp/doc.pdf'

    resp = client.post('/declaration', follow_redirects=True)
    # The error flash must NOT echo back the sensitive bank account number or invalid inputs
    assert sensitive_account.encode('utf-8') not in resp.data, \
        "Sensitive banking details must never be echoed in flashed error messages"
    assert b"Invalid date format" in resp.data
