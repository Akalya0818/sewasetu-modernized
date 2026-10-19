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


# ---------------------------------------------------------------------------
# Test F: Unresolved Eligibility Edge Cases & Config Failure Safety
# ---------------------------------------------------------------------------

def test_f1_unresolved_eligibility_and_boundary_age(test_db):
    """
    Test F.1: Edge cases for applicant eligibility:
    1. NULL dob: Unresolved age eligibility -> Must NOT be approved; remain PENDING.
    2. Malformed dob string: Invalid data -> Must NOT crash job or be approved; remain PENDING.
    3. Applicant aged 59 on submission date who turned 60 before cron run:
       Statutory rule requires age >= 60 ON submission date -> Must NOT be approved; remain PENDING.
    """
    now = datetime(2026, 4, 1, 2, 0, 0)
    overdue_time = now - timedelta(days=20)  # 2026-03-12

    cur = test_db.cursor()
    # 1. NULL dob
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_F1_NULLDOB', 'Missing DOB Citizen', '9800000091', NULL, 'PENDING', %s)
    """, (overdue_time,))

    # 2. Corrupt / invalid dob format
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_F1_CORRUPT', 'Corrupt DOB Citizen', '9800000092', 'NOT_A_DATE', 'PENDING', %s)
    """, (overdue_time,))

    # 3. Turns 60 on 2026-03-25. On submission date (2026-03-12), was 59 years and 352 days old.
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_F1_AGE_SUB', 'Turns 60 Later', '9800000093', '1966-03-25', 'PENDING', %s)
    """, (overdue_time,))
    test_db.commit()

    run_deemed_approval_job(test_db, current_time=now)

    cur.execute("SELECT application_no, status FROM applications WHERE application_no LIKE 'SSP_F1%' ORDER BY application_no")
    results = dict(cur.fetchall())

    assert results['SSP_F1_NULLDOB'] == 'PENDING', "Record with NULL DOB must not be auto-approved"
    assert results['SSP_F1_CORRUPT'] == 'PENDING', "Record with corrupt DOB must not be auto-approved"
    assert results['SSP_F1_AGE_SUB'] == 'PENDING', "Applicant underage on submission date must not be auto-approved"


def test_f2_missing_configuration_and_missing_required_settings(tmp_path):
    """
    Test F.2: Config loading must fail safely and clearly if configuration is missing,
    malformed, or incomplete. Default settings must NOT silently replace failures.
    """
    import deemed_approval

    # 1. Non-existent config directory -> FileNotFoundError
    with pytest.raises(FileNotFoundError):
        deemed_approval.load_config(base_dir=str(tmp_path / "nonexistent"))

    # 2. Non-existent CONFIG_PATH env var -> FileNotFoundError
    with patch.dict(os.environ, {"CONFIG_PATH": str(tmp_path / "missing.ini")}):
        with pytest.raises(FileNotFoundError, match="CONFIG_PATH"):
            deemed_approval.load_config()

    # Helper to create config file
    def create_cfg(content):
        d = tmp_path / f"cfg_{hash(content)}"
        cfg_dir = d / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        ini_file = cfg_dir / "app.ini"
        ini_file.write_text(content, encoding="utf-8")
        return str(d)

    # 3. Missing required sections
    d_no_db = create_cfg("[pension]\nsla_days=15\nmin_age=60\nscheme_deadline=2026-10-31 23:59\n[app]\nsms_gateway_url=http://smsgw:8025\n")
    with pytest.raises(ValueError, match="missing required section \\[database\\]"):
        deemed_approval.load_config(base_dir=d_no_db)

    d_no_pension = create_cfg("[database]\nhost=db\nport=5432\nname=sewasetu\nuser=sewasetu\npassword=sewasetu123\n[app]\nsms_gateway_url=http://smsgw:8025\n")
    with pytest.raises(ValueError, match="missing required section \\[pension\\]"):
        deemed_approval.load_config(base_dir=d_no_pension)

    d_no_app = create_cfg("[database]\nhost=db\nport=5432\nname=sewasetu\nuser=sewasetu\npassword=sewasetu123\n[pension]\nsla_days=15\nmin_age=60\nscheme_deadline=2026-10-31 23:59\n")
    with pytest.raises(ValueError, match="missing required section \\[app\\]"):
        deemed_approval.load_config(base_dir=d_no_app)

    # 4. Missing required settings within sections
    d_no_sla = create_cfg("[database]\nhost=db\nport=5432\nname=sewasetu\nuser=sewasetu\npassword=sewasetu123\n[pension]\nmin_age=60\nscheme_deadline=2026-10-31 23:59\n[app]\nsms_gateway_url=http://smsgw:8025\n")
    with pytest.raises(ValueError, match="missing required setting 'sla_days'"):
        deemed_approval.load_config(base_dir=d_no_sla)

    d_no_min_age = create_cfg("[database]\nhost=db\nport=5432\nname=sewasetu\nuser=sewasetu\npassword=sewasetu123\n[pension]\nsla_days=15\nscheme_deadline=2026-10-31 23:59\n[app]\nsms_gateway_url=http://smsgw:8025\n")
    with pytest.raises(ValueError, match="missing required setting 'min_age'"):
        deemed_approval.load_config(base_dir=d_no_min_age)

    d_no_deadline = create_cfg("[database]\nhost=db\nport=5432\nname=sewasetu\nuser=sewasetu\npassword=sewasetu123\n[pension]\nsla_days=15\nmin_age=60\n[app]\nsms_gateway_url=http://smsgw:8025\n")
    with pytest.raises(ValueError, match="missing required setting 'scheme_deadline'"):
        deemed_approval.load_config(base_dir=d_no_deadline)

    d_no_smsgw = create_cfg("[database]\nhost=db\nport=5432\nname=sewasetu\nuser=sewasetu\npassword=sewasetu123\n[pension]\nsla_days=15\nmin_age=60\nscheme_deadline=2026-10-31 23:59\n[app]\nsms_gateway_url=\n")
    with pytest.raises(ValueError, match="missing required setting 'sms_gateway_url'"):
        deemed_approval.load_config(base_dir=d_no_smsgw)

    # 5. Invalid values
    d_bad_int = create_cfg("[database]\nhost=db\nport=5432\nname=sewasetu\nuser=sewasetu\npassword=sewasetu123\n[pension]\nsla_days=invalid_number\nmin_age=60\nscheme_deadline=2026-10-31 23:59\n[app]\nsms_gateway_url=http://smsgw:8025\n")
    with pytest.raises(ValueError, match="expected integer setting"):
        deemed_approval.load_config(base_dir=d_bad_int)

    d_bad_date = create_cfg("[database]\nhost=db\nport=5432\nname=sewasetu\nuser=sewasetu\npassword=sewasetu123\n[pension]\nsla_days=15\nmin_age=60\nscheme_deadline=invalid_date_format\n[app]\nsms_gateway_url=http://smsgw:8025\n")
    with pytest.raises(ValueError, match="invalid scheme_deadline format"):
        deemed_approval.load_config(base_dir=d_bad_date)


def test_f3_subprocess_fails_on_missing_config_without_db_connection(tmp_path):
    """
    Test F.3: Script executed via subprocess with invalid CONFIG_PATH exits with
    code 1, reports a clear error to stderr, and never connects to PostgreSQL.
    """
    env = os.environ.copy()
    env["CONFIG_PATH"] = str(tmp_path / "nonexistent_app.ini")
    result = subprocess.run(
        [sys.executable, SCRIPT_PATH],
        capture_output=True,
        text=True,
        env=env
    )
    assert result.returncode == 1, "Script must exit with code 1 when configuration is missing"
    assert "Configuration error" in result.stderr
    assert "Database connection" not in result.stderr, "Script must not attempt DB connection when config fails"


# ---------------------------------------------------------------------------
# Test G: Concurrency & Zero-Rowcount Conditional Update Handling
# ---------------------------------------------------------------------------

def test_g_concurrency_conditional_update_zero_rowcount(test_db):
    """
    Test G: Concurrency protection via rowcount on conditional UPDATE:
    Simulates a race condition where candidate records are fetched as PENDING,
    but between candidate selection and UPDATE execution, a concurrent process
    (e.g., an officer adjudication) updates one of the records.
    The conditional update (WHERE id = %s AND status = 'PENDING') affects 0 rows (rowcount == 0).
    Verifications:
    1. The race-lost record is NOT counted as approved.
    2. No SMS notification is queued or dispatched for the race-lost record.
    3. The other eligible candidate is approved and notified normally.
    4. The concurrent officer adjudication on the contested record is preserved.
    """
    import deemed_approval

    now = datetime(2026, 4, 1, 2, 0, 0)
    overdue_time = now - timedelta(days=20)

    cur = test_db.cursor()
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_G1_WIN', 'Citizen First', '9800000101', '1960-01-01', 'PENDING', %s)
    """, (overdue_time,))
    cur.execute("""
        INSERT INTO applications (application_no, applicant_name, mobile, dob, status, submitted_at)
        VALUES ('SSP_G2_RACE', 'Citizen Contested', '9800000102', '1960-01-01', 'PENDING', %s)
    """, (overdue_time,))
    test_db.commit()

    cur.execute("SELECT id FROM applications WHERE application_no = 'SSP_G2_RACE'")
    g2_id = cur.fetchone()[0]

    # Intercept execute on the cursor wrapper to simulate concurrent modification right before G2's deemed approval UPDATE
    orig_cursor_fn = test_db.cursor
    real_cur = orig_cursor_fn()

    class InterceptingCursorWrapper(type(real_cur)):
        def execute(self, query, params=None):
            # If this is the deemed approval UPDATE for SSP_G2, simulate concurrent officer approval first
            if "UPDATE applications" in query and "status = 'PENDING'" in query and params and g2_id in params:
                # Concurrent officer approves the record immediately before this worker tries to deemed-approve it
                self.cursor.execute(
                    "UPDATE applications SET status = 'APPROVED', decided_by = 'OFFICER_CONCURRENT' WHERE id = ?",
                    (g2_id,)
                )
            return super().execute(query, params)

    def intercepting_cursor():
        return InterceptingCursorWrapper(test_db.conn.cursor())

    with patch.object(test_db, 'cursor', side_effect=intercepting_cursor):
        with patch('requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            approved_count = deemed_approval.process_deemed_approvals(test_db, now=now)

            # 1. Only SSP_G1_WIN should be deemed approved; SSP_G2_RACE affected 0 rows
            assert approved_count == 1, f"Expected 1 deemed approval, got {approved_count}"

            # 2. SMS notification verification: exactly 1 SMS sent (for G1 only)
            assert mock_post.call_count == 1, f"Expected 1 SMS sent, got {mock_post.call_count}"
            sent_payload = mock_post.call_args[1]['json']
            assert sent_payload['to'] == '9800000101', "SMS must only be sent to G1"
            assert 'SSP_G1_WIN' in sent_payload['text']

    # 3. Database verification
    cur.execute("SELECT application_no, status, decided_by FROM applications WHERE application_no LIKE 'SSP_G%' ORDER BY application_no")
    records = dict((r[0], (r[1], r[2])) for r in cur.fetchall())

    assert records['SSP_G1_WIN'] == ('DEEMED_APPROVED', 'RTPS-AUTO'), "G1 must be deemed approved by RTPS-AUTO"
    assert records['SSP_G2_RACE'] == ('APPROVED', 'OFFICER_CONCURRENT'), "Contested record must retain concurrent officer decision"


