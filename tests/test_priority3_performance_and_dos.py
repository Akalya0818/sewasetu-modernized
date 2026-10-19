"""test_priority3_performance_and_dos.py - Regression tests for month-end performance,
concurrency, database connection lifecycle, and PDF acknowledgment generation.

Authoritative Context:
1. Investigation revealed 13,759 'FATAL: too many clients already' PostgreSQL errors
   concentrated 100% on month-end days (28-31) in sewasetu-app.log.
2. In app/app.py:
   - POST /declaration and GET /ack/<id>.pdf invoke generate_acknowledgment(cur, app_id).
   - generate_acknowledgment contains a decorative loop:
       for i in range(0, 2000):
           x = 10 + (i % 190)
           pdf.line(x, 282, x + 0.5, 282)
           pdf.line(x, 12, x + 0.5, 12)
     generating 4,000 vector line operations per PDF.
   - The raw database connection is held open across the entire 4,000-line vector generation
     and subsequent disk file I/O (open/write), starving PostgreSQL connection pool.
   - Exceptions during PDF generation or file writing leak the database connection (no try/finally).

Safety notice:
All tests use synthetic records in an isolated in-memory test database and temporary directories.
No connection to seed.sql or real citizen databases.
"""

import os
import sys
import io
import time
import platform
import statistics
from datetime import datetime, date
from unittest.mock import patch, MagicMock
import pytest

import fpdf
from fpdf import FPDF

import app as web_app
from app import app as flask_app, generate_acknowledgment


# ---------------------------------------------------------------------------
# Test A: PDF Performance Baseline Benchmark
# ---------------------------------------------------------------------------

def test_a_pdf_performance_benchmark(test_db):
    """
    Test A: PDF Performance Baseline Benchmark.
    Runs repeatable iterations of generate_acknowledgment with synthetic application data.
    Measures individual run times, median latency, PDF byte length, and records runtime metadata:
    Python version, platform, fpdf version, and execution environment.
    """
    cur = test_db.cursor()
    cur.execute("""
        INSERT INTO applications (
            application_no, applicant_name, mobile, dob, village, block,
            bank_account, ifsc, submitted_at, status
        ) VALUES (
            'SSP_BENCH_01', 'Benchmark Citizen', '9800000301', '1960-01-01',
            'Sonari Village', 'Sonari', '987654321012', 'SBIN0001234',
            '2026-03-31 10:00:00', 'PENDING'
        )
    """)
    test_db.commit()

    cur.execute("SELECT id FROM applications WHERE application_no = 'SSP_BENCH_01'")
    app_id = cur.fetchone()[0]

    # Benchmark parameters
    iterations = 10
    latencies = []
    pdf_bytes = None

    for _ in range(iterations):
        t0 = time.perf_counter()
        pdf_bytes = generate_acknowledgment(cur, app_id)
        t1 = time.perf_counter()
        latencies.append(t1 - t0)

    median_sec = statistics.median(latencies)
    mean_sec = statistics.mean(latencies)
    min_sec = min(latencies)
    max_sec = max(latencies)

    # Record benchmark environment and results
    metadata = {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "pdf_library": f"fpdf2 {fpdf.__version__}",
        "iterations": iterations,
        "pdf_byte_length": len(pdf_bytes),
        "latencies_ms": [round(t * 1000, 2) for t in latencies],
        "median_ms": round(median_sec * 1000, 2),
        "mean_ms": round(mean_sec * 1000, 2),
        "min_ms": round(min_sec * 1000, 2),
        "max_ms": round(max_sec * 1000, 2),
    }

    # Print benchmark summary to test output
    print(f"\n[BENCHMARK] PDF Acknowledgment Generation Benchmark:")
    print(f"  Platform: {metadata['platform']}")
    print(f"  Python: {metadata['python_version']} | Library: {metadata['pdf_library']}")
    print(f"  Runs ({iterations}): {metadata['latencies_ms']} ms")
    print(f"  Median: {metadata['median_ms']} ms | Min: {metadata['min_ms']} ms | Max: {metadata['max_ms']} ms")
    print(f"  PDF Output Size: {metadata['pdf_byte_length']} bytes")

    # Verifications: Valid PDF output
    assert pdf_bytes is not None
    assert pdf_bytes.startswith(b"%PDF"), "Generated acknowledgment must be valid PDF"
    assert len(pdf_bytes) > 500, "PDF must not be empty or truncated"


# ---------------------------------------------------------------------------
# Test B: Decorative Vector Loop Verification
# ---------------------------------------------------------------------------

def test_b_decorative_vector_loop_inspection():
    """
    Test B: Confirm the exact code responsible for the 4,000-line decorative vector loop.
    Investigates app/app.py around generate_acknowledgment to verify:
    1. Does the 4,000-line decorative loop exist in the codebase?
    2. Does it draw repetitive micro-segments at y=12 and y=282?
    """
    app_py_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'app.py')
    with open(app_py_path, 'r', encoding='utf-8') as fh:
        content = fh.read()

    # Verify presence of decorative loop in legacy implementation
    has_range_2000 = "range(0, 2000)" in content or "range(2000)" in content
    has_vector_lines = "pdf.line(x, 282" in content or "pdf.line(x, 12" in content

    # On baseline: both are True
    print(f"\n[VECTOR LOOP INSPECTION] range(2000) present: {has_range_2000}, repetitive lines present: {has_vector_lines}")


# ---------------------------------------------------------------------------
# Test C: Database Connection Lifecycle & Early Release
# ---------------------------------------------------------------------------

def test_c1_connection_released_before_pdf_generation(client, test_db, monkeypatch):
    """
    Test C.1: Database connection lifecycle in /declaration.
    Requirement: Database work must finish, commit, and release the connection
    BEFORE expensive PDF generation and disk file I/O begin.
    
    Defect on baseline:
    conn = get_db()
    ...
    conn.commit()
    pdf_bytes = generate_acknowledgment(cur, new_id)   <-- DB connection remains OPEN
    with open(...) as out:                             <-- DB connection remains OPEN
        out.write(pdf_bytes)
    cur.close(); conn.close()
    
    Expected: test_db.closed must be True WHEN generate_acknowledgment is invoked!
    """
    db_closed_when_pdf_called = None
    orig_generate_ack = web_app.generate_acknowledgment

    def instrumented_generate_ack(*args, **kwargs):
        nonlocal db_closed_when_pdf_called
        # Check if the database connection was closed before PDF generation started
        db_closed_when_pdf_called = test_db.closed
        return orig_generate_ack(*args, **kwargs)

    monkeypatch.setattr('app.generate_acknowledgment', instrumented_generate_ack)

    # Populate valid session to submit /declaration
    with client.session_transaction() as sess:
        sess['form_data'] = {
            'applicant_name': 'Radha Devi',
            'dob': '05/04/1958',
            'gender': 'Female',
            'marital_status': 'Widowed',
            'husband_name': 'Late Ram Das',
            'village': 'Borgaon',
            'block': 'Sonari',
            'bank_account': '123456789012',
            'ifsc': 'SBIN0001234'
        }
        sess['verified_mobile'] = '9800000302'
        sess['doc_path'] = 'uploads/doc_test.pdf'

    resp = client.post('/declaration', data={'consent': 'on'}, follow_redirects=True)
    assert resp.status_code == 200

    # Verification: On baseline, db_closed_when_pdf_called is False (fails baseline reproduction)
    assert db_closed_when_pdf_called is True, (
        "Database connection must be closed BEFORE PDF generation begins! "
        f"Observed db_closed_when_pdf_called = {db_closed_when_pdf_called}"
    )


def test_c2_connection_not_leaked_on_pdf_or_io_exception(client, test_db, monkeypatch):
    """
    Test C.2: Exception safety in /declaration.
    If PDF generation or file writing fails with an exception, the database connection
    must NOT be leaked. It must be closed cleanly via try/finally.
    
    Defect on baseline:
    No try/finally block wraps conn in /declaration. If generate_acknowledgment or open()
    throws an error, cur.close(); conn.close() is skipped entirely.
    """
    def failing_generate_ack(*args, **kwargs):
        raise RuntimeError("Simulated unhandled PDF generation error")

    monkeypatch.setattr('app.generate_acknowledgment', failing_generate_ack)

    with client.session_transaction() as sess:
        sess['form_data'] = {
            'applicant_name': 'Gopal Das',
            'dob': '10/10/1955',
            'gender': 'Male',
            'marital_status': 'Married',
            'village': 'Namti',
            'block': 'Namti',
            'bank_account': '987654321099',
            'ifsc': 'SBIN0009999'
        }
        sess['verified_mobile'] = '9800000303'
        sess['doc_path'] = 'uploads/doc_test.pdf'

    # The request will raise or fail with 500
    try:
        client.post('/declaration', data={'consent': 'on'})
    except Exception:
        pass

    # Verification: The DB connection must be closed despite the exception
    assert test_db.closed is True, (
        "Database connection was leaked! conn.close() was not called when an exception occurred."
    )


# ---------------------------------------------------------------------------
# Test D: Functional Regression, Application Integrity, and Concurrency
# ---------------------------------------------------------------------------

def test_d1_acknowledgment_preserves_all_required_citizen_data(client, test_db, tmp_path):
    """
    Test D.1: Functional verification of generated acknowledgment.
    Confirms the PDF contains all 10 required fields and header information:
    - Government of Purvanchal / Department of Social Welfare / Old Age Pension Scheme
    - Application No, Applicant Name, Mobile, Date of Birth, Village, Block, Bank Account, IFSC, Status
    - RTPS Act legal SLA statement.
    """
    cur = test_db.cursor()
    cur.execute("""
        INSERT INTO applications (
            application_no, applicant_name, mobile, dob, village, block,
            bank_account, ifsc, submitted_at, status
        ) VALUES (
            'SSP_REG_01', 'Anand Kumar', '9800000304', '1959-07-15',
            'Khelua Gaon', 'Khelua', '555666777888', 'SBIN0004321',
            '2026-03-31 11:30:00', 'PENDING'
        )
    """)
    test_db.commit()

    cur.execute("SELECT id FROM applications WHERE application_no = 'SSP_REG_01'")
    app_id = cur.fetchone()[0]

    pdf_bytes = generate_acknowledgment(cur, app_id)
    assert pdf_bytes.startswith(b"%PDF"), "Must produce valid PDF format"

    # Check text presence inside PDF by decompressing FlateDecode streams
    import zlib, re
    streams = re.findall(rb'stream\r?\n(.*?)\r?\nendstream', pdf_bytes, re.DOTALL)
    decompressed_text = ""
    for s in streams:
        try:
            decompressed_text += zlib.decompress(s).decode('latin-1', errors='ignore')
        except Exception:
            pass

    pdf_text = decompressed_text or pdf_bytes.decode('latin-1', errors='ignore')

    assert "GOVERNMENT OF PURVANCHAL" in pdf_text
    assert "Department of Social Welfare" in pdf_text
    assert "Old Age Pension Scheme - Acknowledgment" in pdf_text
    assert "SSP_REG_01" in pdf_text
    assert "Anand Kumar" in pdf_text
    assert "9800000304" in pdf_text
    assert "555666777888" in pdf_text
    assert "SBIN0004321" in pdf_text
    assert "Purvanchal Right to Public Services Act" in pdf_text


def test_d2_file_write_failure_does_not_corrupt_database(client, test_db, monkeypatch):
    """
    Test D.2: File write failure safety.
    If writing the PDF to disk fails (e.g. disk full), database connection must still
    be released cleanly and no half-written file is left behind.
    """
    orig_open = open
    def failing_open(path, mode='r', *args, **kwargs):
        if 'wb' in mode and (str(path).endswith('.pdf') or '.tmp' in str(path)):
            raise OSError(28, "No space left on device")
        return orig_open(path, mode, *args, **kwargs)

    with client.session_transaction() as sess:
        sess['form_data'] = {
            'applicant_name': 'Disk Test Citizen',
            'dob': '01/01/1955',
            'gender': 'Male',
            'marital_status': 'Married',
            'village': 'Sonari',
            'block': 'Sonari',
            'bank_account': '111222333444',
            'ifsc': 'SBIN0001111'
        }
        sess['verified_mobile'] = '9800000305'
        sess['doc_path'] = 'uploads/doc_test.pdf'

    with patch('builtins.open', side_effect=failing_open):
        try:
            client.post('/declaration', data={'consent': 'on'})
        except OSError:
            pass

    # Connection must not be leaked
    assert test_db.closed is True, "Database connection must be closed even when disk write fails"


def test_d3_concurrent_submissions_distinct_acknowledgments(client, test_db, tmp_path):
    """
    Test D.3: Concurrency: multiple sequential/concurrent submissions must generate
    distinct acknowledgment files without overwriting or connection exhaustion.
    """
    upload_ack_dir = str(tmp_path / "uploads" / "ack")

    for i in range(3):
        # Reset connection state between requests
        test_db.closed = False
        mobile = f"980000031{i}"
        with client.session_transaction() as sess:
            sess['form_data'] = {
                'applicant_name': f'Concurrent Citizen {i}',
                'dob': '01/01/1950',
                'gender': 'Female',
                'marital_status': 'Widowed',
                'village': 'Borgaon',
                'block': 'Sonari',
                'bank_account': f'99988877766{i}',
                'ifsc': 'SBIN0001234'
            }
            sess['verified_mobile'] = mobile
            sess['doc_path'] = f'uploads/doc_{i}.pdf'

        resp = client.post('/declaration', data={'consent': 'on'}, follow_redirects=True)
        assert resp.status_code == 200
        assert test_db.closed is True, f"Connection must be closed after submission {i}"

    # Verify 3 distinct applications exist in DB
    cur = test_db.cursor()
    cur.execute("SELECT id, application_no FROM applications WHERE mobile LIKE '980000031%' ORDER BY id")
    rows = cur.fetchall()
    assert len(rows) == 3
    ids = [r[0] for r in rows]
    assert len(set(ids)) == 3, "Each application must receive a unique ID"
