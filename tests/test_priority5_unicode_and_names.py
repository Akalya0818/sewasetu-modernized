"""test_priority5_unicode_and_names.py - Regression tests for Unicode and Indic-name
preservation in name sanitization, database persistence, and acknowledgment PDF generation.

Defects targeted:
1. sanitize(value, maxlen=20) sliced raw UTF-8 bytes instead of Unicode characters,
   truncating Indic names (3 bytes per character) to ~6 characters and potentially
   splitting multi-byte sequences.
2. generate_acknowledgment() used the Type 1 'Helvetica' font, which lacks glyphs
   for Indic scripts (Assamese, Bengali, Devanagari), caught the encoding exception,
   and replaced the citizen's name with a literal '?' in the PDF.
3. Database schema defines applicant_name as VARCHAR(100), but sanitization
   artificially restricted names to 20 bytes.

Safety notice:
All tests use synthetic records in an isolated in-memory test database.
No connection to seed.sql or real citizen databases.
"""

import io
import os
import pytest
import pypdf
from datetime import datetime, date

from app import app as flask_app, sanitize, generate_acknowledgment


# ---------------------------------------------------------------------------
# Test Synthetic Data: Representative Names across Scripts
# ---------------------------------------------------------------------------

SYNTHETIC_INDIC_NAMES = [
    # Assamese names (Eastern Indic script)
    "ৰমেন বৰুৱা",              # Ramen Baruah (10 chars, 28 UTF-8 bytes)
    "প্ৰমোদ বৰা",               # Promod Bora (9 chars, 25 UTF-8 bytes)
    "লক্ষ্মী শইকীয়া",            # Lakshmi Saikia (15 chars, 43 UTF-8 bytes)
    # Devanagari names (Central/Northern Indic script)
    "रामेश्वर प्रसाद शर्मा",     # Rameshwar Prasad Sharma (21 chars, 59 UTF-8 bytes)
    "सुनीতা देवी",               # Sunita Devi (11 chars, 31 UTF-8 bytes)
    # Bengali script variation
    "অমিত কুমার দাস",            # Amit Kumar Das (14 chars, 40 UTF-8 bytes)
    # Standard Latin / English names
    "John Doe",
    "Bhaben Chandra Hazarika"
]


# ---------------------------------------------------------------------------
# Test A: Character Preservation & Sanitization
# ---------------------------------------------------------------------------

def test_a1_indic_names_not_truncated_by_byte_slicing():
    """
    Test A.1: Indic names must not be truncated to 6-7 characters due to 20-byte slicing.
    Defect on baseline: sanitize('ৰমেন বৰুৱা', maxlen=20) returns 'ৰমেন বৰ' (7 chars).
    Expected: Full 10-character name is preserved.
    """
    name = "ৰমেন বৰুৱা"
    result = sanitize(name)
    assert result == name, f"Expected '{name}' ({len(name)} chars), got '{result}' ({len(result)} chars)"


def test_a2_devanagari_names_not_truncated():
    """
    Test A.2: Devanagari names must not be chopped to ~6 characters.
    Defect on baseline: sanitize('रामेश्वर प्रसाद शर्मा') returns 'रामेश्' (6 chars).
    Expected: Full 21-character name is preserved.
    """
    name = "रामेश्वर प्रसाद शर्मा"
    result = sanitize(name)
    assert result == name, f"Expected '{name}', got '{result}'"


def test_a3_multibyte_character_never_split():
    """
    Test A.3: Slicing must never cut a 3-byte UTF-8 character in half or produce
    dangling bytes/replacement characters.
    """
    # Create a string of Indic characters
    indic_str = "অ" * 50  # 50 characters, 150 UTF-8 bytes
    sanitized = sanitize(indic_str, maxlen=30)
    # Must contain exactly 30 valid characters, not truncated mid-byte
    assert len(sanitized) == 30
    assert sanitized == "অ" * 30
    # UTF-8 roundtrip must be clean
    assert sanitized.encode("utf-8").decode("utf-8") == sanitized


def test_a4_names_near_schema_limit_preserved():
    """
    Test A.4: Names up to the database schema limit (VARCHAR(100)) are preserved.
    """
    long_name = "শ্ৰীযুত " + ("বৰুৱা " * 10)  # ~70 characters
    assert len(long_name) < 100
    sanitized = sanitize(long_name)
    assert sanitized == long_name.strip()


def test_a5_ascii_and_whitespace_sanitization():
    """
    Test A.5: Normal ASCII names and whitespace stripping remain functional.
    """
    assert sanitize("  John Doe  ") == "John Doe"
    assert sanitize(None) == ""
    assert sanitize("") == ""


# ---------------------------------------------------------------------------
# Test B: End-to-End Database Persistence
# ---------------------------------------------------------------------------

def test_b1_indic_name_database_roundtrip(test_db):
    """
    Test B.1: Inserting an Assamese name into the applications table preserves
    the exact Unicode characters upon retrieval.
    """
    cur = test_db.cursor()
    name = "ৰমেন বৰুৱা"
    cur.execute("""
        INSERT INTO applications (
            application_no, applicant_name, mobile, dob, village, block,
            bank_account, ifsc, submitted_at, status
        ) VALUES (
            'SSP_UNI_01', %s, '9800000501', '1955-08-15',
            'সোণাৰী', 'Sonari', '1234567890', 'SBIN0001234',
            '2026-03-01 10:00:00', 'PENDING'
        )
    """, (name,))
    test_db.commit()

    cur.execute("SELECT applicant_name, village FROM applications WHERE application_no = 'SSP_UNI_01'")
    row = cur.fetchone()
    assert row[0] == name, f"Retrieved applicant_name '{row[0]}' does not match '{name}'"
    assert row[1] == "সোণাৰী", f"Retrieved village '{row[1]}' does not match 'সোণাৰী'"


def test_b2_submission_journey_preserves_indic_name(client, test_db):
    """
    Test B.2: Full form submission flow preserves Indic name without corruption.
    """
    mobile = "9800000502"
    indic_name = "লক্ষ্মী শইকীয়া"

    # Set up verified session with form data
    with client.session_transaction() as sess:
        sess["verified_mobile"] = mobile
        sess["doc_path"] = "uploads/test.pdf"
        sess["form_data"] = {
            "applicant_name": indic_name,
            "dob": "15/08/1955",
            "village": "Namti",
            "block": "Namti",
            "gender": "Female",
            "marital_status": "Widowed",
            "bank_account": "987654321012",
            "ifsc": "SBIN0005678"
        }

    resp = client.post("/declaration", follow_redirects=True)
    assert resp.status_code == 200

    cur = test_db.cursor()
    cur.execute("SELECT applicant_name FROM applications WHERE mobile = %s", (mobile,))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == indic_name, f"Expected '{indic_name}', but database stored '{row[0]}'"


# ---------------------------------------------------------------------------
# Test C: PDF Acknowledgment Generation
# ---------------------------------------------------------------------------

def test_c1_indic_name_pdf_generation_does_not_replace_with_question_mark():
    """
    Test C.1: PDF generation with an Indic name must NOT replace the name with '?'.
    Defect on baseline: Helvetica font raises FPDFUnicodeEncodingException,
    which is caught and replaced by '?'.
    """
    row = (
        "SSP26999901",
        "ৰমেন বৰুৱা",
        "9800000501",
        date(1955, 8, 15),
        "Sonari",
        "Sonari",
        "1234567890",
        "SBIN0001234",
        datetime(2026, 3, 1, 10, 0, 0),
        "PENDING"
    )

    pdf_bytes = generate_acknowledgment(cur_or_row=row)
    assert pdf_bytes.startswith(b"%PDF")

    # Extract text from generated PDF
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    full_text = "".join(page.extract_text() for page in reader.pages)

    # Must NOT contain 'Applicant Name ?'
    assert "Applicant Name ?" not in full_text, (
        "Baseline defect detected: Applicant name was replaced with '?' in PDF!"
    )
    # Must preserve the actual name
    assert "ৰমেন বৰুৱা" in full_text, (
        f"Generated PDF does not contain the citizen's original Indic name: {full_text}"
    )


def test_c2_devanagari_pdf_generation_preserves_text():
    """
    Test C.2: PDF generation with a Devanagari name preserves the characters.
    """
    row = (
        "SSP26999902",
        "रामेश्वर प्रसाद शर्मा",
        "9800000502",
        date(1958, 5, 20),
        "Borgaon",
        "Borgaon",
        "444455556666",
        "SBIN0004444",
        datetime(2026, 3, 2, 11, 0, 0),
        "PENDING"
    )

    pdf_bytes = generate_acknowledgment(cur_or_row=row)
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    full_text = "".join(page.extract_text() for page in reader.pages)

    assert "Applicant Name ?" not in full_text
    assert "रामेश्वर प्रसाद शर्मा" in full_text


def test_c3_english_name_pdf_generation_preserved():
    """
    Test C.3: Standard ASCII / English names continue to render perfectly.
    """
    row = (
        "SSP26999903",
        "Bhaben Chandra Hazarika",
        "9800000503",
        date(1960, 1, 1),
        "Khelua",
        "Khelua",
        "777788889999",
        "SBIN0007777",
        datetime(2026, 3, 3, 12, 0, 0),
        "PENDING"
    )

    pdf_bytes = generate_acknowledgment(cur_or_row=row)
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    full_text = "".join(page.extract_text() for page in reader.pages)

    assert "Applicant Name ?" not in full_text
    assert "Bhaben Chandra Hazarika" in full_text
    assert "SSP26999903" in full_text
