"""test_priority2_deemed_approval.py - Regression tests for statutory deemed approvals (RTPS Act).

Safety notice:
All tests use synthetic records in an isolated in-memory test database.
No connection to seed.sql or real citizen databases.
External SMS requests are mocked.
"""

import os
import sys
import subprocess
import configparser
from datetime import datetime, timedelta, date
from unittest.mock import patch, MagicMock
import pytest

SCRIPT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app', 'scripts'))
SCRIPT_PATH = os.path.join(SCRIPT_DIR, 'deemed_approval.py')
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))


# ---------------------------------------------------------------------------
# Test A: Configuration Loading & Baseline Crash Reproduction
# ---------------------------------------------------------------------------

def test_a1_baseline_config_loading_crashes_on_settings_ini():
    """
    Test A.1: Reproduce the baseline failure where deemed_approval.py attempts
    to load 'config/settings.ini' instead of 'config/app.ini'.
    Defect: Crashes with FileNotFoundError.
    """
    # Execute the script directly via subprocess as cron does
    result = subprocess.run(
        [sys.executable, SCRIPT_PATH],
        capture_output=True,
        text=True
    )
    # On baseline: exit code is 1, and stderr contains FileNotFoundError: settings.ini
    assert result.returncode == 0, f"Script crashed on execution with error:\n{result.stderr}"
    assert "FileNotFoundError" not in result.stderr
    assert "settings.ini" not in result.stderr


def test_a2_config_loading_from_app_ini():
    """
    Test A.2: Verify the job can locate and load config/app.ini dynamically
    relative to BASE_DIR without depending on developer-specific absolute paths.
    """
    config_path = os.path.join(APP_DIR, 'config', 'app.ini')
    assert os.path.exists(config_path), f"config/app.ini must exist at {config_path}"
    
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    assert cfg.has_section('pension'), "app.ini must contain [pension] section"
    assert cfg.getint('pension', 'sla_days') == 15, "Statutory SLA must be 15 days"
    assert cfg.getint('pension', 'min_age') == 60, "Statutory minimum age must be 60"
    assert cfg.has_option('pension', 'scheme_deadline'), "app.ini must specify scheme_deadline"


# ---------------------------------------------------------------------------
# Synthetic Test Helper for Processing Deemed Approvals
# ---------------------------------------------------------------------------

def run_deemed_approval_job(test_db, current_time=None):
    """
    Helper to execute the deemed approval logic against the isolated test_db.
    Imports and runs process_deemed_approvals from deemed_approval module.
    """
    if current_time is None:
        current_time = datetime.now()
        
    # Attempt to import deemed_approval
    # On baseline: import fails immediately with FileNotFoundError: settings.ini
    sys.path.insert(0, SCRIPT_DIR)
    import deemed_approval
    
    with patch('requests.post') as mock_post:
        mock_post.return_value.status_code = 200
        # Call process function or main
        if hasattr(deemed_approval, 'process_deemed_approvals'):
            count = deemed_approval.process_deemed_approvals(test_db, now=current_time)
        else:
            # Baseline main() connects to hardcoded psycopg2, so we patch connect
            with patch('psycopg2.connect', return_value=test_db):
                count = deemed_approval.main()
        return count


# ---------------------------------------------------------------------------
# Test B: Statutory SLA Boundary & Cutoff Calculation
# ---------------------------------------------------------------------------

def test_b_sla_boundary_and_scheme_window(test_db):
    """
    Test B: Statutory SLA duration and boundary conditions.
    Evidence: app.ini [pension] sla_days = 15; scheme_deadline = 2026-10-31 23:59.
    Boundary test relative to reference time T:
    - App 1: submitted at T - 16 days (older than SLA cutoff) -> DEEMED_APPROVED
    - App 2: submitted at T - 15 days + 1 hour (inside SLA window, not overdue) -> REMAINS PENDING
    - App 3: submitted at T - 15 days exactly -> REMAINS PENDING (exclusive boundary)
    - App 4: submitted after scheme_deadline -> REMAINS PENDING / not auto-approved
    """
    now = datetime(2026, 4, 1, 2, 0, 0)
    cutoff = now - timedelta(days=15)  # 2026-03-17 02:00:00

    cur = test_db.cursor()
    # App 1: Overdue (16 days old, eligible age 65)
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_B1', 'Eligible Overdue', '9800000001', '1960-01-01', 'PENDING', %s)
    """, (now - timedelta(days=16),))

    # App 2: Not overdue (14 days old, eligible age 65)
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_B2', 'Eligible Recent', '9800000002', '1960-01-01', 'PENDING', %s)
    """, (now - timedelta(days=14),))

    # App 3: Exactly at cutoff (15 days old)
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_B3', 'Eligible Exactly Cutoff', '9800000003', '1960-01-01', 'PENDING', %s)
    """, (cutoff,))

    # App 4: Submitted after scheme deadline (2026-11-05, deadline 2026-10-31)
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_B4', 'Outside Scheme Window', '9800000004', '1960-01-01', 'PENDING', '2026-11-05 10:00:00')
    """, ())
    test_db.commit()

    run_deemed_approval_job(test_db, current_time=now)

    # Verify outcomes
    cur.execute("SELECT application_no, status FROM applications WHERE application_no LIKE 'SSP_B%' ORDER BY application_no")
    results = dict(cur.fetchall())
    
    assert results['SSP_B1'] == 'DEEMED_APPROVED', "Overdue eligible record (>15 days) must be DEEMED_APPROVED"
    assert results['SSP_B2'] == 'PENDING', "Recent record (<15 days) must remain PENDING"
    assert results['SSP_B3'] == 'PENDING', "Record exactly at 15 days cutoff must remain PENDING"
    assert results['SSP_B4'] == 'PENDING', "Record submitted outside scheme deadline must not be auto-approved"


# ---------------------------------------------------------------------------
# Test C: Eligibility Rules (Age, Duplicates, Adjudicated Records)
# ---------------------------------------------------------------------------

def test_c_eligibility_verification(test_db):
    """
    Test C: Verify that deemed approvals do NOT indiscriminately approve ineligible records:
    1. Overdue applicant aged >= 60 on submission date -> DEEMED_APPROVED
    2. Overdue applicant aged < 60 on submission date -> MUST NOT be auto-approved (remains PENDING or flagged)
    3. Duplicate active applications for the same mobile:
       Only one application may be approved; duplicate active submissions must not both be approved.
    4. Application already decided by officer (APPROVED or REJECTED) -> MUST NOT be altered.
    5. Non-overdue application -> MUST NOT be altered.
    """
    now = datetime(2026, 4, 1, 2, 0, 0)
    overdue_time = now - timedelta(days=20)
    recent_time = now - timedelta(days=5)

    cur = test_db.cursor()
    # 1. Overdue, age 65 -> Eligible
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_C1_ELIG', 'Eligible Senior', '9800000010', '1960-01-01', 'PENDING', %s)
    """, (overdue_time,))

    # 2. Overdue, age 45 at submission (born 1981) -> Ineligible (underage)
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_C2_UNDER', 'Underage Person', '9800000020', '1981-01-01', 'PENDING', %s)
    """, (overdue_time,))

    # 3. Duplicate active records for same mobile (9800000030)
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_C3_DUP1', 'Citizen A First', '9800000030', '1960-01-01', 'PENDING', %s)
    """, (overdue_time - timedelta(days=2),))
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_C3_DUP2', 'Citizen A Second', '9800000030', '1960-01-01', 'PENDING', %s)
    """, (overdue_time,))

    # 4. Overdue record already REJECTED by an officer
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at, decided_at, decided_by)
        VALUES ('SSP_C4_REJ', 'Already Rejected', '9800000040', '1960-01-01', 'REJECTED', %s, %s, 'OFFICER_01')
    """, (overdue_time, overdue_time + timedelta(days=3)))

    # 5. Overdue record already APPROVED by an officer
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at, decided_at, decided_by)
        VALUES ('SSP_C5_APP', 'Already Approved', '9800000050', '1960-01-01', 'APPROVED', %s, %s, 'OFFICER_02')
    """, (overdue_time, overdue_time + timedelta(days=3)))

    # 6. Non-overdue application (recent)
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_C6_RECENT', 'Recent Applicant', '9800000060', '1960-01-01', 'PENDING', %s)
    """, (recent_time,))
    test_db.commit()

    run_deemed_approval_job(test_db, current_time=now)

    cur.execute("SELECT application_no, status, decided_by FROM applications WHERE application_no LIKE 'SSP_C%' ORDER BY application_no")
    records = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    assert records['SSP_C1_ELIG'][0] == 'DEEMED_APPROVED', "Eligible overdue record must be approved"
    assert records['SSP_C2_UNDER'][0] != 'DEEMED_APPROVED', "Underage applicant (<60) must NOT be deemed approved"
    
    # Duplicate handling: Both duplicate submissions must not both be deemed approved
    dup1_status = records['SSP_C3_DUP1'][0]
    dup2_status = records['SSP_C3_DUP2'][0]
    assert not (dup1_status == 'DEEMED_APPROVED' and dup2_status == 'DEEMED_APPROVED'), \
        "Duplicate active records for the same mobile must not both be auto-approved"

    assert records['SSP_C4_REJ'][0] == 'REJECTED', "Previously rejected record must retain REJECTED status"
    assert records['SSP_C4_REJ'][1] == 'OFFICER_01', "Officer audit trail must be preserved"

    assert records['SSP_C5_APP'][0] == 'APPROVED', "Previously approved record must retain APPROVED status"
    assert records['SSP_C5_APP'][1] == 'OFFICER_02', "Officer audit trail must be preserved"

    assert records['SSP_C6_RECENT'][0] == 'PENDING', "Non-overdue record must remain PENDING"


# ---------------------------------------------------------------------------
# Test D: Idempotency & Transaction Safety
# ---------------------------------------------------------------------------

def test_d_idempotency_and_transaction_safety(test_db):
    """
    Test D: Idempotency:
    - Running the job twice consecutively processes eligible records on run 1.
    - Run 2 must find 0 records to approve, must not alter decided_at or audit fields,
      and must not send duplicate SMS notifications.
    """
    now = datetime(2026, 4, 1, 2, 0, 0)
    overdue_time = now - timedelta(days=20)

    cur = test_db.cursor()
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_D1', 'Idempotent Senior', '9800000070', '1955-05-15', 'PENDING', %s)
    """, (overdue_time,))
    test_db.commit()

    # First run
    run1_count = run_deemed_approval_job(test_db, current_time=now)
    
    cur.execute("SELECT status, decided_at, decided_by FROM applications WHERE application_no = 'SSP_D1'")
    r1 = cur.fetchone()
    assert r1[0] == 'DEEMED_APPROVED'
    run1_decided_at = r1[1]

    # Second run immediately after
    run2_count = run_deemed_approval_job(test_db, current_time=now + timedelta(minutes=5))
    assert run2_count == 0, "Second execution of deemed approval must be idempotent and approve 0 records"

    cur.execute("SELECT status, decided_at, decided_by FROM applications WHERE application_no = 'SSP_D1'")
    r2 = cur.fetchone()
    assert r2[0] == 'DEEMED_APPROVED'
    assert r2[1] == run1_decided_at, "decided_at timestamp must not be overwritten on second run"
    assert r2[2] == 'RTPS-AUTO'


# ---------------------------------------------------------------------------
# Test E: Audit Integrity
# ---------------------------------------------------------------------------

def test_e_audit_integrity(test_db):
    """
    Test E: Audit integrity:
    - When an eligible application is approved, decided_by must be 'RTPS-AUTO'.
    - decided_at must be populated with the adjudication timestamp.
    - Status must be 'DEEMED_APPROVED'.
    - Skipped or pending records must retain their original NULL audit fields.
    """
    now = datetime(2026, 4, 1, 2, 0, 0)
    overdue_time = now - timedelta(days=20)
    recent_time = now - timedelta(days=5)

    cur = test_db.cursor()
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_E1_OVERDUE', 'Audit Test Senior', '9800000080', '1950-01-01', 'PENDING', %s)
    """, (overdue_time,))
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_E2_RECENT', 'Audit Test Recent', '9800000081', '1950-01-01', 'PENDING', %s)
    """, (recent_time,))
    test_db.commit()

    run_deemed_approval_job(test_db, current_time=now)

    cur.execute("SELECT application_no, status, decided_at, decided_by FROM applications WHERE application_no LIKE 'SSP_E%' ORDER BY application_no")
    rows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

    # Approved record audit fields
    status_e1, decided_at_e1, decided_by_e1 = rows['SSP_E1_OVERDUE']
    assert status_e1 == 'DEEMED_APPROVED'
    assert decided_by_e1 == 'RTPS-AUTO', "Audit decided_by must be 'RTPS-AUTO'"
    assert decided_at_e1 is not None, "decided_at must be recorded"

    # Skipped record audit fields
    status_e2, decided_at_e2, decided_by_e2 = rows['SSP_E2_RECENT']
    assert status_e2 == 'PENDING'
    assert decided_at_e2 is None, "Pending records must have NULL decided_at"
    assert decided_by_e2 is None or decided_by_e2 == '', "Pending records must have NULL decided_by"
