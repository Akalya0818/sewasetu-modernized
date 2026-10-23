-- ==============================================================================
-- Seva Setu: Maintenance Window Rollback & Recovery Procedure
-- Compatible with Managed Render PostgreSQL (100% Client-Side Operations)
--
-- EXECUTION CONTEXT & USAGE GUIDE:
-- ------------------------------------------------------------------------------
-- 1. SCENARIO A (In-Flight Abort — NOT A RESTORE):
--    - Trigger: migrate_database.sql failed or was aborted before COMMIT.
--    - Target: Database transaction was rolled back automatically by PostgreSQL.
--      All row modifications were reverted by PostgreSQL. This is NOT a restore.
--      Only PostgreSQL's non-transactional sequence counter must be realigned.
--    - How to run: Execute this file directly with psql:
--        psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migration/recovery_procedure.sql
--      OR execute the SQL statement below directly in an active psql session.
--
-- 2. SCENARIO B (Post-Commit Disaster Recovery — DESTRUCTIVE & UNTESTED):
--    - Trigger: Migration committed, but post-migration smoke tests failed critically.
--    - WARNING: pg_restore --clean drops and replaces all tables.
--      A physical restore drill of this procedure has NOT BEEN PERFORMED against
--      a live database instance.
--    - DO NOT assume delta-export and re-injection procedures are proven safe.
--    - Target: Operator terminal (Bash or PowerShell), NOT an existing psql session.
--    - Pre-conditions: Explicit incident commander disaster declaration, verified
--      backup archive, confirmed target host identity, and exported delta CSVs.
--    - Recommendation: Treat this untested restore as a pre-production blocker;
--      conduct an isolated restore drill on a test database before production cutover.
-- ==============================================================================

-- ==============================================================================
-- SCENARIO A: MIGRATION ABORTED MID-TRANSACTION (SEQUENCE REALIGNMENT ONLY)
-- Execution Context: Executed as a SQL statement inside PostgreSQL (or via psql -f)
-- ==============================================================================
-- Because migrate_database.sql is executed inside BEGIN ... COMMIT with ON_ERROR_STOP,
-- an abort rolls back all row modifications automatically.
-- THIS IS NOT A DATABASE RESTORE.
-- The statement below resets the sequence back to the surviving MAX(id):

SELECT setval(
    pg_get_serial_sequence('applications', 'id'),
    (SELECT COALESCE(MAX(id), 1) FROM applications),
    true
);

-- ==============================================================================
-- SCENARIO B: COMPLETE DISASTER RECOVERY (RESTORING PRE-MIGRATION DUMP)
-- Execution Context: Operator Terminal / Shell Commands (Bash or PowerShell)
-- STATUS: DESTRUCTIVE AND UNTESTED. DO NOT EXECUTE BLINDLY.
-- ==============================================================================

/*
================================================================================
SCENARIO B - OPERATOR TERMINAL RUNBOOK:

Prerequisites:
  1. Formal disaster declaration from human incident commander.
  2. Target database identity confirmed:
     psql "$DATABASE_URL" -c "SELECT current_database(), inet_server_addr();"
  3. Backup archive readability verified via:
     pg_restore --list "$BACKUP_FILE"
  4. Web service traffic is confirmed SUSPENDED and active sessions checked.

--------------------------------------------------------------------------------
STEP 1 [Operator Terminal Shell]: Export delta records created during maintenance
--------------------------------------------------------------------------------
Execute from operator terminal using client-side psql BEFORE running pg_restore:

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
CREATE TEMP TABLE baseline_keys (application_no VARCHAR(20), mobile VARCHAR(15));
\copy baseline_keys FROM 'baseline_keys.csv' WITH CSV HEADER;

-- Export delta applications submitted after baseline snapshot (explicit column order)
\copy (SELECT application_no, applicant_name, mobile, dob, gender, marital_status, husband_name, husband_employer, village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by FROM applications WHERE application_no NOT IN (SELECT application_no FROM baseline_keys)) TO 'delta_applications.csv' WITH CSV HEADER;

-- Export delta portal users created after baseline snapshot (explicit column order)
\copy (SELECT mobile, password_hash FROM portal_users WHERE mobile NOT IN (SELECT mobile FROM baseline_keys)) TO 'delta_portal_users.csv' WITH CSV HEADER;
"

--------------------------------------------------------------------------------
STEP 2 [Operator Terminal Shell]: Restore the pre-migration physical dump
--------------------------------------------------------------------------------
WARNING: Drops and recreates tables. Specify exact backup filename, never a wildcard.
Execute from operator terminal using pg_restore:

pg_restore --clean --if-exists --no-owner --no-privileges -d "$DATABASE_URL" "$BACKUP_FILE"

--------------------------------------------------------------------------------
STEP 3 [Operator Terminal Shell]: Realign sequence and safely re-inject delta submissions
--------------------------------------------------------------------------------
Execute from operator terminal using psql:

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
-- A. Realign sequence after dump restore
SELECT setval(pg_get_serial_sequence('applications', 'id'), (SELECT COALESCE(MAX(id), 1) FROM applications), true);

-- B. Import delta applications using a staging temp table (assigns fresh SERIAL IDs)
-- Explicit column mapping matching delta_applications.csv export order exactly:
CREATE TEMP TABLE staging_delta_apps (LIKE applications INCLUDING DEFAULTS);
ALTER TABLE staging_delta_apps DROP COLUMN id;
\copy staging_delta_apps (application_no, applicant_name, mobile, dob, gender, marital_status, husband_name, husband_employer, village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by) FROM 'delta_applications.csv' WITH CSV HEADER;

INSERT INTO applications (
    application_no, applicant_name, mobile, dob, gender, marital_status,
    husband_name, husband_employer, village, block, bank_account,
    ifsc, doc_path, status, submitted_at, decided_at, decided_by
)
SELECT application_no, applicant_name, mobile, dob, gender, marital_status,
       husband_name, husband_employer, village, block, bank_account,
       ifsc, doc_path, status, submitted_at, decided_at, decided_by
FROM staging_delta_apps
ON CONFLICT (application_no) DO NOTHING;

-- C. Import delta portal accounts (explicit column mapping)
CREATE TEMP TABLE staging_delta_users (LIKE portal_users);
\copy staging_delta_users (mobile, password_hash) FROM 'delta_portal_users.csv' WITH CSV HEADER;

INSERT INTO portal_users (mobile, password_hash)
SELECT mobile, password_hash FROM staging_delta_users
ON CONFLICT (mobile) DO NOTHING;

-- D. Final sequence realignment to ensure nextval exceeds all inserted IDs
SELECT setval(pg_get_serial_sequence('applications', 'id'), (SELECT COALESCE(MAX(id), 1) FROM applications), true);
"
================================================================================
*/
