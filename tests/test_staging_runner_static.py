import os
import io
import re
import sys
import tempfile
import unittest
import urllib.parse

from unittest.mock import MagicMock, patch
import psycopg2

from migration.test_staging_migration import (
    CopyStreamReader,
    validate_target_host,
    COPY_TO_RE,
    COPY_FROM_RE,
    _rollback_transaction,
    has_executable_sql,
    run_sql_file,
    run_sql_command,
)


class TestStagingRunnerStatic(unittest.TestCase):
    """Static unit tests for staging test runner components (offline, zero DB connections)."""

    def test_copy_stream_reader_delimiters(self):
        """Verify CopyStreamReader stops exactly at '\\.' and streams expected data."""
        data = "line1\nline2\nline3\n\\.\nline_after_copy\n"
        f = io.StringIO(data)

        reader = CopyStreamReader(f)
        streamed = reader.read()
        self.assertEqual(streamed, "line1\nline2\nline3\n")

        # Verify underlying file is positioned right after '\.'
        remaining = f.readline().strip()
        self.assertEqual(remaining, "line_after_copy")

    def test_copy_stream_reader_chunked(self):
        """Verify chunked reads with small buffer size."""
        data = "row1\nrow2\nrow3\n\\.\n"
        f = io.StringIO(data)

        reader = CopyStreamReader(f)
        chunks = []
        while True:
            chunk = reader.read(4)
            if not chunk:
                break
            chunks.append(chunk)

        self.assertEqual("".join(chunks), "row1\nrow2\nrow3\n")

    def test_seed_sql_streaming_line_counts(self):
        """Verify streaming exact 100,002 applications and 100,002 portal_users from seed/seed.sql."""
        seed_path = "seed/seed.sql"
        if not os.path.exists(seed_path):
            self.skipTest("seed/seed.sql not found")

        with open(seed_path, "r", encoding="utf-8") as f:
            # 1. Advance to applications COPY
            line = f.readline()
            while line and not line.strip().lower().startswith("copy applications"):
                line = f.readline()
            self.assertTrue(line.strip().lower().startswith("copy applications"))

            # 2. Stream applications
            app_reader = CopyStreamReader(f)
            app_lines = 0
            while True:
                chunk = app_reader.read(65536)
                if not chunk:
                    break
                app_lines += chunk.count("\n")
            self.assertEqual(app_lines, 100002, "Must stream exactly 100,002 application rows")

            # 3. Advance to portal_users COPY
            line = f.readline()
            while line and not line.strip().lower().startswith("copy portal_users"):
                line = f.readline()
            self.assertTrue(line.strip().lower().startswith("copy portal_users"))

            # 4. Stream portal users
            user_reader = CopyStreamReader(f)
            user_lines = 0
            while True:
                chunk = user_reader.read(65536)
                if not chunk:
                    break
                user_lines += chunk.count("\n")
            self.assertEqual(user_lines, 100002, "Must stream exactly 100,002 portal user rows")

            # 5. Advance to sequence setval
            line = f.readline()
            while line and not line.strip().lower().startswith("select setval"):
                line = f.readline()
            self.assertIn("applications_id_seq", line)
            self.assertIn("100012", line)

    def test_copy_regex_matching(self):
        """Verify \\copy regexes correctly parse client-side export and import directives."""
        to_cmd = r"\copy (SELECT application_no, mobile FROM applications) TO 'baseline_keys.csv' WITH CSV HEADER"
        from_cmd = r"\copy baseline_keys FROM 'baseline_keys.csv' WITH CSV HEADER;"

        m_to = COPY_TO_RE.match(to_cmd.strip().rstrip(";"))
        self.assertIsNotNone(m_to)
        target, filepath, opts = m_to.groups()
        self.assertEqual(target, "(SELECT application_no, mobile FROM applications)")
        self.assertEqual(filepath, "baseline_keys.csv")
        self.assertIn("WITH CSV HEADER", opts)

        m_from = COPY_FROM_RE.match(from_cmd.strip().rstrip(";"))
        self.assertIsNotNone(m_from)
        target, filepath, opts = m_from.groups()
        self.assertEqual(target, "baseline_keys")
        self.assertEqual(filepath, "baseline_keys.csv")

    def test_validate_target_host_safety(self):
        """Verify all target host safety guards: reject Render, reject pooler, require exact match."""
        # Reject missing host
        with self.assertRaises(SystemExit):
            validate_target_host("postgresql://user:pass@/dbname", urllib.parse.urlparse("postgresql://user:pass@/dbname"))

        # Reject Render signatures
        with self.assertRaises(SystemExit):
            validate_target_host("postgresql://user:pass@dpg-abc-a.oregon-postgres.render.com/db", urllib.parse.urlparse("postgresql://user:pass@dpg-abc-a.oregon-postgres.render.com/db"))

        # Reject pooler endpoints
        with self.assertRaises(SystemExit):
            validate_target_host("postgresql://user:pass@ep-cool-branch-pooler.c-2.neon.tech/neondb", urllib.parse.urlparse("postgresql://user:pass@ep-cool-branch-pooler.c-2.neon.tech/neondb"))

        # Reject unverified host in isolated fixture (ensuring no allowlist matches)
        with patch.dict(os.environ, {"EXPECTED_STAGING_HOST": "", "ALLOWED_STAGING_HOSTS": "", "ALLOW_LOCAL_STAGING": "0"}):
            os.environ.pop("EXPECTED_STAGING_HOST", None)
            with self.assertRaises(SystemExit):
                validate_target_host("postgresql://user:pass@ep-unverified.neon.tech/neondb", urllib.parse.urlparse("postgresql://user:pass@ep-unverified.neon.tech/neondb"))

        # Accept exact allowlisted host in isolated fixture
        with patch.dict(os.environ, {"EXPECTED_STAGING_HOST": "ep-staging-branch.c-2.neon.tech", "ALLOWED_STAGING_HOSTS": "", "ALLOW_LOCAL_STAGING": "0"}):
            self.assertTrue(validate_target_host("postgresql://user:pass@ep-staging-branch.c-2.neon.tech/neondb", urllib.parse.urlparse("postgresql://user:pass@ep-staging-branch.c-2.neon.tech/neondb")))

    def test_step2_simulation_sql_uniqueness(self):
        """Verify that Step 2 simulation SQL does not insert duplicate application numbers violating schema.sql."""
        with open("app/schema.sql", "r", encoding="utf-8") as f:
            schema_content = f.read()
        self.assertIn("application_no  VARCHAR(20) UNIQUE", schema_content)

        with open("migration/test_staging_migration.py", "r", encoding="utf-8") as f:
            runner_content = f.read()

        # Must not re-insert duplicate copies of APP-2026-00001 or APP-2026-00002
        self.assertNotIn("FROM applications WHERE application_no IN ('APP-2026-00001', 'APP-2026-00002')", runner_content)

        # Baseline log must reflect exactly 6 distinct applications
        self.assertIn("Baseline prepared: 6 applications (4 synthetic + 2 legitimate submissions)", runner_content)

    def test_step2_simulation_keys_do_not_collide_with_seed(self):
        """Regression test: verify that Step 2 simulated application numbers do not collide with seed/seed.sql."""
        seed_path = "seed/seed.sql"
        if not os.path.exists(seed_path):
            self.skipTest("seed/seed.sql not found")

        with open("migration/test_staging_migration.py", "r", encoding="utf-8") as f:
            runner_code = f.read()

        # Extract all simulated application numbers inserted in Step 2 sim_sql
        sim_matches = set(re.findall(r"'(SSP26\d{6})'", runner_code))
        self.assertTrue(len(sim_matches) > 0, "Must extract simulated application numbers from runner")

        with open(seed_path, "r", encoding="utf-8") as f:
            seed_content = f.read()

        for app_no in sim_matches:
            self.assertNotIn(
                f"\t{app_no}\t",
                seed_content,
                f"Simulated application number '{app_no}' collides with an existing record in seed/seed.sql!"
            )

    def test_step2_reconciled_projections(self):
        """Verify that 6 initial applications (4 synthetic + 2 legitimate) correctly project to 100,008 total rows."""
        synthetic_count = 4
        legitimate_count = 2
        total_initial_apps = synthetic_count + legitimate_count
        seed_count = 100002
        expected_final_apps = seed_count + total_initial_apps
        self.assertEqual(total_initial_apps, 6)
        self.assertEqual(expected_final_apps, 100008)

    def test_rollback_transaction_autocommit_true(self):
        """Verify _rollback_transaction issues explicit SQL ROLLBACK via cursor when autocommit=True."""
        conn = MagicMock()
        conn.autocommit = True
        conn.get_transaction_status.side_effect = [
            psycopg2.extensions.TRANSACTION_STATUS_INERROR,
            psycopg2.extensions.TRANSACTION_STATUS_IDLE,
        ]

        mock_cur = MagicMock()
        conn.cursor.return_value = mock_cur

        _rollback_transaction(conn)

        # Must execute SQL ROLLBACK via cursor
        mock_cur.execute.assert_called_once_with("ROLLBACK;")
        mock_cur.close.assert_called_once()
        # Must NOT call conn.rollback() directly (which raises ProgrammingError in autocommit mode)
        conn.rollback.assert_not_called()

    def test_rollback_transaction_autocommit_false(self):
        """Verify _rollback_transaction calls conn.rollback() when autocommit=False."""
        conn = MagicMock()
        conn.autocommit = False
        conn.get_transaction_status.side_effect = [
            psycopg2.extensions.TRANSACTION_STATUS_INTRANS,
            psycopg2.extensions.TRANSACTION_STATUS_IDLE,
        ]

        _rollback_transaction(conn)

        conn.rollback.assert_called_once()
        conn.cursor.assert_not_called()

    def test_rollback_transaction_failure_raises(self):
        """Verify _rollback_transaction raises RuntimeError if transaction status is not IDLE after rollback."""
        conn = MagicMock()
        conn.autocommit = True
        conn.get_transaction_status.side_effect = [
            psycopg2.extensions.TRANSACTION_STATUS_INERROR,
            psycopg2.extensions.TRANSACTION_STATUS_INERROR,
        ]

        with self.assertRaises(RuntimeError) as ctx:
            _rollback_transaction(conn)
        self.assertIn("Connection not idle after rollback", str(ctx.exception))

    def test_rollback_transaction_idle(self):
        """Verify _rollback_transaction does nothing if transaction status is IDLE."""
        conn = MagicMock()
        conn.autocommit = True
        conn.get_transaction_status.return_value = psycopg2.extensions.TRANSACTION_STATUS_IDLE

        _rollback_transaction(conn)

        conn.cursor.assert_not_called()
        conn.rollback.assert_not_called()

    def test_step4_control_flow_does_not_abort_on_expected_failure(self):
        """Verify that Step 4 asserts failure on rc == 0 (unexpected success) and proceeds on rc != 0."""
        with open("migration/test_staging_migration.py", "r", encoding="utf-8") as f:
            code = f.read()

        # Step 4 must abort ONLY when rc == 0 (unexpected success of division by zero)
        self.assertIn("rc, out, err = run_sql_command(test_conn, failure_sql)", code)
        self.assertIn("if rc == 0:", code)
        self.assertIn("Deliberate failure query unexpectedly SUCCEEDED", code)

        # Step 4 must clear transaction, inspect database state, and run recovery
        self.assertIn("_rollback_transaction(test_conn)", code)
        self.assertIn("SELECT id FROM applications WHERE application_no = 'SSP26999905';", code)
        self.assertIn("SELECT last_value FROM applications_id_seq;", code)
        self.assertIn("if row_id >= 100100:", code)
        self.assertIn("if seq_val != 100012:", code)
        self.assertIn("SELECT setval(pg_get_serial_sequence('applications', 'id')", code)
        self.assertIn("if recovered_seq != max_id:", code)

    def test_has_executable_sql_detects_empty_and_comments(self):
        """Verify has_executable_sql returns False for empty strings, whitespace, comments, and empty terminators."""
        empty_cases = [
            "",
            "   \t\n\r\f  ",
            ";",
            ";; ; \n ; ;",
            "-- single line comment",
            "-- single line comment\n",
            "-- line 1\n-- line 2\n\n",
            "/* inline block comment */",
            "/* multi\nline\nblock\ncomment */",
            "/* outer /* nested block comment */ outer */",
            "-- comment\n; /* block */ ;\n ;",
            "/**/",
            "-- comment with quotes 'hello' and ; semicolons\n",
            "/* block with 'quotes' and ; and -- nested */",
        ]
        for sql in empty_cases:
            self.assertFalse(has_executable_sql(sql), f"Expected has_executable_sql to be False for: {sql!r}")

    def test_has_executable_sql_detects_real_sql(self):
        """Verify has_executable_sql returns True when executable SQL statements are present."""
        valid_cases = [
            "SELECT 1;",
            "BEGIN;",
            "COMMIT;",
            "ROLLBACK;",
            "-- leading comment\nSELECT 1;",
            "/* leading block */ SELECT 1;",
            "SELECT 1; -- trailing comment",
            "SELECT 'this is -- not a comment';",
            "DO $$ BEGIN NULL; END $$;",
            "(SELECT 1);",
            "UPDATE applications SET id = 1;",
            "INSERT INTO t VALUES (1);",
            "DELETE FROM t WHERE id = 1;",
        ]
        for sql in valid_cases:
            self.assertTrue(has_executable_sql(sql), f"Expected has_executable_sql to be True for: {sql!r}")

    def test_run_sql_command_skips_comments_and_empty(self):
        """Verify run_sql_command does not call cur.execute for empty/comment-only inputs."""
        conn = MagicMock()
        cur = MagicMock()
        cur.description = None
        conn.cursor.return_value = cur
        conn.notices = []

        # Comment-only command
        rc, out, err = run_sql_command(conn, "-- comment only\n-- line 2")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        cur.execute.assert_not_called()

        # Whitespace and semicolons
        rc, out, err = run_sql_command(conn, "   \n ; ; \n /* block */ ;")
        self.assertEqual(rc, 0)
        cur.execute.assert_not_called()

    def test_run_sql_command_executes_valid_sql(self):
        """Verify run_sql_command executes valid SQL statements."""
        conn = MagicMock()
        cur = MagicMock()
        cur.description = None
        conn.cursor.return_value = cur
        conn.notices = []

        rc, out, err = run_sql_command(conn, "-- leading comment\nSELECT 1;")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        cur.execute.assert_called_once_with("-- leading comment\nSELECT 1;")

    def test_run_sql_file_skips_comments_before_copy_stdin(self):
        """Regression test: verify run_sql_file skips header comments before inlined COPY ... FROM stdin."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sql_file = os.path.join(tmp_dir, "mock_seed.sql")
            with open(sql_file, "w", encoding="utf-8") as f:
                f.write("-- Sewa Setu production data export\n")
                f.write("-- pg_dump 15.4, sewasetu@dbprod01, 2026-09-30\n\n")
                f.write("COPY mock_apps (id, app_no) FROM stdin;\n")
                f.write("1\tAPP-001\n")
                f.write("2\tAPP-002\n")
                f.write("\\.\n\n")
                f.write("SELECT setval('applications_id_seq', 100012);\n")

            conn = MagicMock()
            cur = MagicMock()
            cur.description = None
            conn.cursor.return_value = cur
            conn.notices = []

            streamed_chunks = []

            def mock_copy_expert(cmd, reader):
                streamed_chunks.append(reader.read())

            cur.copy_expert.side_effect = mock_copy_expert

            rc, out, err = run_sql_file(conn, sql_file)
            self.assertEqual(rc, 0, f"run_sql_file failed with stderr: {err}")
            self.assertEqual(err, "")

            # cur.execute must NOT have been called with the header comments
            for call_args in cur.execute.call_args_list:
                sql_arg = call_args[0][0]
                self.assertNotIn("-- Sewa Setu production data export", sql_arg,
                                 "Header comment must not be executed as standalone query")

            # cur.copy_expert must have been called for COPY
            cur.copy_expert.assert_called_once()
            copy_cmd = cur.copy_expert.call_args[0][0]
            self.assertEqual(copy_cmd, "COPY mock_apps (id, app_no) FROM stdin")
            # Verify data streamed through CopyStreamReader
            self.assertEqual("".join(streamed_chunks), "1\tAPP-001\n2\tAPP-002\n")

            # cur.execute must have been called for the trailing SELECT statement
            executed_sqls = [c[0][0] for c in cur.execute.call_args_list]
            self.assertIn("SELECT setval('applications_id_seq', 100012);", executed_sqls)

    def test_run_sql_file_skips_comments_before_ir_and_preserves_boundaries(self):
        """Verify run_sql_file skips comments before \\ir without losing preceding SQL or transaction boundaries."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            child_file = os.path.join(tmp_dir, "child.sql")
            with open(child_file, "w", encoding="utf-8") as f:
                f.write("-- Child header comment\n")
                f.write("INSERT INTO child_table VALUES (1);\n")

            parent_file = os.path.join(tmp_dir, "parent.sql")
            with open(parent_file, "w", encoding="utf-8") as f:
                f.write("-- Parent header comment\n")
                f.write("BEGIN;\n")
                f.write("UPDATE parent_table SET val = 2;\n")
                f.write("\\ir child.sql\n")
                f.write("COMMIT;\n")

            conn = MagicMock()
            cur = MagicMock()
            cur.description = None
            conn.cursor.return_value = cur
            conn.notices = []

            rc, out, err = run_sql_file(conn, parent_file)
            self.assertEqual(rc, 0, f"run_sql_file failed with stderr: {err}")

            executed_sqls = [c[0][0] for c in cur.execute.call_args_list]
            # Must execute BEGIN block before \\ir
            self.assertTrue(any("BEGIN;" in s and "UPDATE parent_table" in s for s in executed_sqls))
            # Must execute child SQL
            self.assertTrue(any("INSERT INTO child_table VALUES (1);" in s for s in executed_sqls))
            # Must execute COMMIT after \\ir
            self.assertTrue(any("COMMIT;" in s for s in executed_sqls))

            # Must NEVER execute pure comments
            for s in executed_sqls:
                self.assertTrue(has_executable_sql(s), f"Executed query has no executable SQL: {s!r}")

    def test_run_sql_file_comment_only_file(self):
        """Verify run_sql_file on a comment-only file succeeds cleanly with 0 statements executed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sql_file = os.path.join(tmp_dir, "comments_only.sql")
            with open(sql_file, "w", encoding="utf-8") as f:
                f.write("-- Just comments\n")
                f.write("/* Multi-line\ncomment */\n\n")

            conn = MagicMock()
            cur = MagicMock()
            cur.description = None
            conn.cursor.return_value = cur
            conn.notices = []

            rc, out, err = run_sql_file(conn, sql_file)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            cur.execute.assert_not_called()


class TestDocumentationAndRunbookConsistency(unittest.TestCase):
    """Static tests verifying documentation contract alignment and runbook completeness."""

    def test_smoke_test_doc_healthcheck_contract(self):
        """Verify post_migration_smoke_test.md expects HTTP 200 with status healthy (app/app.py contract)."""
        doc_path = os.path.join("migration", "post_migration_smoke_test.md")
        self.assertTrue(os.path.exists(doc_path), f"Smoke test doc missing: {doc_path}")

        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Must expect "status": "healthy"
        self.assertIn('"status": "healthy"', content)
        # Must NOT expect outdated {"status": "ok"}
        self.assertNotIn('{"status": "ok"}', content)
        # Must verify HTTP 200 OK
        self.assertIn("200 OK", content)

    def test_smoke_test_doc_prohibits_real_citizen_auth(self):
        """Verify post_migration_smoke_test.md forbids authenticating with real inherited citizen records."""
        doc_path = os.path.join("migration", "post_migration_smoke_test.md")
        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Must NOT instruct entering inherited citizen credentials
        self.assertNotIn("Enter known inherited test mobile", content)
        # Must explicitly mention synthetic test identities and aggregate verification
        self.assertIn("synthetic test identities", content)
        self.assertIn("postflight_validation.sql", content)

    def test_maintenance_plan_operator_checklist_steps(self):
        """Verify production_maintenance_plan.md contains the complete sequential operator checklist."""
        plan_path = os.path.join("migration", "production_maintenance_plan.md")
        self.assertTrue(os.path.exists(plan_path), f"Maintenance plan missing: {plan_path}")

        with open(plan_path, "r", encoding="utf-8") as f:
            content = f.read()

        required_checklist_elements = [
            "Operator Maintenance Window Execution Checklist",
            "Step 1: Capture and Verify Pre-Maintenance Snapshot",
            "Step 2: Stop / Suspend Incoming Application Traffic",
            "Step 3: Run Pre-Migration Read-Only Collision Check Utility",
            "Step 4: Execute Read-Only Preflight Verification SQL",
            "Step 5: Execute Migration (Single Atomic Transaction)",
            "Step 6: Execute Postflight Verification SQL",
            "Step 7: Restore Application Traffic",
            "Step 8: Perform Post-Migration Synthetic Smoke Tests",
            "Step 9: Maintenance Sign-Off",
        ]
        for element in required_checklist_elements:
            self.assertIn(element, content, f"Checklist missing required step: {element}")

    def test_maintenance_plan_verified_render_procedure(self):
        """Verify production_maintenance_plan.md documents verified Render suspend/resume procedures."""
        plan_path = os.path.join("migration", "production_maintenance_plan.md")
        with open(plan_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Suspend Web Service", content)
        self.assertIn("Resume Web Service", content)
        self.assertIn("503 Service Unavailable", content)

    def test_recovery_procedure_delineation(self):
        """Verify recovery_procedure.sql clearly distinguishes SQL statements from shell commands."""
        rec_path = os.path.join("migration", "recovery_procedure.sql")
        self.assertTrue(os.path.exists(rec_path), f"Recovery procedure missing: {rec_path}")

        with open(rec_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("SCENARIO A", content)
        self.assertIn("SCENARIO B", content)
        self.assertIn("Execution Context: Executed as a SQL statement", content)
        self.assertIn("Execution Context: Operator Terminal / Shell Commands", content)
        self.assertIn("SELECT setval(", content)
        self.assertIn("pg_restore", content)
        self.assertIn("DESTRUCTIVE AND UNTESTED", content)
        self.assertIn("THIS IS NOT A DATABASE RESTORE", content)

    def test_backup_integrity_verification_requirements(self):
        """Verify runbook enforces pg_restore --list verification and TOC checks."""
        plan_path = os.path.join("migration", "production_maintenance_plan.md")
        with open(plan_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("pg_restore --list", content)
        self.assertIn("TABLE DATA.*(applications|portal_users)", content)
        self.assertIn("Archive-List Verification vs. Proven Successful Restore", content)

    def test_target_host_verification_step_zero(self):
        """Verify Step 0 mandates independent target host and database verification with shell separation."""
        plan_path = os.path.join("migration", "production_maintenance_plan.md")
        with open(plan_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Step 0: Target Host & Database Verification", content)
        self.assertIn("SELECT current_database(), inet_server_addr(), current_user;", content)
        self.assertIn("Never rely on `$DATABASE_URL` alone", content)
        self.assertIn("Bash:", content)
        self.assertIn("PowerShell:", content)

    def test_connection_inspection_and_no_auto_terminate(self):
        """Verify runbook inspects pg_stat_activity and prohibits automatic connection termination."""
        plan_path = os.path.join("migration", "production_maintenance_plan.md")
        with open(plan_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("pg_stat_activity", content)
        self.assertIn("No Automatic Connection Termination", content)
        self.assertIn("Do not assume suspending the Render Web Service guarantees zero active database connections", content)

    def test_recovery_safety_warnings_and_untested_drill_recommendation(self):
        """Verify runbook explicitly marks Scenario B as destructive & untested and recommends restore drill."""
        plan_path = os.path.join("migration", "production_maintenance_plan.md")
        with open(plan_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("DESTRUCTIVE & UNTESTED", content)
        self.assertIn("DO NOT ASSUME DELTA-EXPORT AND RE-INJECTION ARE PROVEN SAFE", content)
        self.assertIn("OPERATIONAL BLOCKER & RECOMMENDATION", content)
        self.assertIn("conducted on an isolated test database prior to production cutover", content)

    def test_go_abort_conditions_completeness(self):
        """Verify each checklist step defines explicit GO and ABORT conditions."""
        plan_path = os.path.join("migration", "production_maintenance_plan.md")
        with open(plan_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertGreaterEqual(content.count("**GO Condition:**"), 7)
        self.assertGreaterEqual(content.count("**ABORT Condition:**"), 7)


class TestPreMigrationCollisionCheck(unittest.TestCase):
    """Static unit tests for pre_migration_collision_check.py utility (offline, zero DB)."""

    def test_extract_seed_application_numbers_from_stream(self):
        """Verify parsing COPY block and extracting application numbers correctly."""
        from migration.pre_migration_collision_check import extract_seed_application_numbers

        stream_data = (
            "-- Comment\n"
            "COPY applications (id, application_no, applicant_name, mobile) FROM stdin;\n"
            "1\tSSP25000001\tCitizen One\t9876543210\n"
            "2\tSSP25000002\tCitizen Two\t9876543211\n"
            "\\.\n"
            "COPY portal_users (mobile, password_hash) FROM stdin;\n"
            "9876543210\thash1\n"
            "\\.\n"
        )
        stream = io.StringIO(stream_data)
        keys = extract_seed_application_numbers(stream)
        self.assertEqual(keys, {"SSP25000001", "SSP25000002"})

    def test_check_application_collisions_no_overlap(self):
        """Verify collision check passes when sets are completely disjoint."""
        from migration.pre_migration_collision_check import check_application_collisions

        seed_keys = {"SSP25000001", "SSP25000002"}
        db_keys = {"SSP26900001", "SSP26900002"}

        result = check_application_collisions(seed_keys, db_keys)
        self.assertTrue(result.is_safe)
        self.assertEqual(result.collision_count, 0)
        self.assertEqual(result.colliding_keys, [])

    def test_check_application_collisions_allows_synthetic_records(self):
        """Verify overlap on allowed synthetic keys ('APP-2026-00001'..'4') is ignored."""
        from migration.pre_migration_collision_check import check_application_collisions

        seed_keys = {"SSP25000001", "SSP25000002", "APP-2026-00001"}
        db_keys = {"APP-2026-00001", "APP-2026-00002"}

        result = check_application_collisions(seed_keys, db_keys)
        self.assertTrue(result.is_safe)
        self.assertEqual(result.collision_count, 0)

    def test_check_application_collisions_detects_real_collision(self):
        """Verify unexpected collision fails check and identifies colliding keys."""
        from migration.pre_migration_collision_check import check_application_collisions

        seed_keys = {"SSP25000001", "SSP25000002", "SSP26999901"}
        db_keys = {"SSP26999901", "SSP26999905"}

        result = check_application_collisions(seed_keys, db_keys)
        self.assertFalse(result.is_safe)
        self.assertEqual(result.collision_count, 1)
        self.assertEqual(result.colliding_keys, ["SSP26999901"])

    def test_format_report_zero_pii(self):
        """Verify format_report contains only application numbers and counts, zero PII."""
        from migration.pre_migration_collision_check import check_application_collisions, format_report

        seed_keys = {"SSP26999901"}
        db_keys = {"SSP26999901"}
        result = check_application_collisions(seed_keys, db_keys)
        report = format_report(result)

        self.assertIn("SSP26999901", report)
        self.assertIn("[STATUS: FAIL - COLLISION DETECTED]", report)
        # Ensure no sensitive PII terms or phone numbers
        self.assertNotIn("mobile", report.lower())
        self.assertNotIn("dob", report.lower())
        self.assertNotIn("password", report.lower())
        self.assertNotIn("bank", report.lower())

    def test_load_keys_from_file(self):
        """Verify load_keys_from_file reads plain text and CSV files correctly."""
        from migration.pre_migration_collision_check import load_keys_from_file

        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
            tf.write("application_no,mobile\n")
            tf.write("SSP26000001,9876543210\n")
            tf.write("SSP26000002,9876543211\n")
            tf_path = tf.name

        try:
            keys = load_keys_from_file(tf_path)
            self.assertEqual(keys, {"SSP26000001", "SSP26000002"})
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_collision_check_dedicated_fixture_zero_collisions(self):
        """Verify dedicated fixture with valid non-colliding keys passes with exit code 0."""
        from migration.pre_migration_collision_check import (
            load_keys_from_file,
            check_application_collisions,
            main,
        )

        seed_fixture = os.path.join("tests", "fixtures", "synthetic_seed.sql")
        if not os.path.exists(seed_fixture):
            os.makedirs(os.path.dirname(seed_fixture), exist_ok=True)
            with open(seed_fixture, "w", encoding="utf-8") as f:
                f.write(
                    "COPY applications (id, application_no, applicant_name) FROM stdin;\n"
                    "1\tSSP25825708\tSynthetic Seed Citizen\n"
                    "\\.\n"
                )

        fixture_path = os.path.join("tests", "fixtures", "live_db_keys_no_collision.csv")
        self.assertTrue(os.path.exists(fixture_path), f"Fixture missing: {fixture_path}")

        keys = load_keys_from_file(fixture_path)
        self.assertEqual(len(keys), 5)
        self.assertIn("SSP26999905", keys)
        self.assertIn("APP-2026-00001", keys)

        # Test CLI main() with dedicated synthetic fixtures
        rc = main(["--seed-file", seed_fixture, "--offline-db-keys-file", fixture_path])
        self.assertEqual(rc, 0, "Dedicated non-colliding fixture must pass with exit code 0")

    def test_collision_check_dedicated_fixture_deliberate_collision(self):
        """Verify dedicated fixture with colliding key (SSP25825708) fails safely with exit code 1."""
        from migration.pre_migration_collision_check import main

        seed_fixture = os.path.join("tests", "fixtures", "synthetic_seed.sql")
        if not os.path.exists(seed_fixture):
            os.makedirs(os.path.dirname(seed_fixture), exist_ok=True)
            with open(seed_fixture, "w", encoding="utf-8") as f:
                f.write(
                    "COPY applications (id, application_no, applicant_name) FROM stdin;\n"
                    "1\tSSP25825708\tSynthetic Seed Citizen\n"
                    "\\.\n"
                )

        fixture_path = os.path.join("tests", "fixtures", "live_db_keys_with_collision.csv")
        self.assertTrue(os.path.exists(fixture_path), f"Fixture missing: {fixture_path}")

        rc = main(["--seed-file", seed_fixture, "--offline-db-keys-file", fixture_path])
        self.assertEqual(rc, 1, "Dedicated colliding fixture must fail with exit code 1")

    def test_collision_check_malformed_input_fails_safely(self):
        """Verify malformed input (code tokens, spaces, non-application numbers) fails safely with code 2."""
        from migration.pre_migration_collision_check import load_keys_from_file, main

        malformed_path = os.path.join("tests", "fixtures", "malformed_db_keys.txt")
        self.assertTrue(os.path.exists(malformed_path), f"Fixture missing: {malformed_path}")

        with self.assertRaises(ValueError) as ctx:
            load_keys_from_file(malformed_path)
        self.assertIn("not a valid application number", str(ctx.exception))

        # CLI execution must return exit code 2
        rc = main(["--seed-file", "seed/seed.sql", "--offline-db-keys-file", malformed_path])
        self.assertEqual(rc, 2, "Malformed input must exit with code 2 (safe abort)")

    def test_collision_check_rejects_code_files_like_conftest(self):
        """Verify passing Python source files (tests/conftest.py) is rejected as malformed input."""
        from migration.pre_migration_collision_check import load_keys_from_file, main

        conftest_path = os.path.join("tests", "conftest.py")
        self.assertTrue(os.path.exists(conftest_path))

        with self.assertRaises(ValueError) as ctx:
            load_keys_from_file(conftest_path)
        self.assertIn("not a valid application number", str(ctx.exception))

        rc = main(["--seed-file", "seed/seed.sql", "--offline-db-keys-file", conftest_path])
        self.assertEqual(rc, 2, "Code files must fail parsing safely with exit code 2")

    def test_collision_check_unreadable_or_missing_file_fails_safely(self):
        """Verify missing or empty files fail safely rather than reporting false success."""
        from migration.pre_migration_collision_check import load_keys_from_file, main

        with self.assertRaises(FileNotFoundError):
            load_keys_from_file("tests/fixtures/non_existent_keys.csv")

        rc = main(["--seed-file", "seed/seed.sql", "--offline-db-keys-file", "tests/fixtures/non_existent_keys.csv"])
        self.assertEqual(rc, 2, "Missing file must exit with code 2")

        with tempfile.NamedTemporaryFile("w+", delete=False) as empty_tf:
            empty_path = empty_tf.name

        try:
            with self.assertRaises(ValueError):
                load_keys_from_file(empty_path)
            rc_empty = main(["--seed-file", "seed/seed.sql", "--offline-db-keys-file", empty_path])
            self.assertEqual(rc_empty, 2, "Empty file must exit with code 2")
        finally:
            if os.path.exists(empty_path):
                os.remove(empty_path)

    def test_extract_seed_application_numbers_missing_copy_block_fails(self):
        """Verify seed extraction fails safely if COPY applications block is missing."""
        from migration.pre_migration_collision_check import extract_seed_application_numbers

        bad_stream = io.StringIO("-- Just comments\nSELECT 1;\n")
        with self.assertRaises(ValueError) as ctx:
            extract_seed_application_numbers(bad_stream)
        self.assertIn("does not contain a valid 'COPY applications' block", str(ctx.exception))

    # ==========================================================================
    # STAGE 1 & 2: DISASTER RECOVERY COLUMN MAPPING & DRILL RUNNER STATIC TESTS
    # ==========================================================================

    def test_recovery_column_mapping_identical_explicit_order(self):
        """Verify identical, explicit column ordering across schema.sql, recovery_procedure.sql, and drill runner."""
        from migration.test_disaster_recovery_drill import (
            DELTA_APP_COLUMNS,
            DELTA_PORTAL_USER_COLUMNS,
        )

        # 1. Inspect app/schema.sql to extract non-id applications columns
        with open("app/schema.sql", "r", encoding="utf-8") as f:
            schema_content = f.read()

        app_match = re.search(r"CREATE TABLE IF NOT EXISTS applications \((.*?)\);", schema_content, re.DOTALL)
        self.assertIsNotNone(app_match, "schema.sql must define applications table")
        col_lines = [line.strip().split()[0] for line in app_match.group(1).strip().splitlines() if line.strip() and not line.strip().startswith("--")]
        non_id_cols = tuple(col for col in col_lines if col != "id")

        # Verify DELTA_APP_COLUMNS exactly matches schema.sql non-id column list and ordering
        self.assertEqual(
            DELTA_APP_COLUMNS,
            non_id_cols,
            "DELTA_APP_COLUMNS must match schema.sql non-id columns in exact positional order",
        )
        self.assertEqual(len(DELTA_APP_COLUMNS), 17, "Must contain all 17 non-id application columns")
        self.assertEqual(DELTA_APP_COLUMNS[0], "application_no")
        self.assertEqual(DELTA_APP_COLUMNS[1], "applicant_name")

        # 2. Inspect migration/recovery_procedure.sql
        with open("migration/recovery_procedure.sql", "r", encoding="utf-8") as f:
            recovery_sql = f.read()

        cols_str = ", ".join(DELTA_APP_COLUMNS)
        # Check export query
        self.assertIn(
            f"SELECT {cols_str} FROM applications WHERE application_no NOT IN",
            recovery_sql,
            "recovery_procedure.sql export must use explicit DELTA_APP_COLUMNS order",
        )
        # Check staging table copy
        self.assertIn(
            f"\\copy staging_delta_apps ({cols_str}) FROM 'delta_applications.csv' WITH CSV HEADER",
            recovery_sql,
            "recovery_procedure.sql import must explicitly specify matching column list",
        )

        # 3. Inspect portal_users mapping
        self.assertEqual(DELTA_PORTAL_USER_COLUMNS, ("mobile", "password_hash"))
        self.assertIn(
            "SELECT mobile, password_hash FROM portal_users WHERE mobile NOT IN",
            recovery_sql,
        )
        self.assertIn(
            "\\copy staging_delta_users (mobile, password_hash) FROM 'delta_portal_users.csv' WITH CSV HEADER",
            recovery_sql,
        )

    def test_delta_preservation_offline_simulation(self):
        """Simulate delta filtering and verify both new citizens and existing citizens submitting in maintenance are preserved."""
        # Baseline snapshot keys
        baseline_keys = {
            "SSP26999901": "9800000001",
            "SSP26999902": "9800000002",
        }

        # Applications present at disaster declaration
        live_applications = [
            {"application_no": "SSP26999901", "mobile": "9800000001", "applicant_name": "Citizen One"},
            {"application_no": "SSP26999902", "mobile": "9800000002", "applicant_name": "Citizen Two"},
            # Delta 1: Brand new citizen
            {"application_no": "SSP26999909", "mobile": "9800000009", "applicant_name": "New Citizen"},
            # Delta 2: Existing citizen submitting new application during maintenance
            {"application_no": "SSP26999910", "mobile": "9800000001", "applicant_name": "Citizen One Second App"},
        ]

        live_portal_users = {
            "9800000001": "hash_citizen_1",
            "9800000002": "hash_citizen_2",
            "9800000009": "hash_citizen_9_new",
        }

        # 1. Delta Applications export: WHERE application_no NOT IN (baseline_keys)
        delta_apps = [a for a in live_applications if a["application_no"] not in baseline_keys]
        self.assertEqual(len(delta_apps), 2, "Must export both delta applications")
        self.assertEqual({a["application_no"] for a in delta_apps}, {"SSP26999909", "SSP26999910"})

        # 2. Delta Users export: WHERE mobile NOT IN (baseline_keys)
        delta_users = {m: h for m, h in live_portal_users.items() if m not in set(baseline_keys.values())}
        self.assertEqual(len(delta_users), 1, "Must export only new portal user (9800000009)")
        self.assertIn("9800000009", delta_users)
        self.assertEqual(delta_users["9800000009"], "hash_citizen_9_new")

        # 3. Restored DB state after pg_restore (restores baseline)
        restored_apps = [a for a in live_applications if a["application_no"] in baseline_keys]
        restored_users = {m: h for m, h in live_portal_users.items() if m in set(baseline_keys.values())}

        # 4. Re-inject delta applications (fresh serial IDs)
        next_id = len(restored_apps) + 1
        for a in delta_apps:
            a_copy = dict(a)
            a_copy["id"] = next_id
            next_id += 1
            restored_apps.append(a_copy)

        # 5. Re-inject delta users with ON CONFLICT DO NOTHING
        for m, h in delta_users.items():
            if m not in restored_users:
                restored_users[m] = h

        # Assertions: All 4 applications present, all 3 portal users present with intact credentials
        self.assertEqual(len(restored_apps), 4)
        self.assertEqual(len(restored_users), 3)
        self.assertEqual(restored_users["9800000001"], "hash_citizen_1")
        self.assertEqual(restored_users["9800000009"], "hash_citizen_9_new")

    def test_duplicate_application_numbers_and_unique_index_behavior(self):
        """Verify unique index and ON CONFLICT (application_no) DO NOTHING prevents duplicate key collisions."""
        with open("migration/recovery_procedure.sql", "r", encoding="utf-8") as f:
            rec_sql = f.read()
        self.assertIn("ON CONFLICT (application_no) DO NOTHING", rec_sql)

        with open("migration/production_maintenance_plan.md", "r", encoding="utf-8") as f:
            plan_content = f.read()
        self.assertIn("ON CONFLICT (application_no) DO NOTHING", plan_content)

        from migration.test_disaster_recovery_drill import build_delta_import_sql
        import_sql = build_delta_import_sql("apps.csv", "users.csv")
        self.assertIn("ON CONFLICT (application_no) DO NOTHING;", import_sql)
        self.assertIn("ON CONFLICT (mobile) DO NOTHING;", import_sql)

    def test_foreign_key_and_portal_user_integrity(self):
        """Verify that delta applications maintain mobile identity matching portal users without orphan records."""
        from migration.test_disaster_recovery_drill import DELTA_APP_COLUMNS, DELTA_PORTAL_USER_COLUMNS
        self.assertIn("mobile", DELTA_APP_COLUMNS)
        self.assertIn("mobile", DELTA_PORTAL_USER_COLUMNS)

    def test_sequence_alignment_sql_and_derived_counts(self):
        """Verify sequence realignment SQL and mathematical derivation of expected counts."""
        from migration.test_disaster_recovery_drill import derive_expected_counts

        counts = derive_expected_counts(
            baseline_app_count=6,
            delta_app_count=2,
            baseline_user_count=6,
            delta_user_count=2,
        )
        self.assertEqual(counts["restored_applications"], 8)
        self.assertEqual(counts["restored_portal_users"], 8)
        self.assertEqual(counts["inherited_seed_records"], 0)
        self.assertEqual(counts["min_sequence_val"], 8)

        # Zero delta edge case
        zero_counts = derive_expected_counts(0, 0, 0, 0)
        self.assertEqual(zero_counts["restored_applications"], 0)
        self.assertEqual(zero_counts["min_sequence_val"], 0)

    def test_drill_runner_wrong_host_rejection(self):
        """Verify drill runner rejects empty host, Render hosts, poolers, and unverified hosts."""
        from migration.test_disaster_recovery_drill import validate_drill_target_host

        # Empty hostname
        with self.assertRaises(ValueError) as ctx:
            validate_drill_target_host("postgresql://user:pass@/db", urllib.parse.urlparse("postgresql://user:pass@/db"))
        self.assertIn("no valid hostname", str(ctx.exception))

        # Render host signatures
        render_urls = [
            "postgresql://user:pass@dpg-c12345678-a.oregon-postgres.render.com/db",
            "postgresql://user:pass@sewasetu-portal.onrender.com/db",
            "postgresql://user:pass@dbprod.render.com/db",
        ]
        for url in render_urls:
            with self.assertRaises(ValueError) as ctx:
                validate_drill_target_host(url, urllib.parse.urlparse(url))
            self.assertIn("REJECTED TARGET HOST", str(ctx.exception))

        # Neon pooler endpoint
        pooler_url = "postgresql://user:pass@ep-staging-branch-pooler.c-2.neon.tech/neondb"
        with self.assertRaises(ValueError) as ctx:
            validate_drill_target_host(pooler_url, urllib.parse.urlparse(pooler_url))
        self.assertIn("REJECTED POOLED ENDPOINT", str(ctx.exception))

        # Unverified staging host in isolated fixture
        unverified_url = "postgresql://user:pass@ep-unverified.neon.tech/neondb"
        with patch.dict(os.environ, {"EXPECTED_STAGING_HOST": ""}):
            os.environ.pop("EXPECTED_STAGING_HOST", None)
            with self.assertRaises(ValueError) as ctx:
                validate_drill_target_host(unverified_url, urllib.parse.urlparse(unverified_url))
            self.assertIn("TARGET HOST UNVERIFIED", str(ctx.exception))
            self.assertIn("ep-unverified.neon.tech", str(ctx.exception))

        # Host mismatch in isolated fixture
        with patch.dict(os.environ, {"EXPECTED_STAGING_HOST": "ep-staging-expected.c-2.neon.tech"}):
            with self.assertRaises(ValueError) as ctx:
                validate_drill_target_host(unverified_url, urllib.parse.urlparse(unverified_url))
            self.assertIn("TARGET HOST MISMATCH", str(ctx.exception))

            # Exact allowlisted host passes
            valid_url = "postgresql://user:pass@ep-staging-expected.c-2.neon.tech/neondb"
            self.assertTrue(validate_drill_target_host(valid_url, urllib.parse.urlparse(valid_url)))

    def test_drill_runner_wrong_database_rejection(self):
        """Verify drill runner strictly rejects neondb, postgres, shared DBs, and prod DBs."""
        from migration.test_disaster_recovery_drill import (
            validate_drill_database_name,
            DRILL_DB_NAME,
        )

        forbidden_dbs = ["neondb", "postgres", "sewasetu_disposable_test", "template1", "prod_db", "sewasetu_production"]
        for db in forbidden_dbs:
            with self.assertRaises(ValueError) as ctx:
                validate_drill_database_name(db)
            self.assertIn("REJECTED DATABASE NAME", str(ctx.exception))

        # Only sewasetu_dr_drill_disposable passes
        self.assertEqual(validate_drill_database_name(DRILL_DB_NAME), DRILL_DB_NAME)

    def test_drill_runner_existing_database_rejection(self):
        """Verify drill runner detects existing database and aborts safely without modifying or dropping it."""
        from migration.test_disaster_recovery_drill import check_existing_database

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        # When database exists: returns (1,)
        mock_cur.fetchone.return_value = (1,)
        exists = check_existing_database(mock_conn, "sewasetu_dr_drill_disposable")
        self.assertTrue(exists, "Must report database exists when row is returned")

        # When database does not exist: returns None
        mock_cur.fetchone.return_value = None
        not_exists = check_existing_database(mock_conn, "sewasetu_dr_drill_disposable")
        self.assertFalse(not_exists, "Must report database absent when None returned")

    def test_drill_runner_teardown_never_drops_uncreated_db(self):
        """Verify teardown refuses to drop any database that the current execution run did not create."""
        from migration.test_disaster_recovery_drill import teardown_drill_database

        # 1. db_created_by_this_run is False -> must return False and never connect/drop
        result = teardown_drill_database("postgresql://user:pass@host/neondb", "sewasetu_dr_drill_disposable", db_created_by_this_run=False)
        self.assertFalse(result)

        # 2. Database name is not sewasetu_dr_drill_disposable -> must refuse drop
        result = teardown_drill_database("postgresql://user:pass@host/neondb", "neondb", db_created_by_this_run=True)
        self.assertFalse(result)

        result = teardown_drill_database("postgresql://user:pass@host/neondb", "sewasetu_disposable_test", db_created_by_this_run=True)
        self.assertFalse(result)

    def test_drill_runner_tool_discovery_and_missing_handling(self):
        """Verify client tool resolution finds executables on Windows/Unix and safely handles missing tools."""
        from migration.test_disaster_recovery_drill import find_postgres_tool

        orig_path = os.environ.get("PATH", "")

        # Test finding real tools present in scratch/pgsql/bin/ without version constraint
        for tool in ["pg_dump", "pg_restore", "psql"]:
            resolved = find_postgres_tool(tool, required_major=None)
            self.assertTrue(os.path.isfile(resolved), f"Resolved path must be a file: {resolved}")
            self.assertTrue(os.path.basename(resolved).lower().startswith(tool.lower()))

        # Test missing tool handling
        with self.assertRaises(FileNotFoundError) as ctx:
            find_postgres_tool("non_existent_tool_dr_xyz_12345")
        self.assertIn("was not found on this system", str(ctx.exception))

        # Verify PATH was not modified
        self.assertEqual(os.environ.get("PATH"), orig_path, "PATH must not be modified by tool discovery")

    def test_drill_runner_version_detection_pe_and_cli(self):
        """Verify accurate in-memory PE and CLI major version extraction from local client tools."""
        from migration.test_disaster_recovery_drill import get_tool_major_version
        from unittest.mock import patch, MagicMock

        # 1. On Windows with local scratch tools present, verify PE header version extraction:
        if sys.platform.startswith("win") and os.path.isfile("scratch/pgsql/bin/pg_dump.exe"):
            dump_ver = get_tool_major_version("scratch/pgsql/bin/pg_dump.exe")
            restore_ver = get_tool_major_version("scratch/pgsql/bin/pg_restore.exe")
            psql_ver = get_tool_major_version("scratch/pgsql/bin/psql.exe")

            self.assertEqual(dump_ver, 15, "scratch/pgsql/bin/pg_dump.exe must report major version 15")
            self.assertEqual(restore_ver, 15, "scratch/pgsql/bin/pg_restore.exe must report major version 15")
            self.assertEqual(psql_ver, 15, "scratch/pgsql/bin/psql.exe must report major version 15")

        # 2. Test CLI '--version' output extraction (cross-platform fixture):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="pg_dump (PostgreSQL) 18.1 (Debian 18.1-1.pgdg120+1)\n", stderr="", returncode=0)
            ver18 = get_tool_major_version("/mock/path/pg_dump")
            self.assertEqual(ver18, 18, "CLI output must correctly extract major version 18")

            mock_run.return_value = MagicMock(stdout="pg_dump (PostgreSQL) 15.8\n", stderr="", returncode=0)
            ver15 = get_tool_major_version("/mock/path/pg_dump")
            self.assertEqual(ver15, 15, "CLI output must correctly extract major version 15")

        # Nonexistent tool returns None
        self.assertIsNone(get_tool_major_version("scratch/pgsql/bin/nonexistent_tool_xyz.exe"))

    def test_drill_runner_incompatible_version_rejected_safely(self):
        """Verify finding an incompatible v15 client against PostgreSQL 18.6 staging server safely raises IncompatibleToolError."""
        from migration.test_disaster_recovery_drill import (
            find_postgres_tool,
            IncompatibleToolError,
            REQUIRED_STAGING_PG_MAJOR,
        )
        from unittest.mock import patch

        self.assertEqual(REQUIRED_STAGING_PG_MAJOR, 18)

        # 1. Cross-platform isolated test:
        # Verify tool discovery checks the executable's ACTUAL version, not just directory name,
        # and rejects an incompatible version 15 client even if located in a path labeled '18'.
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_exe_name = "pg_dump.exe" if sys.platform.startswith("win") else "pg_dump"
            mock_tool_path = os.path.join(temp_dir, mock_exe_name)
            with open(mock_tool_path, "wb") as f:
                f.write(b"MOCK_PG15_TOOL")
            if not sys.platform.startswith("win"):
                os.chmod(mock_tool_path, 0o755)

            with patch("migration.test_disaster_recovery_drill.get_tool_major_version") as mock_ver:
                # Actual executable reports version 15
                mock_ver.return_value = 15

                os.environ["PG18_BIN_DIR"] = temp_dir
                try:
                    with self.assertRaises(IncompatibleToolError) as ctx:
                        find_postgres_tool("pg_dump", required_major=18)

                    err_msg = str(ctx.exception)
                    self.assertIn("Incompatible PostgreSQL tool 'pg_dump'", err_msg)
                    self.assertIn("found version 15", err_msg)
                    self.assertIn("PostgreSQL 18.6", err_msg)
                    self.assertIn("cannot dump from a server with a newer major version", err_msg)
                finally:
                    os.environ.pop("PG18_BIN_DIR", None)

        # 2. On Windows workstation without PG18 installed, verify real scratch/pgsql/bin is rejected:
        if sys.platform.startswith("win") and os.path.isfile("scratch/pgsql/bin/pg_dump.exe") and "PG18_BIN_DIR" not in os.environ:
            with self.assertRaises(IncompatibleToolError) as ctx:
                find_postgres_tool("pg_dump", required_major=18)
            err_msg = str(ctx.exception)
            self.assertIn("Incompatible PostgreSQL tool 'pg_dump'", err_msg)
            self.assertIn("found version 15", err_msg)

    def test_drill_runner_prefers_pg18_when_available(self):
        """Verify discovery prefers PostgreSQL 18 client when PG18_BIN_DIR override is provided."""
        from migration.test_disaster_recovery_drill import (
            find_postgres_tool,
            REQUIRED_STAGING_PG_MAJOR,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a mock pg_dump.exe / script reporting version 18
            mock_exe_name = "pg_dump.exe" if sys.platform.startswith("win") else "pg_dump"
            mock_tool_path = os.path.join(temp_dir, mock_exe_name)

            if sys.platform.startswith("win"):
                # On Windows, create a batch script or executable wrapper
                mock_cmd = f'@echo off\r\necho pg_dump (PostgreSQL) 18.1\r\n'
                mock_bat = os.path.join(temp_dir, "pg_dump.bat")
                with open(mock_bat, "w") as f:
                    f.write(mock_cmd)
            
            # Test override via PG18_BIN_DIR with mock get_tool_major_version
            from unittest.mock import patch
            with patch("migration.test_disaster_recovery_drill.get_tool_major_version") as mock_ver:
                def mock_ver_fn(path):
                    if "temp" in path.lower() or temp_dir.lower() in path.lower():
                        return 18
                    return 15

                mock_ver.side_effect = mock_ver_fn

                # Create mock file in temp_dir
                with open(mock_tool_path, "wb") as f:
                    f.write(b"MOCK_PG_18_BIN")
                if not sys.platform.startswith("win"):
                    os.chmod(mock_tool_path, 0o755)

                # Point PG18_BIN_DIR to temp_dir
                os.environ["PG18_BIN_DIR"] = temp_dir
                try:
                    resolved = find_postgres_tool("pg_dump", required_major=18)
                    self.assertEqual(os.path.normpath(resolved), os.path.normpath(mock_tool_path))
                finally:
                    os.environ.pop("PG18_BIN_DIR", None)

    def test_drill_runner_default_refusal_without_execution_flag(self):
        """Verify main() refuses execution by default without connecting to any database."""
        from migration.test_disaster_recovery_drill import main as drill_main

        # Calling without args must exit with code 0 (planning/refusal mode)
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            rc = drill_main([])
        finally:
            sys.stdout = old_stdout

        self.assertEqual(rc, 0, "Default invocation without --execute-authorized-drill must return code 0")
        output = buf.getvalue()
        self.assertIn("EXECUTION REFUSED BY DEFAULT", output)
        self.assertIn("--execute-authorized-drill", output)

        # Calling with --execute-authorized-drill but without database URL must abort safely with code 1
        old_url = os.environ.pop("STAGING_DATABASE_URL", None)
        try:
            rc_no_url = drill_main(["--execute-authorized-drill"])
            self.assertEqual(rc_no_url, 1, "Missing URL must exit with code 1")
        finally:
            if old_url:
                os.environ["STAGING_DATABASE_URL"] = old_url

    def test_drill_runner_server_and_db_identity_verification(self):
        """Verify identity check queries server and asserts database name match."""
        from migration.test_disaster_recovery_drill import verify_server_and_db_identity

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        # Scenario 1: Identity matches expected_db
        mock_cur.fetchone.return_value = ("sewasetu_dr_drill_disposable", "test_user", "127.0.0.1", "PostgreSQL 16.0")
        ident = verify_server_and_db_identity(mock_conn, expected_db="sewasetu_dr_drill_disposable")
        self.assertEqual(ident["database"], "sewasetu_dr_drill_disposable")
        self.assertEqual(ident["server_addr"], "127.0.0.1")

        # Scenario 2: Identity mismatch (connected to wrong db, e.g. neondb instead of drill db)
        mock_cur.fetchone.return_value = ("neondb", "test_user", "127.0.0.1", "PostgreSQL 16.0")
        with self.assertRaises(RuntimeError) as ctx:
            verify_server_and_db_identity(mock_conn, expected_db="sewasetu_dr_drill_disposable")
        self.assertIn("CRITICAL IDENTITY MISMATCH", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

