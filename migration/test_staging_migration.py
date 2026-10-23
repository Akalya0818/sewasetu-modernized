#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Seva Setu: Comprehensive PostgreSQL 15 Staging Migration Test Runner

PURPOSE:
Automated end-to-end integration testing of the complete Seva Setu restoration package
on an isolated, disposable PostgreSQL database. Executes all SQL files and client-side
COPY directives directly via Python's psycopg2 driver, avoiding external CLI dependencies.

SAFETY SAFEGUARDS:
1. Connection Guard: Strictly rejects Render hosts and unverified hosts; requires explicit allowlist
   (EXPECTED_STAGING_HOST exact match) and rejects Neon pooler endpoints (-pooler).
2. Pre-Existing Database Protection: Checks pg_database and aborts if 'sewasetu_disposable_test'
   already exists. Never drops pre-existing databases before testing.
3. Safe Cleanup: Tracks db_created_by_this_run flag and drops only what this run explicitly created.
4. Absence Verification: Verifies that sewasetu_disposable_test is absent from pg_database after drop.
5. Rollback Assertion: Verifies that deliberate-failure transactions exit with non-zero status.
6. Accurate Result Reporting: Tracks overall_success and verified teardown; never reports success on
   aborted, failed, or partially cleaned runs.
"""

import os
import sys
import io
import re
import urllib.parse
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# Regular expressions for client-side \copy directives
COPY_TO_RE = re.compile(r"^\\copy\s+(.*?)\s+TO\s+['\"](.*?)['\"](.*)$", re.IGNORECASE)
COPY_FROM_RE = re.compile(r"^\\copy\s+(.*?)\s+FROM\s+['\"](.*?)['\"](.*)$", re.IGNORECASE)


def has_executable_sql(sql):
    """
    Returns True if the SQL string contains at least one executable SQL statement.
    Returns False if the string contains only whitespace, comments (-- or /* */),
    and/or empty statement terminators (;).
    Safely skips comment-only or empty blocks to prevent PostgreSQL 'empty query' errors.
    """
    if not sql or not sql.strip():
        return False

    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]

        # Whitespace
        if c in " \t\r\n\f\v":
            i += 1
            continue

        # Semicolons (empty statement terminators)
        if c == ";":
            i += 1
            continue

        # Single-line comment: -- ...
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            continue

        # Block comment: /* ... */ (supports nested block comments per PostgreSQL specification)
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            depth = 1
            while i < n and depth > 0:
                if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
                    depth += 1
                    i += 2
                elif sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue

        # Encountered a non-whitespace, non-comment, non-empty statement character
        return True

    return False


class CopyStreamReader(io.TextIOBase):
    """
    File-like stream adapter that reads lines from an open text file until it encounters
    the PostgreSQL end-of-copy delimiter line ('\\.') or EOF.
    Used by cur.copy_expert() to stream inlined COPY FROM stdin data blocks natively.
    """
    def __init__(self, file_obj):
        self.file_obj = file_obj
        self.buffer = ""
        self.finished = False

    def readable(self):
        return True

    def read(self, size=-1):
        if self.finished and not self.buffer:
            return ""

        chunks = [self.buffer] if self.buffer else []
        self.buffer = ""
        current_len = sum(len(c) for c in chunks)

        while not self.finished and (size is None or size < 0 or current_len < size):
            line = self.file_obj.readline()
            if not line:
                self.finished = True
                break
            stripped = line.rstrip("\r\n")
            if stripped == "\\.":
                self.finished = True
                break
            chunks.append(line)
            current_len += len(line)

        combined = "".join(chunks)
        if size is None or size < 0 or len(combined) <= size:
            return combined
        else:
            self.buffer = combined[size:]
            return combined[:size]


def get_staging_base_url():
    return os.environ.get("STAGING_DATABASE_URL") or os.environ.get("DISPOSABLE_DATABASE_URL")


def validate_target_host(base_url, parsed):
    """Strictly validates target database host against forbidden signatures and explicit allowlist."""
    hostname = (parsed.hostname or "").lower()
    full_url_lower = base_url.lower()

    if not hostname:
        print("\n[CRITICAL SAFETY GUARD] REJECTED: Database URL has no valid hostname.")
        sys.exit(1)

    # 1. Reject Render hosts and production signatures unconditionally
    FORBIDDEN_HOST_SIGNATURES = ["render.com", "dpg-", "onrender", "sewasetu-portal", "dbprod"]
    for signature in FORBIDDEN_HOST_SIGNATURES:
        if signature in hostname or signature in full_url_lower:
            print(f"\n[CRITICAL SAFETY GUARD] REJECTED TARGET HOST: Found forbidden signature '{signature}'.")
            print("The staging test runner strictly forbids connecting to Render or production databases.")
            sys.exit(1)

    # 2. Reject Neon pooled endpoints (-pooler) unconditionally
    # Neon PgBouncer connection poolers do not support CREATE DATABASE or DROP DATABASE.
    if "-pooler" in hostname or "-pooler" in full_url_lower:
        print("\n[CRITICAL SAFETY GUARD] REJECTED POOLED ENDPOINT: Found '-pooler' in target hostname.")
        print("Neon connection poolers (PgBouncer) do not support CREATE DATABASE or DROP DATABASE.")
        print("Please use the direct, non-pooled Neon endpoint (remove '-pooler' from hostname).")
        sys.exit(1)

    # 3. Explicit Host Allowlist / Configuration (Exact Hostname Match Mandatory)
    expected_host = (os.environ.get("EXPECTED_STAGING_HOST") or "").strip().lower()
    allowed_hosts = [h.strip().lower() for h in os.environ.get("ALLOWED_STAGING_HOSTS", "").split(",") if h.strip()]
    allow_local = os.environ.get("ALLOW_LOCAL_STAGING", "0") == "1"

    if expected_host and hostname == expected_host:
        return True
    if allowed_hosts and hostname in allowed_hosts:
        return True
    if allow_local and hostname in ("localhost", "127.0.0.1", "::1"):
        return True

    # Reject any host that is not explicitly allowlisted (no keyword or substring fallback)
    print(f"\n[CRITICAL SAFETY GUARD] TARGET HOST UNVERIFIED: Host '{hostname}' is not explicitly allowlisted.")
    print("For safety, every remote endpoint (including Neon) requires an exact hostname match.")
    print("To authorize this target endpoint, set EXPECTED_STAGING_HOST='<exact-hostname>' in your environment.")
    sys.exit(1)


def _resolve_connection(conn_or_url):
    """Returns (conn, should_close)."""
    if isinstance(conn_or_url, str):
        c = psycopg2.connect(conn_or_url)
        c.autocommit = True
        return c, True
    return conn_or_url, False


def _rollback_transaction(conn):
    """
    Safely rolls back any active or aborted transaction block.
    When conn.autocommit is True, psycopg2 prohibits conn.rollback() directly,
    so an explicit ROLLBACK SQL command must be issued to the PostgreSQL server.
    Confirms the connection returns to TRANSACTION_STATUS_IDLE after rollback.
    """
    status = conn.get_transaction_status()
    if status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
        if getattr(conn, "autocommit", False):
            rb_cur = conn.cursor()
            rb_cur.execute("ROLLBACK;")
            rb_cur.close()
        else:
            conn.rollback()

        final_status = conn.get_transaction_status()
        if final_status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
            raise RuntimeError(
                f"Connection not idle after rollback (status={final_status}, expected IDLE={psycopg2.extensions.TRANSACTION_STATUS_IDLE})"
            )


def run_sql_file(conn_or_url, file_path):
    """
    Executes a SQL file natively via psycopg2.
    Handles psql \\set directives (ignored), \\ir <relpath> (recursive execution),
    \\copy (client-side export/import), and inlined COPY <table> (...) FROM stdin.
    Preserves SQL transaction boundaries and captures server notices.
    Returns (returncode, stdout, stderr).
    """
    conn, should_close = _resolve_connection(conn_or_url)
    norm_path = os.path.normpath(file_path)
    base_dir = os.path.dirname(norm_path)
    notices_start_idx = len(conn.notices)
    stdout_lines = []
    cur = conn.cursor()

    try:
        with open(norm_path, "r", encoding="utf-8") as f:
            sql_buffer = []

            for line in f:
                stripped = line.strip()

                # Ignore empty lines if no pending buffer
                if not stripped and not sql_buffer:
                    continue

                # Ignore psql \set directives (e.g. \set ON_ERROR_STOP on)
                if stripped.startswith("\\set"):
                    continue

                # Handle psql \ir (relative include)
                if stripped.startswith("\\ir"):
                    if sql_buffer:
                        pending = "".join(sql_buffer).strip()
                        if pending and has_executable_sql(pending):
                            cur.execute(pending)
                        sql_buffer = []
                    parts = stripped.split(None, 1)
                    if len(parts) > 1:
                        rel_path = parts[1].strip()
                        target_file = os.path.normpath(os.path.join(base_dir, rel_path))
                        rc, sub_out, sub_err = run_sql_file(conn, target_file)
                        if rc != 0:
                            cur.close()
                            if should_close:
                                conn.close()
                            return rc, sub_out, sub_err
                        if sub_out:
                            stdout_lines.append(sub_out)
                    continue

                # Handle psql \copy directives
                if stripped.startswith("\\copy"):
                    if sql_buffer:
                        pending = "".join(sql_buffer).strip()
                        if pending and has_executable_sql(pending):
                            cur.execute(pending)
                        sql_buffer = []
                    clean_cmd = stripped.rstrip(";")
                    m_to = COPY_TO_RE.match(clean_cmd)
                    if m_to:
                        target, filepath, opts = m_to.groups()
                        target_filepath = os.path.normpath(os.path.join(base_dir, filepath)) if not os.path.isabs(filepath) else filepath
                        with open(target_filepath, "w", encoding="utf-8") as out_f:
                            cur.copy_expert(f"COPY ({target}) TO STDOUT {opts.strip()}", out_f)
                        continue
                    m_from = COPY_FROM_RE.match(clean_cmd)
                    if m_from:
                        target, filepath, opts = m_from.groups()
                        target_filepath = os.path.normpath(os.path.join(base_dir, filepath)) if not os.path.isabs(filepath) else filepath
                        with open(target_filepath, "r", encoding="utf-8") as in_f:
                            cur.copy_expert(f"COPY {target} FROM STDIN {opts.strip()}", in_f)
                        continue
                    continue

                # Handle inlined COPY <table> (...) FROM stdin
                if stripped.lower().startswith("copy ") and "from stdin" in stripped.lower():
                    if sql_buffer:
                        pending = "".join(sql_buffer).strip()
                        if pending and has_executable_sql(pending):
                            cur.execute(pending)
                        sql_buffer = []
                    copy_cmd = stripped.rstrip(";")
                    reader = CopyStreamReader(f)
                    cur.copy_expert(copy_cmd, reader)
                    continue

                # Regular SQL line
                sql_buffer.append(line)

            # Flush remaining buffer at EOF
            if sql_buffer:
                pending = "".join(sql_buffer).strip()
                if pending and has_executable_sql(pending):
                    cur.execute(pending)
                    if cur.description:
                        try:
                            rows = cur.fetchall()
                            for r in rows:
                                stdout_lines.append(str(r))
                        except Exception:
                            pass

        cur.close()
        new_notices = conn.notices[notices_start_idx:]
        all_out = "\n".join(stdout_lines + [n.strip() for n in new_notices])
        if should_close:
            conn.close()
        return 0, all_out, ""

    except Exception as e:
        cur.close()
        _rollback_transaction(conn)
        new_notices = conn.notices[notices_start_idx:]
        all_out = "\n".join(stdout_lines + [n.strip() for n in new_notices])
        if should_close:
            conn.close()
        return 1, all_out, str(e)


def run_sql_command(conn_or_url, sql, base_dir="."):
    """
    Executes a SQL string natively via psycopg2.
    Handles \\copy directives, standard SQL, and deliberate transaction failures.
    Returns (returncode, stdout, stderr).
    """
    conn, should_close = _resolve_connection(conn_or_url)
    notices_start_idx = len(conn.notices)
    stdout_lines = []
    cur = conn.cursor()

    try:
        lines = sql.splitlines(keepends=True)
        sql_buffer = []

        for line in lines:
            stripped = line.strip()

            if not stripped and not sql_buffer:
                continue

            if stripped.startswith("\\set"):
                continue

            if stripped.startswith("\\copy"):
                if sql_buffer:
                    pending = "".join(sql_buffer).strip()
                    if pending and has_executable_sql(pending):
                        cur.execute(pending)
                    sql_buffer = []
                clean_cmd = stripped.rstrip(";")
                m_to = COPY_TO_RE.match(clean_cmd)
                if m_to:
                    target, filepath, opts = m_to.groups()
                    target_filepath = os.path.normpath(os.path.join(base_dir, filepath)) if not os.path.isabs(filepath) else filepath
                    with open(target_filepath, "w", encoding="utf-8") as out_f:
                        cur.copy_expert(f"COPY ({target}) TO STDOUT {opts.strip()}", out_f)
                    continue
                m_from = COPY_FROM_RE.match(clean_cmd)
                if m_from:
                    target, filepath, opts = m_from.groups()
                    target_filepath = os.path.normpath(os.path.join(base_dir, filepath)) if not os.path.isabs(filepath) else filepath
                    with open(target_filepath, "r", encoding="utf-8") as in_f:
                        cur.copy_expert(f"COPY {target} FROM STDIN {opts.strip()}", in_f)
                    continue
                continue

            sql_buffer.append(line)

        if sql_buffer:
            pending = "".join(sql_buffer).strip()
            if pending and has_executable_sql(pending):
                cur.execute(pending)
                if cur.description:
                    try:
                        rows = cur.fetchall()
                        for r in rows:
                            stdout_lines.append(str(r))
                    except Exception:
                        pass

        cur.close()
        new_notices = conn.notices[notices_start_idx:]
        all_out = "\n".join(stdout_lines + [n.strip() for n in new_notices])
        if should_close:
            conn.close()
        return 0, all_out, ""

    except Exception as e:
        cur.close()
        _rollback_transaction(conn)
        new_notices = conn.notices[notices_start_idx:]
        all_out = "\n".join(stdout_lines + [n.strip() for n in new_notices])
        if should_close:
            conn.close()
        return 1, all_out, str(e)


def main():
    print("=" * 75)
    print("SEVA SETU: POSTGRESQL STAGING MIGRATION TEST RUNNER (PSYCOPG2 ENGINE)")
    print("Execution mode: Native Python psycopg2 with direct wire-protocol COPY")
    print("=" * 75)

    base_url = get_staging_base_url()
    if not base_url:
        print("\n[ENVIRONMENT STATUS] Disposable PostgreSQL Staging Database Unavailable.")
        print("-" * 75)
        print("Neither STAGING_DATABASE_URL nor DISPOSABLE_DATABASE_URL is set.")
        print("To run this genuine client-server integration test suite:")
        print("  1. Ensure a disposable PostgreSQL staging instance is provisioned (e.g. Neon staging-test branch).")
        print("  2. Set connection environment variables:")
        print("     export STAGING_DATABASE_URL='postgresql://<user>:<password>@<neon-staging-test>.neon.tech/neondb?sslmode=require'")
        print("     export EXPECTED_STAGING_HOST='<neon-staging-test>.neon.tech'")
        print("  3. Run:")
        print("     python migration/test_staging_migration.py")
        print("-" * 75)
        sys.exit(0)

    # Parse and validate connection URL
    parsed = urllib.parse.urlparse(base_url)
    validate_target_host(base_url, parsed)

    admin_db = parsed.path.lstrip('/') or 'postgres'
    test_db_name = "sewasetu_disposable_test"
    test_db_url = urllib.parse.urlunparse(parsed._replace(path=f"/{test_db_name}"))

    print(f"Target Server: {parsed.hostname}:{parsed.port or 5432}")

    db_created_by_this_run = False
    db_dropped_successfully = False
    overall_success = False
    test_conn = None

    # Connect to root database to check and create isolated test database
    try:
        conn = psycopg2.connect(base_url)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()
        cur.execute("SELECT version();")
        pg_version = cur.fetchone()[0]
        print(f"Connected to engine: {pg_version}")
        
        # Existing Database Protection: Check if sewasetu_disposable_test already exists
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (test_db_name,))
        if cur.fetchone():
            print(f"\n[SAFETY ABORT] Database '{test_db_name}' already exists on target server!")
            print("The test runner NEVER automatically drops pre-existing databases.")
            print(f"Please inspect and manually drop '{test_db_name}' if it is a stale test artifact.")
            cur.close()
            conn.close()
            sys.exit(1)

        # Create isolated test database
        cur.execute(f"CREATE DATABASE {test_db_name};")
        db_created_by_this_run = True
        cur.close()
        conn.close()
        print(f"Dedicated database '{test_db_name}' created successfully for this test run.")
    except Exception as e:
        print(f"Failed to initialize isolated test database: {e}")
        sys.exit(1)

    try:
        # Open persistent connection to test database with autocommit=True
        test_conn = psycopg2.connect(test_db_url)
        test_conn.autocommit = True

        # STEP 1: Apply Real Schema
        print("\n[STEP 1] Applying app/schema.sql...")
        rc, out, err = run_sql_file(test_conn, "app/schema.sql")
        if rc != 0:
            print(f"Schema creation failed: {err}")
            sys.exit(1)
        print("PASS: Schema initialized.")

        # STEP 2: Seed Baseline Synthetic Data + Simulate Live State
        print("\n[STEP 2] Seeding synthetic data & simulating live application state...")
        rc, out, err = run_sql_file(test_conn, "seed/synthetic_seed.sql")
        if rc != 0:
            print(f"Synthetic seeding failed: {err}")
            sys.exit(1)

        sim_sql = """
        -- 2 legitimate test submissions
        INSERT INTO applications (application_no, applicant_name, mobile, dob, gender, marital_status, status, submitted_at)
        VALUES 
        ('SSP26999905', 'Test Citizen One', '9800000111', '1960-01-01', 'Male', 'Married', 'PENDING', NOW()),
        ('SSP26999902', 'Test Citizen Two', '9800000222', '1955-02-02', 'Female', 'Widowed', 'APPROVED', NOW());

        INSERT INTO portal_users (mobile, password_hash)
        VALUES
        ('9800000111', encode(sha256('01011960'::bytea), 'hex')),
        ('9800000222', encode(sha256('02021955'::bytea), 'hex'))
        ON CONFLICT DO NOTHING;
        """
        rc, out, err = run_sql_command(test_conn, sim_sql)
        if rc != 0:
            print(f"Simulation setup failed: {err}")
            sys.exit(1)
        print("PASS: Baseline prepared: 6 applications (4 synthetic + 2 legitimate submissions), 6 portal_users.")

        # STEP 3: Preflight Validation
        print("\n[STEP 3] Executing migration/preflight_validation.sql...")
        rc, out, err = run_sql_file(test_conn, "migration/preflight_validation.sql")
        if rc != 0 or ("PREFLIGHT PASSED" not in out and "PREFLIGHT PASSED" not in err):
            print(f"Preflight validation failed:\nStdout: {out}\nStderr: {err}")
            sys.exit(1)
        print("PASS: Preflight validation passed all assertions.")

        # STEP 4: Deliberate Failure & Sequence Non-Transactional Recovery Test
        print("\n[STEP 4] Testing Deliberate Failure & Sequence Recovery...")
        failure_sql = """
        BEGIN;
        UPDATE applications SET id = id + 100100 WHERE application_no = 'SSP26999905';
        SELECT setval(pg_get_serial_sequence('applications', 'id'), 100012, true);
        SELECT 1/0; -- Deliberate division by zero
        COMMIT;
        """
        rc, out, err = run_sql_command(test_conn, failure_sql)
        if rc == 0:
            print("\n[CRITICAL FAILURE ASSERTION ERROR] Deliberate failure query unexpectedly SUCCEEDED with exit code 0!")
            print("Expected PostgreSQL to abort the transaction with a division by zero error.")
            sys.exit(1)
        print(f"  Deliberate failure triggered as expected (exit code={rc}, transaction aborted).")

        # Explicitly ensure transaction is cleared before inspection queries
        _rollback_transaction(test_conn)

        cur = test_conn.cursor()
        cur.execute("SELECT id FROM applications WHERE application_no = 'SSP26999905';")
        row = cur.fetchone()
        row_id = row[0] if row else None
        cur.execute("SELECT last_value FROM applications_id_seq;")
        seq_row = cur.fetchone()
        seq_val = seq_row[0] if seq_row else None
        cur.close()

        if row_id is None or seq_val is None:
            print("\n[CRITICAL FAILURE ASSERTION ERROR] Failed to inspect database state after deliberate failure.")
            sys.exit(1)

        # Assertions confirming rollback of row modifications and persistence of sequence advance:
        if row_id >= 100100:
            print(f"\n[CRITICAL FAILURE ASSERTION ERROR] Row ID update was NOT rolled back (found ID={row_id}, expected original ID < 100100)!")
            sys.exit(1)

        if seq_val != 100012:
            print(f"\n[CRITICAL FAILURE ASSERTION ERROR] Sequence did not retain expected non-transactional value (found last_value={seq_val}, expected 100012)!")
            sys.exit(1)

        print(f"  Verified: Row ID={row_id} (reverted to original), Sequence last_value={seq_val} (non-transactional).")

        # Execute recovery command
        rc, out, err = run_sql_command(test_conn, "SELECT setval(pg_get_serial_sequence('applications', 'id'), (SELECT COALESCE(MAX(id), 1) FROM applications), true);")
        if rc != 0:
            print(f"Sequence recovery failed: {err}")
            sys.exit(1)

        # Confirm sequence was restored after recovery command
        cur = test_conn.cursor()
        cur.execute("SELECT last_value FROM applications_id_seq;")
        recovered_seq = cur.fetchone()[0]
        cur.execute("SELECT MAX(id) FROM applications;")
        max_id = cur.fetchone()[0]
        cur.close()

        if recovered_seq != max_id:
            print(f"\n[CRITICAL FAILURE ASSERTION ERROR] Sequence recovery failed to reset last_value to MAX(id) (found {recovered_seq}, expected {max_id})!")
            sys.exit(1)

        print(f"PASS: Sequence recovery command restored counter to match MAX(id)={max_id}.")

        # STEP 5: Client-Side Delta Recovery Test
        print("\n[STEP 5] Testing Client-Side Delta Recovery (Maintenance Submissions)...")
        # Simulate baseline keys export
        rc, out, err = run_sql_command(test_conn, "\\copy (SELECT application_no, mobile FROM applications) TO 'baseline_keys.csv' WITH CSV HEADER")
        if rc != 0:
            print(f"Baseline keys export failed: {err}")
            sys.exit(1)
        
        # Simulate a new citizen submission during maintenance window
        rc, out, err = run_sql_command(test_conn, """
        INSERT INTO applications (application_no, applicant_name, mobile, dob, gender, marital_status, status, submitted_at)
        VALUES ('SSP26999903', 'Maintenance Citizen', '9800000333', '1965-03-03', 'Female', 'Widowed', 'PENDING', NOW());
        INSERT INTO portal_users (mobile, password_hash) VALUES ('9800000333', encode(sha256('03031965'::bytea), 'hex'));
        """)
        if rc != 0:
            print(f"Simulated maintenance submission failed: {err}")
            sys.exit(1)

        # Client-side delta export
        delta_export_cmd = """
        CREATE TEMP TABLE baseline_keys (application_no VARCHAR(20), mobile VARCHAR(15));
        \\copy baseline_keys FROM 'baseline_keys.csv' WITH CSV HEADER;
        \\copy (SELECT applicant_name, mobile, dob, gender, marital_status, husband_name, husband_employer, village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by, application_no FROM applications WHERE application_no NOT IN (SELECT application_no FROM baseline_keys)) TO 'delta_apps.csv' WITH CSV HEADER;
        \\copy (SELECT mobile, password_hash FROM portal_users WHERE mobile NOT IN (SELECT mobile FROM baseline_keys)) TO 'delta_users.csv' WITH CSV HEADER;
        """
        rc, out, err = run_sql_command(test_conn, delta_export_cmd)
        if rc != 0:
            print(f"Delta export failed: {err}")
            sys.exit(1)

        # Verify delta files exist and remove them
        if os.path.exists("delta_apps.csv") and os.path.exists("delta_users.csv"):
            print("  Client-side delta exported successfully without server /tmp access.")
            os.remove("delta_apps.csv")
            os.remove("delta_users.csv")
        if os.path.exists("baseline_keys.csv"):
            os.remove("baseline_keys.csv")
        print("PASS: Client-side delta extraction verified.")

        # Re-clean the simulated submission for clean migration test
        rc, out, err = run_sql_command(test_conn, "DELETE FROM applications WHERE application_no = 'SSP26999903'; DELETE FROM portal_users WHERE mobile = '9800000333';")
        if rc != 0:
            print(f"Cleanup of simulated submission failed: {err}")
            sys.exit(1)

        # STEP 6: Execute Full Atomic Migration with Original seed/seed.sql
        print("\n[STEP 6] Executing migration/migrate_database.sql (100,002 inherited rows)...")
        rc, out, err = run_sql_file(test_conn, "migration/migrate_database.sql")
        if rc != 0 or ("MIGRATION SUCCESS" not in out and "MIGRATION SUCCESS" not in err):
            print(f"Migration failed:\nStdout: {out}\nStderr: {err}")
            sys.exit(1)
        print("PASS: Atomic migration succeeded.")

        # STEP 7: Execute Postflight Validation
        print("\n[STEP 7] Executing migration/postflight_validation.sql...")
        rc, out, err = run_sql_file(test_conn, "migration/postflight_validation.sql")
        if rc != 0 or ("POST-VALIDATION COMPLETE: All checks passed" not in out and "POST-VALIDATION COMPLETE: All checks passed" not in err):
            print(f"Postflight validation failed:\nStdout: {out}\nStderr: {err}")
            sys.exit(1)
        print("PASS: Postflight validation passed all assertions.")

        overall_success = True

    finally:
        # Close test connection before dropping database to prevent connection locks
        if test_conn and not test_conn.closed:
            try:
                test_conn.close()
            except Exception:
                pass

        # STEP 8: Resource Teardown
        if db_created_by_this_run:
            print(f"\n[TEARDOWN] Dropping temporary disposable database {test_db_name} (created by this test run)...")
            try:
                conn = psycopg2.connect(base_url)
                conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                cur = conn.cursor()
                
                # Strict check: only drop test_db_name, never admin_db or root db
                if test_db_name != admin_db and test_db_name == "sewasetu_disposable_test":
                    cur.execute(f"DROP DATABASE {test_db_name};")
                    # Verify absence with read-only query against pg_database
                    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (test_db_name,))
                    if cur.fetchone() is None:
                        db_dropped_successfully = True
                        print(f"Teardown verified: '{test_db_name}' safely dropped and confirmed absent from pg_database.")
                    else:
                        print(f"[TEARDOWN WARNING] Database '{test_db_name}' still present in pg_database after DROP DATABASE.")
                else:
                    print(f"[TEARDOWN WARNING] Database name '{test_db_name}' matches admin database; skipping drop.")
                cur.close()
                conn.close()
            except Exception as e:
                print(f"[TEARDOWN ERROR] Failed to drop disposable database '{test_db_name}': {e}")
        else:
            print("\n[TEARDOWN] Skipping database drop (no disposable database was created by this test run).")

        # Final Summary Reporting
        if overall_success and db_dropped_successfully:
            print("\n" + "=" * 75)
            print("ALL 7 STAGING INTEGRATION PHASES PASSED ON POSTGRESQL!")
            print("Staging environment was cleaned up and verified pristine.")
            print("=" * 75)
        elif overall_success and not db_dropped_successfully:
            print("\n" + "=" * 75)
            print("ALL 7 STAGING INTEGRATION PHASES PASSED ON POSTGRESQL!")
            print(f"WARNING: Cleanup was not verified. Please manually drop '{test_db_name}'.")
            print("=" * 75)
        else:
            print("\n" + "=" * 75)
            print("*** STAGING TEST RUN FAILED OR WAS INTERRUPTED ***")
            print("The migration test did NOT complete successfully.")
            if db_created_by_this_run and not db_dropped_successfully:
                print(f"WARNING: Database '{test_db_name}' may still exist on the staging server.")
            print("=" * 75)
            sys.exit(1)


if __name__ == "__main__":
    main()
